"""A股/期货量化接口：数据集、K线、回测、模拟盘/实盘。

通过工厂函数为两个市场各生成一个 APIRouter，业务逻辑在 service 中按 market key
复用。权限标识按市场区分：module_quant_astock:* / module_quant_futures:*
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Security
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import ResponseSchema, SuccessResponse
from app.core.base_schema import AuthSchema
from app.core.dependencies import AuthPermission, db_getter, db_getter_autocommit
from app.core.router_class import OperationLogRoute

from . import service
from .schema import (
    MarketBacktestCreateSchema,
    MarketBacktestOutSchema,
    MarketCandlesOutSchema,
    MarketDatasetOutSchema,
    MarketImportSchema,
    MarketLiveCreateSchema,
    MarketLiveOutSchema,
    MarketLocalFileSchema,
    MarketTickSchema,
)


def _serializer(schema_type):
    """把 ORM 对象转换为响应模型（日期字段转 ISO 字符串）。"""
    def convert(obj):
        data = {c.name: getattr(obj, c.name, None) for c in obj.__table__.columns}
        for key, value in list(data.items()):
            if hasattr(value, "isoformat"):
                data[key] = value.isoformat()
        return schema_type(**data)
    return convert


def build_router(market_key: str, label: str) -> APIRouter:
    """为指定市场构建路由。"""
    perm = f"module_quant_{market_key}"
    router = APIRouter(
        route_class=OperationLogRoute,
        tags=[f"量化-{label}"],
    )
    ds_out = _serializer(MarketDatasetOutSchema)
    bt_out = _serializer(MarketBacktestOutSchema)
    live_out = _serializer(MarketLiveOutSchema)

    @router.get("/datasets", summary=f"查询{label}数据集", response_model=ResponseSchema[list[MarketDatasetOutSchema]])
    async def list_datasets_controller(
        auth: Annotated[AuthSchema, Security(AuthPermission([f"{perm}:data:query"]))],
        db: Annotated[AsyncSession, Depends(db_getter)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        rows = await service.list_datasets(market_key, db, limit=limit)
        return SuccessResponse(data=[ds_out(item) for item in rows], msg="查询成功")

    @router.get("/files", summary=f"查询可导入的{label}本地 CSV", response_model=ResponseSchema[list[MarketLocalFileSchema]])
    async def files_controller(
        auth: Annotated[AuthSchema, Security(AuthPermission([f"{perm}:data:query"]))],
    ):
        result = service.list_local_csv_files(market_key)
        return SuccessResponse(data=[MarketLocalFileSchema.model_validate(item) for item in result], msg="查询成功")

    @router.post("/import", summary=f"导入{label}本地 CSV", response_model=ResponseSchema[dict])
    async def import_controller(
        data: MarketImportSchema,
        auth: Annotated[AuthSchema, Security(AuthPermission([f"{perm}:data:import"]))],
        db: Annotated[AsyncSession, Depends(db_getter)],
    ):
        result = await service.import_dataset(market_key, data, db=db, auth=auth)
        return SuccessResponse(data=result, msg="导入完成")

    @router.get("/candles/{dataset_id}", summary=f"查询{label}K线", response_model=ResponseSchema[MarketCandlesOutSchema])
    async def candles_controller(
        dataset_id: int,
        auth: Annotated[AuthSchema, Security(AuthPermission([f"{perm}:data:query"]))],
        db: Annotated[AsyncSession, Depends(db_getter)],
        limit: Annotated[int, Query(ge=1, le=20000)] = 2000,
    ):
        result = await service.read_candles(market_key, db, dataset_id, limit=limit)
        return SuccessResponse(data=MarketCandlesOutSchema(**result), msg="查询成功")

    @router.post("/backtest", summary=f"执行{label}回测", response_model=ResponseSchema[MarketBacktestOutSchema])
    async def backtest_controller(
        data: MarketBacktestCreateSchema,
        auth: Annotated[AuthSchema, Security(AuthPermission([f"{perm}:backtest:query"]))],
        db: Annotated[AsyncSession, Depends(db_getter_autocommit)],
    ):
        # 使用不带外层 begin() 的会话：create_backtest 内部需自行 commit + refresh 分步落库。
        obj = await service.create_backtest(market_key, data, db=db, auth=auth)
        return SuccessResponse(data=bt_out(obj), msg="回测完成")

    @router.get("/backtests", summary=f"查询{label}回测记录", response_model=ResponseSchema[list[MarketBacktestOutSchema]])
    async def backtests_controller(
        auth: Annotated[AuthSchema, Security(AuthPermission([f"{perm}:backtest:query"]))],
        db: Annotated[AsyncSession, Depends(db_getter)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        rows = await service.list_backtests(market_key, db, limit=limit)
        return SuccessResponse(data=[bt_out(item) for item in rows], msg="查询成功")

    @router.post("/live", summary=f"创建{label}实时策略实例", response_model=ResponseSchema[MarketLiveOutSchema])
    async def create_live_controller(
        data: MarketLiveCreateSchema,
        auth: Annotated[AuthSchema, Security(AuthPermission([f"{perm}:live:query"]))],
        db: Annotated[AsyncSession, Depends(db_getter_autocommit)],
    ):
        # service.create_live 内部自行 commit + refresh，须用不带外层 begin() 的会话。
        obj = await service.create_live(market_key, data, db=db, auth=auth)
        return SuccessResponse(data=live_out(obj), msg="创建成功")

    @router.get("/live", summary=f"查询{label}实时策略实例", response_model=ResponseSchema[list[MarketLiveOutSchema]])
    async def list_live_controller(
        auth: Annotated[AuthSchema, Security(AuthPermission([f"{perm}:live:query"]))],
        db: Annotated[AsyncSession, Depends(db_getter)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        rows = await service.list_live(market_key, db, limit=limit)
        return SuccessResponse(data=[live_out(item) for item in rows], msg="查询成功")

    @router.post("/live/{strategy_id}/tick", summary=f"运行一次{label}tick", response_model=ResponseSchema[MarketLiveOutSchema])
    async def tick_controller(
        strategy_id: int,
        data: MarketTickSchema,
        auth: Annotated[AuthSchema, Security(AuthPermission([f"{perm}:live:query"]))],
        db: Annotated[AsyncSession, Depends(db_getter_autocommit)],
    ):
        # service.run_tick 内部自行 commit + refresh，须用不带外层 begin() 的会话。
        obj = await service.run_tick(market_key, strategy_id, data, db=db)
        return SuccessResponse(data=live_out(obj), msg="tick 完成")

    return router


AStockRouter = build_router("astock", "A股")
FuturesRouter = build_router("futures", "期货")
