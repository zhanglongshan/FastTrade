"""实时策略实例服务：模拟盘/实盘的创建、启停、tick 驱动与持久化。

目的 | Purpose:
    本模块实现实时（live）策略实例的完整服务层：实例的创建、更新、查询、删除，
    单实例 tick 的驱动与结果落库，以及进程内调度器 :class:`RuntimeManager` 对
    所有运行中实例的统一轮询。
    This module implements the full service layer for live strategy instances:
    creation, update, query and deletion of instances; driving a single tick and
    persisting its results; and in-process scheduling of all running instances
    via :class:`RuntimeManager`.

    模拟盘与实盘共用同一套策略与执行层，仅执行器不同：
    Paper and live modes share the same strategy and execution layers; only the
    executor differs:
    - paper：PaperExecutor 本地虚拟撮合；| paper: PaperExecutor local virtual matching.
    - live：LiveExecutor 视 dry_run 决定是否真实下单。| live: LiveExecutor places real orders depending on dry_run.

    所有实例由进程内 :class:`RuntimeManager` 统一轮询驱动；实例状态（账本、
    最近结果、错误）持久化到数据库，服务重启后可恢复运行状态。
    All instances are driven by the in-process :class:`RuntimeManager` poller;
    instance state (ledger, latest result, errors) is persisted to the database
    so running state can be restored after a service restart.

主要流程 | Main flow:
    创建实例 -> 启动任务 -> 调度器按周期调用 ``run_strategy_tick`` -> 结果落库。
    Create instance -> start task -> scheduler calls ``run_strategy_tick`` by
    interval -> persist results.
"""

# ruff: noqa: E402  # 下方模型导入必须晚于 ImportUtil.find_models 的注册语句 | The model imports below must follow ImportUtil.find_models

import asyncio  # 导入 asyncio 异步并发库 | Import asyncio for task scheduling and sleep
import contextlib  # 导入上下文工具（忽略退订异常）| Import contextlib to suppress unsubscribe errors
import math  # 导入数学库（对齐 bar 边界）| Import math for bar-boundary alignment
import time  # 导入时间库（取当前时间戳）| Import time for the current timestamp
from datetime import UTC, datetime  # 从 datetime 导入 UTC 与 datetime | Import UTC and datetime for timestamp handling

from sqlalchemy import select  # 从 sqlalchemy 导入 select | Import select to build ORM query statements
from sqlalchemy.ext.asyncio import AsyncSession  # 从 sqlalchemy.ext.asyncio 导入 AsyncSession | Import AsyncSession as the async DB session type

from app.config.setting import settings  # 导入全局配置对象 | Import global settings for the tick interval configuration
from app.core.base_model import MappedBase  # 导入 ORM 声明基类 | Import MappedBase to register all ORM models
from app.core.base_schema import AuthSchema  # 导入认证 Schema | Import AuthSchema for extracting the auditing user
from app.core.exceptions import CustomException  # 导入自定义业务异常 | Import CustomException for domain-level errors
from app.core.logger import logger  # 导入全局日志器 | Import logger for runtime logging
from app.utils.crypto_util import CryptoUtil  # 导入加解密工具 | Import CryptoUtil for encrypting/decrypting API secrets
from app.utils.import_util import ImportUtil  # 导入模块发现工具 | Import ImportUtil to discover and register models

# 注册全部 ORM 模型，确保 UserMixin.creator 等跨模块 relationship 可解析
# （CLI 单独运行时不会经过应用工厂的自动发现，需在此显式触发）
# 中文：显式触发模型发现，保证跨模块关系映射可用。
# English: Explicitly trigger model discovery so cross-module relationships resolve.
ImportUtil.find_models(MappedBase)  # 触发所有 ORM 模型注册 | Trigger registration of all ORM models

from ..execution.base import AccountLedger, RunMode  # 导入账本、订单请求与运行模式 | Import ledger, order request and run mode enums
from ..execution.risk import RiskSettings  # 导入风控配置（从策略类读取）| Import risk settings read from the strategy class
from ..execution.runner import LiveExecutor, PaperExecutor, TickResult, run_tick  # 导入执行器与 tick 驱动 | Import executors and the tick driver
from ..execution.stream import market_stream  # 导入行情推送单例（替代轮询）| Import the market-stream singleton that replaces polling
from ..model import QuantLiveStrategyModel, QuantTradeRecordModel  # 导入实时策略与成交流水模型 | Import live strategy and trade record models
from ..schema import (  # 从 schema 导入相关请求/响应模型 | Import related request/response schemas
    QuantLiveStrategyCreateSchema,  # 创建策略请求模型 | Create-strategy request schema
    QuantLiveStrategyOutSchema,  # 策略响应模型 | Strategy output schema
    QuantLiveStrategyUpdateSchema,  # 更新策略请求模型 | Update-strategy request schema
)
from ..strategy.loader import load_strategy  # 导入策略加载器（读取风控属性）| Import the strategy loader to read risk attributes
from ..strategy.trading_base import interval_to_seconds  # 导入周期换算（bar 对齐）| Import the interval helper for bar alignment

# 周期 → tick 间隔秒数（至少为基础间隔）
# 中文：将 K 线周期映射到建议的 tick 间隔（秒），实际取值不会小于基础间隔。
# English: Map candle intervals to suggested tick intervals (seconds); the real
# value is never below the base interval.
_INTERVAL_SECONDS = {  # 周期到秒数的映射常量 | Constant mapping of interval to seconds
    "1m": 60,  # 1 分钟 -> 60 秒 | 1 minute -> 60 seconds
    "3m": 180,  # 3 分钟 -> 180 秒 | 3 minutes -> 180 seconds
    "5m": 300,  # 5 分钟 -> 300 秒 | 5 minutes -> 300 seconds
    "15m": 900,  # 15 分钟 -> 900 秒 | 15 minutes -> 900 seconds
    "30m": 1800,  # 30 分钟 -> 1800 秒 | 30 minutes -> 1800 seconds
    "1h": 3600,  # 1 小时 -> 3600 秒 | 1 hour -> 3600 seconds
    "4h": 14400,  # 4 小时 -> 14400 秒 | 4 hours -> 14400 seconds
    "1d": 86400,  # 1 天 -> 86400 秒 | 1 day -> 86400 seconds
}


# 每个实时实例一把进程内互斥锁：调度器与"手动运行一次"可能并发驱动同一实例，
# 串行化后才能保证 last_signal_bar 的幂等判断有效，避免同一信号重复下单。
# One in-process lock per live instance: the scheduler and the UI's "run once" can drive
# the same instance concurrently; serialising them keeps the last_signal_bar idempotency
# check effective so a single signal cannot be ordered twice.
_TICK_LOCKS: dict[int, asyncio.Lock] = {}  # 实例 ID -> 互斥锁 | Instance id -> lock

# 风控心跳的独立占位：不能复用 last_tick_time，否则 1 分钟心跳会把信号周期的占位一直刷新，
# 要么自身被"半根大周期"窗口全部挡掉，要么把真正的信号 tick 判成"本根已处理"而跳过。
# A dedicated claim for risk heartbeats: reusing last_tick_time lets the 1m heartbeat keep resetting
# the signal-period claim, so either the heartbeat itself is throttled away or the real signal tick
# is mistaken for "already processed" and skipped.
_RISK_TICK_AT: dict[int, float] = {}  # 实例 ID -> 上次风控心跳的单调时钟秒 | Instance id -> last risk heartbeat (monotonic)
_RISK_TICK_WINDOW = 30.0  # 风控心跳最小间隔（秒）| Minimum seconds between risk heartbeats
_SPOT_SHORT_WARNED: set[int] = set()  # 已告警"实盘现货禁空"的实例 ID | Instances already warned about spot shorting


def _tick_lock(strategy_id: int) -> asyncio.Lock:  # 取（或创建）实例锁 | Get (or create) the instance lock
    """取得指定实例的互斥锁，不存在时按需创建 | Return the lock for an instance, creating it on demand.

    参数 | Parameters:
        strategy_id (int): 实时策略实例 ID | Live strategy instance id.

    返回 | Returns:
        asyncio.Lock: 该实例对应的锁对象 | The lock bound to that instance.

    说明 | Note:
        锁随实例数量增长且不会回收；实例数量通常是个位数到几十，可忽略。
        The map grows with the instance count and is never pruned; instance counts are
        typically small (single digits to dozens), so this is negligible.
    """
    lock = _TICK_LOCKS.get(strategy_id)  # 先查已有锁 | Look up an existing lock
    if lock is None:  # 不存在则创建 | Create one when missing
        lock = asyncio.Lock()  # 新建互斥锁 | Build a new lock
        _TICK_LOCKS[strategy_id] = lock  # 登记到映射表 | Register it in the map
    return lock  # 返回锁对象 | Return the lock


def _interval_seconds(interval: str) -> int:  # 周期 → tick 间隔（秒）| Interval -> tick delay in seconds
    """根据 K 线周期返回建议 tick 间隔（秒）。

    目的 | Purpose:
        返回给定周期的建议 tick 间隔，取值至少为配置的基础间隔。
        Return the suggested tick interval (seconds) for a candle period,
        never smaller than the configured base interval.

    参数 | Parameters:
        interval: K 线周期字符串（如 ``1m``、``1h``）| Candle interval string.

    返回 | Returns:
        int: 建议的 tick 间隔秒数 | Suggested tick interval in seconds.

    流程 | Flow:
        读取基础间隔，再与映射表中该周期的值取较大者。
        Read the base interval, then take the max with the mapped value.
    """
    base = int(settings.QUANT_TICK_INTERVAL or 60)  # 基础间隔：配置值或默认 60 秒 | Base interval: configured value or default 60s
    return max(base, _INTERVAL_SECONDS.get(interval, base))  # 返回基础间隔与映射值的较大者 | Return the larger of base and mapped value


async def _claim_tick(  # 抢占本根 K 线的执行权 | Claim the right to run this bar
    strategy_id: int,  # 实例 ID | Instance id
    db: AsyncSession,  # 数据库会话 | DB session
    *,
    interval_seconds: int | None = None,  # K 线周期秒数；None 时按实例周期解析 | Bar period; resolved from the instance when None
    force: bool = False,  # 是否忽略占位强制运行（手动触发用）| Ignore the claim for manual runs
    touch: bool = True,  # 是否写 last_tick_time 占位；风控心跳传 False | Whether to write the last_tick_time claim; risk heartbeats pass False
) -> tuple[QuantLiveStrategyModel | None, bool]:  # 返回 (实例, 是否抢到) | Return (instance, claimed)
    """用数据库行锁抢占"本根 K 线"的执行权 | Use a database row lock to claim this bar.

    目的 | Purpose:
        进程内互斥锁只能约束同一个进程。多副本部署时，两个进程会同时读到相同的
        ``last_result``，``last_signal_bar`` 的幂等判断同时失效，同一根 K 线的信号
        会被下单两次——实盘就是双倍建仓。
        这里用 ``SELECT ... FOR UPDATE`` 把该实例行锁住，在锁内判断"距上次 tick 是否
        不足半根 K 线"，决定本次是否执行；随后立即提交释放锁并写入 ``last_tick_time``
        作为"占位"。锁只在读-判-写这段极短时间内持有，绝不跨越下单网络 IO。
        The in-process lock only guards one process. With multiple replicas, two processes read
        the same ``last_result``, the ``last_signal_bar`` idempotency check fails for both and
        the same bar's signal is ordered twice — a doubled position in live trading.
        ``SELECT ... FOR UPDATE`` locks the instance row, decides inside the lock whether less
        than half a bar has passed, then commits immediately to release the lock while writing
        ``last_tick_time`` as the claim. The lock never spans the order-placement network IO.

    参数 | Parameters:
        strategy_id (int): 实例 ID | Instance id.
        db (AsyncSession): 数据库会话 | DB session.
        interval_seconds (int): K 线周期秒数 | Bar period in seconds.
        force (bool): 手动运行时绕过占位 | Bypass the claim for manual runs.
        touch (bool): True 写 ``last_tick_time``（信号 tick 的常规占位）；False 只做短窗节流，
            不写该字段（风控心跳用）。风控心跳若也写 ``last_tick_time``，1 分钟一次的频率会
            把信号周期的"半根 K 线窗"一直刷新，导致信号 tick 被误判为本根已处理。
            | True writes ``last_tick_time`` (the normal signal-tick claim); False only throttles
            on a short window and leaves the column untouched (risk heartbeats). Letting a
            heartbeat write it would keep resetting the signal period's half-bar window and make
            the real signal tick look like a duplicate.

    返回 | Returns:
        tuple[QuantLiveStrategyModel | None, bool]: 实例与是否抢到执行权；
        实例不存在/已删除时返回 (None, False) | The instance and whether the claim succeeded;
        (None, False) when it is missing or deleted.

    流程 | Flow:
        SELECT FOR UPDATE → 校验存在性 → 判断时间窗 → 写 last_tick_time → 提交释放锁。
        SELECT FOR UPDATE → validate → check the time window → write last_tick_time → commit.
    """
    try:  # 取方言名以决定是否支持行锁 | Read the dialect name to decide on row locks
        dialect = db.get_bind().dialect.name  # 数据方言（postgresql/mysql/sqlite）| Dialect name
    except Exception:  # noqa: BLE001 - 取不到方言时按不支持处理 | Treat an unknown dialect as unsupported
        dialect = ""  # 空串表示不支持 | Empty means unsupported
    if dialect in {"postgresql", "mysql", "mariadb"}:  # 只有支持行锁的库才加 FOR UPDATE | Only engines with row locks get FOR UPDATE
        stmt = select(QuantLiveStrategyModel).where(QuantLiveStrategyModel.id == strategy_id).with_for_update()  # 加行锁读取 | Read with a row lock
    else:  # SQLite 等库忽略 FOR UPDATE，退化为普通读取 | SQLite and friends ignore FOR UPDATE
        stmt = select(QuantLiveStrategyModel).where(QuantLiveStrategyModel.id == strategy_id)  # 普通读取 | Plain read
    obj = (await db.execute(stmt)).scalar_one_or_none()  # 取实例 | Fetch the instance
    if obj is None or obj.is_deleted:  # 不存在或已删除 | Missing or soft-deleted
        await db.rollback()  # 立即释放锁 | Release the lock right away
        return None, False  # 报告未抢到 | Report no claim
    now = _naive_utc()  # 当前 UTC（无时区）| Naive UTC now
    last = obj.last_tick_time  # 上次 tick 时间 | Last tick time
    period = int(interval_seconds) if interval_seconds else interval_to_seconds(obj.interval)  # 周期秒数 | Bar period in seconds
    window = max(1.0, float(period) * 0.5)  # 半根 K 线的判定窗 | Half a bar
    if force:  # 手动运行不写占位时间 | Manual runs must not claim the bar
        await db.commit()  # 提交以释放行锁 | Commit to release the lock
        return obj, True  # 直接放行 | Run immediately
    if not touch:  # 风控心跳：只做短窗节流，绝不写 last_tick_time | Risk heartbeat: short throttle only, never touch last_tick_time
        last_risk = _RISK_TICK_AT.get(strategy_id, 0.0)  # 上次风控心跳（单调时钟）| Last risk heartbeat (monotonic)
        if last_risk and time.monotonic() - last_risk < _RISK_TICK_WINDOW:  # 距上次心跳过近 | Too close to the previous heartbeat
            await db.commit()  # 提交（释放行锁）| Commit to release the lock
            return obj, False  # 本轮跳过 | Skip this round
        _RISK_TICK_AT[strategy_id] = time.monotonic()  # 记录本次心跳时刻 | Record this heartbeat
        await db.commit()  # 提交并释放锁（不修改实例行）| Commit and release without modifying the row
        return obj, True  # 允许本次风控检查 | Allow this risk check
    if last is not None and (now - last).total_seconds() < window:  # 另一个实例已经跑过本根 | Another instance already ran this bar
        await db.commit()  # 提交（释放行锁）| Commit to release the lock
        return obj, False  # 报告未抢到 | Report no claim
    obj.last_tick_time = now  # 写入占位时间 | Write the claim timestamp
    await db.commit()  # 提交占位并释放锁 | Commit the claim and release the lock
    return obj, True  # 报告抢到 | Report a successful claim


def _sleep_seconds_to_next_bar(bar_seconds: int, buffer_seconds: float = 3.0) -> float:  # 计算到下一根 bar 收盘的等待秒数 | Seconds until the next bar closes
    """计算对齐到 K 线边界的休眠时长 | Compute the sleep duration aligned to a candle boundary.

    目的 | Purpose:
        原实现是 ``sleep(周期)``，相位会随每次 tick 的耗时持续漂移，导致 tick 落在
        bar 中间（策略看到半根 K 线），甚至跳过一整根。对齐到 bar 边界后，每次 tick
        都紧跟在 bar 收盘之后，配合"丢弃未收盘 K 线"即可稳定复刻回测时序。
        The old implementation slept a fixed interval, so the phase drifted with each tick's
        duration, landing mid-bar (strategies saw half-formed candles) or skipping whole bars.
        Aligning to the boundary makes every tick fire just after a bar closes, which together
        with dropping the forming candle reproduces the backtest timing exactly.

    参数 | Parameters:
        bar_seconds (int): K 线周期秒数 | Candle period in seconds.
        buffer_seconds (float): 收盘后额外等待，留给交易所发布新 bar | Extra wait for the exchange to publish the new bar.

    返回 | Returns:
        float: 需要休眠的秒数（至少 1 秒）| Seconds to sleep, at least 1.
    """
    period = max(1, int(bar_seconds))  # 周期至少 1 秒 | Period of at least one second
    now = time.time()  # 当前时间戳 | Current timestamp
    next_boundary = (math.floor(now / period) + 1) * period + buffer_seconds  # 下一个 bar 边界加缓冲 | Next boundary plus buffer
    return max(1.0, next_boundary - now)  # 保证至少等待 1 秒 | Ensure at least one second


def _user_id(auth: AuthSchema | None) -> int | None:
    """提取当前用户 ID，仅用于审计字段。

    目的 | Purpose:
        从认证信息中安全提取用户 ID，仅用于 created_id/updated_id 等审计字段。
        Safely extract the user ID from auth info, used only for audit fields.

    参数 | Parameters:
        auth: 认证信息，可为 None | Auth schema, may be None.

    返回 | Returns:
        int | None: 用户 ID 或 None | The user ID or None.
    """
    return getattr(getattr(auth, "user", None), "id", None)  # 逐层安全取 auth.user.id | Safely read auth.user.id via nested getattr


def _naive_utc() -> datetime:
    """返回不带时区信息的 UTC 时间，适配 TIMESTAMP WITHOUT TIME ZONE 列。

    目的 | Purpose:
        生成“无时区”的 UTC 当前时间，用于写入不带时区的数据库时间列。
        Produce a timezone-naive UTC "now" for writing to naive TIMESTAMP columns.

    参数 | Parameters:
        无 | None.

    返回 | Returns:
        datetime: 去掉 tzinfo 的 UTC 当前时间 | Current UTC time with tzinfo removed.
    """
    return datetime.now(UTC).replace(tzinfo=None)  # 取 UTC 现在时间并移除时区信息 | Get UTC now and strip tzinfo


def to_out(obj: QuantLiveStrategyModel) -> QuantLiveStrategyOutSchema:
    """模型转响应（密钥永不外泄）。

    目的 | Purpose:
        将 ORM 模型转换为响应 Schema，并补充 has_api_key 标志，密钥本体绝不出库。
        Convert an ORM model to a response schema, adding has_api_key without
        ever exposing the secret itself.

    参数 | Parameters:
        obj: 实时策略 ORM 模型 | Live strategy ORM model.

    返回 | Returns:
        QuantLiveStrategyOutSchema: 对外的响应对象 | Outbound response object.

    流程 | Flow:
        校验构造响应 -> 根据 api_key 是否存在设置标志 -> 返回。
        Validate into schema -> set flag from api_key presence -> return.
    """
    data = QuantLiveStrategyOutSchema.model_validate(obj)  # 由 ORM 模型校验生成响应对象 | Validate ORM model into response object
    data.has_api_key = bool(obj.api_key)  # 仅标记是否配置了密钥，不泄露内容 | Only flag secret presence, never reveal it
    return data  # 返回脱敏后的响应对象 | Return the sanitized response object


async def create_live_strategy(
    params: QuantLiveStrategyCreateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> QuantLiveStrategyModel:
    """创建实时策略实例。

    参数 | Parameters:
        params: 创建参数（名称、模式、策略、交易对、初始资金、费率、密钥等）
            | Creation parameters (name, mode, strategy, symbol, cash, fees, secrets).
        db: 数据库会话（自动提交模式）| Async DB session (autocommit mode).
        auth: 当前用户，仅用于审计字段 | Current user, for audit fields only.

    返回 | Returns:
        QuantLiveStrategyModel: 新建的实例 ORM 对象 | The newly created ORM instance.

    流程 | Flow:
        先校验策略存在 -> 规范化交易对 -> 建账本 -> 组装模型（密钥加密）-> 入库提交。
        Validate strategy -> normalize symbol -> build ledger -> assemble model
        (encrypt secrets) -> commit to database.
    """
    from ..strategy.loader import load_strategy  # 导入策略加载器用于提前校验 | Import strategy loader for pre-validation

    load_strategy(params.strategy_name)  # 提前校验策略存在，避免建出无法运行的实例 | Pre-validate to avoid an unrunnable instance

    symbol = params.symbol.replace("/", "").replace("_", "").upper()  # 规范化交易对：去分隔符并转大写 | Normalize symbol: strip separators, uppercase
    # 杠杆：1 表示现货全额，>1 表示合约保证金交易；账本按此倍数决定保证金占用与强平判定
    # Leverage: 1 is fully funded spot, >1 is margin trading; the ledger uses it for margin and liquidation.
    ledger = AccountLedger(params.initial_cash, fee_rate=params.fee_rate, slippage=params.slippage, leverage=params.leverage)  # 按初始资金/费率/滑点/杠杆建账户账本 | Build ledger from cash, fee, slippage and leverage
    # 组装 ORM 对象：填充基础字段、账户快照、加密密钥与审计字段
    # Assemble the ORM object: base fields, account snapshot, encrypted secrets and audit fields.
    obj = QuantLiveStrategyModel(
        name=params.name,  # 实例名称 | Instance name
        mode=params.mode,  # 运行模式（paper/live）| Run mode (paper/live)
        strategy_name=params.strategy_name,  # 策略名称 | Strategy name
        symbol=symbol,  # 规范化后的交易对 | Normalized symbol
        interval=params.interval,  # K 线周期 | Candle interval
        initial_cash=params.initial_cash,  # 初始资金 | Initial cash
        fee_rate=params.fee_rate,  # 手续费率 | Fee rate
        slippage=params.slippage,  # 滑点 | Slippage
        leverage=params.leverage,  # 杠杆倍数（1=现货）| Leverage (1 = spot)
        dry_run=params.dry_run,  # 是否模拟下单（实盘模式）| Whether to dry-run orders (live mode)
        allow_short=params.allow_short,  # 是否允许做空 | Whether shorting is allowed
        status=0,  # 初始状态：0 表示停止 | Initial status: 0 means stopped
        account=ledger.snapshot(),  # 账户初始快照 | Initial account snapshot
        api_key=CryptoUtil.encrypt(params.api_key) if params.api_key else None,  # 加密 API Key，空则存 None | Encrypt API key or None
        api_secret=CryptoUtil.encrypt(params.api_secret) if params.api_secret else None,  # 加密 API Secret，空则存 None | Encrypt API secret or None
        description=params.description,  # 描述信息 | Description
        created_id=_user_id(auth),  # 创建人（审计）| Creator id (audit)
    )
    db.add(obj)  # 将新实例加入会话 | Add the new instance to the session
    await db.commit()  # 提交事务落库 | Commit the transaction
    await db.refresh(obj)  # 刷新对象以取回数据库生成字段 | Refresh to load DB-generated fields
    return obj  # 返回新建实例 | Return the created instance


async def update_live_strategy(
    strategy_id: int,
    params: QuantLiveStrategyUpdateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> QuantLiveStrategyModel:
    """更新实时策略实例的安全字段。

    目的 | Purpose:
        仅更新允许变更的字段，并对 API 密钥特殊处理（加密覆盖/清空）。
        Update only whitelisted fields, with special handling for API secrets.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        params: 更新参数（仅含显式传入的字段）| Update parameters (only explicitly set fields).
        db: 数据库会话 | Async DB session.
        auth: 当前用户，仅用于审计 | Current user, for audit only.

    返回 | Returns:
        QuantLiveStrategyModel: 更新后的实例 | The updated instance.

    流程 | Flow:
        取实例 -> 提取差异字段 -> 单独处理密钥 -> 写回其余字段 -> 提交。
        Fetch -> extract changed fields -> handle secrets -> apply rest -> commit.
    """
    obj = await get_live_strategy(strategy_id, db)  # 查询实例（不存在则抛异常）| Fetch instance or raise
    updates = params.model_dump(exclude_unset=True, exclude_none=True)  # 仅取显式设置且非 None 的字段 | Keep only explicitly set, non-None fields
    # 单独处理密钥字段：明文非空则加密覆盖，空字符串表示清空
    # Handle secret fields separately: encrypt non-empty plaintext, empty string clears it.
    for key in ("api_key", "api_secret"):  # 遍历两个密钥字段 | Iterate over the two secret fields
        if key in updates:  # 仅当本次请求包含该密钥字段 | Only when this request includes that secret
            obj_key = getattr(obj, key)  # 读取原值（占位，便于调试/审计）| Read original value (placeholder for debug/audit)
            # 仅在明文非空或明确要更新时覆盖；空字符串表示清空
            # Overwrite only when plaintext is non-empty or explicitly updating; empty string clears.
            value = updates.pop(key)  # 取出并移除密钥值，避免后续通用写回 | Pop the secret so generic loop won't overwrite it
            setattr(obj, key, CryptoUtil.encrypt(value) if value else None)  # 加密后写回，空则置 None | Write back encrypted or None
            _ = obj_key  # 保留原值引用，避免未使用告警 | Keep reference to original to avoid unused warning
    for key, value in updates.items():  # 遍历剩余普通字段 | Iterate over remaining ordinary fields
        setattr(obj, key, value)  # 逐字段写回实例 | Set each field on the instance
    user_id = _user_id(auth)  # 提取当前用户 ID 用于审计 | Extract current user id for audit
    if user_id is not None:  # 仅在取到用户 ID 时记录更新人 | Record updater only when a user id exists
        obj.updated_id = user_id  # 写入更新人审计字段 | Set the updated_by audit field
    await db.commit()  # 提交更新 | Commit the update
    await db.refresh(obj)  # 刷新实例 | Refresh the instance
    return obj  # 返回更新后的实例 | Return the updated instance


async def list_live_strategies(
    db: AsyncSession,
    *,
    mode: str | None = None,
    limit: int = 100,
) -> list[QuantLiveStrategyModel]:
    """查询实时策略实例列表。

    目的 | Purpose:
        分页查询未删除的实例，可按模式过滤，按 ID 倒序返回。
        Query non-deleted instances, optionally filtered by mode, newest first.

    参数 | Parameters:
        db: 数据库会话 | Async DB session.
        mode: 可选模式过滤（paper/live）| Optional mode filter.
        limit: 返回数量上限，默认 100 | Max number of rows, default 100.

    返回 | Returns:
        list[QuantLiveStrategyModel]: 实例列表 | List of instances.
    """
    stmt = select(QuantLiveStrategyModel).where(QuantLiveStrategyModel.is_deleted.is_(False))  # 构造查询：仅未删除实例 | Build query: non-deleted only
    if mode:  # 若指定模式则追加过滤条件 | Append a filter when mode is given
        stmt = stmt.where(QuantLiveStrategyModel.mode == mode)  # 按模式过滤 | Filter by mode
    stmt = stmt.order_by(QuantLiveStrategyModel.id.desc()).limit(limit)  # 按 ID 倒序并限制数量 | Order by id desc and limit
    return list((await db.execute(stmt)).scalars().all())  # 执行查询并转为列表返回 | Execute and return as a list


async def get_live_strategy(strategy_id: int, db: AsyncSession) -> QuantLiveStrategyModel:
    """按 ID 查询实时策略实例。

    目的 | Purpose:
        按主键获取实例，若不存在或已删除则抛出业务异常。
        Fetch an instance by primary key; raise if missing or soft-deleted.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        db: 数据库会话 | Async DB session.

    返回 | Returns:
        QuantLiveStrategyModel: 存在的实例 | The existing instance.
    """
    obj = await db.get(QuantLiveStrategyModel, strategy_id)  # 按主键查询实例 | Fetch instance by primary key
    if obj is None or obj.is_deleted:  # 不存在或已逻辑删除视为无效 | Missing or soft-deleted is invalid
        raise CustomException(msg="实时策略实例不存在")  # 抛出业务异常 | Raise domain exception
    return obj  # 返回有效实例 | Return the valid instance


async def delete_live_strategy(strategy_id: int, *, db: AsyncSession) -> None:
    """删除实例（先停止运行）。

    目的 | Purpose:
        先停止调度任务，再对实例做逻辑删除。
        Stop the scheduler task first, then soft-delete the instance.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        db: 数据库会话 | Async DB session.

    返回 | Returns:
        None: 无返回值 | No return value.
    """
    await runtime_manager.stop(strategy_id)  # 先停止该实例的轮询任务 | Stop the instance's polling task first
    obj = await get_live_strategy(strategy_id, db)  # 查询实例（不存在则抛异常）| Fetch instance or raise
    obj.is_deleted = True  # 标记逻辑删除 | Mark as soft-deleted
    await db.commit()  # 提交删除 | Commit the deletion


async def list_trade_records(
    db: AsyncSession,
    *,
    strategy_id: int | None = None,
    limit: int = 200,
) -> list[QuantTradeRecordModel]:
    """查询实时成交流水。

    目的 | Purpose:
        分页查询未删除的成交流水，可按策略 ID 过滤，按 ID 倒序返回。
        Query non-deleted trade records, optionally filtered by strategy id.

    参数 | Parameters:
        db: 数据库会话 | Async DB session.
        strategy_id: 可选策略 ID 过滤 | Optional strategy id filter.
        limit: 返回数量上限，默认 200 | Max number of rows, default 200.

    返回 | Returns:
        list[QuantTradeRecordModel]: 成交流水列表 | List of trade records.
    """
    stmt = select(QuantTradeRecordModel).where(QuantTradeRecordModel.is_deleted.is_(False))  # 构造查询：仅未删除流水 | Build query: non-deleted only
    if strategy_id is not None:  # 若指定策略 ID 则追加过滤 | Append filter when strategy id given
        stmt = stmt.where(QuantTradeRecordModel.strategy_id == strategy_id)  # 按策略 ID 过滤 | Filter by strategy id
    stmt = stmt.order_by(QuantTradeRecordModel.id.desc()).limit(limit)  # 按 ID 倒序并限制数量 | Order by id desc and limit
    return list((await db.execute(stmt)).scalars().all())  # 执行查询并转为列表返回 | Execute and return as a list


def _build_executor(
    obj: QuantLiveStrategyModel,
    ledger: AccountLedger,
) -> PaperExecutor | LiveExecutor:
    """按实例模式构造执行器。

    目的 | Purpose:
        根据实例的 mode 选择并构造对应的执行器（模拟盘/实盘）。
        Choose and build the executor based on the instance mode.

    参数 | Parameters:
        obj: 实时策略实例 | Live strategy instance.
        ledger: 账户账本 | Account ledger.

    返回 | Returns:
        PaperExecutor | LiveExecutor: 构造好的执行器 | The constructed executor.

    流程 | Flow:
        paper 模式直接返回 PaperExecutor；live 模式解密密钥后返回 LiveExecutor。
        Paper returns PaperExecutor; live decrypts secrets then returns LiveExecutor.
    """
    if obj.mode == RunMode.PAPER.value:  # 模拟盘模式走本地虚拟撮合 | Paper mode uses local virtual matching
        return PaperExecutor(ledger, fee_rate=obj.fee_rate)  # 返回模拟执行器 | Return the paper executor
    api_key = CryptoUtil.decrypt(obj.api_key) if obj.api_key else ""  # 解密 API Key（无则空串）| Decrypt API key or empty string
    api_secret = CryptoUtil.decrypt(obj.api_secret) if obj.api_secret else ""  # 解密 API Secret（无则空串）| Decrypt API secret or empty string
    # 构造实盘执行器：注入账本、解密后的密钥与 dry_run 开关
    # Build the live executor: inject ledger, decrypted secrets and the dry_run flag.
    return LiveExecutor(
        ledger,  # 账户账本 | Account ledger
        api_key=api_key,  # 解密后的 API Key | Decrypted API key
        api_secret=api_secret,  # 解密后的 API Secret | Decrypted API secret
        dry_run=bool(obj.dry_run),  # 是否仅模拟下单 | Whether to only simulate orders
    )


async def run_strategy_tick(
    strategy_id: int,
    *,
    db: AsyncSession,
    candles: list | None = None,
    execution_price: float | None = None,
    depth: tuple[float, float] | None = None,
    risk_only: bool = False,
    force: bool = False,
    fill_basis: str = "",
    spread_ratio: float = 0.0,
) -> TickResult:
    """驱动单个实例运行一次 tick（同一实例串行执行），并持久化账本、结果、成交与状态。

    目的 | Purpose:
        调度器与页面上的"手动运行一次"都会调用本函数。若两者并发进入，
        它们会读到同一份 ``last_result``，``last_signal_bar`` 的幂等判断同时失效，
        同一根 K 线的信号会被重复下单——实盘下即真实的重复建仓。
        因此这里用"每实例一把进程内锁"把 tick 串行化：第二个调用会等待，
        待前一个落库后再运行，此时幂等判断已能看到新的信号 bar。
        Both the scheduler and the UI's "run once" call this function. If they enter
        concurrently they read the same ``last_result``, the ``last_signal_bar``
        idempotency check fails for both, and the same bar's signal is ordered twice —
        a real duplicate position in live mode. A per-instance in-process lock serialises
        ticks: the second caller waits, then runs after the first commit so the
        idempotency check sees the new signal bar.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        db: 数据库会话 | Async DB session.
        candles: 可选的本地 K 线窗口；传入时不请求币安（便于离线验证）
            | Optional local candle window; when provided, Binance is not called.
        execution_price: 行情流给出的"下一根开盘价"，None 时由实现自行解析
            | Next-open price from the stream; resolved internally when None.
        depth: (买一量, 卖一量) 盘口深度，用于给成交设上限 | Top-of-book depth used to cap fills.
        risk_only: 只跑风控不跑策略（盘中 1 分钟风控检查）| Run risk only (intra-bar 1m risk check).
        force: 忽略数据库行锁占位（手动触发）| Ignore the DB row-lock claim (manual trigger).
        fill_basis: 行情流给出的成交价口径（next_open/mid_fallback）；空串时按 next_open
            | Fill-price basis reported by the stream; empty means next_open.
        spread_ratio: 半价差比例，仅 mid_fallback 时用于把中间价修正为可成交价
            | Half-spread used to turn a mid price into a tradeable one on mid_fallback.

    返回 | Returns:
        TickResult: 本次 tick 结果 | The tick result.

    流程 | Flow:
        取实例锁 -> 等待前一个 tick 结束 -> 执行 tick 实现 -> 返回结果。
        Acquire the instance lock -> wait for any in-flight tick -> run the implementation -> return.
    """
    async with _tick_lock(strategy_id):  # 同一实例的 tick 串行执行 | Serialise ticks per instance
        return await _run_strategy_tick_impl(  # 执行实际 tick 逻辑 | Run the actual tick logic
            strategy_id,  # 实例 ID | Instance id
            db=db,  # 数据库会话 | DB session
            candles=candles,  # 可选本地行情 | Optional local candles
            execution_price=execution_price,  # 事件驱动成交价 | Event-driven fill price
            depth=depth,  # 盘口深度 | Book depth
            risk_only=risk_only,  # 是否只跑风控 | Risk-only flag
            force=force,  # 是否忽略占位 | Whether to bypass the claim
            fill_basis=fill_basis,  # 成交价口径 | Fill-price basis
            spread_ratio=spread_ratio,  # 半价差比例 | Half-spread ratio
        )


def _skipped_result(obj: QuantLiveStrategyModel, reason: str) -> TickResult:  # 构造"未执行"的结果 | Build a "skipped" result
    """构造一个未执行 tick 的结果，供上层返回与展示 | Build a skipped-tick result for the caller to return and display.

    目的 | Purpose:
        行锁判定为"本根已由其他实例处理"时并没有真正运行策略，但仍需返回结构一致的
        结果对象，避免调用方做分支判断。
        When the row lock says another instance already ran this bar, the strategy did not run,
        but a structurally identical result must still be returned so callers need no branches.

    参数 | Parameters:
        obj (QuantLiveStrategyModel): 实例 | The instance.
        reason (str): 跳过原因 | Reason for skipping.

    返回 | Returns:
        TickResult: 带上次账户快照与说明日志的结果 | Result carrying the previous snapshot and an explanatory log.
    """
    last = obj.last_result or {}  # 上次结果 | Previous result
    return TickResult(  # 构造结果 | Build the result
        mode=obj.mode,  # 运行模式 | Run mode
        symbol=obj.symbol,  # 交易对 | Symbol
        interval=obj.interval,  # 周期 | Interval
        strategy=obj.strategy_name,  # 策略名 | Strategy name
        price=float(last.get("price") or 0.0),  # 上次价格 | Previous price
        equity=float(last.get("equity") or 0.0),  # 上次净值 | Previous equity
        cash=float(last.get("cash") or 0.0),  # 上次现金 | Previous cash
        position=dict(last.get("position") or {}),  # 上次持仓 | Previous position
        fills=[],  # 无成交 | No fills
        logs=[reason],  # 跳过原因 | Skip reason
        rejected=[],  # 无被拒订单 | No rejections
        tick_time=_naive_utc().isoformat(),  # 当前时间 | Current time
        signal_bar=str(last.get("signal_bar") or ""),  # 保留上次信号 bar（幂等不能丢）| Keep the previous signal bar for idempotency
        fill_basis="skipped",  # 口径标记为跳过 | Mark the basis as skipped
        leverage=float(getattr(obj, "leverage", 1.0) or 1.0),  # 实例杠杆 | Instance leverage
        liquidation_price=float(last.get("liquidation_price") or 0.0),  # 上次强平价 | Previous liquidation price
    )


async def _run_strategy_tick_impl(
    strategy_id: int,
    *,
    db: AsyncSession,
    candles: list | None = None,
    execution_price: float | None = None,
    depth: tuple[float, float] | None = None,
    risk_only: bool = False,
    force: bool = False,
    fill_basis: str = "",
    spread_ratio: float = 0.0,
) -> TickResult:
    """驱动单个实例运行一次 tick，并持久化账本、结果、成交与状态。

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        db: 数据库会话 | Async DB session.
        candles: 可选的本地 K 线窗口；传入时不请求币安（便于离线验证）
            | Optional local candle window; when provided, Binance is not called.
        execution_price: 事件驱动路径的成交价 | Fill price for the event-driven path.
        depth: 盘口深度 | Book depth.
        risk_only: 只跑风控 | Risk-only flag.
        force: 忽略行锁占位 | Bypass the row-lock claim.
        fill_basis: 行情流给出的成交价口径 | Fill-price basis from the stream.
        spread_ratio: 半价差比例 | Half-spread ratio.

    返回 | Returns:
        TickResult: 本次 tick 结果 | The tick result.

    流程 | Flow:
        行锁抢占 -> 取实例与账本 -> 构造执行器 -> 运行 tick（异常则落库并抛出）->
        持久化账户/结果/状态 -> 落成交流水 -> 提交返回。
        Claim the bar under a row lock -> fetch instance/ledger -> build executor -> run tick
        (persist+raise on error) -> persist account/result/status -> persist fills -> commit.
    """
    claimed, ok = await _claim_tick(  # 用数据库行锁抢占本根 K 线 | Claim this bar under a row lock
        strategy_id,  # 实例 ID | Instance id
        db,  # 数据库会话 | DB session
        force=force,  # 手动触发可绕过 | Manual runs may bypass
        touch=not risk_only,  # 风控心跳不写 last_tick_time | Risk heartbeats leave the claim column alone
    )
    if claimed is None:  # 实例不存在或已删除 | Missing or deleted
        raise CustomException(msg="实时策略实例不存在")  # 抛出业务异常 | Raise a domain exception
    if not ok:  # 本根已被其他实例处理 | Another instance handled this bar
        return _skipped_result(claimed, "本根 K 线已由其他实例处理，跳过以避免重复下单")  # 返回跳过结果 | Return the skipped result
    obj = claimed  # 使用已加锁读取的实例 | Use the instance read under the lock
    try:  # 策略文件可能被改动，读取失败时退化为无风控，让 run_tick 抛出可落库的错误 | Fall back to no risk if loading fails
        info = load_strategy(obj.strategy_name)  # 读取策略类以获取风控配置 | Load the strategy class to read risk config
        risk = RiskSettings.from_strategy(info.cls)  # 从策略类读取止损/熔断配置（与回测同一份）| Read risk config, identical to backtest
    except Exception as exc:  # noqa: BLE001 - 加载失败不应掩盖真正的运行错误 | Do not mask the real runtime error
        logger.warning(f"策略风控配置读取失败({obj.strategy_name})：{exc}")  # 记录告警 | Log a warning
        risk = RiskSettings()  # 退化为无风控 | Fall back to no risk rules
    # 真实下单走的是币安现货接口，没有保证金账户：此时按杠杆记账会与交易所余额长期偏离，
    # 因此强制回落到 1 倍并告警（而不是中断策略，避免已持仓的实例失去风控保护）。
    # Real orders go through Binance spot, which has no margin account. Booking leverage there
    # would drift from the exchange balance forever, so fall back to 1x with a warning instead
    # of aborting the tick (an open position still needs risk protection).
    leverage = float(getattr(obj, "leverage", 1.0) or 1.0)  # 实例配置的杠杆 | Configured leverage
    if obj.mode == RunMode.LIVE.value and not bool(obj.dry_run) and leverage > 1:  # 真实现货下单场景 | Real spot execution
        logger.warning(f"实盘现货不支持杠杆，实例 {obj.id} 已按 1 倍记账（配置为 {leverage} 倍）")  # 记录告警 | Log a warning
        leverage = 1.0  # 强制 1 倍 | Force 1x
    ledger = AccountLedger.from_snapshot(obj.account, fee_rate=obj.fee_rate, slippage=obj.slippage, risk=risk, leverage=leverage)  # 从快照恢复账户账本（杠杆以实例配置为准）| Restore the ledger, using the instance's leverage
    executor = _build_executor(obj, ledger)  # 按模式构造执行器 | Build executor by mode
    # 实盘现货不能做空：策略里若有 sellshort/buycover 分支，回测会成交、实盘会静默拒单，
    # 绩效完全不可比。这里在真实现货实例上告警一次，避免"回测赚钱、实盘空仓"的困惑。
    # Spot live cannot short: a strategy with sellshort/buycover branches trades in the backtest but
    # gets silently rejected live, making performance incomparable. Warn once per instance so the
    # "profitable backtest, idle live account" surprise does not happen.
    if isinstance(executor, LiveExecutor) and not executor.dry_run and ("sellshort" in vars(info.cls) or "buycover" in vars(info.cls)):  # 真实现货 + 含空头分支 | Real spot with short branches
        if obj.id not in _SPOT_SHORT_WARNED:  # 每个实例只告警一次 | Warn once per instance
            _SPOT_SHORT_WARNED.add(obj.id)  # 记录已告警 | Remember the warning
            logger.warning(f"策略 {obj.strategy_name} 含 sellshort/buycover 分支，但实盘现货不支持做空，这些信号将被拒绝")  # 记录告警 | Log the warning
    # 实盘先与交易所对账：用真实可用余额覆盖虚拟资金，避免按 10000 虚拟本金下真实单
    # Reconcile first in live mode: overwrite virtual cash with the real balance before sizing.
    if isinstance(executor, LiveExecutor) and not executor.dry_run:  # 仅真实下单的实盘实例需要对账 | Only real live instances need reconciliation
        last_price = float((obj.last_result or {}).get("price") or 0.0)  # 上次 tick 价格，用于持仓成本兜底 | Last tick price for cost fallback
        # 对账节流 30 秒：1 分钟周期下每根都做一次，同时避免 1m 风控检查触发额外 4 次 REST
        # Throttle to 30s: reconcile once per bar at 1m, avoiding four extra REST calls per 1m risk check.
        summary = executor.sync_from_exchange(obj.symbol, price=last_price, min_interval=30.0)  # 余额/成交/挂单全量对账 | Full reconciliation
        if summary.get("changed"):  # 有变化时记录，便于审计 | Log when something changed
            logger.info(f"实盘对账修正({obj.symbol})：{summary}")  # 记录修正明细 | Log the adjustments
    allow_short = bool(obj.allow_short) if obj.mode == RunMode.PAPER.value else bool(obj.allow_short and obj.dry_run)  # 计算是否允许做空：实盘仅 dry_run 时允许 | Decide if shorting is allowed
    # 运行一次 tick；任何失败都落库以便页面排查，然后重新抛出
    # Run one tick; persist any failure for UI troubleshooting, then re-raise.
    try:
        result = await run_tick(
            mode=obj.mode,  # 运行模式 | Run mode
            executor=executor,  # 执行器实例 | Executor instance
            ledger=ledger,  # 账户账本 | Account ledger
            strategy_name=obj.strategy_name,  # 策略名称 | Strategy name
            symbol=obj.symbol,  # 交易对 | Symbol
            interval=obj.interval,  # K 线周期 | Candle interval
            candles=candles,  # 可选本地 K 线窗口 | Optional local candle window
            allow_short=allow_short,  # 是否允许做空 | Whether shorting is allowed
            drop_unclosed=True,  # 丢弃未收盘 K 线，与回测时序对齐 | Drop the forming candle to match backtest timing
            last_signal_bar=str((obj.last_result or {}).get("signal_bar") or ""),  # 上次已处理的信号 bar（幂等）| Last processed signal bar for idempotency
            execution_price=execution_price,  # 事件驱动成交价（新 bar 开盘）| Event-driven fill price
            depth=depth,  # 盘口深度约束 | Book depth constraint
            risk_only=risk_only,  # 只跑风控（盘中 1m 检查）| Risk-only intra-bar check
            fill_basis_hint=fill_basis,  # 成交价口径（next_open/mid_fallback）| Fill-price basis
            spread_ratio=spread_ratio,  # 半价差（仅 mid_fallback 生效）| Half-spread for mid-based fills
        )
    except Exception as exc:  # noqa: BLE001 - 任何失败落库，供页面排查 | Persist any failure for UI troubleshooting
        obj.status = 2  # 标记异常状态 | Mark error status
        obj.last_error = str(exc)  # 记录错误文案 | Store error message
        obj.last_tick_time = _naive_utc()  # 记录 tick 时间 | Record tick time
        await db.commit()  # 提交错误信息 | Commit the error info
        raise  # 重新抛出异常交由调用方处理 | Re-raise for the caller

    # 持久化账户与最近结果 | Persist account snapshot and latest result
    obj.account = executor.snapshot()  # 写回最新账户快照 | Write back latest account snapshot
    payload = result.to_dict()  # 转成可落库字典 | Convert into a persistable dict
    if risk_only:  # 盘中风控结果不能覆盖信号 bar | An intra-bar risk result must not overwrite the signal bar
        previous = dict(obj.last_result or {})  # 取上次结果 | Read the previous result
        payload["signal_bar"] = str(previous.get("signal_bar") or "")  # 保留原信号 bar（幂等关键）| Keep the previous signal bar
        payload["risk_bar"] = result.signal_bar  # 单独记录风控所用 bar | Record the risk bar separately
    obj.last_result = payload  # 写回最近一次 tick 结果 | Write back latest tick result
    obj.last_error = result.error  # 写回本次错误信息（可能为 None）| Write back latest error (may be None)
    obj.last_tick_time = _naive_utc()  # 更新最近 tick 时间 | Update last tick time
    if obj.status == 2:  # 若之前处于异常状态 | If previously in error status
        obj.status = 1  # 异常后成功运行，自动恢复运行状态 | Recover running status after a successful tick

    # 落成交流水 | Persist trade fills
    for fill in result.fills:  # 遍历本次成交列表 | Iterate over the fills
        trade_time: datetime | None = None  # 成交时间初始化为 None | Initialize trade time to None
        if fill.get("timestamp"):  # 若成交记录带时间戳 | If the fill carries a timestamp
            try:
                trade_time = datetime.fromisoformat(str(fill["timestamp"])).replace(tzinfo=None)  # 解析 ISO 时间并去掉时区 | Parse ISO time and strip tzinfo
            except ValueError:  # 时间戳格式非法时回退 | Fall back when timestamp is malformed
                trade_time = _naive_utc()  # 使用当前 UTC 时间兜底 | Fall back to current UTC time
        db.add(
            QuantTradeRecordModel(
                strategy_id=obj.id,  # 关联策略 ID | Associated strategy id
                mode=result.mode,  # 运行模式 | Run mode
                symbol=result.symbol,  # 交易对 | Symbol
                side=fill["side"],  # 买卖方向 | Buy/sell side
                qty=float(fill["qty"]),  # 成交数量 | Filled quantity
                price=float(fill["price"]),  # 成交价格 | Filled price
                fee=float(fill["fee"]),  # 手续费 | Fee
                pnl=(float(fill["pnl"]) if fill.get("pnl") is not None else None),  # 盈亏（有则转 float）| PnL if present
                reason=fill.get("reason"),  # 成交原因 | Fill reason
                trade_time=trade_time or _naive_utc(),  # 成交时间（兜底当前时间）| Trade time (fallback now)
                created_id=obj.created_id,  # 创建人（审计）| Creator id (audit)
            )
        )
    await db.commit()  # 提交账本、结果与流水 | Commit ledger, result and fills
    await db.refresh(obj)  # 刷新实例 | Refresh the instance
    return result  # 返回本次 tick 结果 | Return the tick result


class RuntimeManager:
    """进程内实时策略调度器：为每个运行中的实例维护一个 asyncio 任务。

    目的 | Purpose:
        在进程内为每个运行中的策略实例创建并维护一个 asyncio 轮询任务，按周期
        驱动 tick，并支持启动、停止、恢复与关闭。
        Create and maintain one asyncio polling task per running strategy
        instance in-process, driving ticks by interval, with start/stop/restore/
        shutdown support.

    关键属性 | Key attributes:
        _tasks: ``dict[int, asyncio.Task]``，实例 ID 到其轮询任务的映射
            | Mapping from strategy id to its polling task.
        _running: ``bool``，调度器运行标志（预留）| Scheduler running flag (reserved).
    """

    def __init__(self) -> None:
        """初始化调度器。

        目的 | Purpose:
            初始化任务表与运行标志为空/关闭状态。
            Initialize the task map to empty and the running flag to False.
        """
        self._tasks: dict[int, asyncio.Task] = {}  # 实例 ID -> 轮询任务映射 | Map of strategy id -> polling task
        self._running = False  # 调度器运行标志（初始关闭）| Scheduler running flag (initially False)

    def is_running(self, strategy_id: int) -> bool:
        """实例是否已有运行任务。

        目的 | Purpose:
            判断实例当前是否存在未完成的任务。
            Check whether an instance currently has an unfinished task.

        参数 | Parameters:
            strategy_id: 实例 ID | Instance ID.

        返回 | Returns:
            bool: 存在且未完成任务时为 True | True if a task exists and is not done.
        """
        task = self._tasks.get(strategy_id)  # 取该实例的任务（可能不存在）| Get the task for the instance (may be absent)
        return task is not None and not task.done()  # 任务存在且未完成才算运行中 | Running only if task exists and is not done

    async def start(self, strategy_id: int) -> None:
        """启动实例轮询任务。

        目的 | Purpose:
            将实例状态置为运行，并为其创建轮询任务（若尚未运行）。
            Set the instance status to running and create a polling task if not
            already running.

        参数 | Parameters:
            strategy_id: 实例 ID | Instance ID.

        返回 | Returns:
            None: 无返回值 | No return value.
        """
        from app.core.database import async_db_session  # 导入异步数据库会话上下文 | Import the async DB session context manager

        async with async_db_session() as db:  # 打开数据库会话 | Open a DB session
            obj = await get_live_strategy(strategy_id, db)  # 查询实例（不存在则抛异常）| Fetch instance or raise
            if obj.status == 1 and self.is_running(strategy_id):  # 已在运行则无需重复处理 | Already running, nothing to do
                return  # 直接返回 | Return early
            obj.status = 1  # 置为运行状态 | Set status to running
            obj.last_error = None  # 清空错误 | Clear last error
            await db.commit()  # 提交状态变更 | Commit the status change

        if self.is_running(strategy_id):  # 会话外再次确认，避免重复建任务 | Re-check outside session to avoid duplicate tasks
            return  # 已运行则直接返回 | Return early
        task = asyncio.create_task(self._loop(strategy_id), name=f"quant-live-{strategy_id}")  # 创建命名轮询任务 | Create a named polling task
        self._tasks[strategy_id] = task  # 记录任务到映射表 | Store the task in the map
        logger.info(f"✅ 量化实时策略启动：{strategy_id} ({obj.mode})")  # 记录启动日志 | Log successful start

    async def stop(self, strategy_id: int) -> None:
        """停止实例轮询任务并落库状态。

        目的 | Purpose:
            取消实例的轮询任务，并将数据库状态置为停止。
            Cancel the instance's polling task and set DB status to stopped.

        参数 | Parameters:
            strategy_id: 实例 ID | Instance ID.

        返回 | Returns:
            None: 无返回值 | No return value.
        """
        from app.core.database import async_db_session  # 导入异步数据库会话上下文 | Import the async DB session context manager

        task = self._tasks.pop(strategy_id, None)  # 取出并移除任务 | Pop the task from the map
        # 清理该实例的进程内占位：心跳时间戳与"已告警"标记都属于实例级状态，停止后应释放
        # Drop the per-instance in-process state: heartbeat timestamps and the warned flag are
        # instance-scoped and should not outlive a stopped instance.
        _RISK_TICK_AT.pop(strategy_id, None)  # 清掉心跳占位 | Drop the heartbeat claim
        _SPOT_SHORT_WARNED.discard(strategy_id)  # 清掉告警标记 | Drop the warned flag
        if task and not task.done():  # 任务存在且未完成时取消 | Cancel if the task exists and is unfinished
            task.cancel()  # 请求取消任务 | Request task cancellation
            try:
                await task  # 等待取消完成 | Await the cancellation
            except asyncio.CancelledError:  # 捕获取消异常属正常 | CancelledError is expected
                pass  # 忽略取消异常 | Ignore the cancellation error
        async with async_db_session() as db:  # 打开数据库会话 | Open a DB session
            obj = await db.get(QuantLiveStrategyModel, strategy_id)  # 按主键查询实例 | Fetch instance by primary key
            if obj and not obj.is_deleted:  # 实例存在且未删除才更新 | Update only if instance exists and is not deleted
                obj.status = 0  # 置为停止状态 | Set status to stopped
                await db.commit()  # 提交状态变更 | Commit the status change

    async def _loop(self, strategy_id: int) -> None:
        """事件驱动循环：由行情推送的收盘事件驱动 tick，REST 轮询只做兜底。

        目的 | Purpose:
            旧实现 ``sleep(周期)`` 有两个问题：相位随 tick 耗时漂移、收盘后固定延迟，
            策略总是在"行情已经走了一段"之后才决策。改为订阅 kline 收盘事件后，tick
            在收盘瞬间触发；同时订阅 bookTicker 拿到盘口深度。对周期大于 1 分钟的实例，
            额外用 1 分钟收盘事件做**风控专检**，把止损发现延迟从一根大周期缩短到 1 分钟。
            The old ``sleep(period)`` drifted in phase and always decided after a fixed delay.
            Subscribing to the kline close event fires the tick at the close itself and the
            bookTicker subscription supplies depth. For intervals above one minute, 1m closes
            drive risk-only checks, cutting stop-detection lag from one big bar to one minute.

        参数 | Parameters:
            strategy_id: 实例 ID | Instance ID.

        返回 | Returns:
            None: 无返回值 | No return value.

        流程 | Flow:
            读实例 → 订阅行情 → 等待收盘事件 → 新 K 线则全量 tick、否则风控专检 →
            行情不可用时退回 REST 轮询 → 取消/停止时退订并清理任务。
            Read instance → subscribe → wait for a close → full tick on a new bar (risk-only
            otherwise) → fall back to REST polling when the stream is unavailable →
            unsubscribe and clean up on cancellation/stop.
        """
        from app.core.database import async_db_session  # 导入异步数据库会话上下文 | Import the async DB session context manager

        symbol = ""  # 交易对 | Symbol
        interval = "1m"  # K 线周期 | Candle interval
        bar_seconds = 60  # bar 周期秒数 | Bar period in seconds
        async with async_db_session() as db:  # 打开数据库会话读取周期 | Open a DB session to read the interval
            obj = await db.get(QuantLiveStrategyModel, strategy_id)  # 按主键查询实例 | Fetch instance by primary key
            if obj is None or obj.is_deleted:  # 实例不存在则直接退出 | Exit when the instance is missing
                self._tasks.pop(strategy_id, None)  # 清理任务映射 | Clean up the task map
                return  # 结束循环 | End the loop
            symbol = obj.symbol  # 记录交易对 | Remember the symbol
            interval = obj.interval  # 记录周期 | Remember the interval
            bar_seconds = interval_to_seconds(obj.interval)  # 周期秒数 | Bar period in seconds

        drive_interval = interval if bar_seconds <= 60 else "1m"  # 驱动周期：大周期用 1m 做风控心跳 | Driving interval: 1m heartbeats for larger bars
        subscribed = False  # 是否成功订阅推送 | Whether the stream subscription succeeded
        signal_seq = 0  # 已处理的信号序号 | Processed signal counter
        try:  # 订阅失败时退回 REST 轮询，功能不缺失 | Fall back to REST polling when subscribing fails
            await market_stream.subscribe(symbol, interval)  # 订阅信号周期的 K 线 | Subscribe to the signal interval
            if drive_interval != interval:  # 大周期额外订阅 1m 心跳 | Extra 1m heartbeat for larger bars
                await market_stream.subscribe(symbol, drive_interval)  # 订阅 1m | Subscribe to 1m
            subscribed = True  # 标记订阅成功 | Mark success
            signal_seq = market_stream.bar_seq(symbol, interval)  # 以当前序号为基线 | Baseline the counter
        except Exception as exc:  # noqa: BLE001 - 订阅异常不应终止实例 | Subscription errors must not kill the instance
            logger.warning(f"行情订阅失败({symbol})，退回 REST 轮询：{type(exc).__name__}: {exc}")  # 记录告警 | Log a warning

        try:  # 保证退出时一定退订 | Always unsubscribe on exit
            while True:  # 无限循环直到取消或停止 | Loop until cancelled or stopped
                try:  # 单轮异常不中断循环 | One bad round must not stop the loop
                    got = False  # 本轮是否收到收盘事件 | Whether a close event arrived
                    ready = subscribed and market_stream.is_connected()  # 连接是否就绪 | Whether the socket is ready
                    if subscribed and not ready:  # 刚启动/断线时短暂等待重连 | Briefly wait for a reconnect
                        ready = await market_stream.wait_connected(5.0)  # 最多 5 秒 | At most five seconds
                    if ready:  # 已连接才等推送 | Only wait for a push when connected
                        # 超时给一根 K 线加 90 秒余量：超时说明推送异常，立即走 REST 兜底
                        # Timeout is one bar plus 90s of slack: on expiry the stream is unhealthy, so fall back.
                        timeout = float(bar_seconds) + 90.0 if drive_interval == interval else 150.0  # 等待上限 | Wait ceiling
                        # 传入已处理的信号序号，避免"订阅与首次等待之间恰好收盘"被漏掉
                        # Pass the processed signal counter so a close landing during startup is not missed.
                        after = signal_seq if drive_interval == interval else None  # 信号周期用已处理序号 | Use the processed counter on the signal interval
                        got = await market_stream.wait_bar(symbol, drive_interval, timeout=timeout, after_seq=after)  # 等待收盘事件 | Wait for a close
                    if not got:  # 推送不可用或超时：退回 REST 轮询一次 | Stream unavailable or timed out: poll REST once
                        if subscribed and not market_stream.is_connected():  # 连接已断开 | The connection dropped
                            logger.warning(f"行情连接断开({symbol})，本轮走 REST 兜底")  # 记录告警 | Log a warning
                            await market_stream.start()  # 尝试重连 | Try to reconnect
                        async with async_db_session() as db:  # 打开新会话 | Open a session
                            obj = await db.get(QuantLiveStrategyModel, strategy_id)  # 读取实例 | Fetch the instance
                            if obj is None or obj.is_deleted or obj.status != 1:  # 已停止 | Stopped
                                break  # 退出循环 | Break out
                            await run_strategy_tick(strategy_id, db=db)  # 先补一次 REST tick | Poll once right away
                        # 再对齐到下一根收盘，避免相位漂移；先 tick 后睡也避免了启动时白等一整根
                        # Then align to the next close; ticking before sleeping also avoids a wasted bar on startup.
                        await asyncio.sleep(_sleep_seconds_to_next_bar(bar_seconds))  # 对齐到下一根收盘 | Align to the next close
                        continue  # 进入下一轮 | Next round

                    async with async_db_session() as db:  # 每轮打开新会话 | Open a fresh session each round
                        obj = await db.get(QuantLiveStrategyModel, strategy_id)  # 每轮重新读取实例状态 | Re-read instance state each round
                        if obj is None or obj.is_deleted or obj.status != 1:  # 实例失效或不在运行则退出 | Exit if missing, deleted or not running
                            break  # 跳出循环 | Break out
                        current_seq = market_stream.bar_seq(symbol, interval)  # 当前信号序号 | Current signal counter
                        full_tick = drive_interval == interval or current_seq != signal_seq  # 是否到了信号周期收盘 | Whether the signal bar closed
                        signal_seq = current_seq  # 更新基线 | Update the baseline
                        bars = market_stream.bars(symbol, interval) if full_tick else market_stream.bars(symbol, drive_interval)  # 取对应窗口 | Pick the matching window
                        # 带口径解析：拿不到新 bar 开盘价时返回 mid_fallback 与半价差，
                        # 由 run_tick 落到结果里并修正买卖价，避免"近似价"被当成"精确开盘价"。
                        # Basis-aware resolution: when the new bar's open is unavailable it returns
                        # mid_fallback plus the half-spread, which run_tick records and applies so an
                        # approximation is never reported as an exact open.
                        price, basis, spread = await market_stream.execution_price_with_basis(symbol, interval if full_tick else drive_interval)  # 解析成交价与口径 | Resolve price and basis
                        await run_strategy_tick(  # 驱动一次 tick | Drive one tick
                            strategy_id,  # 实例 ID | Instance id
                            db=db,  # 会话 | Session
                            candles=bars or None,  # 已收盘窗口（不足时让实现走 REST）| Closed window, or None to fall back
                            execution_price=price or None,  # 事件驱动的开盘价 | Event-driven open price
                            depth=market_stream.depth(symbol),  # 盘口深度 | Book depth
                            risk_only=not full_tick,  # 非信号周期只做风控 | Risk-only outside the signal bar
                            fill_basis=basis,  # 成交价口径 | Fill-price basis
                            spread_ratio=spread,  # 半价差比例 | Half-spread ratio
                        )
                except asyncio.CancelledError:  # 任务被取消时退出 | Exit on task cancellation
                    break  # 跳出轮询循环 | Break out of the loop
                except Exception as exc:  # noqa: BLE001 - 单实例异常不阻塞调度器 | Single instance error must not block the scheduler
                    logger.error(f"❌ 量化实时策略 tick 失败({strategy_id})：{exc}")  # 记录错误但继续轮询 | Log the error and keep polling
                    await asyncio.sleep(1.0)  # 短暂退避，避免异常时忙等 | Brief backoff to avoid a hot loop
        finally:  # 无论何种退出都退订 | Always unsubscribe
            if subscribed:  # 仅退订成功订阅过的键 | Only unsubscribe what was subscribed
                with contextlib.suppress(Exception):  # 退订失败不影响清理 | A failed unsubscribe must not block cleanup
                    await market_stream.unsubscribe(symbol, interval)  # 退订信号周期 | Unsubscribe the signal interval
                    if drive_interval != interval:  # 大周期还需退订 1m | Also unsubscribe the 1m heartbeat
                        await market_stream.unsubscribe(symbol, drive_interval)  # 退订 1m | Unsubscribe 1m
            self._tasks.pop(strategy_id, None)  # 循环结束后清理任务映射 | Remove task from map when loop ends

    async def restore(self) -> None:
        """服务启动时恢复所有 status=1 的实例任务。

        目的 | Purpose:
            查询所有运行中且未删除的实例，并为其恢复轮询任务。
            Restore polling tasks for all running, non-deleted instances on startup.

        返回 | Returns:
            None: 无返回值 | No return value.
        """
        from app.core.database import async_db_session  # 导入异步数据库会话上下文 | Import the async DB session context manager

        async with async_db_session() as db:  # 打开数据库会话 | Open a DB session
            # 查询运行中且未删除的实例 ID 列表
            # Query ids of instances that are running and not deleted.
            rows = (
                await db.execute(
                    select(QuantLiveStrategyModel.id).where(
                        QuantLiveStrategyModel.status == 1,  # 仅状态为运行的实例 | Only running instances
                        QuantLiveStrategyModel.is_deleted.is_(False),  # 且未逻辑删除 | And not soft-deleted
                    )
                )
            ).scalars().all()  # 取回所有匹配的 ID | Retrieve all matching ids
        for strategy_id in rows:  # 遍历需恢复的实例 | Iterate over instances to restore
            if not self.is_running(strategy_id):  # 仅恢复未在运行的任务 | Restore only those not already running
                await self.start(strategy_id)  # 启动该实例的轮询任务 | Start the polling task

    async def shutdown(self) -> None:
        """关闭所有实例任务（不修改数据库状态，重启后可恢复）。

        目的 | Purpose:
            取消并等待所有轮询任务结束，清空任务表；不改数据库状态以便重启恢复。
            Cancel and await all polling tasks, then clear the map, leaving DB
            status untouched so tasks can be restored after restart.

        返回 | Returns:
            None: 无返回值 | No return value.
        """
        for task in list(self._tasks.values()):  # 遍历所有任务的快照 | Iterate over a snapshot of all tasks
            if not task.done():  # 仅取消未完成的任务 | Cancel only unfinished tasks
                task.cancel()  # 请求取消任务 | Request task cancellation
        for task in list(self._tasks.values()):  # 再次遍历等待任务结束 | Iterate again to await completion
            try:
                await task  # 等待任务结束 | Await the task
            except asyncio.CancelledError:  # 捕获取消异常属正常 | CancelledError is expected
                pass  # 忽略取消异常 | Ignore the cancellation error
        self._tasks.clear()  # 清空任务映射表 | Clear the task map


runtime_manager = RuntimeManager()  # 模块级单例调度器 | Module-level singleton scheduler
