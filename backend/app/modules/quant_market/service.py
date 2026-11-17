"""A股/期货量化通用服务：数据集、K线、回测、实时策略。

与 web3 模块共用回测引擎（quant.backtest.engine）与策略加载器
（quant.strategy.loader），仅数据与表不同。实时 tick 用账户账本
（quant.execution.base.AccountLedger）在本地撮合，柜台适配器提供价格。
"""

import asyncio
import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.path_conf import PROJECT_ROOT, QUANT_DATA_DIR
from app.core.base_schema import AuthSchema
from app.core.exceptions import CustomException

from ..quant.backtest.engine import load_candles, run_backtest
from ..quant.execution.base import AccountLedger
from ..quant.strategy.loader import load_strategy
from .broker import get_broker
from .config import MarketConfig, get_market
from .model import MARKET_MODELS
from .schema import (
    MarketBacktestCreateSchema,
    MarketCandleSchema,
    MarketImportSchema,
    MarketLiveCreateSchema,
    MarketTickSchema,
)

CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def _naive_utc() -> datetime:
    """返回 naive UTC，适配 TIMESTAMP WITHOUT TIME ZONE 列。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _relative(path: Path) -> str:
    """绝对路径转项目相对路径。"""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _resolve_path(file_path: str) -> Path:
    """解析并校验本地 CSV 路径，必须在允许数据目录内。"""
    raw = Path(file_path).expanduser()
    candidates = [raw] if raw.is_absolute() else [PROJECT_ROOT / raw, QUANT_DATA_DIR / raw]
    for candidate in candidates:
        resolved = candidate.resolve()
        for allowed in (QUANT_DATA_DIR, PROJECT_ROOT / "backend" / "data" / "quant"):
            try:
                resolved.relative_to(allowed.resolve())
                return resolved
            except ValueError:
                continue
    raise CustomException(msg=f"文件必须位于允许目录内：{QUANT_DATA_DIR}")


def _read_csv(path: Path) -> list[dict[str, Any]]:
    """读取标准 OHLCV CSV。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise CustomException(msg=f"CSV 缺少必需列：{', '.join(missing)}")
        for line_no, raw in enumerate(reader, start=2):
            try:
                rows.append({
                    "date": str(raw["date"]).strip(),
                    "open": float(raw["open"]),
                    "high": float(raw["high"]),
                    "low": float(raw["low"]),
                    "close": float(raw["close"]),
                    "volume": float(raw["volume"]),
                })
            except (TypeError, ValueError) as exc:
                raise CustomException(msg=f"CSV 第 {line_no} 行格式无效：{exc}") from exc
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """写入标准 OHLCV CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _merge(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 date 去重合并。"""
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for row in group:
            merged[row["date"]] = row
    return sorted(merged.values(), key=lambda item: item["date"])


def _dataset_name(market: str, symbol: str, interval: str) -> str:
    """生成稳定数据集名。"""
    return f"{market}_{symbol}_{interval}".lower().replace("/", "").replace(".", "")


async def import_dataset(
    market_key: str,
    params: MarketImportSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> dict[str, Any]:
    """导入本地 CSV 到市场数据目录并登记数据集。"""
    market = get_market(market_key)
    model = MARKET_MODELS[market_key]["dataset"]
    source_path = _resolve_path(params.file_path)
    rows = _read_csv(source_path)
    if not rows:
        raise CustomException(msg="CSV 文件为空")

    target = market.data_dir / f"{params.symbol.replace('/', '_')}_{params.interval}.csv"
    merged = _merge(_read_csv(target), rows)
    _write_csv(target, merged)

    name = _dataset_name(market_key, params.symbol, params.interval)
    result = await db.execute(select(model).where(model.name == name))
    obj = result.scalar_one_or_none()
    values = {
        "symbol": params.symbol,
        "interval": params.interval,
        "source": params.source,
        "file_path": _relative(target),
        "row_count": len(merged),
        "start_time": _csv_time(merged[0]["date"]),
        "end_time": _csv_time(merged[-1]["date"]),
        "description": params.description,
    }
    user_id = getattr(getattr(auth, "user", None), "id", None)
    if obj is None:
        obj = model(name=name, status=1, created_id=user_id, **values)
        db.add(obj)
    else:
        for key, value in values.items():
            setattr(obj, key, value)
        if user_id is not None:
            obj.updated_id = user_id
    await db.commit()
    await db.refresh(obj)
    return {"id": obj.id, "name": name, "row_count": len(merged), "file_path": _relative(target)}


def _csv_time(value: str) -> datetime:
    """ISO8601 字符串转 naive UTC。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    except ValueError:
        return _naive_utc()


async def list_datasets(market_key: str, db: AsyncSession, *, limit: int = 100) -> list[Any]:
    """查询市场数据集列表。"""
    model = MARKET_MODELS[market_key]["dataset"]
    stmt = select(model).where(model.is_deleted.is_(False)).order_by(model.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


def list_local_csv_files(market_key: str) -> list[dict[str, Any]]:
    """列出允许导入的 CSV 文件（QUANT_DATA_DIR 下，供页面选择）。"""
    result: list[dict[str, Any]] = []
    if not QUANT_DATA_DIR.exists():
        return result
    for path in QUANT_DATA_DIR.rglob("*.csv"):
        if not path.is_file():
            continue
        stat = path.stat()
        result.append({
            "path": _relative(path),
            "name": path.name,
            "size": stat.st_size,
            "modified_time": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        })
    return sorted(result, key=lambda item: item["modified_time"], reverse=True)


async def get_dataset(market_key: str, db: AsyncSession, dataset_id: int) -> Any:
    """按 ID 查询数据集。"""
    model = MARKET_MODELS[market_key]["dataset"]
    obj = await db.get(model, dataset_id)
    if obj is None or obj.is_deleted:
        raise CustomException(msg="数据集不存在")
    return obj


async def read_candles(market_key: str, db: AsyncSession, dataset_id: int, *, limit: int = 2000) -> dict[str, Any]:
    """读取数据集 K 线供图表使用。"""
    obj = await get_dataset(market_key, db, dataset_id)
    path = Path(obj.file_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    rows = _read_csv(path)
    total = len(rows)
    shown = rows[-limit:] if total > limit else rows
    return {
        "dataset_id": obj.id,
        "name": obj.name,
        "symbol": obj.symbol,
        "interval": obj.interval,
        "total": total,
        "candles": [MarketCandleSchema(**row) for row in shown],
    }


async def create_backtest(
    market_key: str,
    params: MarketBacktestCreateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> Any:
    """创建并同步执行回测。"""
    dataset = await get_dataset(market_key, db, params.dataset_id)
    info = load_strategy(params.strategy_name, directory=get_market(market_key).strategy_dir)
    model = MARKET_MODELS[market_key]["backtest"]
    name = params.name or f"{dataset.symbol}_{dataset.interval}_{info.name}_{datetime.now(UTC):%m%d%H%M%S}"
    user_id = getattr(getattr(auth, "user", None), "id", None)
    obj = model(
        name=name,
        strategy_name=info.name,
        symbol=dataset.symbol,
        interval=dataset.interval,
        initial_cash=params.initial_cash,
        fee_rate=params.fee_rate,
        status=0,
        started_time=_naive_utc(),
        description=params.description,
        created_id=user_id,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    try:
        path = Path(dataset.file_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        candles = await asyncio.to_thread(load_candles, path)
        result = await asyncio.to_thread(
            run_backtest,
            candles,
            info.cls,
            mode=info.mode,
            symbol=dataset.symbol,
            interval=dataset.interval,
            initial_cash=params.initial_cash,
            fee_rate=params.fee_rate,
            slippage=params.slippage,
        )
    except Exception as exc:  # noqa: BLE001 - 失败也要落库
        obj.status = 2
        obj.error = str(exc)
        obj.finished_time = _naive_utc()
        await db.commit()
        await db.refresh(obj)
        raise CustomException(msg=f"回测失败：{exc}") from exc
    obj.status = 1
    obj.result = result
    obj.finished_time = _naive_utc()
    await db.commit()
    await db.refresh(obj)
    return obj


async def list_backtests(market_key: str, db: AsyncSession, *, limit: int = 100) -> list[Any]:
    """查询回测列表。"""
    model = MARKET_MODELS[market_key]["backtest"]
    stmt = select(model).where(model.is_deleted.is_(False)).order_by(model.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def create_live(
    market_key: str,
    params: MarketLiveCreateSchema,
    *,
    db: AsyncSession,
    auth: AuthSchema | None = None,
) -> Any:
    """创建实时策略实例。"""
    model = MARKET_MODELS[market_key]["live"]
    user_id = getattr(getattr(auth, "user", None), "id", None)
    ledger = AccountLedger(
        initial_cash=params.initial_cash,
        fee_rate=params.fee_rate,
        slippage=params.slippage,
    )
    obj = model(
        name=params.name,
        mode=params.mode,
        strategy_name=params.strategy_name,
        symbol=params.symbol,
        interval=params.interval,
        initial_cash=params.initial_cash,
        fee_rate=params.fee_rate,
        slippage=params.slippage,
        dry_run=params.dry_run,
        allow_short=params.allow_short,
        status=0,
        broker_config=params.broker_config,
        account=ledger.snapshot(),
        description=params.description,
        created_id=user_id,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def list_live(market_key: str, db: AsyncSession, *, limit: int = 100) -> list[Any]:
    """查询实时策略实例列表。"""
    model = MARKET_MODELS[market_key]["live"]
    stmt = select(model).where(model.is_deleted.is_(False)).order_by(model.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def run_tick(
    market_key: str,
    strategy_id: int,
    params: MarketTickSchema,
    *,
    db: AsyncSession,
) -> Any:
    """手动运行一次 tick。

    行情优先级：显式 price > 离线 CSV > 柜台适配器（XTP/CTP，默认模拟）。
    撮合复用 web3 的 run_tick + PaperExecutor，保证三套市场策略结果一致。
    """
    from ..quant.execution.runner import PaperExecutor, run_tick as engine_run_tick

    market = get_market(market_key)
    live_model = MARKET_MODELS[market_key]["live"]
    trade_model = MARKET_MODELS[market_key]["trade"]
    obj = await db.get(live_model, strategy_id)
    if obj is None or obj.is_deleted:
        raise CustomException(msg="实时策略实例不存在")

    candles_path: Path | None = None
    if params.candles_file:
        raw = Path(params.candles_file).expanduser()
        candles_path = raw if raw.is_absolute() else PROJECT_ROOT / raw

    price = params.price
    candles = None
    if candles_path is not None:
        candles = await asyncio.to_thread(load_candles, candles_path)

    # 未显式指定 CSV 时，自动使用该市场同代码已导入的数据集，保证页面开箱可用
    if candles is None:
        dataset_model = MARKET_MODELS[market_key]["dataset"]
        ds = (
            await db.execute(
                select(dataset_model)
                .where(dataset_model.symbol == obj.symbol, dataset_model.is_deleted.is_(False))
                .order_by(dataset_model.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if ds is not None:
            ds_path = Path(ds.file_path)
            if not ds_path.is_absolute():
                ds_path = PROJECT_ROOT / ds_path
            candles = await asyncio.to_thread(load_candles, ds_path)

    if candles:
        if price is None:
            price = float(candles[-1].close)
    else:
        # 无离线数据时回退到柜台适配器取价（需在 broker_config.prices 配置）
        if price is None:
            broker = get_broker(market.broker_type)
            broker.connect(obj.broker_config or {})
            try:
                price = broker.latest_price(obj.symbol)
            finally:
                broker.disconnect()

    ledger = AccountLedger.from_snapshot(
        obj.account,
        fee_rate=obj.fee_rate,
        slippage=obj.slippage,
    )
    # 实盘 dry_run 时用模拟撮合；真实 SDK 接入后可替换为对应执行器
    executor = PaperExecutor(ledger, fee_rate=obj.fee_rate)
    result = await engine_run_tick(
        mode=obj.mode,
        executor=executor,
        ledger=ledger,
        strategy_name=obj.strategy_name,
        symbol=obj.symbol,
        interval=obj.interval,
        candles=candles,
        allow_short=bool(obj.allow_short),
    )
    result_dict = result.to_dict()
    if result.error:
        obj.status = 2
        obj.last_error = result.error
    else:
        obj.status = 1
        obj.last_error = None
    obj.account = ledger.snapshot(price=price)
    obj.last_result = result_dict
    obj.last_tick_time = _naive_utc()

    for fill in result_dict.get("fills") or []:
        if not isinstance(fill, dict):
            continue
        db.add(trade_model(
            strategy_id=obj.id,
            mode=obj.mode,
            symbol=obj.symbol,
            side=str(fill.get("side", "")),
            qty=float(fill.get("qty") or 0),
            price=float(fill.get("price") or 0),
            fee=float(fill.get("fee") or 0),
            pnl=float(fill["pnl"]) if fill.get("pnl") is not None else None,
            reason=params.reason or str(fill.get("reason") or "")[:200],
            trade_time=_naive_utc(),
        ))
    await db.commit()
    await db.refresh(obj)
    return obj
