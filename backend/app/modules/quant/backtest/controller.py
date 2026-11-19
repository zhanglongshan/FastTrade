"""量化回测接口 | Quant backtest API endpoints.

提供回测执行、记录列表与详情三个 HTTP 接口 | Provides three HTTP endpoints: run backtest, list records and get detail.
"""

from typing import Annotated  # 导入 Annotated，用于给参数附加依赖注入/校验元数据 | Import Annotated for attaching DI/validation metadata to parameters

from fastapi import APIRouter, Depends, Query, Security  # 导入路由、依赖、查询参数与安全依赖 | Import router, dependency, query and security primitives
from sqlalchemy.ext.asyncio import AsyncSession  # 导入异步数据库会话类型 | Import the async database session type

from app.common.response import ResponseSchema, SuccessResponse  # 导入统一响应包装 | Import unified response wrappers
from app.core.base_schema import AuthSchema  # 导入认证信息模型 | Import the auth schema
from app.core.dependencies import AuthPermission, db_getter, db_getter_autocommit  # 导入权限校验与数据库会话依赖 | Import permission and DB session dependencies
from app.core.router_class import OperationLogRoute  # 导入带操作日志的自定义路由类 | Import the custom route class with operation logging

from ..schema import QuantBacktestCreateSchema, QuantBacktestOutSchema  # 导入回测请求/响应模型 | Import backtest request/response schemas
from .service import create_backtests, get_backtest, list_backtests  # 导入回测服务函数 | Import backtest service functions

QuantBacktestRouter = APIRouter(route_class=OperationLogRoute, prefix="/backtest", tags=["量化-回测"])  # 创建回测路由器，带操作日志、前缀与标签 | Create the backtest router with logging, prefix and tags


@QuantBacktestRouter.post(  # 注册 POST 路由：执行回测 | Register POST route: run backtest
    "/run",  # 相对路径 | Relative path
    summary="执行策略回测（支持多个数据集）",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[list[QuantBacktestOutSchema]],  # 响应模型：回测记录列表 | Response model: list of backtest records
)  # 装饰器结束 | End of decorator
async def run_quant_backtest_controller(  # 执行回测控制器 | Backtest execution controller
    data: QuantBacktestCreateSchema,  # 请求体：回测参数 | Request body: backtest parameters
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:backtest:run"]))],  # 认证信息，需 backtest:run 权限 | Auth info, requires backtest:run permission
    db: Annotated[AsyncSession, Depends(db_getter_autocommit)],  # 数据库会话（不带外层 begin，service 自行分步提交）| DB session (no outer begin; service commits stepwise)
):
    """对每个选中的数据集执行回测，返回各自的记录 | Run a backtest for each selected dataset and return their records.

    使用不带外层 begin() 的会话：service 内部需自行 commit + refresh 分步落库。
    Uses a session without an outer begin(): the service commits + refreshes stepwise itself.

    参数 | Args:
        data (QuantBacktestCreateSchema): 回测参数 | Backtest parameters.
        auth (AuthSchema): 当前用户信息 | Current user info.
        db (AsyncSession): 数据库会话 | Database session.

    返回 | Returns:
        ResponseSchema: 包含所有回测记录的响应 | Response containing all backtest records.
    """
    items = await create_backtests(data, db=db, auth=auth)  # 调用服务执行批量回测 | Call the service to run batch backtests
    failed = sum(1 for item in items if item.status == 2)  # 统计失败记录数 | Count failed records
    message = f"回测完成：{len(items) - failed} 成功" + (f"，{failed} 失败" if failed else "")  # 构造提示信息，含成功/失败数量 | Build the message with success/failure counts
    return SuccessResponse(data=[QuantBacktestOutSchema.model_validate(item) for item in items], msg=message)  # 把 ORM 对象转成响应模型并返回 | Convert ORM objects to response models and return


@QuantBacktestRouter.get(  # 注册 GET 路由：回测记录列表 | Register GET route: list backtests
    "/list",  # 相对路径 | Relative path
    summary="查询回测记录列表",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[list[QuantBacktestOutSchema]],  # 响应模型：回测记录列表 | Response model: list of backtest records
)  # 装饰器结束 | End of decorator
async def list_quant_backtests_controller(  # 查询回测列表控制器 | List backtests controller
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:backtest:query"]))],  # 认证信息，需 backtest:query 权限 | Auth info, requires backtest:query permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 只读数据库会话 | Read-only database session
    limit: Annotated[int, Query(ge=1, le=500, description="返回条数")] = 100,  # 返回条数，1~500，默认 100 | Number of records, 1~500, default 100
):
    """查询历史回测记录 | List historical backtest records.

    参数 | Args:
        auth (AuthSchema): 当前用户信息 | Current user info.
        db (AsyncSession): 数据库会话 | Database session.
        limit (int): 返回条数 | Number of records to return.

    返回 | Returns:
        ResponseSchema: 回测记录列表 | List of backtest records.
    """
    items = await list_backtests(db, limit=limit)  # 调用服务查询记录 | Call the service to fetch records
    return SuccessResponse(data=[QuantBacktestOutSchema.model_validate(item) for item in items], msg="查询成功")  # 转换并返回 | Convert and return


@QuantBacktestRouter.get(  # 注册 GET 路由：回测详情 | Register GET route: backtest detail
    "/detail/{backtest_id}",  # 带路径参数的相对路径 | Relative path with a path parameter
    summary="查询回测详情",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[QuantBacktestOutSchema],  # 响应模型：单条回测记录 | Response model: a single backtest record
)  # 装饰器结束 | End of decorator
async def get_quant_backtest_controller(  # 查询回测详情控制器 | Backtest detail controller
    backtest_id: int,  # 路径参数：回测记录 ID | Path parameter: backtest record ID
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:backtest:query"]))],  # 认证信息，需 backtest:query 权限 | Auth info, requires backtest:query permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 只读数据库会话 | Read-only database session
):
    """查询单条回测记录，含净值曲线与成交明细 | Get a single backtest record, including equity curve and trade details.

    参数 | Args:
        backtest_id (int): 回测记录 ID | Backtest record ID.
        auth (AuthSchema): 当前用户信息 | Current user info.
        db (AsyncSession): 数据库会话 | Database session.

    返回 | Returns:
        ResponseSchema: 回测记录详情 | Backtest record detail.
    """
    obj = await get_backtest(db, backtest_id)  # 调用服务按 ID 查询 | Call the service to fetch by ID
    return SuccessResponse(data=QuantBacktestOutSchema.model_validate(obj), msg="查询成功")  # 转成响应模型并返回 | Convert to response model and return
