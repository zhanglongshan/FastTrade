"""量化工具命令行：下载数据、导入 CSV、列出策略、执行回测。

命令同时挂载在 `quant` 与 `mingling` 两个名称下，`mingling` 为兼容别名。
"""

import asyncio
import json
import os
from typing import Annotated

import typer

quant_cli = typer.Typer(name="quant", help="量化工具：下载数据 / 导入数据 / 回测", no_args_is_help=True)


def _setup_env(env: str) -> None:
    """必须在导入 settings 之前设置 ENVIRONMENT。"""
    os.environ["ENVIRONMENT"] = env


def _print_json(data: object) -> None:
    """统一输出 JSON，便于脚本处理。"""
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


@quant_cli.command("strategies", help="列出 user_data/strategies 下自动发现的策略")
def strategies_command() -> None:
    """打印全部可用策略。"""
    from app.modules.quant.strategy.loader import load_strategies

    items = load_strategies()
    if not items:
        typer.echo("未发现策略，请在 user_data/strategies 下添加 .py 文件")
        return
    for info in items:
        typer.echo(f"{info.name}\t{info.mode}\t{info.timeframe or '-'}\t{info.file}")


@quant_cli.command("download", help="从币安下载 K 线到本地 CSV 并登记数据集")
def download_command(
    symbol: Annotated[str, typer.Option("--symbol", "-s", help="交易对，如 BTC/USDT")],
    interval: Annotated[str, typer.Option("--interval", "-i", help="周期，如 1h")],
    start: Annotated[str | None, typer.Option("--start", help="起始时间 YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option("--end", help="结束时间 YYYY-MM-DD")] = None,
    source: Annotated[
        str,
        typer.Option("--source", help="下载方式：api-官方REST，mirror-公开镜像(仅现货)，archive-历史归档"),
    ] = "api",
    market: Annotated[str, typer.Option("--market", help="市场：spot-现货，futures-U本位合约")] = "spot",
    days: Annotated[int | None, typer.Option("--days", help="下载最近 N 天，优先于起止时间")] = None,
    erase: Annotated[bool, typer.Option("--erase", help="清空已有数据后重新下载")] = False,
    fill_recent: Annotated[
        bool,
        typer.Option("--fill-recent/--no-fill-recent", help="归档方式是否用公开镜像补齐近端数据"),
    ] = True,
    limit: Annotated[int, typer.Option("--limit", help="REST 单页条数，上限 1000")] = 1000,
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """下载币安 K 线，支持官方 REST / 公开镜像 / 历史归档三种来源。"""
    _setup_env(env)
    from app.core.database import async_db_session
    from app.modules.quant.data.service import download_binance_data
    from app.modules.quant.schema import QuantDataDownloadSchema

    params = QuantDataDownloadSchema(
        symbol=symbol,
        interval=interval,
        start_time=start,
        end_time=end,
        days=days,
        source=source,
        market=market,
        erase=erase,
        fill_recent=fill_recent,
        limit=limit,
    )

    async def _run() -> dict:
        """在异步会话中执行下载。"""
        async with async_db_session() as db:
            return await download_binance_data(params, db=db)

    _print_json(asyncio.run(_run()))


@quant_cli.command("download-archive", help="从币安历史归档下载 K 线（等价 --source archive）")
def download_archive_command(
    symbol: Annotated[str, typer.Option("--symbol", "-s", help="交易对，如 BTC/USDT")],
    interval: Annotated[str, typer.Option("--interval", "-i", help="周期，如 1h")],
    start: Annotated[str, typer.Option("--start", help="起始时间 YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="结束时间 YYYY-MM-DD")],
    market: Annotated[str, typer.Option("--market", help="市场：spot-现货，futures-U本位合约")] = "spot",
    erase: Annotated[bool, typer.Option("--erase", help="清空已有数据后重新下载")] = False,
    fill_recent: Annotated[
        bool,
        typer.Option("--fill-recent/--no-fill-recent", help="是否用公开镜像补齐归档延迟窗口"),
    ] = True,
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """历史归档下载快捷命令。"""
    download_command(
        symbol=symbol,
        interval=interval,
        start=start,
        end=end,
        source="archive",
        market=market,
        days=None,
        erase=erase,
        fill_recent=fill_recent,
        limit=1000,
        env=env,
    )


@quant_cli.command("import", help="导入本地 CSV 并登记数据集")
def import_csv_command(
    file_path: Annotated[str, typer.Argument(help="CSV 路径，需在允许的数据目录内")],
    symbol: Annotated[str, typer.Option("--symbol", "-s", help="交易对，如 BTC/USDT")],
    interval: Annotated[str, typer.Option("--interval", "-i", help="周期，如 1h")],
    source: Annotated[str, typer.Option("--source", help="来源标识")] = "local",
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """导入本地 CSV。"""
    _setup_env(env)
    from app.core.database import async_db_session
    from app.modules.quant.data.service import import_local_csv
    from app.modules.quant.schema import QuantDataImportSchema

    params = QuantDataImportSchema(file_path=file_path, symbol=symbol, interval=interval, source=source)

    async def _run() -> dict:
        """在异步会话中执行导入。"""
        async with async_db_session() as db:
            return await import_local_csv(params, db=db)

    _print_json(asyncio.run(_run()))


@quant_cli.command("backtest", help="用指定数据集与策略执行回测")
def backtest_command(
    dataset_id: Annotated[int, typer.Option("--dataset", "-d", help="数据集ID")],
    strategy: Annotated[str, typer.Option("--strategy", "-s", help="策略类名")],
    initial_cash: Annotated[float, typer.Option("--cash", help="初始资金")] = 10000.0,
    fee_rate: Annotated[float, typer.Option("--fee", help="单边手续费率")] = 0.001,
    slippage: Annotated[float, typer.Option("--slippage", help="滑点比例")] = 0.0,
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """执行回测并打印结果摘要。"""
    _setup_env(env)
    from app.core.database import async_db_session
    from app.modules.quant.backtest.service import create_backtest
    from app.modules.quant.schema import QuantBacktestCreateSchema, QuantBacktestOutSchema

    params = QuantBacktestCreateSchema(
        dataset_id=dataset_id,
        strategy_name=strategy,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
        slippage=slippage,
        name=f"CLI-{strategy}-{dataset_id}",
    )

    async def _run() -> dict:
        """在异步会话中执行回测。"""
        async with async_db_session() as db:
            obj = await create_backtest(params, db=db)
            return QuantBacktestOutSchema.model_validate(obj).model_dump(mode="json")

    _print_json(asyncio.run(_run()))


@quant_cli.command("live-list", help="列出模拟盘/实盘策略实例")
def live_list_command(
    mode: Annotated[str | None, typer.Option("--mode", help="过滤模式：paper/live")] = None,
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """打印实时策略实例。"""
    _setup_env(env)
    from app.core.database import async_db_session
    from app.modules.quant.runtime.service import list_live_strategies, to_out

    async def _run() -> list[dict]:
        """查询实例。"""
        async with async_db_session() as db:
            return [to_out(obj).model_dump(mode="json") for obj in await list_live_strategies(db, mode=mode)]

    _print_json(asyncio.run(_run()))


@quant_cli.command("live-create", help="创建模拟盘/实盘策略实例")
def live_create_command(
    name: Annotated[str, typer.Option("--name", help="实例名称")],
    strategy: Annotated[str, typer.Option("--strategy", "-s", help="策略类名")],
    symbol: Annotated[str, typer.Option("--symbol", help="交易对，如 BTC/USDT")],
    interval: Annotated[str, typer.Option("--interval", "-i", help="周期，如 1h")],
    mode: Annotated[str, typer.Option("--mode", help="paper-模拟盘/live-实盘")] = "paper",
    cash: Annotated[float, typer.Option("--cash", help="初始资金")] = 10000.0,
    dry_run: Annotated[bool, typer.Option("--dry-run/--no-dry-run", help="实盘是否模拟运行")] = True,
    api_key: Annotated[str | None, typer.Option("--api-key", help="币安 API Key（实盘真实下单需要）")] = None,
    api_secret: Annotated[str | None, typer.Option("--api-secret", help="币安 API Secret")] = None,
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """创建实时策略实例。"""
    _setup_env(env)
    from app.core.database import async_db_session
    from app.modules.quant.runtime.service import create_live_strategy, to_out
    from app.modules.quant.schema import QuantLiveStrategyCreateSchema

    params = QuantLiveStrategyCreateSchema(
        name=name,
        mode=mode,
        strategy_name=strategy,
        symbol=symbol,
        interval=interval,
        initial_cash=cash,
        dry_run=dry_run,
        api_key=api_key,
        api_secret=api_secret,
    )

    async def _run() -> dict:
        """创建实例。"""
        async with async_db_session() as db:
            return to_out(await create_live_strategy(params, db=db)).model_dump(mode="json")

    _print_json(asyncio.run(_run()))


@quant_cli.command("live-tick", help="手动运行一次实时策略 tick")
def live_tick_command(
    strategy_id: Annotated[int, typer.Argument(help="实时策略实例ID")],
    data_file: Annotated[str | None, typer.Option("--data-file", help="可选：用本地 CSV 驱动本次 tick（离线验证）")] = None,
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """运行一次 tick。"""
    _setup_env(env)
    from app.core.database import async_db_session
    from app.modules.quant.runtime.service import run_strategy_tick

    candles = None
    if data_file:
        from pathlib import Path

        from app.modules.quant.backtest.engine import load_candles

        candles = load_candles(Path(data_file))

    async def _run() -> dict:
        """执行 tick。"""
        async with async_db_session() as db:
            return (await run_strategy_tick(strategy_id, db=db, candles=candles)).to_dict()

    _print_json(asyncio.run(_run()))


@quant_cli.command("live-start", help="启动实时策略轮询")
def live_start_command(
    strategy_id: Annotated[int, typer.Argument(help="实时策略实例ID")],
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """启动实例。"""
    _setup_env(env)
    from app.core.database import async_db_session
    from app.modules.quant.runtime.service import runtime_manager

    async def _run() -> dict:
        """启动实例。"""
        async with async_db_session() as db:
            await runtime_manager.start(strategy_id)
        return {"strategy_id": strategy_id, "status": 1}

    _print_json(asyncio.run(_run()))


@quant_cli.command("live-stop", help="停止实时策略轮询")
def live_stop_command(
    strategy_id: Annotated[int, typer.Argument(help="实时策略实例ID")],
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """停止实例。"""
    _setup_env(env)
    from app.modules.quant.runtime.service import runtime_manager

    async def _run() -> dict:
        """停止实例。"""
        await runtime_manager.stop(strategy_id)
        return {"strategy_id": strategy_id, "status": 0}

    _print_json(asyncio.run(_run()))


@quant_cli.command("init-menu", help="幂等补入量化菜单到既有数据库（侧栏显示量化目录）")
def init_menu_command(
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """为已初始化的数据库补入「量化」菜单树；新库无需执行（sys_menu.json 已含）。"""
    _setup_env(env)
    from app.core.database import async_db_session
    from app.modules.quant.menu_init import ensure_quant_menus

    async def _run() -> dict:
        """在事务中幂等补菜单。"""
        async with async_db_session() as db:
            result = await ensure_quant_menus(db)
            await db.commit()
            return result

    result = asyncio.run(_run())
    typer.echo(
        f"量化菜单处理完成：新建 {result['created']} 项，"
        f"已存在跳过 {result['skipped']} 项。刷新前端（重新登录）即可在侧栏看到「量化」。"
    )
