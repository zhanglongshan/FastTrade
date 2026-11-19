"""币安 K 线下载器：现货 REST、公开 REST 镜像与历史归档 ZIP。

用途 | Purpose:
    提供统一的多来源 K 线抓取能力，把币安官方 REST、公开 REST 镜像与历史归档 ZIP
    三种数据源的返回统一为 ``date/open/high/low/close/volume`` 标准行，再交给上层去重落盘。

设计参考 | Design reference:
    freqtrade 的 ``binance_mirror.py`` / ``binance_public_data.py``。

主要流程 | Main flow:
    1. 实时/近端数据走 REST 分页（``fetch_rest``）；
    2. 长历史走 ``data.binance.vision`` 的月/日归档 ZIP：整月用月文件、残缺月用日文件（``fetch_archive``）；
    3. ``fetch_rows`` 作为总入口按来源分派、校验参数、补齐归档延迟窗口，最后裁剪并按日期去重。
"""

from __future__ import annotations  # 启用延迟注解求值，允许在注解中引用尚未定义的类型 | Enable postponed annotation evaluation

import asyncio  # 导入异步 IO 库，用于并发控制与退避等待 | Import asyncio for concurrency control and async sleeps
import io  # 导入内存字节流，用于把响应内容当文件读 ZIP | Import io for in-memory byte buffers used to read ZIP data
import re  # 导入正则表达式，用于校验与解析 K 线周期字符串 | Import re to validate and parse K-line interval strings
import zipfile  # 导入 ZIP 解压模块，用于读取归档内的 CSV | Import zipfile to read K-line CSV files inside archives
from dataclasses import dataclass, field  # 导入数据类装饰器与字段工厂，用于结果容器 | Import dataclass and field for lightweight result containers
from datetime import UTC, date, datetime, timedelta  # 导入日期时间类型，用于时间换算与逐月迭代 | Import UTC/date/datetime/timedelta for time conversions and month iteration
from typing import Any  # 导入任意类型标注，用于宽松标注原始 K 线字段 | Import Any for loosely typed raw kline values

import httpx  # 导入异步 HTTP 客户端，用于发起 GET 请求 | Import httpx for async HTTP GET requests

from app.core.exceptions import CustomException  # 导入业务异常类，用于抛出领域错误 | Import CustomException for raising domain-specific errors

# 官方现货 / 合约 REST 地址 | Official spot / futures REST endpoints
BINANCE_API_KLINES_URL = "https://api.binance.com/api/v3/klines"  # 官方现货 REST K 线接口 | Official spot REST klines endpoint
BINANCE_FAPI_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"  # 官方 U 本位合约 REST K 线接口 | Official USD-M futures REST klines endpoint
# 公开 REST 镜像：仅现货，适合 api.binance.com 受限的网络环境 | Public REST mirror: spot only, for networks where the official API is blocked
MIRROR_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"  # 公开镜像仅支持现货 | Public mirror only supports spot markets
# 历史归档：现货 spot、U 本位合约 futures/um | Historical archives: spot and USD-M futures/um
ARCHIVE_BASE_URL = "https://data.binance.vision/data"  # 历史归档根地址 | Historical archive base URL
ARCHIVE_MARKETS = {"spot": "spot", "futures": "futures/um"}  # 市场名到归档路径片段的映射 | Mapping from market name to archive path segment
ARCHIVE_DELAY_DAYS = 2  # 归档数据的延迟天数 | Delay in days before archived data becomes available

PAGE_LIMIT = 1000  # 单次 REST 请求最多返回的 K 线数量 | Max candles returned by a single REST request
MAX_ATTEMPTS = 3  # 请求失败时的最大尝试次数 | Max number of attempts for a failed request
REQUEST_TIMEOUT = 60.0  # 单次 HTTP 请求的超时秒数 | HTTP request timeout in seconds
ARCHIVE_CONCURRENCY = 8  # 归档 ZIP 下载的最大并发数 | Max concurrency for archive ZIP downloads

CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]  # 标准 OHLCV 列顺序 | Standard OHLCV column order
_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}  # 周期单位到毫秒的换算表 | Interval unit to milliseconds lookup


@dataclass  # 声明普通数据类，用于承载一次下载的原始结果与统计 | Declare a dataclass holding raw results and stats of one download
class FetchResult:
    """一次下载的原始结果与统计信息 | Raw rows and statistics of a single download.

    Purpose | 用途:
        作为所有抓取函数的可变累加容器，同时携带请求数、归档数、缺失片段与警告。

    Key attributes | 关键属性:
        rows: 中文：标准化后的 OHLCV 行列表；English: list of normalized OHLCV rows.
        requests: 中文：发出的 HTTP 请求总数；English: total number of HTTP requests made.
        archive_files: 中文：成功解析的归档文件数量；English: number of archive files parsed successfully.
        missing_periods: 中文：缺失的时间片段标识列表；English: list of missing period labels.
        warnings: 中文：非致命警告信息列表；English: list of non-fatal warning messages.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)  # 标准化 OHLCV 行 | Normalized OHLCV rows (mutable default)
    requests: int = 0  # 已发出的 HTTP 请求计数 | Counter of issued HTTP requests
    archive_files: int = 0  # 成功解析的归档文件数 | Count of successfully parsed archive files
    missing_periods: list[str] = field(default_factory=list)  # 缺失片段标识列表 | List of missing period labels
    warnings: list[str] = field(default_factory=list)  # 非致命警告列表 | List of non-fatal warnings


@dataclass(frozen=True)  # 声明只读数据类，作为归档文件的不可变描述 | Declare a frozen dataclass as an immutable archive descriptor
class ArchiveFile:
    """一个归档 ZIP 的访问地址与对应的时间片段标识 | URL and period label of one archive ZIP.

    Purpose | 用途:
        描述单个待下载归档：完整月份或具体某一天。

    Key attributes | 关键属性:
        url: 中文：归档 ZIP 的完整下载地址；English: full download URL of the archive ZIP.
        period: 中文：YYYY-MM 或 YYYY-MM-DD 形式的时间片段标识；English: period label in YYYY-MM or YYYY-MM-DD form.
    """

    url: str  # 归档 ZIP 的完整下载地址 | Full download URL of the archive ZIP
    period: str  # 时间片段标识（月或日） | Period label (month or day)


def now_ms() -> int:  # 获取当前 UTC 毫秒时间戳 | Get the current UTC timestamp in milliseconds
    """当前 UTC 毫秒时间戳 | Current UTC timestamp in milliseconds.

    Purpose | 用途:
        以毫秒为单位返回当前 UTC 时间，供时间窗计算使用。

    Parameters | 参数:
        无 | None.

    Returns | 返回:
        int: 中文：当前 UTC 时间的毫秒时间戳；English: current UTC time in milliseconds.

    Flow | 流程:
        取 UTC now 的 POSIX 秒数并乘以 1000 后取整 | Take UTC now's POSIX seconds, multiply by 1000, and truncate to int.
    """
    return int(datetime.now(UTC).timestamp() * 1000)  # UTC 秒时间戳乘 1000 取整 | Convert UTC now to integer milliseconds


def to_ms(value: datetime) -> int:  # 把 datetime 转成毫秒时间戳 | Convert a datetime to milliseconds
    """datetime 转毫秒时间戳 | Convert a datetime to a millisecond timestamp.

    Purpose | 用途:
        把给定的 datetime 统一成供币安 API 使用的毫秒时间戳。

    Parameters | 参数:
        value: 中文：待转换的 datetime 对象；English: the datetime object to convert.

    Returns | 返回:
        int: 中文：对应的毫秒时间戳；English: the corresponding millisecond timestamp.

    Flow | 流程:
        取 POSIX 秒数乘 1000 取整 | Take POSIX seconds, multiply by 1000, and truncate to int.
    """
    return int(value.timestamp() * 1000)  # 秒乘 1000 取整得到毫秒 | Multiply seconds by 1000 and truncate to int


def ms_to_datetime(value: int) -> datetime:  # 毫秒时间戳转 UTC datetime | Convert milliseconds to a UTC datetime
    """毫秒时间戳转 UTC datetime | Convert a millisecond timestamp to a UTC datetime.

    Purpose | 用途:
        把毫秒时间戳还原为带 UTC 时区的 datetime，供日期迭代与输出使用。

    Parameters | 参数:
        value: 中文：毫秒时间戳；English: a millisecond timestamp.

    Returns | 返回:
        datetime: 中文：带 UTC 时区的 datetime；English: a timezone-aware UTC datetime.

    Flow | 流程:
        除以 1000 得到秒并指定 UTC 时区构造 datetime | Divide by 1000 to seconds and build a datetime with UTC tz.
    """
    return datetime.fromtimestamp(value / 1000, tz=UTC)  # 除以 1000 并附加 UTC 时区 | Convert seconds to datetime with UTC timezone


def normalize_timestamp(raw: Any) -> int:  # 把币安时间戳统一成毫秒 | Normalize Binance timestamps to milliseconds
    """把币安时间戳统一成毫秒 | Normalize Binance timestamps to milliseconds.

    Purpose | 用途:
        归档 CSV 格式并不一致：现货多为微秒、合约多为毫秒、老文件可能是秒。
        位数是唯一可靠的判据（参考 freqtrade ``binance_mirror.normalize_timestamp``）。

    Parameters | 参数:
        raw: 中文：原始时间戳（字符串或数字）；English: the raw timestamp (string or number).

    Returns | 返回:
        int: 中文：统一后的毫秒时间戳；English: the normalized millisecond timestamp.

    Flow | 流程:
        先转整数并测位数，再按秒/毫秒/微秒/纳秒做除法或乘法 | Convert to int, measure digit count, then divide or multiply by the detected unit.
    """
    value = int(float(raw))  # 先转浮点再转整数，兼容字符串与带小数的值 | Coerce to float then int for strings/decimals
    digits = len(str(abs(value)))  # 用绝对值的十进制位数判断单位 | Use digit count of absolute value to detect the unit

    if digits >= 19:  # 19 位及以上视为纳秒 | 19+ digits means nanoseconds
        return value // 1_000_000  # 纳秒除以一百万得毫秒 | Divide nanoseconds by one million to get milliseconds
    if digits >= 16:  # 16 位及以上视为微秒 | 16+ digits means microseconds
        return value // 1_000  # 微秒除以一千得毫秒 | Divide microseconds by one thousand to get milliseconds
    if digits >= 13:  # 13 位及以上视为毫秒 | 13+ digits means milliseconds
        return value  # 已是毫秒，直接返回 | Already milliseconds, return as-is
    return value * 1_000  # 其余按秒处理，乘以一千得毫秒 | Treat as seconds and multiply by one thousand


def timeframe_to_ms(interval: str) -> int:  # 返回周期对应的毫秒数 | Return the millisecond span of an interval
    """返回周期对应的毫秒数；月线按 31 天做保守估计 | Return milliseconds of an interval; months use a conservative 31-day estimate.

    Purpose | 用途:
        把币安周期字符串换算为毫秒，用于时间窗与分页步长判断。

    Parameters | 参数:
        interval: 中文：周期字符串，如 1m/5m/1h/1d/1w/1M；English: interval string such as 1m/5m/1h/1d/1w/1M.

    Returns | 返回:
        int: 中文：周期对应的毫秒数；English: the millisecond span of the interval.

    Raises | 异常:
        CustomException: 中文：周期格式不支持时抛出；English: raised when the interval format is unsupported.

    Flow | 流程:
        先特判月线，再用正则解析数字与单位并查表相乘 | Special-case monthly, then regex-parse number and unit and multiply via lookup.
    """
    if interval == "1M":  # 月线特判，按 31 天保守估计 | Special-case monthly using a conservative 31-day estimate
        return 31 * 86_400_000  # 31 天对应的毫秒数 | 31 days expressed in milliseconds
    match = re.fullmatch(r"(\d+)([smhdw])", interval)  # 正则解析数字与单位的组合 | Regex-match a number followed by a unit
    if not match:  # 未匹配说明格式不支持 | No match means the interval format is unsupported
        raise CustomException(msg=f"不支持的 K 线周期：{interval}")  # 抛出不支持周期的业务异常 | Raise business error for unsupported interval
    return int(match.group(1)) * _UNIT_MS[match.group(2)]  # 数字乘以单位毫秒得到总毫秒 | Multiply count by unit milliseconds


def row_from_raw(item: list[Any] | tuple[Any, ...]) -> dict[str, Any]:  # 把原始数组转成标准 OHLCV 行 | Convert a raw row to a standard OHLCV dict
    """把币安 klines 数组 / 归档 CSV 前六列转成标准 OHLCV 行 | Convert Binance klines / archive CSV columns to a standard OHLCV row.

    Purpose | 用途:
        作为所有数据源的统一出口，把位置化数据映射成带命名的标准行。

    Parameters | 参数:
        item: 中文：原始 klines 数组或 CSV 拆分后的字段序列；English: raw klines array or a sequence of CSV fields.

    Returns | 返回:
        dict[str, Any]: 中文：含 date/open/high/low/close/volume 的标准行；English: a standard row with date/open/high/low/close/volume.

    Raises | 异常:
        CustomException: 中文：列数不足时抛出；English: raised when there are too few columns.

    Flow | 流程:
        先校验列数，再把时间戳归一化后转 ISO 字符串，其余字段转 float | Validate length, normalize timestamp to ISO string, and cast the rest to float.
    """
    if len(item) < 6:  # 至少需要开高低收量六个字段 | At least open/high/low/close/volume fields are required
        raise CustomException(msg=f"K 线数据列数不足：{item!r}")  # 抛出列数不足的业务异常 | Raise business error for insufficient columns
    return {  # 构造标准 OHLCV 行 | Build the standard OHLCV row
        "date": ms_to_datetime(normalize_timestamp(item[0])).isoformat(),  # 第 0 列：时间戳归一化后转 ISO 字符串 | Column 0: normalized timestamp to ISO string
        "open": float(item[1]),  # 第 1 列：开盘价转浮点 | Column 1: open price to float
        "high": float(item[2]),  # 第 2 列：最高价转浮点 | Column 2: high price to float
        "low": float(item[3]),  # 第 3 列：最低价转浮点 | Column 3: low price to float
        "close": float(item[4]),  # 第 4 列：收盘价转浮点 | Column 4: close price to float
        "volume": float(item[5]),  # 第 5 列：成交量转浮点 | Column 5: volume to float
    }  # 返回构造好的标准行 | Return the constructed standard row


def parse_archive_csv(content: bytes) -> list[dict[str, Any]]:  # 解析归档 ZIP 内的 kline CSV | Parse a kline CSV inside an archive ZIP
    """解析归档 ZIP 内的 kline CSV | Parse a kline CSV from inside an archive ZIP.

    Purpose | 用途:
        兼容币安历史遗留问题（issue #283）：现货归档无表头、合约归档有表头。
        通过首字符是否为数字判断，比按市场猜测更可靠。

    Parameters | 参数:
        content: 中文：CSV 文件的原始字节内容；English: raw byte content of the CSV file.

    Returns | 返回:
        list[dict[str, Any]]: 中文：解析出的标准 OHLCV 行列表；English: list of parsed standard OHLCV rows.

    Flow | 流程:
        解码并去空行 → 视情况跳过表头 → 逐行拆分、校验列数、转换并容错 | Decode and drop blanks, optionally skip header, then split/validate/convert each line with error tolerance.
    """
    text = content.decode("utf-8-sig", errors="replace")  # 解码字节并容忍 BOM 与非法字符 | Decode bytes tolerating BOM and invalid chars
    lines = [line for line in text.splitlines() if line.strip()]  # 按行拆分并剔除空白行 | Split into lines and drop blank ones
    if lines and not lines[0][0].isdigit():  # 首字符非数字说明存在表头 | Non-digit first char indicates a header row
        lines = lines[1:]  # 跳过表头行 | Skip the header line
    rows: list[dict[str, Any]] = []  # 累积解析出的标准行 | Accumulator for parsed standard rows
    for line in lines:  # 遍历每一行数据 | Iterate over each data line
        parts = line.split(",")  # 按逗号拆分成字段 | Split the line into fields by comma
        if len(parts) < 6:  # 列数不足则跳过该行 | Skip rows with too few columns
            continue  # 继续下一行 | Move to next line
        try:  # 尝试把前六列转成标准行 | Try converting the first six columns into a standard row
            rows.append(row_from_raw(parts[:6]))  # 取前六列转换并追加 | Convert first six columns and append
        except (TypeError, ValueError):  # 数值转换失败则忽略该行 | Ignore rows with invalid numeric values
            continue  # 继续下一行 | Move to next line
    return rows  # 返回解析出的全部标准行 | Return all parsed standard rows


def build_archive_urls(  # 生成归档 ZIP 地址列表 | Build the list of archive ZIP URLs
    symbol: str,  # 交易对，如 BTCUSDT | Trading symbol, e.g. BTCUSDT
    interval: str,  # K 线周期 | K-line interval
    market: str,  # 市场类型：spot 或 futures | Market type: spot or futures
    start_ms: int,  # 起始时间（毫秒） | Start time in milliseconds
    end_ms: int,  # 结束时间（毫秒） | End time in milliseconds
) -> list[ArchiveFile]:  # 返回归档文件描述列表 | Return a list of archive file descriptors
    """生成归档 ZIP 地址：完整月份用月文件，残缺月份用日文件 | Build archive URLs: monthly files for full months, daily for partial ones.

    Purpose | 用途:
        这样下载多年历史时请求数保持在"月"级别，同时首尾残缺月份仍能覆盖。

    Parameters | 参数:
        symbol: 中文：交易对；English: trading symbol.
        interval: 中文：K 线周期；English: K-line interval.
        market: 中文：市场类型 spot/futures；English: market type spot/futures.
        start_ms: 中文：起始时间毫秒戳；English: start time in milliseconds.
        end_ms: 中文：结束时间毫秒戳；English: end time in milliseconds.

    Returns | 返回:
        list[ArchiveFile]: 中文：待下载的归档文件描述列表；English: list of archive file descriptors to download.

    Raises | 异常:
        CustomException: 中文：市场类型不支持时抛出；English: raised when the market type is unsupported.

    Flow | 流程:
        校验市场 → 逐月迭代：整月生成月文件、残缺月按天生成日文件 | Validate market, then iterate months: monthly file for full months, daily files for partial ones.
    """
    if market not in ARCHIVE_MARKETS:  # 校验市场是否在归档映射中 | Check the market exists in the archive mapping
        raise CustomException(msg=f"不支持的归档市场：{market}，可选：{sorted(ARCHIVE_MARKETS)}")  # 抛出不支持市场的异常 | Raise error for unsupported market
    start_day = ms_to_datetime(start_ms).date()  # 起始时间对应的日期（只取日期部分） | Start date (date part only)
    end_day = ms_to_datetime(end_ms).date()  # 结束时间对应的日期 | End date (date part only)
    prefix = f"{ARCHIVE_BASE_URL}/{ARCHIVE_MARKETS[market]}"  # 拼接归档根路径与市场路径片段 | Join archive base URL with market segment
    files: list[ArchiveFile] = []  # 累积待下载的归档描述 | Accumulator for archive descriptors
    cursor = date(start_day.year, start_day.month, 1)  # 游标定位到起始月的第一天 | Cursor points to the first day of the start month

    while cursor <= end_day:  # 逐月推进直到超过结束日 | Iterate month by month until past the end day
        next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)  # 计算下个月第一天（跨年自动进位） | Compute first day of next month, rolling over year
        month_last = next_month - timedelta(days=1)  # 当前月最后一天 | Last day of the current month

        if cursor >= start_day and month_last <= end_day:  # 整月都落在请求窗内，可用月文件 | Full month inside the window -> use monthly file
            stamp = f"{cursor.year:04d}-{cursor.month:02d}"  # 生成 YYYY-MM 形式的片段标识 | Build YYYY-MM period label
            files.append(  # 追加一个月文件描述 | Append a monthly archive descriptor
                ArchiveFile(  # 构造月文件 URL 与标识 | Build monthly file URL and label
                    f"{prefix}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{stamp}.zip",  # 月文件完整下载地址 | Full URL of the monthly archive
                    stamp,  # 该月的时间片段标识 | Period label for the month
                )  # 结束 ArchiveFile 构造 | End ArchiveFile construction
            )  # 结束 files.append | End append
        else:  # 残缺月份，需要逐天生成日文件 | Partial month -> generate daily files
            day = max(cursor, start_day)  # 从月首与起始日中较晚者开始 | Start from the later of month start and requested start
            last_day = min(month_last, end_day)  # 到月末与结束日中较早者结束 | End at the earlier of month end and requested end
            while day <= last_day:  # 逐天推进 | Iterate day by day
                stamp = day.isoformat()  # 生成 YYYY-MM-DD 形式的片段标识 | Build YYYY-MM-DD period label
                files.append(  # 追加一个日文件描述 | Append a daily archive descriptor
                    ArchiveFile(  # 构造日文件 URL 与标识 | Build daily file URL and label
                        f"{prefix}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{stamp}.zip",  # 日文件完整下载地址 | Full URL of the daily archive
                        stamp,  # 该日的时间片段标识 | Period label for the day
                    )  # 结束 ArchiveFile 构造 | End ArchiveFile construction
                )  # 结束 files.append | End append
                day += timedelta(days=1)  # 游标前进一天 | Advance the day cursor by one
        cursor = next_month  # 游标前进到下个月 | Advance the month cursor to next month
    return files  # 返回全部归档文件描述 | Return all archive descriptors


def merge_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:  # 按日期去重合并多组 K 线 | Merge row groups deduplicated by date
    """按 date 去重合并，后写入的数据覆盖先写入的数据 | Merge by date; later rows override earlier ones.

    Purpose | 用途:
        把多来源（已有 CSV 与新下载/导入）数据按日期合并，避免重复行。

    Parameters | 参数:
        *groups: 中文：若干组标准 OHLCV 行列表；English: several groups of standard OHLCV rows.

    Returns | 返回:
        list[dict[str, Any]]: 中文：按 date 升序排列的去重行列表；English: deduplicated rows sorted by date ascending.

    Flow | 流程:
        用日期作字典键覆盖写入，再按 date 排序输出 | Use date as dict key for overwrite semantics, then sort output by date.
    """
    merged: dict[str, dict[str, Any]] = {}  # 以日期为键的去重字典 | Deduplicating dict keyed by date
    for group in groups:  # 遍历每一组数据（顺序决定覆盖优先级） | Iterate each group (order decides override priority)
        for row in group:  # 遍历组内每一行 | Iterate each row in the group
            merged[row["date"]] = row  # 同日期后写覆盖先写 | Later row with same date overwrites earlier one
    return sorted(merged.values(), key=lambda item: item["date"])  # 按日期升序返回 | Return values sorted by date ascending


def clip_rows(rows: list[dict[str, Any]], start_ms: int, end_ms: int) -> list[dict[str, Any]]:  # 把 K 线裁剪到请求时间窗 | Clip candles to the requested time window
    """把 K 线裁剪到请求时间窗（闭区间） | Clip candles to the requested time window (inclusive).

    Purpose | 用途:
        归档数据常带窗口外的行，需按开盘时间裁剪到 [start_ms, end_ms]。

    Parameters | 参数:
        rows: 中文：待裁剪的标准 OHLCV 行列表；English: list of standard OHLCV rows to clip.
        start_ms: 中文：窗口起始毫秒戳；English: window start in milliseconds.
        end_ms: 中文：窗口结束毫秒戳；English: window end in milliseconds.

    Returns | 返回:
        list[dict[str, Any]]: 中文：落在闭区间内的行列表；English: rows whose open time falls within the inclusive interval.

    Flow | 流程:
        逐行解析开盘时间，无法解析则跳过，落在区间内则保留 | Parse each row's open time, skip unparsable ones, keep those inside the interval.
    """
    kept: list[dict[str, Any]] = []  # 累积保留的行 | Accumulator for kept rows
    for row in rows:  # 遍历每一行 | Iterate each row
        try:  # 尝试解析 date 字段为毫秒开盘时间 | Try parsing the date field into millisecond open time
            opened = int(datetime.fromisoformat(row["date"]).timestamp() * 1000)  # ISO 字符串转毫秒开盘时间 | Convert ISO string to millisecond open time
        except (KeyError, TypeError, ValueError):  # date 缺失或格式非法则跳过 | Skip rows with missing or invalid date
            continue  # 继续下一行 | Move to next row
        if start_ms <= opened <= end_ms:  # 开盘时间落在闭区间内 | Open time within the inclusive window
            kept.append(row)  # 保留该行 | Keep this row
    return kept  # 返回裁剪后的行 | Return the clipped rows


def _client() -> httpx.AsyncClient:  # 创建带连接复用的 HTTP 客户端 | Create an HTTP client with connection reuse
    """创建带连接复用的 HTTP 客户端 | Create an HTTP client with connection reuse.

    Purpose | 用途:
        统一构造 httpx 客户端，配置超时、UA 与连接池上限。

    Parameters | 参数:
        无 | None.

    Returns | 返回:
        httpx.AsyncClient: 中文：配置好的异步 HTTP 客户端；English: configured async HTTP client.

    Flow | 流程:
        用超时、请求头与连接限制实例化客户端 | Instantiate the client with timeout, headers, and connection limits.
    """
    return httpx.AsyncClient(  # 构造异步客户端 | Build the async client
        timeout=REQUEST_TIMEOUT,  # 设置请求超时 | Set request timeout
        headers={"User-Agent": "FastTradeQuant/2.0"},  # 设置自定义 User-Agent | Set custom User-Agent header
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),  # 限制总连接与保活连接数 | Limit total and keep-alive connections
    )  # 返回配置好的客户端 | Return the configured client


async def _get(client: httpx.AsyncClient, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response | None:  # GET 公共数据地址 | GET a public data URL
    """GET 公共数据地址；404 视为"该片段无数据"，其他瞬时失败指数退避重试 | GET a URL; 404 means no data for that period, other transient failures retry with exponential backoff.

    Purpose | 用途:
        统一处理公开数据的 HTTP 获取与重试，屏蔽网络抖动。

    Parameters | 参数:
        client: 中文：复用的异步 HTTP 客户端；English: the reusable async HTTP client.
        url: 中文：请求地址；English: request URL.
        params: 中文：可选的查询参数字典；English: optional query parameter dict.

    Returns | 返回:
        httpx.Response | None: 中文：成功返回响应，404 返回 None；English: the response on success, None on 404.

    Raises | 异常:
        CustomException: 中文：重试耗尽仍失败时抛出；English: raised when all retry attempts fail.

    Flow | 流程:
        循环尝试：404 立即返回 None，成功返回响应，HTTP 错误记录并指数退避 | Loop attempts: 404 returns None immediately, success returns response, HTTP errors are recorded with exponential backoff.
    """
    last_error: Exception | None = None  # 记录最后一次异常，供最终报错使用 | Store the last exception for final error reporting
    for attempt in range(1, MAX_ATTEMPTS + 1):  # 按最大尝试次数循环 | Loop up to the maximum number of attempts
        try:  # 尝试发起 GET 请求 | Try issuing the GET request
            response = await client.get(url, params=params)  # 发起异步 GET | Issue the async GET request
            if response.status_code == 404:  # 404 表示该片段无数据 | 404 means no data for this period
                return None  # 返回 None 表示无数据 | Return None to signal no data
            response.raise_for_status()  # 其他非 2xx 状态码抛 HTTPError | Raise HTTPError for other non-2xx statuses
            return response  # 返回成功响应 | Return the successful response
        except httpx.HTTPError as exc:  # 捕获 HTTP 相关异常 | Catch HTTP-related exceptions
            last_error = exc  # 记录本次异常 | Record this exception
            if attempt < MAX_ATTEMPTS:  # 还有重试机会 | If retries remain
                await asyncio.sleep(2 ** (attempt - 1))  # 指数退避等待 | Exponential backoff sleep
    raise CustomException(msg=f"币安数据请求失败：{last_error}")  # 重试耗尽后抛出业务异常 | Raise business error after retries are exhausted


async def fetch_rest(  # REST 分页拉取 K 线 | Fetch candles via REST pagination
    client: httpx.AsyncClient,  # 复用的异步 HTTP 客户端 | Reusable async HTTP client
    *,  # 后续参数强制关键字传入 | Force keyword-only arguments below
    url: str,  # REST 接口地址 | REST endpoint URL
    symbol: str,  # 交易对 | Trading symbol
    interval: str,  # K 线周期 | K-line interval
    start_ms: int,  # 起始时间毫秒戳 | Start time in milliseconds
    end_ms: int,  # 结束时间毫秒戳 | End time in milliseconds
    limit: int,  # 期望每页条数 | Desired page size
    result: FetchResult,  # 累加结果容器 | Accumulating result container
) -> FetchResult:  # 返回填充后的结果容器 | Return the populated result container
    """REST 分页拉取 K 线 | Fetch candles via paginated REST calls.

    Purpose | 用途:
        通过游标分页把 [start_ms, end_ms] 内的 K 线全部拉回，兼容月线。

    游标使用 ``最后一根开盘时间 + 1ms``，对月线同样适用，不会因固定步长跳过整月。

    Parameters | 参数:
        client: 中文：复用的异步 HTTP 客户端；English: reusable async HTTP client.
        url: 中文：REST 接口地址；English: REST endpoint URL.
        symbol: 中文：交易对；English: trading symbol.
        interval: 中文：K 线周期；English: K-line interval.
        start_ms: 中文：起始毫秒戳；English: start time in milliseconds.
        end_ms: 中文：结束毫秒戳；English: end time in milliseconds.
        limit: 中文：期望每页条数；English: desired page size.
        result: 中文：累加结果容器；English: accumulating result container.

    Returns | 返回:
        FetchResult: 中文：填充了行与请求统计的结果容器；English: result container populated with rows and request stats.

    Flow | 流程:
        以游标为起点循环请求 → 解析 JSON → 累加行 → 用最后一根开盘时间推进游标 → 数据不足或无新数据时结束 | Loop from cursor, parse JSON, accumulate rows, advance cursor by last open time, stop when a short page or no new data.
    """
    cursor = start_ms  # 分页游标，初始为起始时间 | Pagination cursor, initially the start time
    while cursor <= end_ms:  # 游标未超过结束时间则继续翻页 | Keep paging while cursor is within the end time

        # 发起一次分页请求：参数含交易对、周期、游标时间窗与页大小 | Issue one paged request with symbol, interval, time window, and page size
        response = await _get(  # 调用公共 GET 封装 | Call the shared GET helper
            client,  # 复用客户端 | Reuse the client
            url,  # 请求地址 | Request URL
            params={  # 构造查询参数 | Build query parameters
                "symbol": symbol,  # 交易对 | Trading symbol
                "interval": interval,  # K 线周期 | K-line interval
                "startTime": cursor,  # 本页起始游标 | Page start cursor
                "endTime": end_ms,  # 固定结束时间 | Fixed end time
                "limit": min(limit, PAGE_LIMIT),  # 页大小不超过平台上限 | Page size capped by platform limit
            },  # 参数字典结束 | End params dict
        )  # 结束 _get 调用 | End _get call
        result.requests += 1  # 累加请求计数 | Increment request counter
        if response is None:  # 404 表示该区间无数据 | 404 means no data in this range
            break  # 结束翻页 | Stop paging
        try:  # 尝试解析响应 JSON | Try parsing the JSON response
            payload = response.json()  # 反序列化为 Python 对象 | Deserialize into a Python object
        except ValueError as exc:  # JSON 格式非法 | Invalid JSON format
            raise CustomException(msg=f"币安返回格式异常：{str(exc)[:200]}") from exc  # 抛出格式异常并保留原异常 | Raise format error chained from original
        if not isinstance(payload, list):  # 期望返回列表，否则格式异常 | Expect a list, otherwise malformed
            raise CustomException(msg=f"币安返回格式异常：{str(payload)[:200]}")  # 抛出格式异常 | Raise format error
        if not payload:  # 空列表说明没有更多数据 | Empty list means no more data
            break  # 结束翻页 | Stop paging
        result.rows.extend(row_from_raw(item) for item in payload)  # 把本页所有原始行转换后累加 | Convert and accumulate all raw rows in this page
        last_opened = normalize_timestamp(payload[-1][0])  # 取最后一根 K 线的开盘时间 | Take the last candle's open time
        cursor = last_opened + 1  # 游标前移 1ms，避免重复取最后一根 | Advance cursor by 1ms to avoid re-fetching the last candle
        if len(payload) < limit or last_opened >= end_ms:  # 页未满或已到结束时间，说明拉取完成 | Short page or reached end -> done
            break  # 结束翻页 | Stop paging
    return result  # 返回填充后的结果容器 | Return the populated result container


async def fetch_archive(  # 并发下载归档 ZIP 并解析 CSV | Concurrently download archive ZIPs and parse CSVs
    client: httpx.AsyncClient,  # 复用的异步 HTTP 客户端 | Reusable async HTTP client
    *,  # 后续参数强制关键字传入 | Force keyword-only arguments below
    symbol: str,  # 交易对 | Trading symbol
    interval: str,  # K 线周期 | K-line interval
    market: str,  # 市场类型 | Market type
    start_ms: int,  # 起始时间毫秒戳 | Start time in milliseconds
    end_ms: int,  # 结束时间毫秒戳 | End time in milliseconds
    result: FetchResult,  # 累加结果容器 | Accumulating result container
) -> FetchResult:  # 返回填充后的结果容器 | Return the populated result container
    """并发下载归档 ZIP 并解析 CSV | Concurrently download archive ZIPs and parse their CSVs.

    Purpose | 用途:
        在并发上限内下载所有归档片段，解析内部 CSV，并记录缺失与警告。

    Parameters | 参数:
        client: 中文：复用的异步 HTTP 客户端；English: reusable async HTTP client.
        symbol: 中文：交易对；English: trading symbol.
        interval: 中文：K 线周期；English: K-line interval.
        market: 中文：市场类型；English: market type.
        start_ms: 中文：起始毫秒戳；English: start time in milliseconds.
        end_ms: 中文：结束毫秒戳；English: end time in milliseconds.
        result: 中文：累加结果容器；English: accumulating result container.

    Returns | 返回:
        FetchResult: 中文：填充了行、缺失片段与警告的结果容器；English: result container populated with rows, missing periods, and warnings.

    Flow | 流程:
        生成归档列表 → 用信号量限并发 → 逐文件下载解析 → 汇总行、统计缺失并生成警告 | Build archive list, limit concurrency with a semaphore, download/parse each file, then aggregate rows and record warnings.
    """
    files = build_archive_urls(symbol, interval, market, start_ms, end_ms)  # 生成所有待下载的归档描述 | Build all archive descriptors to download
    if not files:  # 没有归档片段则直接返回 | Return early if there are no archives
        return result  # 返回未改动的结果容器 | Return the unchanged result container
    semaphore = asyncio.Semaphore(ARCHIVE_CONCURRENCY)  # 用信号量限制并发下载数 | Semaphore to cap concurrent downloads

    async def _one(archive_file: ArchiveFile) -> list[dict[str, Any]]:  # 下载并解析单个归档 | Download and parse one archive
        """下载并解析单个归档 ZIP | Download and parse a single archive ZIP.

        Purpose | 用途:
            在信号量约束下获取一个归档，解压其内所有 CSV 并解析为标准行。

        Parameters | 参数:
            archive_file: 中文：单个归档文件描述；English: a single archive file descriptor.

        Returns | 返回:
            list[dict[str, Any]]: 中文：该归档解析出的标准行列表；English: standard rows parsed from this archive.

        Flow | 流程:
            获取信号量 → GET 下载 → 404 记为缺失 → 解压并解析所有 CSV → 失败记警告 | Acquire semaphore, GET the file, record 404 as missing, unzip and parse all CSVs, record warnings on failure.
        """
        async with semaphore:  # 获取信号量许可，限制并发 | Acquire a semaphore permit to limit concurrency
            response = await _get(client, archive_file.url)  # 下载该归档 ZIP | Download the archive ZIP
            result.requests += 1  # 累加请求计数 | Increment request counter
            if response is None:  # 404 表示该片段无数据 | 404 means this period has no data
                result.missing_periods.append(archive_file.period)  # 记录缺失片段 | Record the missing period
                return []  # 返回空行列表 | Return an empty row list
            try:  # 尝试把响应内容当 ZIP 解压并解析 | Try unzipping the response content and parsing CSVs
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:  # 用内存字节流打开 ZIP | Open the ZIP from an in-memory byte buffer
                    collected: list[dict[str, Any]] = []  # 累积该归档解析出的行 | Accumulator for rows from this archive
                    for name in archive.namelist():  # 遍历归档内所有文件名 | Iterate over all file names in the archive
                        if name.lower().endswith(".csv"):  # 仅处理 CSV 文件 | Only process CSV files
                            collected.extend(parse_archive_csv(archive.read(name)))  # 解析该 CSV 并追加结果 | Parse the CSV and extend results
                    return collected  # 返回该归档的全部行 | Return all rows from this archive
            except (zipfile.BadZipFile, OSError, UnicodeError) as exc:  # 归档损坏或解码失败 | Catch corrupt archive or decode errors
                result.warnings.append(f"{archive_file.period}: 归档解析失败 {exc}")  # 记录解析失败警告 | Record a parse-failure warning
                return []  # 返回空行列表 | Return an empty row list

    batches = await asyncio.gather(*(_one(item) for item in files))  # 并发下载所有归档并汇总各批结果 | Concurrently download all archives and gather per-file rows
    for batch in batches:  # 遍历每批解析结果 | Iterate each batch of parsed rows
        result.rows.extend(batch)  # 把该批行累加到总结果 | Extend the overall result with this batch
    result.archive_files = len(files) - len(result.missing_periods)  # 统计成功解析的归档数量 | Count successfully parsed archives
    if result.missing_periods:  # 存在缺失片段时生成汇总警告 | Build a summary warning when periods are missing
        preview = ", ".join(result.missing_periods[:5])  # 取前五个缺失片段做预览 | Preview the first five missing periods
        result.warnings.append(f"归档缺失片段 {len(result.missing_periods)} 个：{preview}")  # 记录缺失汇总警告 | Record the missing-period summary warning
    return result  # 返回填充后的结果容器 | Return the populated result container


async def fetch_rows(  # 按来源拉取 K 线并归一化 | Fetch and normalize candles by source
    *,  # 所有参数强制关键字传入 | Force all arguments to be keyword-only
    source: str,  # 下载来源：api/mirror/archive | Data source: api/mirror/archive
    market: str,  # 市场类型：spot/futures | Market type: spot/futures
    symbol: str,  # 交易对 | Trading symbol
    interval: str,  # K 线周期 | K-line interval
    start_ms: int,  # 起始时间毫秒戳 | Start time in milliseconds
    end_ms: int,  # 结束时间毫秒戳 | End time in milliseconds
    limit: int = PAGE_LIMIT,  # 每页条数，默认平台上限 | Page size, defaulting to platform limit
    fill_recent: bool = True,  # 是否用镜像补齐归档延迟窗口 | Whether to fill the archive delay window via mirror
) -> FetchResult:  # 返回统一的归一化结果 | Return the unified normalized result
    """按来源拉取 K 线并归一化 | Fetch and normalize candles by source.

    Purpose | 用途:
        作为总入口按来源分派下载策略，校验参数，处理归档延迟补齐，最后裁剪去重。

    - ``api``：币安官方 REST（现货或 U 本位合约）；
    - ``mirror``：公开 REST 镜像，仅现货；
    - ``archive``：币安历史归档（月/日 ZIP），默认自动用公开镜像补齐归档延迟窗口。

    Parameters | 参数:
        source: 中文：下载来源 api/mirror/archive；English: data source api/mirror/archive.
        market: 中文：市场类型 spot/futures；English: market type spot/futures.
        symbol: 中文：交易对；English: trading symbol.
        interval: 中文：K 线周期；English: K-line interval.
        start_ms: 中文：起始毫秒戳；English: start time in milliseconds.
        end_ms: 中文：结束毫秒戳；English: end time in milliseconds.
        limit: 中文：每页条数；English: page size.
        fill_recent: 中文：是否补齐归档延迟窗口；English: whether to fill the archive delay window.

    Returns | 返回:
        FetchResult: 中文：裁剪去重后的归一化结果；English: the clipped and deduplicated normalized result.

    Raises | 异常:
        CustomException: 中文：参数非法、归档延迟冲突或无数据时抛出；English: raised on invalid params, archive delay conflicts, or empty results.

    Flow | 流程:
        校验来源/市场 → 按来源走归档或 REST 分支 → 可选补齐近端 → 裁剪去重并确保非空 | Validate source/market, branch to archive or REST, optionally fill recent data, then clip/dedupe and ensure non-empty.
    """
    if source not in {"api", "mirror", "archive"}:  # 校验下载来源 | Validate the download source
        raise CustomException(msg=f"不支持的下载方式：{source}，可选：api / mirror / archive")  # 抛出不支持来源的异常 | Raise error for unsupported source
    if market not in {"spot", "futures"}:  # 校验市场类型 | Validate the market type
        raise CustomException(msg=f"不支持的市场：{market}，可选：spot / futures")  # 抛出不支持市场的异常 | Raise error for unsupported market
    if source == "mirror" and market != "spot":  # 镜像仅支持现货 | Mirror only supports spot markets
        raise CustomException(msg="公开 REST 镜像仅支持现货（spot），合约请选择 api 或 archive")  # 抛镜像市场限制异常 | Raise mirror market restriction error

    result = FetchResult()  # 创建累加结果容器 | Create the accumulating result container
    if source == "archive":  # 归档分支：历史数据 + 可选近端补齐 | Archive branch: historical data plus optional recent fill

        archive_end_ms = to_ms(datetime.now(UTC) - timedelta(days=ARCHIVE_DELAY_DAYS))  # 归档可用数据的截止时间 | Cutoff time for available archive data
        if start_ms >= archive_end_ms:  # 起始时间晚于归档截止，归档无法覆盖 | Start time is past the archive cutoff
            raise CustomException(  # 抛出归档延迟冲突异常 | Raise archive-delay conflict error
                msg=f"归档数据有 {ARCHIVE_DELAY_DAYS} 天延迟，请选择更早的开始时间，或改用 api / mirror"  # 延迟提示信息 | Delay hint message
            )  # 结束异常抛出 | End error raise
        effective_end = min(end_ms, archive_end_ms)  # 归档实际拉取结束时间取较早者 | Effective archive end is the earlier of the two
        async with _client() as client:  # 创建并管理 HTTP 客户端生命周期 | Create and manage the HTTP client lifecycle
            await fetch_archive(  # 下载并解析归档历史数据 | Download and parse archive history
                client,  # 复用客户端 | Reuse the client
                symbol=symbol,  # 交易对 | Trading symbol
                interval=interval,  # K 线周期 | K-line interval
                market=market,  # 市场类型 | Market type
                start_ms=start_ms,  # 起始时间 | Start time
                end_ms=effective_end,  # 归档有效结束时间 | Effective archive end time
                result=result,  # 累加结果容器 | Accumulating result container
            )  # 结束 fetch_archive 调用 | End fetch_archive call
        if fill_recent and end_ms > archive_end_ms:  # 需要补齐归档截止之后的近端数据 | Need to fill recent data past the archive cutoff
            recent = FetchResult()  # 独立的近端结果容器 | Separate result container for recent data
            try:  # 尝试用 REST 拉取近端数据 | Try fetching recent data via REST
                async with _client() as client:  # 创建并管理 HTTP 客户端 | Create and manage the HTTP client
                    await fetch_rest(  # 用镜像或合约 REST 拉取近端 | Fetch recent data via mirror or futures REST
                        client,  # 复用客户端 | Reuse the client
                        url=MIRROR_KLINES_URL if market == "spot" else BINANCE_FAPI_KLINES_URL,  # 现货走镜像、合约走官方 FAPI | Mirror for spot, official FAPI for futures
                        symbol=symbol,  # 交易对 | Trading symbol
                        interval=interval,  # K 线周期 | K-line interval
                        start_ms=archive_end_ms + 1,  # 从归档截止后 1ms 开始 | Start 1ms after the archive cutoff
                        end_ms=end_ms,  # 到用户请求的结束时间 | End at the requested end time
                        limit=limit,  # 每页条数 | Page size
                        result=recent,  # 累加到近端结果容器 | Accumulate into the recent result container
                    )  # 结束 fetch_rest 调用 | End fetch_rest call
                result.rows.extend(recent.rows)  # 合并近端行到总结果 | Merge recent rows into overall result
                result.requests += recent.requests  # 累加近端请求数 | Accumulate recent request count
            except CustomException as exc:  # 近端补数失败不中断主流程 | Recent fill failure must not abort the main flow
                result.warnings.append(f"近端补数失败（可稍后用 api 重新下载）：{exc.msg}")  # 记录补数失败警告 | Record recent-fill warning
    else:  # 非归档分支：直接走 REST | Non-archive branch: go straight to REST

        # 根据来源与市场选择 REST 地址：镜像 / 合约官方 / 现货官方 | Pick REST URL by source and market: mirror / futures official / spot official
        url = (  # 选中 URL 赋给变量 | Assign the chosen URL
            MIRROR_KLINES_URL  # mirror 来源用公开镜像 | Mirror source uses public mirror
            if source == "mirror"  # 条件：来源为 mirror | Condition: source is mirror
            else BINANCE_FAPI_KLINES_URL if market == "futures" else BINANCE_API_KLINES_URL  # 否则合约用 FAPI、现货用官方 | Otherwise FAPI for futures, official for spot
        )  # 结束 URL 表达式 | End URL expression
        try:  # 尝试用 REST 拉取 K 线 | Try fetching candles via REST
            async with _client() as client:  # 创建并管理 HTTP 客户端 | Create and manage the HTTP client
                await fetch_rest(  # REST 分页拉取 | Paginated REST fetch
                    client,  # 复用客户端 | Reuse the client
                    url=url,  # 选中的 REST 地址 | Chosen REST URL
                    symbol=symbol,  # 交易对 | Trading symbol
                    interval=interval,  # K 线周期 | K-line interval
                    start_ms=start_ms,  # 起始时间 | Start time
                    end_ms=end_ms,  # 结束时间 | End time
                    limit=limit,  # 每页条数 | Page size
                    result=result,  # 累加结果容器 | Accumulating result container
                )  # 结束 fetch_rest 调用 | End fetch_rest call
        except CustomException as exc:  # 捕获 REST 拉取异常 | Catch REST fetch errors
            if source == "api":  # api 来源失败时给出可操作的替代建议 | For api source, suggest actionable alternatives
                raise CustomException(  # 抛出带建议的异常 | Raise error with suggestions
                    msg=f"{exc.msg}；当前网络可能无法访问币安官方接口，可改用 --source mirror 或 --source archive"  # 替代方案提示 | Alternative-source hint
                ) from exc  # 保留原异常链 | Chain from the original exception
            raise  # 其他来源直接向上抛出 | Re-raise for other sources

    result.rows = merge_rows(clip_rows(result.rows, start_ms, end_ms))  # 先裁剪时间窗再去重合并 | Clip to the time window then deduplicate and merge
    if not result.rows:  # 没有任何数据则返回空结果错误 | Error out when no rows were retrieved
        raise CustomException(msg="币安未返回数据，请检查交易对、周期、市场类型与时间范围")  # 抛出无数据异常 | Raise no-data error
    return result  # 返回最终归一化结果 | Return the final normalized result
