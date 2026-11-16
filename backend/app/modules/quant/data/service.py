"""量化数据服务：币安 K 线下载、本地 CSV 导入与元数据管理。"""

import asyncio
import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.path_conf import (
    BASE_DIR,
    PROJECT_ROOT,
    QUANT_BINANCE_DIR,
    QUANT_DATA_DIR,
)
from app.core.base_schema import AuthSchema
from app.core.exceptions import CustomException

from ..model import QuantDatasetModel
from ..schema import QuantDataDownloadSchema, QuantDataImportSchema

# CSV 标准列：与 freqtrade OHLCV 约定一致
CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]

# 允许 API 导入的目录：防止通过 ../ 读取任意本地文件
ALLOWED_IMPORT_DIRS = [QUANT_DATA_DIR, BASE_DIR / "data" / "quant"]

# 币安 REST 接口
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def _relative_path(path: Path) -> str:
    """把绝对路径转成相对项目根目录的路径，便于跨机器迁移。"""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _parse_time_to_ms(value: str | int | None) -> int | None:
    """把 YYYY-MM-DD / ISO8601 / 毫秒时间戳统一转成毫秒时间戳。"""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _ms_to_datetime(value: int) -> datetime:
    """毫秒时间戳转 UTC datetime。"""
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _row_from_klines(item: list[Any]) -> dict[str, Any]:
    """把币安 klines 数组转成标准 OHLCV 行。"""
    opened_ms = int(item[0])
    return {
        "date": _ms_to_datetime(opened_ms).isoformat(),
        "open": float(item[1]),
        "high": float(item[2]),
        "low": float(item[3]),
        "close": float(item[4]),
        "volume": float(item[5]),
    }


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    """读取并校验 CSV；表头缺列或数值非法时直接抛业务异常。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        headers = set(reader.fieldnames or [])
        missing = [column for column in CSV_COLUMNS if column not in headers]
        if missing:
            raise CustomException(msg=f"CSV 缺少必需列：{', '.join(missing)}")
        for line_no, raw in enumerate(reader, start=2):
            try:
                rows.append(
                    {
                        "date": str(raw["date"]).strip(),
                        "open": float(raw["open"]),
                        "high": float(raw["high"]),
                        "low": float(raw["low"]),
                        "close": float(raw["close"]),
                        "volume": float(raw["volume"]),
                    }
                )
            except (TypeError, ValueError) as exc:
                raise CustomException(msg=f"CSV 第 {line_no} 行数值格式无效：{exc}") from exc
    return rows


def _write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """写入标准 OHLCV CSV。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _merge_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 date 去重合并多组 K 线，后写入的数据覆盖旧数据。"""
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for row in group:
            merged[row["date"]] = row
    return sorted(merged.values(), key=lambda item: item["date"])


async def _request_klines(
    client: httpx.AsyncClient,
    *,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int | None,
    limit: int,
) -> list[list[Any]]:
    """请求单页 K 线，失败重试 3 次。"""
    params: dict[str, Any] = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "limit": limit,
    }
    if end_ms is not None:
        params["endTime"] = end_ms

    last_error: Exception | None = None
    for _ in range(3):
        try:
            response = await client.get(BINANCE_KLINES_URL, params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise CustomException(msg=f"币安返回格式异常：{str(payload)[:200]}")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            await asyncio.sleep(0.5)
    raise CustomException(msg=f"币安数据下载失败：{last_error}")


def _dataset_name(source: str, symbol: str, interval: str) -> str:
    """生成稳定的数据集名称。"""
    return f"{source}_{symbol}_{interval}".lower().replace("/", "")


async def upsert_dataset(
    db: AsyncSession,
    *,
    source: str,
    symbol: str,
    interval: str,
    file_path: Path,
    rows: list[dict[str, Any]],
    description: str | None = None,
    auth: AuthSchema | None = None,
) -> QuantDatasetModel:
    """新增或更新数据集元数据。"""
    name = _dataset_name(source, symbol, interval)
    result = await db.execute(select(QuantDatasetModel).where(QuantDatasetModel.name == name))
    obj = result.scalar_one_or_none()
    user_id = getattr(getattr(auth, "user", None), "id", None)

    values = {
        "symbol": symbol,
        "interval": interval,
        "source": source,
        "file_path": _relative_path(file_path),
        "row_count": len(rows),
        "start_time": _ms_to_datetime(int(datetime.fromisoformat(rows[0]["date"]).timestamp() * 1000)) if rows else None,
        "end_time": _ms_to_datetime(int(datetime.fromisoformat(rows[-1]["date"]).timestamp() * 1000)) if rows else None,
        "description": description,
    }
    if obj is None:
        obj = QuantDatasetModel(name=name, status=1, created_id=user_id, **values)
        db.add(obj)
    else:
        for key, value in values.items():
            setattr(obj, key, value)
        if user_id is not None:
            obj.updated_id = user_id

    await db.commit()
    await db.refresh(obj)
    return obj


async def download_binance_data(
    params: QuantDataDownloadSchema,
    *,
    db: AsyncSession | None = None,
    auth: AuthSchema | None = None,
) -> dict[str, Any]:
    """从币安下载 K 线并合并写入本地 CSV，同时登记数据集。

    参数:
    - params: 下载参数（交易对、周期、时间范围等）。
    - db: 数据库会话；CLI 可传入，纯文件下载场景可为 None。
    - auth: 当前用户，仅用于审计字段。

    返回:
    - dict[str, Any]: 数据集摘要与写入文件路径。
    """
    symbol = params.symbol
    interval = params.interval
    start_ms = _parse_time_to_ms(params.start_time)
    end_ms = _parse_time_to_ms(params.end_time)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if end_ms is None:
        end_ms = now_ms
    if start_ms is None:
        # 只给结束时间时，从结束时间往前推 30 天，避免旧时间点与默认值冲突
        start_ms = end_ms - 30 * 24 * 60 * 60 * 1000
    if start_ms >= end_ms:
        raise CustomException(msg="起始时间必须早于结束时间")

    rows: list[dict[str, Any]] = []
    cursor = start_ms
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "FastTradeQuant/1.0"}) as client:
        while cursor < end_ms:
            page = await _request_klines(
                client,
                symbol=symbol,
                interval=interval,
                start_ms=cursor,
                end_ms=end_ms,
                limit=params.limit,
            )
            if not page:
                break
            rows.extend(_row_from_klines(item) for item in page)
            last_open_ms = int(page[-1][0])
            cursor = last_open_ms + 1
            if len(page) < params.limit:
                break
            await asyncio.sleep(0.15)  # 轻度限速，避免触发币安权重限制

    if not rows:
        raise CustomException(msg="币安未返回数据，请检查交易对、周期与时间范围")

    file_path = QUANT_BINANCE_DIR / f"{symbol}_{interval}.csv"
    merged = _merge_rows(_read_csv_rows(file_path), rows)
    _write_csv_rows(file_path, merged)

    dataset = None
    if db is not None:
        dataset = await upsert_dataset(
            db,
            source="binance",
            symbol=symbol,
            interval=interval,
            file_path=file_path,
            rows=merged,
            description=params.description,
            auth=auth,
        )

    return {
        "name": _dataset_name("binance", symbol, interval),
        "symbol": symbol,
        "interval": interval,
        "file_path": _relative_path(file_path),
        "downloaded": len(rows),
        "row_count": len(merged),
        "dataset_id": dataset.id if dataset else None,
    }


def _resolve_import_path(file_path: str) -> Path:
    """解析并校验待导入 CSV 路径，必须位于允许目录内。"""
    raw = Path(file_path).expanduser()
    candidates = [raw] if raw.is_absolute() else [PROJECT_ROOT / raw, QUANT_DATA_DIR / raw]
    for candidate in candidates:
        resolved = candidate.resolve()
        for allowed in ALLOWED_IMPORT_DIRS:
            try:
                resolved.relative_to(allowed.resolve())
                return resolved
            except ValueError:
                continue
    allowed_text = ", ".join(str(path) for path in ALLOWED_IMPORT_DIRS)
    raise CustomException(msg=f"文件必须位于允许目录内：{allowed_text}")


async def import_local_csv(
    params: QuantDataImportSchema,
    *,
    db: AsyncSession | None = None,
    auth: AuthSchema | None = None,
) -> dict[str, Any]:
    """导入本地 CSV，归一化后写入 user_data/data 并登记数据集。"""
    source_path = _resolve_import_path(params.file_path)
    rows = _read_csv_rows(source_path)
    if not rows:
        raise CustomException(msg="CSV 文件为空")

    target_path = QUANT_DATA_DIR / f"{params.source}_{params.symbol}_{params.interval}.csv"
    merged = _merge_rows(_read_csv_rows(target_path), rows)
    _write_csv_rows(target_path, merged)

    dataset = None
    if db is not None:
        dataset = await upsert_dataset(
            db,
            source=params.source,
            symbol=params.symbol,
            interval=params.interval,
            file_path=target_path,
            rows=merged,
            description=params.description,
            auth=auth,
        )

    return {
        "name": _dataset_name(params.source, params.symbol, params.interval),
        "symbol": params.symbol,
        "interval": params.interval,
        "file_path": _relative_path(target_path),
        "imported": len(rows),
        "row_count": len(merged),
        "dataset_id": dataset.id if dataset else None,
    }


async def list_datasets(
    db: AsyncSession,
    *,
    symbol: str | None = None,
    interval: str | None = None,
    limit: int = 100,
) -> list[QuantDatasetModel]:
    """查询数据集列表。"""
    stmt = select(QuantDatasetModel).where(QuantDatasetModel.is_deleted.is_(False))
    if symbol:
        stmt = stmt.where(QuantDatasetModel.symbol == symbol.replace("/", "").upper())
    if interval:
        stmt = stmt.where(QuantDatasetModel.interval == interval)
    stmt = stmt.order_by(QuantDatasetModel.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def get_dataset(db: AsyncSession, dataset_id: int) -> QuantDatasetModel:
    """按 ID 查询数据集，不存在则抛业务异常。"""
    obj = await db.get(QuantDatasetModel, dataset_id)
    if obj is None or obj.is_deleted:
        raise CustomException(msg="数据集不存在")
    return obj


def list_local_csv_files() -> list[dict[str, Any]]:
    """列出允许目录下的 CSV 文件，供页面选择导入。"""
    result: list[dict[str, Any]] = []
    for root in ALLOWED_IMPORT_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*.csv"):
            if not path.is_file():
                continue
            stat = path.stat()
            result.append(
                {
                    "path": _relative_path(path),
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_time": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                }
            )
    return sorted(result, key=lambda item: item["modified_time"], reverse=True)


def resolve_dataset_csv(file_path: str) -> Path:
    """把数据集相对路径解析为绝对路径并校验存在。"""
    raw = Path(file_path).expanduser()
    path = raw if raw.is_absolute() else PROJECT_ROOT / raw
    resolved = path.resolve()
    if not resolved.is_file():
        raise CustomException(msg=f"数据文件不存在：{resolved}")
    return resolved
