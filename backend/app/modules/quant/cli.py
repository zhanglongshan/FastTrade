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
    limit: Annotated[int, typer.Option("--limit", help="单页条数，上限 1000")] = 1000,
    env: Annotated[str, typer.Option("--env", help="运行环境")] = "dev",
) -> None:
    """下载币安历史 K 线。"""
    _setup_env(env)
    from app.core.database import async_db_session
    from app.modules.quant.data.service import download_binance_data
    from app.modules.quant.schema import QuantDataDownloadSchema

    params = QuantDataDownloadSchema(symbol=symbol, interval=interval, start_time=start, end_time=end, limit=limit)

    async def _run() -> dict:
        """在异步会话中执行下载。"""
        async with async_db_session() as db:
            return await download_binance_data(params, db=db)

    _print_json(asyncio.run(_run()))


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
