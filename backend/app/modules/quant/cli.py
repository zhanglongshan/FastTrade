"""量化工具命令行：下载数据、导入 CSV、列出策略、执行回测 | Quant CLI tool: download data, import CSV, list strategies, run backtests.

命令同时挂载在 `quant` 与 `mingling` 两个名称下，`mingling` 为兼容别名 | Commands are registered under both `quant` and `mingling`, where `mingling` is a compatibility alias.

主要流程 / Main flow:
1. 使用 _setup_env 在导入 settings 之前设定运行环境 | Use _setup_env to set the environment before settings are imported.
2. 各命令把命令行参数转换为 Pydantic 参数模型 | Each command converts CLI arguments into a Pydantic parameter model.
3. 在异步数据库会话中调用对应服务，并把结果以 JSON 输出 | Call the matching service inside an async DB session and print the result as JSON.
"""

import asyncio  # 导入 asyncio，用于运行异步协程 | Import asyncio for running async coroutines
import json  # 导入 json，用于序列化输出 | Import json for serializing output
import os  # 导入 os，用于设置环境变量 | Import os for setting environment variables
from typing import Annotated  # 导入 Annotated，用于为命令参数附加 Typer 选项元数据 | Import Annotated to attach Typer option metadata to command parameters

import typer  # 导入 Typer，用于构建命令行应用 | Import Typer for building the command-line application

quant_cli = typer.Typer(name="quant", help="量化工具：下载数据 / 导入数据 / 回测", no_args_is_help=True)  # 创建名为 quant 的 Typer 应用，无参数时显示帮助 | Create the quant Typer app, showing help when no arguments are given


def _setup_env(env: str) -> None:
    """设置运行环境 | Set the runtime environment.

    用途 / Purpose: 必须在导入 settings 之前设置 ENVIRONMENT，确保配置读取正确 | ENVIRONMENT must be set before importing settings so configuration is loaded correctly.

    参数 / Parameters:
        env: 环境名称字符串，如 dev | Environment name string, e.g. dev.

    返回 / Returns:
        None，仅产生设置环境变量的副作用 | None; only has the side effect of setting an environment variable.

    流程 / Logic: 把传入的环境名写入 os.environ 的 ENVIRONMENT 键 | Write the given environment name into the ENVIRONMENT key of os.environ.
    """
    os.environ["ENVIRONMENT"] = env  # 将运行环境写入环境变量 | Write the runtime environment into the environment variables


def _print_json(data: object) -> None:
    """统一输出 JSON | Print JSON in a uniform way.

    用途 / Purpose: 以统一格式输出 JSON，便于脚本处理 | Emit JSON in a uniform format for easier script processing.

    参数 / Parameters:
        data: 任意可序列化对象 | Any serializable object.

    返回 / Returns:
        None，仅输出文本 | None; only prints text.

    流程 / Logic: 使用 json.dumps 序列化（保留中文、缩进 2 空格、非字符串值转字符串），再经 typer.echo 输出 | Serialize with json.dumps (Chinese preserved, 2-space indent, non-string values stringified) then print via typer.echo.
    """
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))  # 序列化为 JSON 并输出 | Serialize to JSON and print


@quant_cli.command("strategies", help="列出 user_data/strategies 下自动发现的策略")  # 注册 strategies 命令，列出自动发现的策略 | Register the strategies command to list auto-discovered strategies
def strategies_command() -> None:
    """打印全部可用策略 | Print all available strategies.

    用途 / Purpose: 列出 user_data/strategies 目录下自动发现的策略 | List strategies auto-discovered under user_data/strategies.

    参数 / Parameters:
        无 | None.

    返回 / Returns:
        None，仅输出文本 | None; only prints text.

    流程 / Logic: 调用 load_strategies 获取策略列表；为空时提示并退出，否则逐行打印名称、模式、周期与文件 | Call load_strategies; if empty print a hint and return, otherwise print name, mode, timeframe and file per line.
    """
    from app.modules.quant.strategy.loader import load_strategies  # 延迟导入策略加载函数，避免提前触发配置加载 | Lazily import the strategy loader to avoid early config loading

    items = load_strategies()  # 加载并返回策略信息列表 | Load and return the list of strategy information
    if not items:  # 若未发现任何策略 | If no strategies were found
        typer.echo("未发现策略，请在 user_data/strategies 下添加 .py 文件")  # 提示用户添加策略文件 | Prompt the user to add a strategy file
        return  # 提前退出命令 | Exit the command early
    for info in items:  # 遍历每个策略信息 | Iterate over each strategy information
        typer.echo(f"{info.name}\t{info.mode}\t{info.timeframe or '-'}\t{info.file}")  # 以制表符分隔打印策略字段 | Print strategy fields separated by tabs


@quant_cli.command("download", help="从币安下载 K 线到本地 CSV 并登记数据集")  # 注册 download 命令，下载币安 K 线 | Register the download command for Binance candles
def download_command(
    symbol: Annotated[str, typer.Option("--symbol", "-s", help="交易对，如 BTC/USDT")],  # 交易对参数，必填 | Symbol parameter, required
    interval: Annotated[str, typer.Option("--interval", "-i", help="周期，如 1h")],  # K 线周期参数，必填 | Kline interval parameter, required
    start: Annotated[str | None, typer.Option("--start", help="起始时间 YYYY-MM-DD")] = None,  # 起始时间参数，可空 | Start time parameter, nullable
    end: Annotated[str | None, typer.Option("--end", help="结束时间 YYYY-MM-DD")] = None,  # 结束时间参数，可空 | End time parameter, nullable
    source: Annotated[  # 下载方式参数，默认 api | Download source parameter, defaults to api
        str,  # 参数基础类型为字符串 | Base parameter type is str
        typer.Option("--source", help="下载方式：api-官方REST，mirror-公开镜像(仅现货)，archive-历史归档"),  # Typer 选项及帮助文本 | Typer option and help text
    ] = "api",  # 下载方式默认值为 api | Default download source is api
    market: Annotated[str, typer.Option("--market", help="市场：spot-现货，futures-U本位合约")] = "spot",  # 市场类型参数，默认 spot | Market type parameter, defaults to spot
    days: Annotated[int | None, typer.Option("--days", help="下载最近 N 天，优先于起止时间")] = None,  # 最近 N 天参数，可空 | Last N days parameter, nullable
    erase: Annotated[bool, typer.Option("--erase", help="清空已有数据后重新下载")] = False,  # 清空重写开关，默认 False | Erase-and-redownload switch, defaults to False
    fill_recent: Annotated[  # 补齐近端数据开关，默认 True | Backfill recent data switch, defaults to True
        bool,  # 参数基础类型为布尔 | Base parameter type is bool
        typer.Option("--fill-recent/--no-fill-recent", help="归档方式是否用公开镜像补齐近端数据"),  # Typer 布尔开关及帮助文本 | Typer boolean flag and help text
    ] = True,  # 补齐近端数据默认开启 | Backfill recent data enabled by default
    limit: Annotated[int, typer.Option("--limit", help="REST 单页条数，上限 1000")] = 1000,  # 单次请求条数，默认 1000 | Rows per request, defaults to 1000
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """下载币安 K 线，支持官方 REST / 公开镜像 / 历史归档三种来源 | Download Binance candles from official REST, public mirror or historical archive.

    用途 / Purpose: 按参数从币安下载 K 线并登记数据集 | Download candles from Binance per parameters and register a dataset.

    参数 / Parameters:
        symbol: 交易对，如 BTC/USDT | Trading symbol, e.g. BTC/USDT.
        interval: K 线周期，如 1h | Kline interval, e.g. 1h.
        start: 起始时间，可空 | Start time, nullable.
        end: 结束时间，可空 | End time, nullable.
        source: 下载方式 api/mirror/archive | Download source api/mirror/archive.
        market: 市场 spot/futures | Market spot/futures.
        days: 下载最近 N 天，可空 | Last N days, nullable.
        erase: 是否清空已有数据 | Whether to erase existing data.
        fill_recent: 是否用镜像补齐近端数据 | Whether to backfill recent data via mirror.
        limit: REST 单次请求条数 | Rows per REST request.
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出 JSON 结果 | None; only prints the JSON result.

    流程 / Logic: 设置环境并延迟导入依赖，构造下载参数模型，定义异步协程在会话中调用服务，最后同步运行并打印结果 | Set environment and lazily import dependencies, build the parameter model, define an async coroutine calling the service in a session, then run synchronously and print the result.
    """
    _setup_env(env)  # 先设置运行环境，确保后续配置读取正确 | Set the environment first so later configuration loads correctly
    from app.core.database import async_db_session  # 延迟导入异步数据库会话上下文 | Lazily import the async DB session context
    from app.modules.quant.data.service import download_binance_data  # 延迟导入数据下载服务 | Lazily import the data download service
    from app.modules.quant.schema import QuantDataDownloadSchema  # 延迟导入下载参数模型 | Lazily import the download parameter schema

    params = QuantDataDownloadSchema(  # 构造下载参数模型 | Build the download parameter model
        symbol=symbol,  # 传入交易对 | Pass the trading symbol
        interval=interval,  # 传入 K 线周期 | Pass the kline interval
        start_time=start,  # 传入起始时间 | Pass the start time
        end_time=end,  # 传入结束时间 | Pass the end time
        days=days,  # 传入最近天数 | Pass the number of recent days
        source=source,  # 传入下载方式 | Pass the download source
        market=market,  # 传入市场类型 | Pass the market type
        erase=erase,  # 传入清空重写开关 | Pass the erase switch
        fill_recent=fill_recent,  # 传入补齐近端数据开关 | Pass the backfill switch
        limit=limit,  # 传入单次请求条数 | Pass the per-request row limit
    )  # 下载参数模型构造结束 | End of download parameter model construction

    async def _run() -> dict:
        """在异步会话中执行下载 | Execute the download inside an async session.

        用途 / Purpose: 在数据库会话生命周期内调用下载服务 | Call the download service within a DB session lifecycle.

        参数 / Parameters:
            无 | None.

        返回 / Returns:
            下载结果的字典 | A dict describing the download result.

        流程 / Logic: 进入 async_db_session 上下文，await 下载服务并返回结果 | Enter the async_db_session context, await the download service and return its result.
        """
        async with async_db_session() as db:  # 获取异步数据库会话 | Acquire an async database session
            return await download_binance_data(params, db=db)  # 在会话中下载数据并返回结果 | Download data in the session and return the result

    _print_json(asyncio.run(_run()))  # 同步运行协程并以 JSON 打印结果 | Run the coroutine synchronously and print the result as JSON


@quant_cli.command("download-archive", help="从币安历史归档下载 K 线（等价 --source archive）")  # 注册 download-archive 命令，历史归档下载快捷方式 | Register the download-archive shortcut command
def download_archive_command(
    symbol: Annotated[str, typer.Option("--symbol", "-s", help="交易对，如 BTC/USDT")],  # 交易对参数，必填 | Symbol parameter, required
    interval: Annotated[str, typer.Option("--interval", "-i", help="周期，如 1h")],  # K 线周期参数，必填 | Kline interval parameter, required
    start: Annotated[str, typer.Option("--start", help="起始时间 YYYY-MM-DD")],  # 起始时间参数，必填 | Start time parameter, required
    end: Annotated[str, typer.Option("--end", help="结束时间 YYYY-MM-DD")],  # 结束时间参数，必填 | End time parameter, required
    market: Annotated[str, typer.Option("--market", help="市场：spot-现货，futures-U本位合约")] = "spot",  # 市场类型参数，默认 spot | Market type parameter, defaults to spot
    erase: Annotated[bool, typer.Option("--erase", help="清空已有数据后重新下载")] = False,  # 清空重写开关，默认 False | Erase-and-redownload switch, defaults to False
    fill_recent: Annotated[  # 补齐近端数据开关，默认 True | Backfill recent data switch, defaults to True
        bool,  # 参数基础类型为布尔 | Base parameter type is bool
        typer.Option("--fill-recent/--no-fill-recent", help="是否用公开镜像补齐归档延迟窗口"),  # Typer 布尔开关及帮助文本 | Typer boolean flag and help text
    ] = True,  # 补齐近端数据默认开启 | Backfill recent data enabled by default
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """历史归档下载快捷命令 | Shortcut command for historical archive download.

    用途 / Purpose: 固定 source=archive 转调 download_command，简化历史数据下载 | Delegate to download_command with source fixed to archive.

    参数 / Parameters:
        symbol: 交易对 | Trading symbol.
        interval: K 线周期 | Kline interval.
        start: 起始时间 | Start time.
        end: 结束时间 | End time.
        market: 市场 spot/futures | Market spot/futures.
        erase: 是否清空已有数据 | Whether to erase existing data.
        fill_recent: 是否补齐归档延迟窗口 | Whether to backfill the archive delay window.
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出 JSON 结果 | None; only prints the JSON result.

    流程 / Logic: 将归档参数原样转发给 download_command，source 固定为 archive、days 为 None、limit 为 1000 | Forward parameters to download_command with source fixed to archive, days None and limit 1000.
    """
    download_command(  # 转调通用下载命令 | Delegate to the general download command
        symbol=symbol,  # 传入交易对 | Pass the trading symbol
        interval=interval,  # 传入 K 线周期 | Pass the kline interval
        start=start,  # 传入起始时间 | Pass the start time
        end=end,  # 传入结束时间 | Pass the end time
        source="archive",  # 固定下载方式为历史归档 | Fix the source to historical archive
        market=market,  # 传入市场类型 | Pass the market type
        days=None,  # 归档方式不使用相对天数 | Archive mode does not use relative days
        erase=erase,  # 传入清空重写开关 | Pass the erase switch
        fill_recent=fill_recent,  # 传入补齐近端数据开关 | Pass the backfill switch
        limit=1000,  # 归档下载固定 limit 为 1000 | Fix limit to 1000 for archive download
        env=env,  # 传入运行环境 | Pass the runtime environment
    )  # 通用下载命令调用结束 | End of the general download command call


@quant_cli.command("import", help="导入本地 CSV 并登记数据集")  # 注册 import 命令，导入本地 CSV | Register the import command for local CSV
def import_csv_command(
    file_path: Annotated[str, typer.Argument(help="CSV 路径，需在允许的数据目录内")],  # 本地 CSV 路径参数，必填 | Local CSV path parameter, required
    symbol: Annotated[str, typer.Option("--symbol", "-s", help="交易对，如 BTC/USDT")],  # 交易对参数，必填 | Symbol parameter, required
    interval: Annotated[str, typer.Option("--interval", "-i", help="周期，如 1h")],  # K 线周期参数，必填 | Kline interval parameter, required
    source: Annotated[str, typer.Option("--source", help="来源标识")] = "local",  # 来源标识参数，默认 local | Source identifier parameter, defaults to local
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """导入本地 CSV | Import a local CSV.

    用途 / Purpose: 把允许目录内的本地 CSV 登记为量化数据集 | Register a local CSV within the allowed directory as a quant dataset.

    参数 / Parameters:
        file_path: 本地 CSV 路径 | Local CSV path.
        symbol: 交易对 | Trading symbol.
        interval: K 线周期 | Kline interval.
        source: 来源标识，默认 local | Source identifier, defaults to local.
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出 JSON 结果 | None; only prints the JSON result.

    流程 / Logic: 设置环境并延迟导入依赖，构造导入参数模型，定义异步协程在会话中调用导入服务，最后同步运行并打印结果 | Set environment and lazily import dependencies, build the import parameter model, define an async coroutine calling the import service in a session, then run synchronously and print the result.
    """
    _setup_env(env)  # 先设置运行环境 | Set the environment first
    from app.core.database import async_db_session  # 延迟导入异步数据库会话上下文 | Lazily import the async DB session context
    from app.modules.quant.data.service import import_local_csv  # 延迟导入本地 CSV 导入服务 | Lazily import the local CSV import service
    from app.modules.quant.schema import QuantDataImportSchema  # 延迟导入导入参数模型 | Lazily import the import parameter schema

    params = QuantDataImportSchema(file_path=file_path, symbol=symbol, interval=interval, source=source)  # 构造导入参数模型 | Build the import parameter model

    async def _run() -> dict:
        """在异步会话中执行导入 | Execute the import inside an async session.

        用途 / Purpose: 在数据库会话生命周期内调用导入服务 | Call the import service within a DB session lifecycle.

        参数 / Parameters:
            无 | None.

        返回 / Returns:
            导入结果的字典 | A dict describing the import result.

        流程 / Logic: 进入 async_db_session 上下文，await 导入服务并返回结果 | Enter the async_db_session context, await the import service and return its result.
        """
        async with async_db_session() as db:  # 获取异步数据库会话 | Acquire an async database session
            return await import_local_csv(params, db=db)  # 在会话中导入数据并返回结果 | Import data in the session and return the result

    _print_json(asyncio.run(_run()))  # 同步运行协程并以 JSON 打印结果 | Run the coroutine synchronously and print the result as JSON


@quant_cli.command("backtest", help="用指定数据集与策略执行回测")  # 注册 backtest 命令，执行回测 | Register the backtest command
def backtest_command(
    dataset_id: Annotated[int, typer.Option("--dataset", "-d", help="数据集ID")],  # 数据集 ID 参数，必填 | Dataset ID parameter, required
    strategy: Annotated[str, typer.Option("--strategy", "-s", help="策略类名")],  # 策略类名参数，必填 | Strategy class name parameter, required
    initial_cash: Annotated[float, typer.Option("--cash", help="初始资金")] = 10000.0,  # 初始资金参数，默认 10000.0 | Initial cash parameter, defaults to 10000.0
    fee_rate: Annotated[float, typer.Option("--fee", help="单边手续费率")] = 0.001,  # 单边手续费率参数，默认 0.001 | One-way fee rate parameter, defaults to 0.001
    slippage: Annotated[float, typer.Option("--slippage", help="滑点比例")] = 0.0,  # 滑点比例参数，默认 0.0 | Slippage ratio parameter, defaults to 0.0
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """执行回测并打印结果摘要 | Run a backtest and print the result summary.

    用途 / Purpose: 用指定数据集与策略执行一次回测并输出摘要 | Run one backtest with a given dataset and strategy and print a summary.

    参数 / Parameters:
        dataset_id: 数据集 ID | Dataset ID.
        strategy: 策略类名 | Strategy class name.
        initial_cash: 初始资金，默认 10000.0 | Initial cash, defaults to 10000.0.
        fee_rate: 单边手续费率，默认 0.001 | One-way fee rate, defaults to 0.001.
        slippage: 滑点比例，默认 0.0 | Slippage ratio, defaults to 0.0.
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出 JSON 结果 | None; only prints the JSON result.

    流程 / Logic: 设置环境并延迟导入依赖，构造回测参数模型并在会话中创建回测，转为输出模型后以 JSON 打印 | Set environment and lazily import dependencies, build the backtest parameter model, create the backtest in a session, convert to an output model and print as JSON.
    """
    _setup_env(env)  # 先设置运行环境 | Set the environment first
    from app.core.database import async_db_session  # 延迟导入异步数据库会话上下文 | Lazily import the async DB session context
    from app.modules.quant.backtest.service import create_backtest  # 延迟导入创建回测服务 | Lazily import the create-backtest service
    from app.modules.quant.schema import QuantBacktestCreateSchema, QuantBacktestOutSchema  # 延迟导入回测创建与响应模型 | Lazily import backtest create and output schemas

    params = QuantBacktestCreateSchema(  # 构造回测创建参数模型 | Build the backtest creation parameter model
        dataset_id=dataset_id,  # 传入数据集 ID | Pass the dataset ID
        strategy_name=strategy,  # 传入策略类名 | Pass the strategy class name
        initial_cash=initial_cash,  # 传入初始资金 | Pass the initial cash
        fee_rate=fee_rate,  # 传入手续费率 | Pass the fee rate
        slippage=slippage,  # 传入滑点比例 | Pass the slippage ratio
        name=f"CLI-{strategy}-{dataset_id}",  # 自动生成回测名称 | Auto-generate the backtest name
    )  # 回测创建参数模型构造结束 | End of backtest parameter model construction

    async def _run() -> dict:
        """在异步会话中执行回测 | Execute the backtest inside an async session.

        用途 / Purpose: 在数据库会话中创建回测并序列化结果 | Create the backtest in a DB session and serialize the result.

        参数 / Parameters:
            无 | None.

        返回 / Returns:
            回测响应的字典 | A dict of the backtest response.

        流程 / Logic: 进入会话上下文，await 创建回测，用输出模型校验并以 json 模式导出字典 | Enter the session context, await backtest creation, validate with the output model and dump to a dict in json mode.
        """
        async with async_db_session() as db:  # 获取异步数据库会话 | Acquire an async database session
            obj = await create_backtest(params, db=db)  # 在会话中创建回测并获得 ORM 对象 | Create the backtest and obtain the ORM object
            return QuantBacktestOutSchema.model_validate(obj).model_dump(mode="json")  # 校验并导出为 JSON 字典 | Validate and dump to a JSON-mode dict

    _print_json(asyncio.run(_run()))  # 同步运行协程并以 JSON 打印结果 | Run the coroutine synchronously and print the result as JSON


@quant_cli.command("live-list", help="列出模拟盘/实盘策略实例")  # 注册 live-list 命令，列出实时策略实例 | Register the live-list command
def live_list_command(
    mode: Annotated[str | None, typer.Option("--mode", help="过滤模式：paper/live")] = None,  # 模式过滤参数，可空 | Mode filter parameter, nullable
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """打印实时策略实例 | Print live strategy instances.

    用途 / Purpose: 列出模拟盘/实盘策略实例 | List paper/live strategy instances.

    参数 / Parameters:
        mode: 模式过滤 paper/live，可空 | Mode filter paper/live, nullable.
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出 JSON 结果 | None; only prints the JSON result.

    流程 / Logic: 设置环境并延迟导入依赖，在会话中查询实例列表并逐个转为输出字典，最后以 JSON 打印 | Set environment and lazily import dependencies, query instances in a session and convert each to an output dict, then print as JSON.
    """
    _setup_env(env)  # 先设置运行环境 | Set the environment first
    from app.core.database import async_db_session  # 延迟导入异步数据库会话上下文 | Lazily import the async DB session context
    from app.modules.quant.runtime.service import list_live_strategies, to_out  # 延迟导入实例查询与转换函数 | Lazily import instance listing and conversion helpers

    async def _run() -> list[dict]:
        """查询实例 | Query instances.

        用途 / Purpose: 在数据库会话中查询并序列化实时策略实例 | Query and serialize live strategy instances in a DB session.

        参数 / Parameters:
            无 | None.

        返回 / Returns:
            实例字典列表 | A list of instance dicts.

        流程 / Logic: 进入会话上下文，await 查询实例列表并用列表推导逐个转换为 JSON 字典 | Enter the session context, await the instance list and convert each via a list comprehension into a JSON dict.
        """
        async with async_db_session() as db:  # 获取异步数据库会话 | Acquire an async database session
            return [to_out(obj).model_dump(mode="json") for obj in await list_live_strategies(db, mode=mode)]  # 查询并转换每个实例为 JSON 字典 | Query and convert each instance into a JSON dict

    _print_json(asyncio.run(_run()))  # 同步运行协程并以 JSON 打印结果 | Run the coroutine synchronously and print the result as JSON


@quant_cli.command("live-create", help="创建模拟盘/实盘策略实例")  # 注册 live-create 命令，创建实时策略实例 | Register the live-create command
def live_create_command(
    name: Annotated[str, typer.Option("--name", help="实例名称")],  # 实例名称参数，必填 | Instance name parameter, required
    strategy: Annotated[str, typer.Option("--strategy", "-s", help="策略类名")],  # 策略类名参数，必填 | Strategy class name parameter, required
    symbol: Annotated[str, typer.Option("--symbol", help="交易对，如 BTC/USDT")],  # 交易对参数，必填 | Symbol parameter, required
    interval: Annotated[str, typer.Option("--interval", "-i", help="周期，如 1h")],  # K 线周期参数，必填 | Kline interval parameter, required
    mode: Annotated[str, typer.Option("--mode", help="paper-模拟盘/live-实盘")] = "paper",  # 运行模式参数，默认 paper | Run mode parameter, defaults to paper
    cash: Annotated[float, typer.Option("--cash", help="初始资金")] = 10000.0,  # 初始资金参数，默认 10000.0 | Initial cash parameter, defaults to 10000.0
    dry_run: Annotated[bool, typer.Option("--dry-run/--no-dry-run", help="实盘是否模拟运行")] = True,  # 模拟运行开关，默认 True | Dry-run switch, defaults to True
    api_key: Annotated[str | None, typer.Option("--api-key", help="币安 API Key（实盘真实下单需要）")] = None,  # 币安 API Key 参数，可空 | Binance API key parameter, nullable
    api_secret: Annotated[str | None, typer.Option("--api-secret", help="币安 API Secret")] = None,  # 币安 API Secret 参数，可空 | Binance API secret parameter, nullable
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """创建实时策略实例 | Create a live strategy instance.

    用途 / Purpose: 创建模拟盘或实盘策略实例并输出创建结果 | Create a paper or live strategy instance and print the result.

    参数 / Parameters:
        name: 实例名称 | Instance name.
        strategy: 策略类名 | Strategy class name.
        symbol: 交易对 | Trading symbol.
        interval: K 线周期 | Kline interval.
        mode: 运行模式 paper/live，默认 paper | Run mode paper/live, defaults to paper.
        cash: 初始资金，默认 10000.0 | Initial cash, defaults to 10000.0.
        dry_run: 是否模拟运行，默认 True | Whether to dry-run, defaults to True.
        api_key: 币安 API Key，可空 | Binance API key, nullable.
        api_secret: 币安 API Secret，可空 | Binance API secret, nullable.
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出 JSON 结果 | None; only prints the JSON result.

    流程 / Logic: 设置环境并延迟导入依赖，构造创建参数模型，在会话中创建实例并转为输出字典，最后以 JSON 打印 | Set environment and lazily import dependencies, build the create parameter model, create the instance in a session, convert to an output dict and print as JSON.
    """
    _setup_env(env)  # 先设置运行环境 | Set the environment first
    from app.core.database import async_db_session  # 延迟导入异步数据库会话上下文 | Lazily import the async DB session context
    from app.modules.quant.runtime.service import create_live_strategy, to_out  # 延迟导入创建实例与转换函数 | Lazily import instance creation and conversion helpers
    from app.modules.quant.schema import QuantLiveStrategyCreateSchema  # 延迟导入创建实例参数模型 | Lazily import the create-instance parameter schema

    params = QuantLiveStrategyCreateSchema(  # 构造创建实例参数模型 | Build the create-instance parameter model
        name=name,  # 传入实例名称 | Pass the instance name
        mode=mode,  # 传入运行模式 | Pass the run mode
        strategy_name=strategy,  # 传入策略类名 | Pass the strategy class name
        symbol=symbol,  # 传入交易对 | Pass the trading symbol
        interval=interval,  # 传入 K 线周期 | Pass the kline interval
        initial_cash=cash,  # 传入初始资金 | Pass the initial cash
        dry_run=dry_run,  # 传入模拟运行开关 | Pass the dry-run switch
        api_key=api_key,  # 传入币安 API Key | Pass the Binance API key
        api_secret=api_secret,  # 传入币安 API Secret | Pass the Binance API secret
    )  # 创建实例参数模型构造结束 | End of create-instance parameter model construction

    async def _run() -> dict:
        """创建实例 | Create the instance.

        用途 / Purpose: 在数据库会话中创建实时策略实例并序列化 | Create a live strategy instance in a DB session and serialize it.

        参数 / Parameters:
            无 | None.

        返回 / Returns:
            实例响应的字典 | A dict of the instance response.

        流程 / Logic: 进入会话上下文，await 创建实例，转为输出模型并以 json 模式导出字典 | Enter the session context, await instance creation, convert to the output model and dump to a dict in json mode.
        """
        async with async_db_session() as db:  # 获取异步数据库会话 | Acquire an async database session
            return to_out(await create_live_strategy(params, db=db)).model_dump(mode="json")  # 创建实例并转换为 JSON 字典 | Create the instance and convert to a JSON dict

    _print_json(asyncio.run(_run()))  # 同步运行协程并以 JSON 打印结果 | Run the coroutine synchronously and print the result as JSON


@quant_cli.command("live-tick", help="手动运行一次实时策略 tick")  # 注册 live-tick 命令，手动运行一次 tick | Register the live-tick command
def live_tick_command(
    strategy_id: Annotated[int, typer.Argument(help="实时策略实例ID")],  # 实时策略实例 ID 参数，必填 | Live strategy instance ID parameter, required
    data_file: Annotated[str | None, typer.Option("--data-file", help="可选：用本地 CSV 驱动本次 tick（离线验证）")] = None,  # 可选本地 CSV 路径，默认 None | Optional local CSV path, defaults to None
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """运行一次 tick | Run one tick.

    用途 / Purpose: 手动驱动实时策略执行一次 tick，可选用本地 CSV 离线验证 | Manually drive one live strategy tick, optionally using a local CSV for offline validation.

    参数 / Parameters:
        strategy_id: 实时策略实例 ID | Live strategy instance ID.
        data_file: 可选本地 CSV 路径，用于离线验证 | Optional local CSV path for offline validation.
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出 JSON 结果 | None; only prints the JSON result.

    流程 / Logic: 设置环境；若提供 data_file 则加载 K 线，然后在会话中运行一次 tick 并输出字典结果 | Set environment; if data_file is given load candles, then run one tick in a session and print the dict result.
    """
    _setup_env(env)  # 先设置运行环境 | Set the environment first
    from app.core.database import async_db_session  # 延迟导入异步数据库会话上下文 | Lazily import the async DB session context
    from app.modules.quant.runtime.service import run_strategy_tick  # 延迟导入运行 tick 服务 | Lazily import the run-tick service

    candles = None  # 初始化 K 线列表为 None，表示走在线数据源 | Initialize candles to None, meaning use the online data source
    if data_file:  # 若提供了本地 CSV 路径用于离线验证 | If a local CSV path was given for offline validation
        from pathlib import Path  # 延迟导入 Path，用于构造路径对象 | Lazily import Path to build a path object

        from app.modules.quant.backtest.engine import load_candles  # 延迟导入 K 线加载函数 | Lazily import the candle loading function

        candles = load_candles(Path(data_file))  # 从本地 CSV 加载 K 线 | Load candles from the local CSV

    async def _run() -> dict:
        """执行 tick | Execute the tick.

        用途 / Purpose: 在数据库会话中运行一次策略 tick 并序列化结果 | Run one strategy tick in a DB session and serialize the result.

        参数 / Parameters:
            无 | None.

        返回 / Returns:
            tick 结果的字典 | A dict of the tick result.

        流程 / Logic: 进入会话上下文，await 运行 tick 并调用 to_dict 导出 | Enter the session context, await the tick run and export via to_dict.
        """
        async with async_db_session() as db:  # 获取异步数据库会话 | Acquire an async database session
            return (await run_strategy_tick(strategy_id, db=db, candles=candles, force=True)).to_dict()  # 运行 tick 并导出为字典（CLI 属手动触发，绕过多实例占位）| Run the tick and export a dict (a manual CLI run bypasses the multi-instance claim)

    _print_json(asyncio.run(_run()))  # 同步运行协程并以 JSON 打印结果 | Run the coroutine synchronously and print the result as JSON


@quant_cli.command("live-start", help="启动实时策略轮询")  # 注册 live-start 命令，启动策略轮询 | Register the live-start command
def live_start_command(
    strategy_id: Annotated[int, typer.Argument(help="实时策略实例ID")],  # 实时策略实例 ID 参数，必填 | Live strategy instance ID parameter, required
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """启动实例 | Start an instance.

    用途 / Purpose: 启动指定实时策略实例的轮询 | Start polling for a given live strategy instance.

    参数 / Parameters:
        strategy_id: 实时策略实例 ID | Live strategy instance ID.
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出 JSON 结果 | None; only prints the JSON result.

    流程 / Logic: 设置环境并在会话中调用 runtime_manager.start，返回包含实例 ID 与运行状态 1 的字典 | Set environment and call runtime_manager.start in a session, returning a dict with the instance ID and status 1.
    """
    _setup_env(env)  # 先设置运行环境 | Set the environment first
    from app.core.database import async_db_session  # 延迟导入异步数据库会话上下文 | Lazily import the async DB session context
    from app.modules.quant.runtime.service import runtime_manager  # 延迟导入运行时管理器 | Lazily import the runtime manager

    async def _run() -> dict:
        """启动实例 | Start the instance.

        用途 / Purpose: 在数据库会话中启动策略轮询 | Start strategy polling in a DB session.

        参数 / Parameters:
            无 | None.

        返回 / Returns:
            包含实例 ID 与状态的字典 | A dict containing the instance ID and status.

        流程 / Logic: 进入会话上下文，await 启动实例，返回状态 1 的字典 | Enter the session context, await starting the instance, return a dict with status 1.
        """
        async with async_db_session() as db:  # 获取异步数据库会话 | Acquire an async database session
            await runtime_manager.start(strategy_id)  # 启动指定实例的轮询 | Start polling for the given instance
        return {"strategy_id": strategy_id, "status": 1}  # 返回实例 ID 与运行状态 1 | Return the instance ID and running status 1

    _print_json(asyncio.run(_run()))  # 同步运行协程并以 JSON 打印结果 | Run the coroutine synchronously and print the result as JSON


@quant_cli.command("live-stop", help="停止实时策略轮询")  # 注册 live-stop 命令，停止策略轮询 | Register the live-stop command
def live_stop_command(
    strategy_id: Annotated[int, typer.Argument(help="实时策略实例ID")],  # 实时策略实例 ID 参数，必填 | Live strategy instance ID parameter, required
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """停止实例 | Stop an instance.

    用途 / Purpose: 停止指定实时策略实例的轮询 | Stop polling for a given live strategy instance.

    参数 / Parameters:
        strategy_id: 实时策略实例 ID | Live strategy instance ID.
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出 JSON 结果 | None; only prints the JSON result.

    流程 / Logic: 设置环境后 await 停止实例，返回包含实例 ID 与停止状态 0 的字典 | Set environment, await stopping the instance, return a dict with the instance ID and stopped status 0.
    """
    _setup_env(env)  # 先设置运行环境 | Set the environment first
    from app.modules.quant.runtime.service import runtime_manager  # 延迟导入运行时管理器 | Lazily import the runtime manager

    async def _run() -> dict:
        """停止实例 | Stop the instance.

        用途 / Purpose: 停止指定实例的轮询 | Stop polling for the given instance.

        参数 / Parameters:
            无 | None.

        返回 / Returns:
            包含实例 ID 与状态的字典 | A dict containing the instance ID and status.

        流程 / Logic: await runtime_manager.stop 后返回状态 0 的字典 | Await runtime_manager.stop, then return a dict with status 0.
        """
        await runtime_manager.stop(strategy_id)  # 停止指定实例的轮询 | Stop polling for the given instance
        return {"strategy_id": strategy_id, "status": 0}  # 返回实例 ID 与停止状态 0 | Return the instance ID and stopped status 0

    _print_json(asyncio.run(_run()))  # 同步运行协程并以 JSON 打印结果 | Run the coroutine synchronously and print the result as JSON


@quant_cli.command("init-menu", help="幂等补入三套量化菜单到既有数据库")  # 注册 init-menu 命令，幂等补入量化菜单 | Register the init-menu command to idempotently add quant menus
def init_menu_command(
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",  # 运行环境参数，默认 dev | Environment parameter, defaults to dev
) -> None:
    """为已初始化的数据库补入菜单树；新库无需执行（sys_menu.json 已含） | Add menu trees to an already-initialized database; new databases need not run this (sys_menu.json already includes them).

    用途 / Purpose: 幂等补入三套量化菜单到既有数据库 | Idempotently add three quant menu trees to an existing database.

    参数 / Parameters:
        env: 运行环境 | Runtime environment.

    返回 / Returns:
        None，仅输出处理结果文本 | None; only prints a processing summary.

    流程 / Logic: 设置环境并在事务中幂等补菜单后提交，最后用 typer.echo 输出新建与跳过数量 | Set environment, idempotently add menus in a transaction and commit, then print created/skipped counts via typer.echo.
    """
    _setup_env(env)  # 先设置运行环境 | Set the environment first
    from app.core.database import async_db_session  # 延迟导入异步数据库会话上下文 | Lazily import the async DB session context
    from app.modules.quant.menu_init import ensure_quant_menus  # 延迟导入菜单补入函数 | Lazily import the menu ensure function

    async def _run() -> dict:
        """在事务中幂等补菜单 | Idempotently add menus within a transaction.

        用途 / Purpose: 在数据库事务中补入菜单并提交 | Add menus in a DB transaction and commit.

        参数 / Parameters:
            无 | None.

        返回 / Returns:
            包含 created 与 skipped 计数的字典 | A dict with created and skipped counts.

        流程 / Logic: 进入会话上下文，await 补菜单，提交事务并返回结果字典 | Enter the session context, await menu insertion, commit the transaction and return the result dict.
        """
        async with async_db_session() as db:  # 获取异步数据库会话 | Acquire an async database session
            result = await ensure_quant_menus(db)  # 幂等补入菜单并获取处理结果 | Idempotently add menus and obtain the result
            await db.commit()  # 提交事务使菜单变更生效 | Commit the transaction to persist menu changes
            return result  # 返回处理结果字典 | Return the result dict

    result = asyncio.run(_run())  # 同步运行协程并获取结果 | Run the coroutine synchronously and get the result
    typer.echo(  # 输出菜单处理结果 | Print the menu processing summary
        f"量化菜单处理完成：新建 {result['created']} 项，"  # 输出新建数量 | Print the number of created items
        f"已存在跳过 {result['skipped']} 项。刷新前端（重新登录）即可看到"  # 输出跳过数量与刷新提示 | Print the number of skipped items and a refresh hint
        f"「web3-量化 / 量化-A股 / 量化-期货」。"  # 输出三套菜单名称 | Print the three menu names
    )  # 菜单处理结果输出结束 | End of menu processing summary output
