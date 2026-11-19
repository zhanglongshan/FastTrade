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

import asyncio  # 导入 asyncio 异步并发库 | Import asyncio for task scheduling and sleep
from datetime import UTC, datetime  # 从 datetime 导入 UTC 与 datetime | Import UTC and datetime for timestamp handling
from typing import Any  # 从 typing 导入 Any | Import Any for loose/optional type hints

from sqlalchemy import select  # 从 sqlalchemy 导入 select | Import select to build ORM query statements
from sqlalchemy.ext.asyncio import AsyncSession  # 从 sqlalchemy.ext.asyncio 导入 AsyncSession | Import AsyncSession as the async DB session type

from app.config.setting import settings  # 导入全局配置对象 | Import global settings for the tick interval configuration
from app.core.base_schema import AuthSchema  # 导入认证 Schema | Import AuthSchema for extracting the auditing user
from app.core.base_model import MappedBase  # 导入 ORM 声明基类 | Import MappedBase to register all ORM models
from app.core.exceptions import CustomException  # 导入自定义业务异常 | Import CustomException for domain-level errors
from app.core.logger import logger  # 导入全局日志器 | Import logger for runtime logging
from app.utils.crypto_util import CryptoUtil  # 导入加解密工具 | Import CryptoUtil for encrypting/decrypting API secrets
from app.utils.import_util import ImportUtil  # 导入模块发现工具 | Import ImportUtil to discover and register models

# 注册全部 ORM 模型，确保 UserMixin.creator 等跨模块 relationship 可解析
# （CLI 单独运行时不会经过应用工厂的自动发现，需在此显式触发）
# 中文：显式触发模型发现，保证跨模块关系映射可用。
# English: Explicitly trigger model discovery so cross-module relationships resolve.
ImportUtil.find_models(MappedBase)  # 触发所有 ORM 模型注册 | Trigger registration of all ORM models

from ..execution.base import AccountLedger, OrderRequest, RunMode  # 导入账本、订单请求与运行模式 | Import ledger, order request and run mode enums
from ..execution.runner import LiveExecutor, PaperExecutor, TickResult, run_tick  # 导入执行器与 tick 驱动 | Import executors and the tick driver
from ..model import QuantLiveStrategyModel, QuantTradeRecordModel  # 导入实时策略与成交流水模型 | Import live strategy and trade record models
from ..schema import (  # 从 schema 导入相关请求/响应模型 | Import related request/response schemas
    QuantLiveStrategyCreateSchema,  # 创建策略请求模型 | Create-strategy request schema
    QuantLiveStrategyOutSchema,  # 策略响应模型 | Strategy output schema
    QuantLiveStrategyUpdateSchema,  # 更新策略请求模型 | Update-strategy request schema
    QuantTradeRecordOutSchema,  # 成交流水响应模型 | Trade record output schema
)

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


def _interval_seconds(interval: str) -> int:
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
    ledger = AccountLedger(params.initial_cash, fee_rate=params.fee_rate, slippage=params.slippage)  # 按初始资金/费率/滑点建账户账本 | Build ledger from cash, fee and slippage
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
) -> TickResult:
    """驱动单个实例运行一次 tick，并持久化账本、结果、成交与状态。

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        db: 数据库会话 | Async DB session.
        candles: 可选的本地 K 线窗口；传入时不请求币安（便于离线验证）
            | Optional local candle window; when provided, Binance is not called.

    返回 | Returns:
        TickResult: 本次 tick 结果 | The tick result.

    流程 | Flow:
        取实例与账本 -> 构造执行器 -> 运行 tick（异常则落库并抛出）-> 持久化
        账户/结果/状态 -> 落成交流水 -> 提交返回。
        Fetch instance/ledger -> build executor -> run tick (persist+raise on
        error) -> persist account/result/status -> persist fills -> commit.
    """
    obj = await get_live_strategy(strategy_id, db)  # 查询实例（不存在则抛异常）| Fetch instance or raise
    ledger = AccountLedger.from_snapshot(obj.account, fee_rate=obj.fee_rate, slippage=obj.slippage)  # 从快照恢复账户账本 | Restore ledger from snapshot
    executor = _build_executor(obj, ledger)  # 按模式构造执行器 | Build executor by mode
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
        )
    except Exception as exc:  # noqa: BLE001 - 任何失败落库，供页面排查 | Persist any failure for UI troubleshooting
        obj.status = 2  # 标记异常状态 | Mark error status
        obj.last_error = str(exc)  # 记录错误文案 | Store error message
        obj.last_tick_time = _naive_utc()  # 记录 tick 时间 | Record tick time
        await db.commit()  # 提交错误信息 | Commit the error info
        raise  # 重新抛出异常交由调用方处理 | Re-raise for the caller

    # 持久化账户与最近结果 | Persist account snapshot and latest result
    obj.account = executor.snapshot()  # 写回最新账户快照 | Write back latest account snapshot
    obj.last_result = result.to_dict()  # 写回最近一次 tick 结果 | Write back latest tick result
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
        """轮询循环：按实例周期定时 tick。

        目的 | Purpose:
            按实例周期休眠并反复驱动 tick，直到被取消或实例不再运行。
            Sleep by the instance interval and repeatedly drive ticks until
            cancelled or the instance is no longer running.

        参数 | Parameters:
            strategy_id: 实例 ID | Instance ID.

        返回 | Returns:
            None: 无返回值 | No return value.
        """
        from app.core.database import async_db_session  # 导入异步数据库会话上下文 | Import the async DB session context manager

        interval = 60  # 默认间隔 60 秒 | Default interval of 60 seconds
        async with async_db_session() as db:  # 打开数据库会话读取周期 | Open a DB session to read the interval
            obj = await db.get(QuantLiveStrategyModel, strategy_id)  # 按主键查询实例 | Fetch instance by primary key
            if obj:  # 实例存在时按周期计算间隔 | Compute interval from period when instance exists
                interval = _interval_seconds(obj.interval)  # 取该周期对应的 tick 间隔 | Get the tick interval for this period
        while True:  # 无限轮询直到取消或退出条件 | Poll forever until cancelled or break
            try:
                await asyncio.sleep(interval)  # 按间隔休眠 | Sleep for the interval
                async with async_db_session() as db:  # 每轮打开新会话 | Open a fresh session each round
                    obj = await db.get(QuantLiveStrategyModel, strategy_id)  # 每轮重新读取实例状态 | Re-read instance state each round
                    if obj is None or obj.is_deleted or obj.status != 1:  # 实例失效或不在运行则退出 | Exit if missing, deleted or not running
                        break  # 跳出轮询循环 | Break out of the loop
                    await run_strategy_tick(strategy_id, db=db)  # 驱动一次 tick | Drive one tick
            except asyncio.CancelledError:  # 任务被取消时退出 | Exit on task cancellation
                break  # 跳出轮询循环 | Break out of the loop
            except Exception as exc:  # noqa: BLE001 - 单实例异常不阻塞调度器 | Single instance error must not block the scheduler
                logger.error(f"❌ 量化实时策略 tick 失败({strategy_id})：{exc}")  # 记录错误但继续轮询 | Log the error and keep polling
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
