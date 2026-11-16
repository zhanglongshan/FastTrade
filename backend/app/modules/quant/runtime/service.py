"""实时策略实例服务：模拟盘/实盘的创建、启停、tick 驱动与持久化。

模拟盘与实盘共用同一套策略与执行层，仅执行器不同：
- paper：PaperExecutor 本地虚拟撮合；
- live：LiveExecutor 视 dry_run 决定是否真实下单。

所有实例由进程内 :class:`RuntimeManager` 统一轮询驱动；实例状态（账本、
最近结果、错误）持久化到数据库，服务重启后可恢复运行状态。
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.setting import settings
from app.core.base_schema import AuthSchema
from app.core.base_model import MappedBase
from app.core.exceptions import CustomException
from app.core.logger import logger
from app.utils.crypto_util import CryptoUtil
from app.utils.import_util import ImportUtil

# 注册全部 ORM 模型，确保 UserMixin.creator 等跨模块 relationship 可解析
# （CLI 单独运行时不会经过应用工厂的自动发现，需在此显式触发）
ImportUtil.find_models(MappedBase)

from ..execution.base import AccountLedger, OrderRequest, RunMode
from ..execution.runner import LiveExecutor, PaperExecutor, TickResult, run_tick
from ..model import QuantLiveStrategyModel, QuantTradeRecordModel
from ..schema import (
    QuantLiveStrategyCreateSchema,
    QuantLiveStrategyOutSchema,
    QuantLiveStrategyUpdateSchema,
    QuantTradeRecordOutSchema,
)

# 周期 → tick 间隔秒数（至少为基础间隔）
_INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def _interval_seconds(interval: str) -> int:
    """根据 K 线周期返回建议 tick 间隔（秒）。"""
    base = int(settings.QUANT_TICK_INTERVAL or 60)
    return max(base, _INTERVAL_SECONDS.get(interval, base))


def _user_id(auth: AuthSchema | None) -> int | None:
    """提取当前用户 ID，仅用于审计字段。"""
    return getattr(getattr(auth, "user", None), "id", None)


def _naive_utc() -> datetime:
    """返回不带时区信息的 UTC 时间，适配 TIMESTAMP WITHOUT TIME ZONE 列。"""
    return datetime.now(UTC).replace(tzinfo=None)


def to_out(obj: QuantLiveStrategyModel) -> QuantLiveStrategyOutSchema:
    """模型转响应（密钥永不外泄）。"""
    data = QuantLiveStrategyOutSchema.model_validate(obj)
    data.has_api_key = bool(obj.api_key)
    return data


async def create_live_strategy(
    params: QuantLiveStrategyCreateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> QuantLiveStrategyModel:
    """创建实时策略实例。

    参数:
    - params: 创建参数。
    - db: 数据库会话。
    - auth: 当前用户，仅审计用。

    返回:
    - QuantLiveStrategyModel: 新建实例。
    """
    from ..strategy.loader import load_strategy

    load_strategy(params.strategy_name)  # 提前校验策略存在，避免建出无法运行的实例

    symbol = params.symbol.replace("/", "").replace("_", "").upper()
    ledger = AccountLedger(params.initial_cash, fee_rate=params.fee_rate, slippage=params.slippage)
    obj = QuantLiveStrategyModel(
        name=params.name,
        mode=params.mode,
        strategy_name=params.strategy_name,
        symbol=symbol,
        interval=params.interval,
        initial_cash=params.initial_cash,
        fee_rate=params.fee_rate,
        slippage=params.slippage,
        dry_run=params.dry_run,
        allow_short=params.allow_short,
        status=0,
        account=ledger.snapshot(),
        api_key=CryptoUtil.encrypt(params.api_key) if params.api_key else None,
        api_secret=CryptoUtil.encrypt(params.api_secret) if params.api_secret else None,
        description=params.description,
        created_id=_user_id(auth),
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def update_live_strategy(
    strategy_id: int,
    params: QuantLiveStrategyUpdateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> QuantLiveStrategyModel:
    """更新实时策略实例的安全字段。"""
    obj = await get_live_strategy(strategy_id, db)
    updates = params.model_dump(exclude_unset=True, exclude_none=True)
    for key in ("api_key", "api_secret"):
        if key in updates:
            obj_key = getattr(obj, key)
            # 仅在明文非空或明确要更新时覆盖；空字符串表示清空
            value = updates.pop(key)
            setattr(obj, key, CryptoUtil.encrypt(value) if value else None)
            _ = obj_key
    for key, value in updates.items():
        setattr(obj, key, value)
    user_id = _user_id(auth)
    if user_id is not None:
        obj.updated_id = user_id
    await db.commit()
    await db.refresh(obj)
    return obj


async def list_live_strategies(
    db: AsyncSession,
    *,
    mode: str | None = None,
    limit: int = 100,
) -> list[QuantLiveStrategyModel]:
    """查询实时策略实例列表。"""
    stmt = select(QuantLiveStrategyModel).where(QuantLiveStrategyModel.is_deleted.is_(False))
    if mode:
        stmt = stmt.where(QuantLiveStrategyModel.mode == mode)
    stmt = stmt.order_by(QuantLiveStrategyModel.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def get_live_strategy(strategy_id: int, db: AsyncSession) -> QuantLiveStrategyModel:
    """按 ID 查询实时策略实例。"""
    obj = await db.get(QuantLiveStrategyModel, strategy_id)
    if obj is None or obj.is_deleted:
        raise CustomException(msg="实时策略实例不存在")
    return obj


async def delete_live_strategy(strategy_id: int, *, db: AsyncSession) -> None:
    """删除实例（先停止运行）。"""
    await runtime_manager.stop(strategy_id)
    obj = await get_live_strategy(strategy_id, db)
    obj.is_deleted = True
    await db.commit()


async def list_trade_records(
    db: AsyncSession,
    *,
    strategy_id: int | None = None,
    limit: int = 200,
) -> list[QuantTradeRecordModel]:
    """查询实时成交流水。"""
    stmt = select(QuantTradeRecordModel).where(QuantTradeRecordModel.is_deleted.is_(False))
    if strategy_id is not None:
        stmt = stmt.where(QuantTradeRecordModel.strategy_id == strategy_id)
    stmt = stmt.order_by(QuantTradeRecordModel.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


def _build_executor(
    obj: QuantLiveStrategyModel,
    ledger: AccountLedger,
) -> PaperExecutor | LiveExecutor:
    """按实例模式构造执行器。"""
    if obj.mode == RunMode.PAPER.value:
        return PaperExecutor(ledger, fee_rate=obj.fee_rate)
    api_key = CryptoUtil.decrypt(obj.api_key) if obj.api_key else ""
    api_secret = CryptoUtil.decrypt(obj.api_secret) if obj.api_secret else ""
    return LiveExecutor(
        ledger,
        api_key=api_key,
        api_secret=api_secret,
        dry_run=bool(obj.dry_run),
    )


async def run_strategy_tick(
    strategy_id: int,
    *,
    db: AsyncSession,
    candles: list | None = None,
) -> TickResult:
    """驱动单个实例运行一次 tick，并持久化账本、结果、成交与状态。

    参数:
    - strategy_id: 实例 ID。
    - db: 数据库会话。
    - candles: 可选的本地 K 线窗口；传入时不请求币安（便于离线验证）。

    返回:
    - TickResult: 本次 tick 结果。
    """
    obj = await get_live_strategy(strategy_id, db)
    ledger = AccountLedger.from_snapshot(obj.account, fee_rate=obj.fee_rate, slippage=obj.slippage)
    executor = _build_executor(obj, ledger)
    allow_short = bool(obj.allow_short) if obj.mode == RunMode.PAPER.value else bool(obj.allow_short and obj.dry_run)

    try:
        result = await run_tick(
            mode=obj.mode,
            executor=executor,
            ledger=ledger,
            strategy_name=obj.strategy_name,
            symbol=obj.symbol,
            interval=obj.interval,
            candles=candles,
            allow_short=allow_short,
        )
    except Exception as exc:  # noqa: BLE001 - 任何失败落库，供页面排查
        obj.status = 2
        obj.last_error = str(exc)
        obj.last_tick_time = _naive_utc()
        await db.commit()
        raise

    # 持久化账户与最近结果
    obj.account = executor.snapshot()
    obj.last_result = result.to_dict()
    obj.last_error = result.error
    obj.last_tick_time = _naive_utc()
    if obj.status == 2:
        obj.status = 1  # 异常后成功运行，自动恢复运行状态

    # 落成交流水
    for fill in result.fills:
        trade_time: datetime | None = None
        if fill.get("timestamp"):
            try:
                trade_time = datetime.fromisoformat(str(fill["timestamp"])).replace(tzinfo=None)
            except ValueError:
                trade_time = _naive_utc()
        db.add(
            QuantTradeRecordModel(
                strategy_id=obj.id,
                mode=result.mode,
                symbol=result.symbol,
                side=fill["side"],
                qty=float(fill["qty"]),
                price=float(fill["price"]),
                fee=float(fill["fee"]),
                pnl=(float(fill["pnl"]) if fill.get("pnl") is not None else None),
                reason=fill.get("reason"),
                trade_time=trade_time or _naive_utc(),
                created_id=obj.created_id,
            )
        )
    await db.commit()
    await db.refresh(obj)
    return result


class RuntimeManager:
    """进程内实时策略调度器：为每个运行中的实例维护一个 asyncio 任务。"""

    def __init__(self) -> None:
        """初始化调度器。"""
        self._tasks: dict[int, asyncio.Task] = {}
        self._running = False

    def is_running(self, strategy_id: int) -> bool:
        """实例是否已有运行任务。"""
        task = self._tasks.get(strategy_id)
        return task is not None and not task.done()

    async def start(self, strategy_id: int) -> None:
        """启动实例轮询任务。"""
        from app.core.database import async_db_session

        async with async_db_session() as db:
            obj = await get_live_strategy(strategy_id, db)
            if obj.status == 1 and self.is_running(strategy_id):
                return
            obj.status = 1
            obj.last_error = None
            await db.commit()

        if self.is_running(strategy_id):
            return
        task = asyncio.create_task(self._loop(strategy_id), name=f"quant-live-{strategy_id}")
        self._tasks[strategy_id] = task
        logger.info(f"✅ 量化实时策略启动：{strategy_id} ({obj.mode})")

    async def stop(self, strategy_id: int) -> None:
        """停止实例轮询任务并落库状态。"""
        from app.core.database import async_db_session

        task = self._tasks.pop(strategy_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with async_db_session() as db:
            obj = await db.get(QuantLiveStrategyModel, strategy_id)
            if obj and not obj.is_deleted:
                obj.status = 0
                await db.commit()

    async def _loop(self, strategy_id: int) -> None:
        """轮询循环：按实例周期定时 tick。"""
        from app.core.database import async_db_session

        interval = 60
        async with async_db_session() as db:
            obj = await db.get(QuantLiveStrategyModel, strategy_id)
            if obj:
                interval = _interval_seconds(obj.interval)
        while True:
            try:
                await asyncio.sleep(interval)
                async with async_db_session() as db:
                    obj = await db.get(QuantLiveStrategyModel, strategy_id)
                    if obj is None or obj.is_deleted or obj.status != 1:
                        break
                    await run_strategy_tick(strategy_id, db=db)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - 单实例异常不阻塞调度器
                logger.error(f"❌ 量化实时策略 tick 失败({strategy_id})：{exc}")
        self._tasks.pop(strategy_id, None)

    async def restore(self) -> None:
        """服务启动时恢复所有 status=1 的实例任务。"""
        from app.core.database import async_db_session

        async with async_db_session() as db:
            rows = (
                await db.execute(
                    select(QuantLiveStrategyModel.id).where(
                        QuantLiveStrategyModel.status == 1,
                        QuantLiveStrategyModel.is_deleted.is_(False),
                    )
                )
            ).scalars().all()
        for strategy_id in rows:
            if not self.is_running(strategy_id):
                await self.start(strategy_id)

    async def shutdown(self) -> None:
        """关闭所有实例任务（不修改数据库状态，重启后可恢复）。"""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()


runtime_manager = RuntimeManager()
