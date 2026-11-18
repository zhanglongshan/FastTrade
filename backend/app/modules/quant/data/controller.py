"""量化数据接口：下载、导入与数据集查询。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Security
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import ResponseSchema, SuccessResponse
from app.core.base_schema import AuthSchema
from app.core.dependencies import AuthPermission, db_getter
from app.core.router_class import OperationLogRoute

from ..schema import (
    QuantCandlesOutSchema,
    QuantDataDownloadSchema,
    QuantDataImportSchema,
    QuantDatasetOutSchema,
    QuantLocalFileSchema,
)
from .service import (
    download_binance_data,
    get_dataset,
    import_local_csv,
    list_datasets,
    list_local_csv_files,
    read_dataset_candles,
)

QuantDataRouter = APIRouter(route_class=OperationLogRoute, prefix="/data", tags=["量化-数据"])


@QuantDataRouter.get(
    "/list",
    summary="查询量化数据集列表",
    response_model=ResponseSchema[list[QuantDatasetOutSchema]],
)
async def list_quant_datasets_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    symbol: Annotated[str | None, Query(description="按交易对筛选")] = None,
    interval: Annotated[str | None, Query(description="按周期筛选")] = None,
    market: Annotated[str | None, Query(description="按市场筛选：spot-现货，futures-合约")] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="返回条数")] = 100,
):
    """查询已登记的本地数据集。"""
    datasets = await list_datasets(db, symbol=symbol, interval=interval, market=market, limit=limit)
    return SuccessResponse(data=[QuantDatasetOutSchema.model_validate(item) for item in datasets], msg="查询成功")


@QuantDataRouter.get(
    "/files",
    summary="查询可导入的本地 CSV 文件",
    response_model=ResponseSchema[list[QuantLocalFileSchema]],
)
async def list_quant_files_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:query"]))],
):
    """列出允许导入的本地 CSV 文件。"""
    return SuccessResponse(data=[QuantLocalFileSchema.model_validate(item) for item in list_local_csv_files()], msg="查询成功")


@QuantDataRouter.get(
    "/detail/{dataset_id}",
    summary="查询数据集详情",
    response_model=ResponseSchema[QuantDatasetOutSchema],
)
async def get_quant_dataset_controller(
    dataset_id: int,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """查询单个数据集。"""
    obj = await get_dataset(db, dataset_id)
    return SuccessResponse(data=QuantDatasetOutSchema.model_validate(obj), msg="查询成功")


@QuantDataRouter.get(
    "/candles/{dataset_id}",
    summary="查询数据集 K 线（图表用）",
    response_model=ResponseSchema[QuantCandlesOutSchema],
)
async def get_quant_candles_controller(
    dataset_id: int,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    limit: Annotated[int, Query(ge=1, le=20000, description="返回条数，默认最后 2000 根")] = 2000,
    tail: Annotated[bool, Query(description="True 取最后 limit 根，False 取最前 limit 根")] = True,
):
    """读取已登记数据集的 K 线，供图表与明细表格展示。"""
    result = await read_dataset_candles(db, dataset_id, limit=limit, tail=tail)
    return SuccessResponse(data=QuantCandlesOutSchema.model_validate(result), msg="查询成功")


@QuantDataRouter.post(
    "/download",
    summary="下载币安 K 线数据",
    response_model=ResponseSchema[dict],
)
async def download_quant_data_controller(
    data: QuantDataDownloadSchema,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:download"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """从币安下载指定交易对与周期的 K 线到本地 CSV。"""
    result = await download_binance_data(data, db=db, auth=auth)
    return SuccessResponse(data=result, msg="数据下载完成")


@QuantDataRouter.post(
    "/import",
    summary="导入本地 CSV 数据",
    response_model=ResponseSchema[dict],
)
async def import_quant_data_controller(
    data: QuantDataImportSchema,
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:import"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
):
    """导入本地 CSV 并登记数据集。"""
    result = await import_local_csv(data, db=db, auth=auth)
    return SuccessResponse(data=result, msg="数据导入完成")
