"""量化币安下载器单元测试：REST 分页、归档规划/解析、落盘路径。"""

import io
import zipfile
from datetime import UTC, datetime

from app.modules.quant.data import service
from app.modules.quant.data.binance_download import (
    FetchResult,
    build_archive_urls,
    clip_rows,
    fetch_archive,
    fetch_rest,
    merge_rows,
    normalize_timestamp,
    parse_archive_csv,
)
from app.modules.quant.schema import QuantDataDownloadSchema

START = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2024, 3, 16, tzinfo=UTC).timestamp() * 1000)


class FakeResponse:
    """模拟 httpx 响应。"""

    def __init__(self, *, content: bytes = b"", json_data: object = None, status_code: int = 200) -> None:
        self.content = content
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """非 2xx 时抛错。"""
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        """返回模拟 JSON。"""
        return self._json


class MappingClient:
    """按 URL 返回预设内容的模拟客户端。"""

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def get(self, url: str, *, params: dict | None = None) -> FakeResponse:
        """记录调用并按 URL 命中内容，未命中视为 404。"""
        self.calls.append(url)
        if url not in self.mapping:
            return FakeResponse(status_code=404)
        return FakeResponse(content=self.mapping[url])


class SequenceClient:
    """按顺序返回预设 JSON 的模拟客户端。"""

    def __init__(self, payloads: list[object]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    async def get(self, url: str, *, params: dict | None = None) -> FakeResponse:
        """记录请求参数并弹出下一页。"""
        self.calls.append(dict(params or {}))
        return FakeResponse(json_data=self.payloads.pop(0))


def _zip_csv(text: str) -> bytes:
    """生成只包含一份 CSV 的归档 ZIP。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("klines.csv", text)
    return buffer.getvalue()


def test_normalize_timestamp_by_digit_count() -> None:
    """归档时间戳的秒/毫秒/微秒/纳秒都能归一到毫秒。"""
    assert normalize_timestamp("1704067200") == START
    assert normalize_timestamp("1704067200000") == START
    assert normalize_timestamp("1704067200000000") == START
    assert normalize_timestamp("1704067200000000000") == START


def test_parse_archive_csv_with_and_without_header() -> None:
    """现货无表头、合约有表头的归档 CSV 都能解析。"""
    no_header = "1704067200000000,100,110,90,105,10".encode()
    with_header = "open_time,open,high,low,close,volume\n1704067200000,100,110,90,105,10".encode()
    assert parse_archive_csv(no_header)[0]["date"] == "2024-01-01T00:00:00+00:00"
    assert parse_archive_csv(with_header)[0]["close"] == 105.0


def test_build_archive_urls_uses_monthly_for_full_months() -> None:
    """整月使用月归档，残缺月份使用日归档。"""
    urls = build_archive_urls("BTCUSDT", "1h", "spot", START, END)
    monthly = [item.url for item in urls if "/monthly/" in item.url]
    daily = [item.url for item in urls if "/daily/" in item.url]
    assert len(monthly) == 2
    assert monthly[0].endswith("/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip")
    assert len(daily) == 16  # 3 月 1 日至 16 日
    assert daily[-1].endswith("BTCUSDT-1h-2024-03-16.zip")


def test_merge_and_clip_rows() -> None:
    """合并按时间排序去重，裁剪保留闭区间内数据。"""
    row = {"date": "2024-01-01T00:00:00+00:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
    later = {"date": "2024-04-01T00:00:00+00:00", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2}
    assert merge_rows([row, row], [later]) == [row, later]
    assert clip_rows([row, later], START, END) == [row]


async def test_fetch_rest_paginates_with_last_open_plus_one() -> None:
    """REST 分页游标为最后一根 K 线开盘时间 + 1ms，避免重复与漏数据。"""
    rows = [
        [START, 1, 2, 0, 1, 10],
        [START + 3_600_000, 1, 2, 0, 1, 10],
    ]
    client = SequenceClient([rows, [[START + 7_200_000, 1, 2, 0, 1, 10]]])
    result = FetchResult()
    await fetch_rest(
        client,
        url="https://example.test/klines",
        symbol="BTCUSDT",
        interval="1h",
        start_ms=START,
        end_ms=END,
        limit=2,
        result=result,
    )
    assert len(result.rows) == 3
    assert client.calls[1]["startTime"] == START + 3_600_000 + 1
    assert result.requests == 2


async def test_fetch_archive_parses_zips_and_records_missing_periods() -> None:
    """归档下载解析 ZIP、跳过 404 并记录缺失片段。"""
    january = f"{START // 1000},100,110,90,105,10"
    urls = build_archive_urls("BTCUSDT", "1h", "spot", START, END)
    mapping = {urls[0].url: _zip_csv(january)}
    client = MappingClient(mapping)
    result = FetchResult()
    await fetch_archive(
        client,
        symbol="BTCUSDT",
        interval="1h",
        market="spot",
        start_ms=START,
        end_ms=END,
        result=result,
    )
    assert result.rows[0]["close"] == 105.0
    assert result.archive_files == 1
    assert len(result.missing_periods) == len(urls) - 1


async def test_download_target_paths_and_erase(monkeypatch, tmp_path) -> None:
    """现货与合约分目录写入，erase 会丢弃本地已有数据。"""

    async def fake_fetch_rows(**kwargs) -> FetchResult:
        """返回固定的一根新 K 线。"""
        return FetchResult(
            rows=[{"date": "2024-01-01T00:00:00+00:00", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1}]
        )

    monkeypatch.setattr(service, "QUANT_BINANCE_DIR", tmp_path)
    monkeypatch.setattr(service, "fetch_rows", fake_fetch_rows)

    futures = QuantDataDownloadSchema(
        symbol="BTC/USDT",
        interval="1h",
        start_time="2024-01-01",
        end_time="2024-01-02",
        source="archive",
        market="futures",
    )
    result = await service.download_binance_data(futures)
    assert result["file_path"].replace("\\", "/").endswith("futures/BTCUSDT_1h.csv")
    assert result["source"] == "archive"

    # 手工塞入一条旧数据，验证 erase 生效
    target = tmp_path / "futures" / "BTCUSDT_1h.csv"
    with target.open("a", encoding="utf-8") as file:
        file.write("2023-01-01T00:00:00+00:00,1,1,1,1,1\n")

    params = futures.model_copy(update={"erase": True})
    result = await service.download_binance_data(params)
    assert result["row_count"] == 1
