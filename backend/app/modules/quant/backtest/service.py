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


def _naive_utc() -> datetime:
    """返回 naive UTC，适配 TIMESTAMP WITHOUT TIME ZONE 列。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _market_of(dataset) -> str:
    """优先取数据集的市场字段，旧数据兜底按 source 判断。"""
    market = getattr(dataset, "market", None)
    if market in {"spot", "futures"}:
        return market
    return "futures" if "futures" in str(getattr(dataset, "source", "")).lower() else "spot"


async def create_backtests(
    params: QuantBacktestCreateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> list[QuantBacktestModel]:
    """按选中的数据集逐个执行回测，每个数据集生成一条独立记录。

    单个数据集失败不会中断其他数据集：失败记录 status=2 并保存 error，
    页面可逐个查看结果。交易对、周期、市场均来自数据集本身。
    """
    info = load_strategy(params.strategy_name)
    # 先统一校验，避免跑到一半才因某个数据集不存在而中断
    datasets = [await get_dataset(db, dataset_id) for dataset_id in params.dataset_ids]
    multi = len(datasets) > 1
    timestamp = f"{datetime.now(UTC):%m%d%H%M%S}"
    user_id = getattr(getattr(auth, "user", None), "id", None)

    results: list[QuantBacktestModel] = []
    for dataset in datasets:
        market = _market_of(dataset)
        if params.name:
            name = f"{params.name}_{dataset.symbol}_{dataset.interval}" if multi else params.name
        else:
            name = f"{dataset.symbol}_{dataset.interval}_{info.name}_{timestamp}"

        obj = QuantBacktestModel(
            name=name,
            strategy_name=info.name,
            symbol=dataset.symbol,
            interval=dataset.interval,
            market=market,
            initial_cash=params.initial_cash,
            fee_rate=params.fee_rate,
            status=0,
            started_time=_naive_utc(),
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
                # 现货不允许做空，合约允许
                allow_short=market == "futures",
            )
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落库，便于页面排查
            obj.status = 2
            obj.error = str(exc)
            obj.finished_time = _naive_utc()
            await db.commit()
            await db.refresh(obj)
            results.append(obj)
            continue

        obj.status = 1
        obj.result = result
        obj.error = None
        obj.finished_time = _naive_utc()
        await db.commit()
        await db.refresh(obj)
        results.append(obj)

    return results


async def create_backtest(
    params: QuantBacktestCreateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> QuantBacktestModel:
    """单数据集回测（CLI 等旧调用方兼容），内部复用多选实现。"""
    results = await create_backtests(params, db=db, auth=auth)
    obj = results[0]
    if obj.status == 2:
        raise CustomException(msg=f"回测失败：{obj.error}")
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
