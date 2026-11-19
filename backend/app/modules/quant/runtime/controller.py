"""量化实时策略接口：模拟盘与实盘共用，按 mode 区分执行器。

目的 | Purpose:
    本模块定义实时策略实例的 FastAPI 路由接口，对上层提供创建、查询、更新、
    启停、手动 tick、成交流水查询与删除等 HTTP 端点。模拟盘与实盘共用同一套
    接口，执行器的差异由服务层根据 mode 处理。
    This module defines the FastAPI routes for live strategy instances,
    exposing HTTP endpoints to create, query, update, start/stop, manually tick,
    list trade records and delete instances. Paper and live modes share the same
    endpoints; executor differences are handled by the service layer by mode.

主要流程 | Main flow:
    路由接收请求 -> 权限校验（AuthPermission）-> 调用 service 层 -> 用
    SuccessResponse 包装响应返回。
    Route receives request -> permission check -> call service -> wrap with
    SuccessResponse and return.
"""

from typing import Annotated  # 从 typing 导入 Annotated | Import Annotated for dependency/parameter metadata

from fastapi import APIRouter, Depends, Query, Security  # 从 fastapi 导入路由与依赖注入相关类型 | Import router and DI helpers
from sqlalchemy.ext.asyncio import AsyncSession  # 导入异步数据库会话类型 | Import the async DB session type

from app.common.response import ResponseSchema, SuccessResponse  # 导入统一响应模型与成功响应构造器 | Import response schema and success builder
from app.core.base_schema import AuthSchema  # 导入认证 Schema | Import AuthSchema for authenticated user injection
from app.core.dependencies import AuthPermission, db_getter, db_getter_autocommit  # 导入权限与数据库依赖 | Import permission and DB dependencies
from app.core.router_class import OperationLogRoute  # 导入带操作日志的路由类 | Import the operation-log route class

from ..schema import (  # 从 schema 导入实时策略相关请求/响应模型 | Import live strategy request/response schemas
    QuantLiveStrategyCreateSchema,  # 创建策略请求模型 | Create-strategy request schema
    QuantLiveStrategyOutSchema,  # 策略响应模型 | Strategy output schema
    QuantLiveStrategyUpdateSchema,  # 更新策略请求模型 | Update-strategy request schema
    QuantTradeRecordOutSchema,  # 成交流水响应模型 | Trade record output schema
)
from .service import (  # 从 service 导入业务函数与单例调度器 | Import service functions and the singleton scheduler
    create_live_strategy,  # 创建实例服务 | Create instance service
    delete_live_strategy,  # 删除实例服务 | Delete instance service
    get_live_strategy,  # 查询单个实例服务 | Get instance service
    list_live_strategies,  # 查询实例列表服务 | List instances service
    list_trade_records,  # 查询成交流水服务 | List trade records service
    run_strategy_tick,  # 手动运行 tick 服务 | Manual tick service
    runtime_manager,  # 单例调度器 | Singleton runtime manager
    to_out,  # 模型转响应（脱敏）| Model-to-response (sanitize) helper
    update_live_strategy,  # 更新实例服务 | Update instance service
)

QuantLiveRouter = APIRouter(route_class=OperationLogRoute, prefix="/live", tags=["量化-实时策略"])  # 实时策略路由：带操作日志、前缀 /live | Live strategy router with op-log and /live prefix


@QuantLiveRouter.post(  # POST 创建实例 | POST to create an instance
    "/create",  # 创建路径 | Create path
    summary="创建实时策略实例（模拟盘/实盘）",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[QuantLiveStrategyOutSchema],  # 响应模型 | Response model
)
async def create_live_strategy_controller(
    data: QuantLiveStrategyCreateSchema,  # 请求体：创建参数 | Request body: creation params
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:create"]))],  # 注入认证用户，校验创建权限 | Inject auth user, check create permission
    db: Annotated[AsyncSession, Depends(db_getter_autocommit)],  # 注入自动提交会话 | Inject autocommit DB session
):
    """创建模拟盘或实盘策略实例。

    目的 | Purpose:
        接收创建请求，调用服务层创建实例并返回脱敏后的响应。
        Accept a creation request, call the service to create an instance and
        return the sanitized response.

    参数 | Parameters:
        data: 创建参数 | Creation parameters.
        auth: 认证用户（含创建权限）| Authenticated user with create permission.
        db: 自动提交数据库会话 | Autocommit DB session.

    返回 | Returns:
        ResponseSchema[QuantLiveStrategyOutSchema]: 包装后的实例响应 | Wrapped instance response.

    说明 | Note:
        使用不带外层 begin() 的会话：create_live_strategy 内部需自行 commit + refresh。
        Uses a session without outer begin(): the service commits and refreshes itself.
    """
    obj = await create_live_strategy(data, db=db, auth=auth)  # 调用服务创建实例 | Call service to create the instance
    return SuccessResponse(data=to_out(obj), msg="实例创建成功")  # 脱敏后包装成功响应 | Wrap sanitized result in success response


@QuantLiveRouter.get(  # GET 查询实例列表 | GET to list instances
    "/list",  # 列表路径 | List path
    summary="查询实时策略实例列表",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[list[QuantLiveStrategyOutSchema]],  # 响应模型（列表）| Response model (list)
)
async def list_live_strategies_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:query"]))],  # 注入认证用户，校验查询权限 | Inject auth user, check query permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 注入数据库会话 | Inject DB session
    mode: Annotated[str | None, Query(description="按模式过滤：paper/live")] = None,  # 可选模式过滤查询参数 | Optional mode filter query param
    limit: Annotated[int, Query(ge=1, le=500)] = 100,  # 数量上限（1~500，默认 100）| Row limit (1-500, default 100)
):
    """查询实时策略实例。

    目的 | Purpose:
        按可选模式与数量上限查询实例列表，并对每条记录脱敏。
        Query instances by optional mode and limit, sanitizing each row.

    参数 | Parameters:
        auth: 认证用户（含查询权限）| Authenticated user with query permission.
        db: 数据库会话 | DB session.
        mode: 模式过滤（paper/live，可选）| Mode filter (optional).
        limit: 返回数量上限 | Max number of rows.

    返回 | Returns:
        ResponseSchema[list[QuantLiveStrategyOutSchema]]: 包装后的实例列表 | Wrapped instance list.
    """
    items = await list_live_strategies(db, mode=mode, limit=limit)  # 调用服务查询实例列表 | Call service to list instances
    return SuccessResponse(data=[to_out(item) for item in items], msg="查询成功")  # 逐条脱敏并包装响应 | Sanitize each item and wrap response


@QuantLiveRouter.get(  # GET 查询实例详情 | GET to get one instance
    "/detail/{strategy_id}",  # 详情路径（含实例 ID）| Detail path with strategy id
    summary="查询实时策略实例详情",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[QuantLiveStrategyOutSchema],  # 响应模型 | Response model
)
async def get_live_strategy_controller(
    strategy_id: int,  # 路径参数：实例 ID | Path param: strategy id
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:query"]))],  # 注入认证用户，校验查询权限 | Inject auth user, check query permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 注入数据库会话 | Inject DB session
):
    """查询单个实时策略实例（不含密钥明文）。

    目的 | Purpose:
        按 ID 查询实例详情，返回脱敏后的数据。
        Fetch one instance by id and return sanitized data.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        auth: 认证用户（含查询权限）| Authenticated user with query permission.
        db: 数据库会话 | DB session.

    返回 | Returns:
        ResponseSchema[QuantLiveStrategyOutSchema]: 包装后的实例详情 | Wrapped instance detail.
    """
    obj = await get_live_strategy(strategy_id, db)  # 调用服务查询单个实例 | Call service to get the instance
    return SuccessResponse(data=to_out(obj), msg="查询成功")  # 脱敏后包装成功响应 | Wrap sanitized result in success response


@QuantLiveRouter.put(  # PUT 更新实例 | PUT to update an instance
    "/update/{strategy_id}",  # 更新路径（含实例 ID）| Update path with strategy id
    summary="更新实时策略实例",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[QuantLiveStrategyOutSchema],  # 响应模型 | Response model
)
async def update_live_strategy_controller(
    strategy_id: int,  # 路径参数：实例 ID | Path param: strategy id
    data: QuantLiveStrategyUpdateSchema,  # 请求体：更新参数 | Request body: update params
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:update"]))],  # 注入认证用户，校验更新权限 | Inject auth user, check update permission
    db: Annotated[AsyncSession, Depends(db_getter_autocommit)],  # 注入自动提交会话 | Inject autocommit DB session
):
    """更新实例的安全字段（费率、滑点、dry_run、密钥等）。

    目的 | Purpose:
        接收更新请求，调用服务更新实例安全字段并返回脱敏响应。
        Accept an update request, update safe fields via the service and return
        a sanitized response.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        data: 更新参数 | Update parameters.
        auth: 认证用户（含更新权限）| Authenticated user with update permission.
        db: 自动提交数据库会话 | Autocommit DB session.

    返回 | Returns:
        ResponseSchema[QuantLiveStrategyOutSchema]: 包装后的更新结果 | Wrapped update result.
    """
    obj = await update_live_strategy(strategy_id, data, db=db, auth=auth)  # 调用服务更新实例 | Call service to update the instance
    return SuccessResponse(data=to_out(obj), msg="更新成功")  # 脱敏后包装成功响应 | Wrap sanitized result in success response


@QuantLiveRouter.post(  # POST 启动实例 | POST to start an instance
    "/start/{strategy_id}",  # 启动路径（含实例 ID）| Start path with strategy id
    summary="启动实时策略",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[dict],  # 响应模型（字典）| Response model (dict)
)
async def start_live_strategy_controller(
    strategy_id: int,  # 路径参数：实例 ID | Path param: strategy id
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:start"]))],  # 注入认证用户，校验启动权限 | Inject auth user, check start permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 注入数据库会话 | Inject DB session
):
    """启动实例轮询任务。

    目的 | Purpose:
        先确认实例存在，再启动其轮询任务并返回新状态。
        Confirm the instance exists, start its polling task and return the new status.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        auth: 认证用户（含启动权限）| Authenticated user with start permission.
        db: 数据库会话 | DB session.

    返回 | Returns:
        ResponseSchema[dict]: 含实例 ID 与状态 1 的响应 | Response with id and status 1.
    """
    await get_live_strategy(strategy_id, db)  # 先校验实例存在（不存在则抛异常）| Verify instance exists first
    await runtime_manager.start(strategy_id)  # 启动实例轮询任务 | Start the polling task
    return SuccessResponse(data={"strategy_id": strategy_id, "status": 1}, msg="已启动")  # 返回已启动状态 | Return started status


@QuantLiveRouter.post(  # POST 停止实例 | POST to stop an instance
    "/stop/{strategy_id}",  # 停止路径（含实例 ID）| Stop path with strategy id
    summary="停止实时策略",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[dict],  # 响应模型（字典）| Response model (dict)
)
async def stop_live_strategy_controller(
    strategy_id: int,  # 路径参数：实例 ID | Path param: strategy id
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:start"]))],  # 注入认证用户，校验启停权限 | Inject auth user, check start/stop permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 注入数据库会话 | Inject DB session
):
    """停止实例轮询任务。

    目的 | Purpose:
        先确认实例存在，再停止其轮询任务并返回新状态。
        Confirm the instance exists, stop its polling task and return the new status.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        auth: 认证用户（含启停权限）| Authenticated user with start/stop permission.
        db: 数据库会话 | DB session.

    返回 | Returns:
        ResponseSchema[dict]: 含实例 ID 与状态 0 的响应 | Response with id and status 0.
    """
    await get_live_strategy(strategy_id, db)  # 先校验实例存在（不存在则抛异常）| Verify instance exists first
    await runtime_manager.stop(strategy_id)  # 停止实例轮询任务 | Stop the polling task
    return SuccessResponse(data={"strategy_id": strategy_id, "status": 0}, msg="已停止")  # 返回已停止状态 | Return stopped status


@QuantLiveRouter.post(  # POST 手动运行一次 tick | POST to run one tick manually
    "/tick/{strategy_id}",  # 手动 tick 路径（含实例 ID）| Manual tick path with strategy id
    summary="手动运行一次 tick",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[dict],  # 响应模型（字典）| Response model (dict)
)
async def tick_live_strategy_controller(
    strategy_id: int,  # 路径参数：实例 ID | Path param: strategy id
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:run"]))],  # 注入认证用户，校验运行权限 | Inject auth user, check run permission
    db: Annotated[AsyncSession, Depends(db_getter_autocommit)],  # 注入自动提交会话 | Inject autocommit DB session
):
    """立即拉取最新行情运行一次策略（无需等待轮询）。

    目的 | Purpose:
        手动触发一次 tick，立即返回本次运行结果。
        Manually trigger one tick and immediately return its result.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        auth: 认证用户（含运行权限）| Authenticated user with run permission.
        db: 自动提交数据库会话 | Autocommit DB session.

    返回 | Returns:
        ResponseSchema[dict]: 含 tick 结果字典的响应 | Response with the tick result dict.
    """
    result = await run_strategy_tick(strategy_id, db=db, force=True)  # 手动运行绕过多实例占位，强制跑一次 | Manual runs bypass the multi-instance claim
    return SuccessResponse(data=result.to_dict(), msg="运行完成")  # 将结果转字典并包装响应 | Convert result to dict and wrap response


@QuantLiveRouter.get(  # GET 查询成交流水 | GET to list trade records
    "/trades/{strategy_id}",  # 成交流水路径（含实例 ID）| Trade records path with strategy id
    summary="查询实例成交流水",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[list[QuantTradeRecordOutSchema]],  # 响应模型（流水列表）| Response model (record list)
)
async def list_trade_records_controller(
    strategy_id: int,  # 路径参数：实例 ID | Path param: strategy id
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:query"]))],  # 注入认证用户，校验查询权限 | Inject auth user, check query permission
    db: Annotated[AsyncSession, Depends(db_getter)],  # 注入数据库会话 | Inject DB session
    limit: Annotated[int, Query(ge=1, le=500)] = 200,  # 数量上限（1~500，默认 200）| Row limit (1-500, default 200)
):
    """查询实例的实时成交流水。

    目的 | Purpose:
        按实例 ID 与数量上限查询成交流水，并转换为响应模型。
        Query trade records by instance id and limit, converting to output schema.

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        auth: 认证用户（含查询权限）| Authenticated user with query permission.
        db: 数据库会话 | DB session.
        limit: 返回数量上限 | Max number of rows.

    返回 | Returns:
        ResponseSchema[list[QuantTradeRecordOutSchema]]: 包装后的成交流水列表 | Wrapped trade record list.
    """
    items = await list_trade_records(db, strategy_id=strategy_id, limit=limit)  # 调用服务查询成交流水 | Call service to list trade records
    return SuccessResponse(data=[QuantTradeRecordOutSchema.model_validate(item) for item in items], msg="查询成功")  # 逐条校验为响应模型并包装 | Validate each into output schema and wrap


@QuantLiveRouter.delete(  # DELETE 删除实例 | DELETE to remove an instance
    "/delete/{strategy_id}",  # 删除路径（含实例 ID）| Delete path with strategy id
    summary="删除实时策略实例",  # 接口摘要 | Endpoint summary
    response_model=ResponseSchema[dict],  # 响应模型（字典）| Response model (dict)
)
async def delete_live_strategy_controller(
    strategy_id: int,  # 路径参数：实例 ID | Path param: strategy id
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_quant:live:delete"]))],  # 注入认证用户，校验删除权限 | Inject auth user, check delete permission
    db: Annotated[AsyncSession, Depends(db_getter_autocommit)],  # 注入自动提交会话 | Inject autocommit DB session
):
    """删除实时策略实例（先停止任务）。

    目的 | Purpose:
        调用服务删除实例（服务内部会先停止轮询任务）。
        Delete an instance via the service (which stops its task first).

    参数 | Parameters:
        strategy_id: 实例 ID | Instance ID.
        auth: 认证用户（含删除权限）| Authenticated user with delete permission.
        db: 自动提交数据库会话 | Autocommit DB session.

    返回 | Returns:
        ResponseSchema[dict]: 含被删除实例 ID 的响应 | Response with the deleted id.
    """
    await delete_live_strategy(strategy_id, db=db)  # 调用服务删除实例 | Call service to delete the instance
    return SuccessResponse(data={"strategy_id": strategy_id}, msg="删除成功")  # 返回删除成功响应 | Return deletion success response
