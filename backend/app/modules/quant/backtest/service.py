"""回测任务服务：校验数据集与策略，执行回测并持久化结果。"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_schema import AuthSchema
from app.core.exceptions import CustomException

from ..data.service import get_dataset, resolve_dataset_csv
from ..model import QuantBacktestModel
from ..schema import QuantBacktestCreateSchema
from ..strategy.loader import load_strategy
from .engine import load_candles, run_backtest


async def create_backtest(
    params: QuantBacktestCreateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> QuantBacktestModel:
    """创建并同步执行回测。

    参数:
    - params: 回测参数。
    - db: 数据库会话。
    - auth: 当前用户，仅用于审计字段。

    返回:
    - QuantBacktestModel: 已完成的回测记录。
    """
    dataset = await get_dataset(db, params.dataset_id)
    info = load_strategy(params.strategy_name)
    user_id = getattr(getattr(auth, "user", None), "id", None)
    name = params.name or f"{dataset.symbol}_{dataset.interval}_{info.name}_{datetime.now(UTC):%m%d%H%M%S}"

    obj = QuantBacktestModel(
        name=name,
        strategy_name=info.name,
        symbol=dataset.symbol,
        interval=dataset.interval,
        initial_cash=params.initial_cash,
        fee_rate=params.fee_rate,
        status=0,
        started_time=datetime.now(UTC),
        description=params.description,
        created_id=user_id,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)

    try:
        csv_path = resolve_dataset_csv(dataset.file_path)
        candles = await asyncio.to_thread(load_candles, csv_path)
        result = await asyncio.to_thread(
            run_backtest,
            candles,
            info.cls,
            mode=info.mode,
            symbol=dataset.symbol,
            interval=dataset.interval,
            initial_cash=params.initial_cash,
            fee_rate=params.fee_rate,
            slippage=params.slippage,
        )
    except Exception as exc:  # noqa: BLE001 - 任何失败都要落库，便于页面排查
        obj.status = 2
        obj.error = str(exc)
        obj.finished_time = datetime.now(UTC)
        await db.commit()
        await db.refresh(obj)
        if isinstance(exc, CustomException):
            raise
        raise CustomException(msg=f"回测失败：{exc}") from exc

    obj.status = 1
    obj.result = result
    obj.error = None
    obj.finished_time = datetime.now(UTC)
    await db.commit()
    await db.refresh(obj)
    return obj


async def list_backtests(db: AsyncSession, *, limit: int = 100) -> list[QuantBacktestModel]:
    """查询回测记录。"""
    stmt = (
        select(QuantBacktestModel)
        .where(QuantBacktestModel.is_deleted.is_(False))
        .order_by(QuantBacktestModel.id.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_backtest(db: AsyncSession, backtest_id: int) -> QuantBacktestModel:
    """按 ID 查询回测记录。"""
    obj = await db.get(QuantBacktestModel, backtest_id)
    if obj is None or obj.is_deleted:
        raise CustomException(msg="回测记录不存在")
    return obj
