"""量化实时策略接口：模拟盘与实盘共用，按 mode 区分执行器。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Security
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import ResponseSchema, SuccessResponse
from app.core.base_schema import AuthSchema
from app.core.dependencies import AuthPermission, db_getter
from app.core.router_class import OperationLogRoute

from ..schema import (
    QuantLiveStrategyCreateSchema,
    QuantLiveStrategyOutSchema,
    QuantLiveStrategyUpdateSchema,
    QuantTradeRecordOutSchema,
)
from .service import (
    create_live_strategy,
    delete_live_strategy,
    get_live_strategy,
    list_live_strategies,
    list_trade_records,
    run_strategy_tick,
    runtime_manager,
    to_out,
    update_live_strategy,
)

QuantLiveRouter = APIRouter(route_class=OperationLogRoute, prefix="/live", tags=["量化-实时策略"])


@QuantLiveRouter.post(
    "/create",
    summary="创建实时策略实例（模拟盘/实盘）",
    response_model=ResponseSchema[QuantLiveStrategyOutSchema],
)
async def create_live_strategy_controller(
    data: QuantLiveStrategyCreateSchema,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:create"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """创建模拟盘或实盘策略实例。"""
    obj = await create_live_strategy(data, db=db, auth=auth)
    return SuccessResponse(data=to_out(obj), msg="实例创建成功")


@QuantLiveRouter.get(
    "/list",
    summary="查询实时策略实例列表",
    response_model=ResponseSchema[list[QuantLiveStrategyOutSchema]],
)
async def list_live_strategies_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    mode: Annotated[str | None, Query(description="按模式过滤：paper/live")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """查询实时策略实例。"""
    items = await list_live_strategies(db, mode=mode, limit=limit)
    return SuccessResponse(data=[to_out(item) for item in items], msg="查询成功")


@QuantLiveRouter.get(
    "/detail/{strategy_id}",
    summary="查询实时策略实例详情",
    response_model=ResponseSchema[QuantLiveStrategyOutSchema],
)
async def get_live_strategy_controller(
    strategy_id: int,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """查询单个实时策略实例（不含密钥明文）。"""
    obj = await get_live_strategy(strategy_id, db)
    return SuccessResponse(data=to_out(obj), msg="查询成功")


@QuantLiveRouter.put(
    "/update/{strategy_id}",
    summary="更新实时策略实例",
    response_model=ResponseSchema[QuantLiveStrategyOutSchema],
)
async def update_live_strategy_controller(
    strategy_id: int,
    data: QuantLiveStrategyUpdateSchema,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:update"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """更新实例的安全字段（费率、滑点、dry_run、密钥等）。"""
    obj = await update_live_strategy(strategy_id, data, db=db, auth=auth)
    return SuccessResponse(data=to_out(obj), msg="更新成功")


@QuantLiveRouter.post(
    "/start/{strategy_id}",
    summary="启动实时策略",
    response_model=ResponseSchema[dict],
)
async def start_live_strategy_controller(
    strategy_id: int,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:start"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """启动实例轮询任务。"""
    await get_live_strategy(strategy_id, db)
    await runtime_manager.start(strategy_id)
    return SuccessResponse(data={"strategy_id": strategy_id, "status": 1}, msg="已启动")


@QuantLiveRouter.post(
    "/stop/{strategy_id}",
    summary="停止实时策略",
    response_model=ResponseSchema[dict],
)
async def stop_live_strategy_controller(
    strategy_id: int,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:start"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """停止实例轮询任务。"""
    await get_live_strategy(strategy_id, db)
    await runtime_manager.stop(strategy_id)
    return SuccessResponse(data={"strategy_id": strategy_id, "status": 0}, msg="已停止")


@QuantLiveRouter.post(
    "/tick/{strategy_id}",
    summary="手动运行一次 tick",
    response_model=ResponseSchema[dict],
)
async def tick_live_strategy_controller(
    strategy_id: int,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:run"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """立即拉取最新行情运行一次策略（无需等待轮询）。"""
    result = await run_strategy_tick(strategy_id, db=db)
    return SuccessResponse(data=result.to_dict(), msg="运行完成")


@QuantLiveRouter.get(
    "/trades/{strategy_id}",
    summary="查询实例成交流水",
    response_model=ResponseSchema[list[QuantTradeRecordOutSchema]],
)
async def list_trade_records_controller(
    strategy_id: int,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    """查询实例的实时成交流水。"""
    items = await list_trade_records(db, strategy_id=strategy_id, limit=limit)
    return SuccessResponse(data=[QuantTradeRecordOutSchema.model_validate(item) for item in items], msg="查询成功")


@QuantLiveRouter.delete(
    "/delete/{strategy_id}",
    summary="删除实时策略实例",
    response_model=ResponseSchema[dict],
)
async def delete_live_strategy_controller(
    strategy_id: int,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:delete"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """删除实时策略实例（先停止任务）。"""
    await delete_live_strategy(strategy_id, db=db)
    return SuccessResponse(data={"strategy_id": strategy_id}, msg="删除成功")
