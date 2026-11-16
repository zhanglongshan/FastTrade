"""量化回测接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Security
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import ResponseSchema, SuccessResponse
from app.core.base_schema import AuthSchema
from app.core.dependencies import AuthPermission, db_getter
from app.core.router_class import OperationLogRoute

from ..schema import QuantBacktestCreateSchema, QuantBacktestOutSchema
from .service import create_backtest, get_backtest, list_backtests

QuantBacktestRouter = APIRouter(route_class=OperationLogRoute, prefix="/backtest", tags=["量化-回测"])


@QuantBacktestRouter.post(
    "/run",
    summary="执行策略回测",
    response_model=ResponseSchema[QuantBacktestOutSchema],
)
async def run_quant_backtest_controller(
    data: QuantBacktestCreateSchema,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:backtest:run"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """用指定数据集与策略执行回测，返回汇总结果。"""
    obj = await create_backtest(data, db=db, auth=auth)
    return SuccessResponse(data=QuantBacktestOutSchema.model_validate(obj), msg="回测完成")


@QuantBacktestRouter.get(
    "/list",
    summary="查询回测记录列表",
    response_model=ResponseSchema[list[QuantBacktestOutSchema]],
)
async def list_quant_backtests_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:backtest:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    limit: Annotated[int, Query(ge=1, le=500, description="返回条数")] = 100,
):
    """查询历史回测记录。"""
    items = await list_backtests(db, limit=limit)
    return SuccessResponse(data=[QuantBacktestOutSchema.model_validate(item) for item in items], msg="查询成功")


@QuantBacktestRouter.get(
    "/detail/{backtest_id}",
    summary="查询回测详情",
    response_model=ResponseSchema[QuantBacktestOutSchema],
)
async def get_quant_backtest_controller(
    backtest_id: int,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:backtest:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """查询单条回测记录，含净值曲线与成交明细。"""
    obj = await get_backtest(db, backtest_id)
    return SuccessResponse(data=QuantBacktestOutSchema.model_validate(obj), msg="查询成功")
