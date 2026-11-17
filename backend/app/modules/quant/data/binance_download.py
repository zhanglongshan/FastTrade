"""币安 K 线下载器：现货 REST、公开 REST 镜像与历史归档 ZIP。

设计参考 freqtrade 的 ``binance_mirror.py`` / ``binance_public_data.py``：
- 实时/近端数据走 REST 分页；
- 长历史走 ``data.binance.vision`` 的月/日归档 ZIP，整月用月文件、残缺月用日文件；
- 所有来源统一输出 ``date/open/high/low/close/volume`` 的标准行，再交给上层去重落盘。
"""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.core.exceptions import CustomException

# 官方现货 / 合约 REST
BINANCE_API_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_FAPI_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
# 公开 REST 镜像：仅现货，适合 api.binance.com 受限的网络环境
MIRROR_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
# 历史归档：现货 spot、U 本位合约 futures/um
ARCHIVE_BASE_URL = "https://data.binance.vision/data"
ARCHIVE_MARKETS = {"spot": "spot", "futures": "futures/um"}
ARCHIVE_DELAY_DAYS = 2

PAGE_LIMIT = 1000
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 60.0
ARCHIVE_CONCURRENCY = 8

CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


@dataclass
class FetchResult:
    """一次下载的原始结果与统计信息。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0
    archive_files: int = 0
    missing_periods: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArchiveFile:
    """一个归档 ZIP 的访问地址与对应的时间片段标识。"""

    url: str
    period: str


def now_ms() -> int:
    """当前 UTC 毫秒时间戳。"""
    return int(datetime.now(UTC).timestamp() * 1000)


def to_ms(value: datetime) -> int:
    """datetime 转毫秒时间戳。"""
    return int(value.timestamp() * 1000)


def ms_to_datetime(value: int) -> datetime:
    """毫秒时间戳转 UTC datetime。"""
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def normalize_timestamp(raw: Any) -> int:
    """把币安时间戳统一成毫秒。

    归档 CSV 格式并不一致：现货多为微秒、合约多为毫秒、老文件可能是秒。
    位数是唯一可靠的判据（参考 freqtrade ``binance_mirror.normalize_timestamp``）。
    """
    value = int(float(raw))
    digits = len(str(abs(value)))
    if digits >= 19:
        return value // 1_000_000
    if digits >= 16:
        return value // 1_000
    if digits >= 13:
        return value
    return value * 1_000


def timeframe_to_ms(interval: str) -> int:
    """返回周期对应的毫秒数；月线按 31 天做保守估计。"""
    if interval == "1M":
        return 31 * 86_400_000
    match = re.fullmatch(r"(\d+)([smhdw])", interval)
    if not match:
        raise CustomException(msg=f"不支持的 K 线周期：{interval}")
    return int(match.group(1)) * _UNIT_MS[match.group(2)]


def row_from_raw(item: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    """把币安 klines 数组 / 归档 CSV 前六列转成标准 OHLCV 行。"""
    if len(item) < 6:
        raise CustomException(msg=f"K 线数据列数不足：{item!r}")
    return {
        "date": ms_to_datetime(normalize_timestamp(item[0])).isoformat(),
        "open": float(item[1]),
        "high": float(item[2]),
        "low": float(item[3]),
        "close": float(item[4]),
        "volume": float(item[5]),
    }


def parse_archive_csv(content: bytes) -> list[dict[str, Any]]:
    """解析归档 ZIP 内的 kline CSV。

    币安历史遗留问题（issue #283）：现货归档无表头、合约归档有表头。
    通过首字符是否为数字判断，比按市场猜测更可靠。
    """
    text = content.decode("utf-8-sig", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and not lines[0][0].isdigit():
        lines = lines[1:]
    rows: list[dict[str, Any]] = []
    for line in lines:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            rows.append(row_from_raw(parts[:6]))
        except (TypeError, ValueError):
            continue
    return rows


def build_archive_urls(
    symbol: str,
    interval: str,
    market: str,
    start_ms: int,
    end_ms: int,
) -> list[ArchiveFile]:
    """生成归档 ZIP 地址：完整月份用月文件，残缺月份用日文件。

    这样下载多年历史时请求数保持在"月"级别，同时首尾残缺月份仍能覆盖。
    """
    if market not in ARCHIVE_MARKETS:
        raise CustomException(msg=f"不支持的归档市场：{market}，可选：{sorted(ARCHIVE_MARKETS)}")
    start_day = ms_to_datetime(start_ms).date()
    end_day = ms_to_datetime(end_ms).date()
    prefix = f"{ARCHIVE_BASE_URL}/{ARCHIVE_MARKETS[market]}"
    files: list[ArchiveFile] = []
    cursor = date(start_day.year, start_day.month, 1)
    while cursor <= end_day:
        next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        month_last = next_month - timedelta(days=1)
        if cursor >= start_day and month_last <= end_day:
            stamp = f"{cursor.year:04d}-{cursor.month:02d}"
            files.append(
                ArchiveFile(
                    f"{prefix}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{stamp}.zip",
                    stamp,
                )
            )
        else:
            day = max(cursor, start_day)
            last_day = min(month_last, end_day)
            while day <= last_day:
                stamp = day.isoformat()
                files.append(
                    ArchiveFile(
                        f"{prefix}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{stamp}.zip",
                        stamp,
                    )
                )
                day += timedelta(days=1)
        cursor = next_month
    return files


def merge_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 date 去重合并，后写入的数据覆盖先写入的数据。"""
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for row in group:
            merged[row["date"]] = row
    return sorted(merged.values(), key=lambda item: item["date"])


def clip_rows(rows: list[dict[str, Any]], start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """把 K 线裁剪到请求时间窗（闭区间）。"""
    kept: list[dict[str, Any]] = []
    for row in rows:
        try:
            opened = int(datetime.fromisoformat(row["date"]).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            continue
        if start_ms <= opened <= end_ms:
            kept.append(row)
    return kept


def _client() -> httpx.AsyncClient:
    """创建带连接复用的 HTTP 客户端。"""
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "FastTradeQuant/2.0"},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


async def _get(client: httpx.AsyncClient, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response | None:
    """GET 公共数据地址；404 视为"该片段无数据"，其他瞬时失败指数退避重试。"""
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.get(url, params=params)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))
    raise CustomException(msg=f"币安数据请求失败：{last_error}")


async def fetch_rest(
    client: httpx.AsyncClient,
    *,
    url: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int,
    result: FetchResult,
) -> FetchResult:
    """REST 分页拉取 K 线。

    游标使用 ``最后一根开盘时间 + 1ms``，对月线同样适用，不会因固定步长跳过整月。
    """
    cursor = start_ms
    while cursor <= end_ms:
        response = await _get(
            client,
            url,
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": min(limit, PAGE_LIMIT),
            },
        )
        result.requests += 1
        if response is None:
            break
        try:
            payload = response.json()
        except ValueError as exc:
            raise CustomException(msg=f"币安返回格式异常：{str(exc)[:200]}") from exc
        if not isinstance(payload, list):
            raise CustomException(msg=f"币安返回格式异常：{str(payload)[:200]}")
        if not payload:
            break
        result.rows.extend(row_from_raw(item) for item in payload)
        last_opened = normalize_timestamp(payload[-1][0])
        cursor = last_opened + 1
        if len(payload) < limit or last_opened >= end_ms:
            break
    return result


async def fetch_archive(
    client: httpx.AsyncClient,
    *,
    symbol: str,
    interval: str,
    market: str,
    start_ms: int,
    end_ms: int,
    result: FetchResult,
) -> FetchResult:
    """并发下载归档 ZIP 并解析 CSV。"""
    files = build_archive_urls(symbol, interval, market, start_ms, end_ms)
    if not files:
        return result
    semaphore = asyncio.Semaphore(ARCHIVE_CONCURRENCY)

    async def _one(archive_file: ArchiveFile) -> list[dict[str, Any]]:
        async with semaphore:
            response = await _get(client, archive_file.url)
            result.requests += 1
            if response is None:
                result.missing_periods.append(archive_file.period)
                return []
            try:
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    collected: list[dict[str, Any]] = []
                    for name in archive.namelist():
                        if name.lower().endswith(".csv"):
                            collected.extend(parse_archive_csv(archive.read(name)))
                    return collected
            except (zipfile.BadZipFile, OSError, UnicodeError) as exc:
                result.warnings.append(f"{archive_file.period}: 归档解析失败 {exc}")
                return []

    batches = await asyncio.gather(*(_one(item) for item in files))
    for batch in batches:
        result.rows.extend(batch)
    result.archive_files = len(files) - len(result.missing_periods)
    if result.missing_periods:
        preview = ", ".join(result.missing_periods[:5])
        result.warnings.append(f"归档缺失片段 {len(result.missing_periods)} 个：{preview}")
    return result


async def fetch_rows(
    *,
    source: str,
    market: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = PAGE_LIMIT,
    fill_recent: bool = True,
) -> FetchResult:
    """按来源拉取 K 线并归一化。

    - ``api``：币安官方 REST（现货或 U 本位合约）；
    - ``mirror``：公开 REST 镜像，仅现货；
    - ``archive``：币安历史归档（月/日 ZIP），默认自动用公开镜像补齐归档延迟窗口。
    """
    if source not in {"api", "mirror", "archive"}:
        raise CustomException(msg=f"不支持的下载方式：{source}，可选：api / mirror / archive")
    if market not in {"spot", "futures"}:
        raise CustomException(msg=f"不支持的市场：{market}，可选：spot / futures")
    if source == "mirror" and market != "spot":
        raise CustomException(msg="公开 REST 镜像仅支持现货（spot），合约请选择 api 或 archive")

    result = FetchResult()
    if source == "archive":
        archive_end_ms = to_ms(datetime.now(UTC) - timedelta(days=ARCHIVE_DELAY_DAYS))
        if start_ms >= archive_end_ms:
            raise CustomException(
                msg=f"归档数据有 {ARCHIVE_DELAY_DAYS} 天延迟，请选择更早的开始时间，或改用 api / mirror"
            )
        effective_end = min(end_ms, archive_end_ms)
        async with _client() as client:
            await fetch_archive(
                client,
                symbol=symbol,
                interval=interval,
                market=market,
                start_ms=start_ms,
                end_ms=effective_end,
                result=result,
            )
        if fill_recent and end_ms > archive_end_ms:
            recent = FetchResult()
            try:
                async with _client() as client:
                    await fetch_rest(
                        client,
                        url=MIRROR_KLINES_URL if market == "spot" else BINANCE_FAPI_KLINES_URL,
                        symbol=symbol,
                        interval=interval,
                        start_ms=archive_end_ms + 1,
                        end_ms=end_ms,
                        limit=limit,
                        result=recent,
                    )
                result.rows.extend(recent.rows)
                result.requests += recent.requests
            except CustomException as exc:
                result.warnings.append(f"近端补数失败（可稍后用 api 重新下载）：{exc.msg}")
    else:
        url = (
            MIRROR_KLINES_URL
            if source == "mirror"
            else BINANCE_FAPI_KLINES_URL if market == "futures" else BINANCE_API_KLINES_URL
        )
        async with _client() as client:
            await fetch_rest(
                client,
                url=url,
                symbol=symbol,
                interval=interval,
                start_ms=start_ms,
                end_ms=end_ms,
                limit=limit,
                result=result,
            )

    result.rows = merge_rows(clip_rows(result.rows, start_ms, end_ms))
    if not result.rows:
        raise CustomException(msg="币安未返回数据，请检查交易对、周期、市场类型与时间范围")
    return result
