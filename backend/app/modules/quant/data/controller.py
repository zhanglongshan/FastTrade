"""量化数据接口：下载、导入与数据集查询 | Quant data API endpoints: download, import and dataset queries."""

from typing import Annotated  # 导入 Annotated，用于给参数附加依赖注入/校验元数据 | Import Annotated for attaching DI/validation metadata to parameters

from fastapi import APIRouter, Depends, Query, Security  # 导入路由、依赖、查询参数与安全依赖 | Import router, dependency, query and security primitives
from sqlalchemy.ext.asyncio import AsyncSession  # 导入异步数据库会话类型 | Import the async database session type

from app.common.response import ResponseSchema, SuccessResponse  # 导入统一响应包装 | Import unified response wrappers
from app.core.base_schema import AuthSchema  # 导入认证信息模型 | Import the auth schema
from app.core.dependencies import AuthPermission, db_getter  # 导入权限校验与数据库会话依赖 | Import permission and DB session dependencies
from app.core.router_class import OperationLogRoute  # 导入带操作日志的自定义路由类 | Import the custom route class with operation logging

from ..schema import (  # 导入请求/响应模型 | Import request/response schemas
    QuantCandlesOutSchema,  # K 线响应 | Candles response
    QuantDataDownloadSchema,  # 下载参数 | Download parameters
    QuantDataImportSchema,  # 导入参数 | Import parameters
    QuantDatasetOutSchema,  # 数据集响应 | Dataset response
    QuantLocalFileSchema,  # 本地文件响应 | Local file response
)  # schema 导入结束 | End of schema import
from .service import (  # 导入数据服务函数 | Import data service functions
    download_binance_data,  # 下载币安数据 | Download Binance data
    get_dataset,  # 查询单个数据集 | Get a dataset
    import_local_csv,  # 导入本地 CSV | Import local CSV
    list_datasets,  # 查询数据集列表 | List datasets
    list_local_csv_files,  # 列出本地 CSV 文件 | List local CSV files
    read_dataset_candles,  # 读取数据集 K 线 | Read dataset candles
)  # service 导入结束 | End of service import

QuantDataRouter = APIRouter(route_class=OperationLogRoute, prefix="/data", tags=["量化-数据"])  # 创建数据路由器，带操作日志、前缀与标签 | Create the data router with logging, prefix and tags


@QuantDataRouter.get(  # 注册 GET 路由：数据集列表 | Register GET route: list datasets
    "/list",  # 相对路径 | Relative path
    summary="查询量化数据集列表",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[list[QuantDatasetOutSchema]],  # 响应模型：数据集列表 | Response model: list of datasets
)  # 装饰器结束 | End of decorator
async def list_quant_datasets_controller(  # 查询数据集列表控制器 | List datasets controller
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:query"]))],  # 认证信息，需 data:query 权限 | Auth info, requires data:query permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 只读数据库会话 | Read-only database session
    symbol: Annotated[str | None, Query(description="按交易对筛选")] = None,  # 可选交易对筛选 | Optional pair filter
    interval: Annotated[str | None, Query(description="按周期筛选")] = None,  # 可选周期筛选 | Optional interval filter
    market: Annotated[str | None, Query(description="按市场筛选：spot-现货，futures-合约")] = None,  # 可选市场筛选 | Optional market filter
    limit: Annotated[int, Query(ge=1, le=500, description="返回条数")] = 100,  # 返回条数，1~500，默认 100 | Row count, 1~500, default 100
):
    """查询已登记的本地数据集 | List registered local datasets.

    参数 | Args:
        auth (AuthSchema): 当前用户信息 | Current user info.
        db (AsyncSession): 数据库会话 | Database session.
        symbol (str | None): 交易对筛选 | Pair filter.
        interval (str | None): 周期筛选 | Interval filter.
        market (str | None): 市场筛选 | Market filter.
        limit (int): 返回条数 | Row limit.

    返回 | Returns:
        ResponseSchema: 数据集列表 | List of datasets.
    """
    datasets = await list_datasets(db, symbol=symbol, interval=interval, market=market, limit=limit)  # 调用服务查询（含筛选）| Call the service with filters
    return SuccessResponse(data=[QuantDatasetOutSchema.model_validate(item) for item in datasets], msg="查询成功")  # 转换 ORM 对象并返回 | Convert ORM objects and return


@QuantDataRouter.get(  # 注册 GET 路由：可导入文件列表 | Register GET route: importable files
    "/files",  # 相对路径 | Relative path
    summary="查询可导入的本地 CSV 文件",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[list[QuantLocalFileSchema]],  # 响应模型：本地文件列表 | Response model: list of local files
)  # 装饰器结束 | End of decorator
async def list_quant_files_controller(  # 查询可导入文件控制器 | List importable files controller
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:query"]))],  # 认证信息，需 data:query 权限 | Auth info, requires data:query permission
):
    """列出允许导入的本地 CSV 文件 | List local CSV files that are allowed for import.

    参数 | Args:
        auth (AuthSchema): 当前用户信息 | Current user info.

    返回 | Returns:
        ResponseSchema: 本地文件列表 | List of local files.
    """
    return SuccessResponse(data=[QuantLocalFileSchema.model_validate(item) for item in list_local_csv_files()], msg="查询成功")  # 转换文件信息并返回 | Convert file info and return


@QuantDataRouter.get(  # 注册 GET 路由：数据集详情 | Register GET route: dataset detail
    "/detail/{dataset_id}",  # 带路径参数的相对路径 | Relative path with a path parameter
    summary="查询数据集详情",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[QuantDatasetOutSchema],  # 响应模型：单个数据集 | Response model: a single dataset
)  # 装饰器结束 | End of decorator
async def get_quant_dataset_controller(  # 查询数据集详情控制器 | Dataset detail controller
    dataset_id: int,  # 路径参数：数据集 ID | Path parameter: dataset ID
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:query"]))],  # 认证信息，需 data:query 权限 | Auth info, requires data:query permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 只读数据库会话 | Read-only database session
):
    """查询单个数据集 | Get a single dataset.

    参数 | Args:
        dataset_id (int): 数据集 ID | Dataset ID.
        auth (AuthSchema): 当前用户信息 | Current user info.
        db (AsyncSession): 数据库会话 | Database session.

    返回 | Returns:
        ResponseSchema: 数据集详情 | Dataset detail.
    """
    obj = await get_dataset(db, dataset_id)  # 调用服务按 ID 查询 | Call the service to fetch by ID
    return SuccessResponse(data=QuantDatasetOutSchema.model_validate(obj), msg="查询成功")  # 转换并返回 | Convert and return


@QuantDataRouter.get(  # 注册 GET 路由：K 线数据 | Register GET route: candles
    "/candles/{dataset_id}",  # 带路径参数的相对路径 | Relative path with a path parameter
    summary="查询数据集 K 线（图表用）",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[QuantCandlesOutSchema],  # 响应模型：K 线数据 | Response model: candles
)  # 装饰器结束 | End of decorator
async def get_quant_candles_controller(  # 查询 K 线控制器 | Candles controller
    dataset_id: int,  # 路径参数：数据集 ID | Path parameter: dataset ID
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:query"]))],  # 认证信息，需 data:query 权限 | Auth info, requires data:query permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 只读数据库会话 | Read-only database session
    limit: Annotated[int, Query(ge=1, le=20000, description="返回条数，默认最后 2000 根")] = 2000,  # 返回条数，默认 2000 | Row count, default 2000
    tail: Annotated[bool, Query(description="True 取最后 limit 根，False 取最前 limit 根")] = True,  # True 取末尾，False 取开头 | True = tail, False = head
):
    """读取已登记数据集的 K 线，供图表与明细表格展示 | Read candles of a registered dataset for charts and detail tables.

    参数 | Args:
        dataset_id (int): 数据集 ID | Dataset ID.
        auth (AuthSchema): 当前用户信息 | Current user info.
        db (AsyncSession): 数据库会话 | Database session.
        limit (int): 返回条数 | Row limit.
        tail (bool): 取末尾还是开头 | Tail or head.

    返回 | Returns:
        ResponseSchema: K 线与元信息 | Candles and metadata.
    """
    result = await read_dataset_candles(db, dataset_id, limit=limit, tail=tail)  # 调用服务读取 K 线 | Call the service to read candles
    return SuccessResponse(data=QuantCandlesOutSchema.model_validate(result), msg="查询成功")  # 转换并返回 | Convert and return


@QuantDataRouter.post(  # 注册 POST 路由：下载数据 | Register POST route: download data
    "/download",  # 相对路径 | Relative path
    summary="下载币安 K 线数据",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[dict],  # 响应模型：通用字典 | Response model: generic dict
)  # 装饰器结束 | End of decorator
async def download_quant_data_controller(  # 下载数据控制器 | Download data controller
    data: QuantDataDownloadSchema,  # 请求体：下载参数 | Request body: download parameters
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:download"]))],  # 认证信息，需 data:download 权限 | Auth info, requires data:download permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 数据库会话 | Database session
):
    """从币安下载指定交易对与周期的 K 线到本地 CSV | Download candles of a given pair and interval from Binance to a local CSV.

    参数 | Args:
        data (QuantDataDownloadSchema): 下载参数 | Download parameters.
        auth (AuthSchema): 当前用户信息 | Current user info.
        db (AsyncSession): 数据库会话 | Database session.

    返回 | Returns:
        ResponseSchema: 下载摘要 | Download summary.
    """
    result = await download_binance_data(data, db=db, auth=auth)  # 调用服务执行下载 | Call the service to download
    return SuccessResponse(data=result, msg="数据下载完成")  # 返回结果 | Return the result


@QuantDataRouter.post(  # 注册 POST 路由：导入数据 | Register POST route: import data
    "/import",  # 相对路径 | Relative path
    summary="导入本地 CSV 数据",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[dict],  # 响应模型：通用字典 | Response model: generic dict
)  # 装饰器结束 | End of decorator
async def import_quant_data_controller(  # 导入数据控制器 | Import data controller
    data: QuantDataImportSchema,  # 请求体：导入参数 | Request body: import parameters
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:data:import"]))],  # 认证信息，需 data:import 权限 | Auth info, requires data:import permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 数据库会话 | Database session
):
    """导入本地 CSV 并登记数据集 | Import a local CSV and register the dataset.

    参数 | Args:
        data (QuantDataImportSchema): 导入参数 | Import parameters.
        auth (AuthSchema): 当前用户信息 | Current user info.
        db (AsyncSession): 数据库会话 | Database session.

    返回 | Returns:
        ResponseSchema: 导入摘要 | Import summary.
    """
    result = await import_local_csv(data, db=db, auth=auth)  # 调用服务执行导入 | Call the service to import
    return SuccessResponse(data=result, msg="数据导入完成")  # 返回结果 | Return the result
