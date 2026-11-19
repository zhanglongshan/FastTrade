"""回测任务服务：校验数据集与策略，执行回测并持久化结果 | Backtest service: validates datasets and strategies, runs backtests and persists results."""

import asyncio  # 导入 asyncio，用于把 CPU 密集的回测放到线程中执行，避免阻塞事件循环 | Import asyncio to run CPU-heavy backtests in threads without blocking the loop
from datetime import UTC, datetime  # 导入 UTC 时区与 datetime，用于生成时间戳 | Import UTC timezone and datetime for generating timestamps

from sqlalchemy import select  # 导入 select，用于构造查询 | Import select to build queries
from sqlalchemy.ext.asyncio import AsyncSession  # 导入异步数据库会话类型 | Import the async database session type

from app.core.base_schema import AuthSchema  # 导入认证信息模型，用于审计字段 | Import the auth schema for audit fields
from app.core.exceptions import CustomException  # 导入业务异常类 | Import the business exception class

from ..data.service import get_dataset, resolve_dataset_csv  # 导入数据集查询与 CSV 路径解析函数 | Import dataset lookup and CSV path resolution helpers
from ..model import QuantBacktestModel  # 导入回测记录 ORM 模型 | Import the backtest record ORM model
from ..schema import QuantBacktestCreateSchema  # 导入回测创建参数模型 | Import the backtest creation schema
from ..strategy.loader import load_strategy  # 导入策略动态加载函数 | Import the strategy dynamic loader
from .engine import load_candles, run_backtest  # 导入 K 线加载与回测执行函数 | Import candle loading and backtest execution functions


def _naive_utc() -> datetime:
    """返回 naive UTC，适配 TIMESTAMP WITHOUT TIME ZONE 列 | Return naive UTC to fit TIMESTAMP WITHOUT TIME ZONE columns.

    返回 | Returns:
        datetime: 不带时区信息的当前 UTC 时间 | Current UTC time without timezone info.
    """
    return datetime.now(UTC).replace(tzinfo=None)  # 取当前 UTC 时间并去掉 tzinfo | Get current UTC time and strip tzinfo


def _market_of(dataset) -> str:
    """优先取数据集的市场字段，旧数据兜底按 source 判断 | Prefer the dataset's market field; fall back to inferring from source for legacy data.

    参数 | Args:
        dataset: 数据集 ORM 对象 | The dataset ORM object.

    返回 | Returns:
        str: "spot" 或 "futures" | Either "spot" or "futures".
    """
    market = getattr(dataset, "market", None)  # 读取 market 字段，兼容旧对象无此属性 | Read the market field, tolerant of legacy objects lacking it
    if market in {"spot", "futures"}:  # 字段值合法 | Field value is valid
        return market  # 直接返回 | Return it directly
    return "futures" if "futures" in str(getattr(dataset, "source", "")).lower() else "spot"  # 兜底：source 含 futures 判为合约，否则现货 | Fallback: futures if source contains "futures", else spot


async def create_backtests(
    params: QuantBacktestCreateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> list[QuantBacktestModel]:
    """按选中的数据集逐个执行回测，每个数据集生成一条独立记录 | Run a backtest per selected dataset, producing one record each.

    单个数据集失败不会中断其他数据集：失败记录 status=2 并保存 error，
    页面可逐个查看结果。交易对、周期、市场均来自数据集本身。
    A failure in one dataset does not abort the others: failed records get status=2 with the error saved,
    so the UI can inspect them individually. Pair, interval and market all come from the dataset itself.

    参数 | Args:
        params (QuantBacktestCreateSchema): 回测创建参数 | Backtest creation parameters.
        db (AsyncSession): 异步数据库会话 | Async database session.
        auth (AuthSchema | None): 当前用户，用于审计字段 | Current user, used for audit fields.

    返回 | Returns:
        list[QuantBacktestModel]: 每个数据集对应的回测记录 | One backtest record per dataset.
    """
    info = load_strategy(params.strategy_name)  # 加载策略类及其元信息（mode/cls/name）| Load the strategy class and its metadata (mode/cls/name)
    # 先统一校验，避免跑到 halfway 才因某个数据集不存在而中断 | Validate all datasets up front to avoid mid-run failure
    datasets = [await get_dataset(db, dataset_id) for dataset_id in params.dataset_ids]  # 一次性查询所有数据集，缺失会立即抛异常 | Fetch all datasets at once; a missing one raises immediately
    multi = len(datasets) > 1  # 是否为多数据集回测（决定命名是否带后缀）| Whether this is a multi-dataset run (affects naming)
    timestamp = f"{datetime.now(UTC):%m%d%H%M%S}"  # 生成时间戳用于自动命名 | Generate a timestamp for auto-naming
    user_id = getattr(getattr(auth, "user", None), "id", None)  # 安全提取当前用户 ID | Safely extract the current user ID

    results: list[QuantBacktestModel] = []  # 收集所有回测记录 | Accumulate all backtest records
    for dataset in datasets:  # 逐数据集执行回测 | Iterate over datasets and run a backtest for each
        market = _market_of(dataset)  # 判断该数据集的市场类型 | Determine the dataset's market type
        if params.name:  # 用户提供了自定义名称 | User supplied a custom name
            name = f"{params.name}_{dataset.symbol}_{dataset.interval}" if multi else params.name  # 多选时附加交易对与周期以区分 | Append pair and interval when multi to distinguish records
        else:  # 未提供名称，自动生成 | No name given, generate automatically
            name = f"{dataset.symbol}_{dataset.interval}_{info.name}_{timestamp}"  # 交易对_周期_策略_时间戳 | pair_interval_strategy_timestamp

        obj = QuantBacktestModel(  # 创建回测记录对象（状态=运行中）| Create a backtest record object (status=running)
            name=name,  # 回测名称 | Backtest name
            strategy_name=info.name,  # 策略类名 | Strategy class name
            symbol=dataset.symbol,  # 交易对 | Trading pair
            interval=dataset.interval,  # K 线周期 | Candle interval
            market=market,  # 市场类型 | Market type
            initial_cash=params.initial_cash,  # 初始资金 | Initial capital
            fee_rate=params.fee_rate,  # 手续费率 | Fee rate
            status=0,  # 状态 0=运行中 | Status 0 = running
            started_time=_naive_utc(),  # 记录开始时间 | Record start time
            description=params.description,  # 备注 | Description
            created_id=user_id,  # 审计：创建人 ID | Audit: creator ID
        )  # 回测记录构造结束 | End of record construction
        db.add(obj)  # 加入会话 | Add to the session
        await db.commit()  # 提交，先拿到主键 | Commit first to obtain the primary key
        await db.refresh(obj)  # 刷新对象，填充数据库生成的字段 | Refresh to populate DB-generated fields

        try:  # 单个数据集的回测过程，失败不中断整体 | Backtest a single dataset; failure does not abort the whole batch
            csv_path = resolve_dataset_csv(dataset.file_path)  # 解析数据集相对路径为绝对 CSV 路径 | Resolve the dataset's relative path to an absolute CSV path
            candles = await asyncio.to_thread(load_candles, csv_path)  # 在线程中加载 K 线，避免阻塞事件循环 | Load candles in a thread to avoid blocking the event loop
            result = await asyncio.to_thread(  # 在线程中执行回测 | Run the backtest in a thread
                run_backtest,  # 回测函数 | Backtest function
                candles,  # K 线序列 | Candle series
                info.cls,  # 策略类 | Strategy class
                mode=info.mode,  # 策略模式 | Strategy mode
                symbol=dataset.symbol,  # 交易对 | Trading pair
                interval=dataset.interval,  # K 线周期 | Candle interval
                initial_cash=params.initial_cash,  # 初始资金 | Initial capital
                fee_rate=params.fee_rate,  # 手续费率 | Fee rate
                slippage=params.slippage,  # 滑点比例 | Slippage ratio
                # 现货不允许做空，合约允许 | Spot disallows shorting; futures allows it
                allow_short=market == "futures",  # 仅合约允许做空 | Only futures markets allow shorting
            )  # 回测执行结束 | End of backtest call
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落库，便于页面排查 | Any failure must be persisted for UI diagnosis
            obj.status = 2  # 标记失败 | Mark as failed
            obj.error = str(exc)  # 保存错误信息 | Save the error message
            obj.finished_time = _naive_utc()  # 记录结束时间 | Record finish time
            await db.commit()  # 提交失败状态 | Commit the failed status
            await db.refresh(obj)  # 刷新对象 | Refresh the object
            results.append(obj)  # 加入结果列表 | Append to results
            continue  # 继续下一个数据集 | Continue to the next dataset

        obj.status = 1  # 标记成功 | Mark as success
        obj.result = result  # 保存回测结果 | Save the backtest result
        obj.error = None  # 清空错误（理论上本来就是空）| Clear any error (normally already empty)
        obj.finished_time = _naive_utc()  # 记录结束时间 | Record finish time
        await db.commit()  # 提交成功状态与结果 | Commit the success status and result
        await db.refresh(obj)  # 刷新对象 | Refresh the object
        results.append(obj)  # 加入结果列表 | Append to results

    return results  # 返回所有数据集的回测记录 | Return all backtest records


async def create_backtest(
    params: QuantBacktestCreateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> QuantBacktestModel:
    """单数据集回测（CLI 等旧调用方兼容），内部复用多选实现 | Single-dataset backtest (legacy CLI compatibility), internally reusing the multi-select implementation.

    参数 | Args:
        params (QuantBacktestCreateSchema): 回测创建参数 | Backtest creation parameters.
        db (AsyncSession): 异步数据库会话 | Async database session.
        auth (AuthSchema | None): 当前用户 | Current user.

    返回 | Returns:
        QuantBacktestModel: 单条回测记录 | A single backtest record.

    异常 | Raises:
        CustomException: 回测失败时抛出 | Raised when the backtest fails.
    """
    results = await create_backtests(params, db=db, auth=auth)  # 复用多选实现 | Reuse the multi-select implementation
    obj = results[0]  # 取第一条（也是唯一一条）结果 | Take the first (and only) result
    if obj.status == 2:  # 回测失败 | Backtest failed
        raise CustomException(msg=f"回测失败：{obj.error}")  # 抛出带错误原因的业务异常 | Raise a business exception with the error reason
    return obj  # 返回成功记录 | Return the successful record


async def list_backtests(db: AsyncSession, *, limit: int = 100) -> list[QuantBacktestModel]:
    """查询回测记录 | List backtest records.

    参数 | Args:
        db (AsyncSession): 异步数据库会话 | Async database session.
        limit (int): 返回条数上限 | Maximum number of records to return.

    返回 | Returns:
        list[QuantBacktestModel]: 按 ID 倒序的回测记录 | Backtest records ordered by ID descending.
    """
    stmt = (  # 构造查询：未删除、按 ID 倒序、限制条数 | Build query: not deleted, ordered by ID descending, limited
        select(QuantBacktestModel)  # 选择回测模型 | Select the backtest model
        .where(QuantBacktestModel.is_deleted.is_(False))  # 过滤软删除记录 | Exclude soft-deleted records
        .order_by(QuantBacktestModel.id.desc())  # 按 ID 倒序（最新在前）| Order by ID descending (newest first)
        .limit(limit)  # 限制返回条数 | Limit the number of rows
    )  # 查询构造结束 | End of query construction
    return list((await db.execute(stmt)).scalars().all())  # 执行查询并把结果转成列表 | Execute the query and convert scalars to a list


async def get_backtest(db: AsyncSession, backtest_id: int) -> QuantBacktestModel:
    """按 ID 查询回测记录 | Get a backtest record by ID.

    参数 | Args:
        db (AsyncSession): 异步数据库会话 | Async database session.
        backtest_id (int): 回测记录 ID | Backtest record ID.

    返回 | Returns:
        QuantBacktestModel: 找到的回测记录 | The found backtest record.

    异常 | Raises:
        CustomException: 记录不存在或已删除时抛出 | Raised when the record is missing or deleted.
    """
    obj = await db.get(QuantBacktestModel, backtest_id)  # 按主键查询 | Query by primary key
    if obj is None or obj.is_deleted:  # 记录不存在或已软删除 | Record missing or soft-deleted
        raise CustomException(msg="回测记录不存在")  # 抛业务异常 | Raise a business exception
    return obj  # 返回记录 | Return the record
