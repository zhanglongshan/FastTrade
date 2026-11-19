"""策略查询接口 | Strategy query API.

目的 | Purpose:
    暴露 FastAPI 路由，供前端查询由 loader 自动发现的全部可用量化策略。
    Expose a FastAPI route for the frontend to query all available quantitative strategies discovered by the loader.

主要流程 | Main flow:
    1. 定义带操作日志的 APIRouter 前缀 /strategies | Define an APIRouter with operation logging and prefix /strategies.
    2. 注册 GET 路由，带权限校验与响应模型 | Register a GET route with permission check and response model.
    3. 调用 load_strategies 发现策略并转换为输出 Schema 列表 | Call load_strategies to discover strategies and convert them to output schemas.
    4. 用统一成功响应包装返回 | Wrap the result in the standard success response.
"""

from typing import Annotated  # 导入 Annotated | Import Annotated for dependency injection metadata

from fastapi import APIRouter, Security  # 导入 APIRouter 与 Security | Import APIRouter and Security for routing and auth dependency

from app.common.response import ResponseSchema, SuccessResponse  # 导入响应模型与成功响应 | Import response schema and success response helpers
from app.core.base_schema import AuthSchema  # 导入认证数据结构 | Import the auth data schema
from app.core.dependencies import AuthPermission  # 导入权限依赖 | Import the permission dependency
from app.core.router_class import OperationLogRoute  # 导入操作日志路由类 | Import the operation-log route class

from ..schema import QuantStrategyOutSchema  # 导入策略输出 Schema | Import the strategy output schema
from .loader import load_strategies  # 导入策略发现函数 | Import the strategy discovery function

QuantStrategyRouter = APIRouter(route_class=OperationLogRoute, prefix="/strategies", tags=["量化-策略"])  # 创建策略路由，带操作日志/前缀/标签 | Create the strategy router with logging, prefix and tags


@QuantStrategyRouter.get(  # 注册 GET 路由 | Register a GET route
    "",  # 空子路径 | Empty sub-path
    summary="查询可用策略",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[list[QuantStrategyOutSchema]],  # 响应模型为策略列表的统一响应 | Response model is the wrapped strategy-list response
)  # 路由装饰器结束 | End of route decorator
async def list_quant_strategies_controller(  # 查询可用策略控制器 | Controller listing available strategies
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:strategy:query"]))],  # 注入认证信息并校验查询权限 | Inject auth info and enforce query permission
):  # 函数签名结束 | End of function signature
    """列出 user_data/strategies 下自动发现的全部策略。| List all auto-discovered strategies under user_data/strategies.

    Purpose:
        发现所有可用策略并转换为输出 Schema，以统一成功响应返回。
        Discover all available strategies, convert them to output schemas and return a standard success response.
    Parameters:
        - auth: 已通过权限校验的认证信息（由依赖注入提供）| Authenticated info that passed permission check (provided by dependency injection).
    Returns:
        ResponseSchema[list[QuantStrategyOutSchema]]: 包含策略列表的成功响应 | Success response containing the strategy list.
    Flow:
        调用 load_strategies 发现策略 -> 列表推导构造输出 Schema -> 包装为成功响应 | Call load_strategies -> build output schemas via list comprehension -> wrap as success response.
    """
    items = [  # 用列表推导收集策略输出对象 | Collect strategy output objects via list comprehension
        QuantStrategyOutSchema(  # 构造单条策略输出 | Construct one strategy output
            name=info.name,  # 策略名称 | Strategy name
            file=info.file,  # 策略文件路径 | Strategy file path
            mode=info.mode,  # 策略模式 | Strategy mode
            timeframe=info.timeframe,  # 策略周期 | Strategy timeframe
            doc=info.doc,  # 策略文档 | Strategy docstring
        )  # 单条输出构造结束 | End of single output construction
        for info in load_strategies()  # 遍历自动发现的策略 | Iterate auto-discovered strategies
    ]  # 列表推导结束 | End of list comprehension
    return SuccessResponse(data=items, msg="查询成功")  # 返回统一成功响应 | Return the standard success response
