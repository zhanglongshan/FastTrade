"""策略查询接口。"""

from typing import Annotated

from fastapi import APIRouter, Security

from app.common.response import ResponseSchema, SuccessResponse
from app.core.base_schema import AuthSchema
from app.core.dependencies import AuthPermission
from app.core.router_class import OperationLogRoute

from ..schema import QuantStrategyOutSchema
from .loader import load_strategies

QuantStrategyRouter = APIRouter(route_class=OperationLogRoute, prefix="/strategies", tags=["量化-策略"])


@QuantStrategyRouter.get(
    "",
    summary="查询可用策略",
    response_model=ResponseSchema[list[QuantStrategyOutSchema]],
)
async def list_quant_strategies_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:strategy:query"]))],
):
    """列出 user_data/strategies 下自动发现的全部策略。"""
    items = [
        QuantStrategyOutSchema(
            name=info.name,
            file=info.file,
            mode=info.mode,
            timeframe=info.timeframe,
            doc=info.doc,
        )
        for info in load_strategies()
    ]
    return SuccessResponse(data=items, msg="查询成功")
