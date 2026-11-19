"""量化数据服务：币安 K 线下载、本地 CSV 导入与元数据管理 | Quant data service: Binance candle download, local CSV import and metadata management."""

import csv  # 导入 CSV 读写模块 | Import the CSV module
import re  # 导入正则模块，用于匹配日期格式 | Import regex for matching date formats
from datetime import UTC, datetime, timedelta  # 导入 UTC 时区、datetime 与时间增量 | Import UTC, datetime and timedelta
from pathlib import Path  # 导入 Path 用于路径操作 | Import Path for path operations
from typing import Any  # 导入 Any 用于宽松类型标注 | Import Any for loose typing

from sqlalchemy import select  # 导入 select 构造查询 | Import select to build queries
from sqlalchemy.ext.asyncio import AsyncSession  # 导入异步会话类型 | Import the async session type

from app.config.path_conf import (  # 导入项目路径配置 | Import project path configuration
    BASE_DIR,  # 后端基础目录 | Backend base directory
    PROJECT_ROOT,  # 项目根目录 | Project root directory
    QUANT_BINANCE_DIR,  # 币安数据存放目录 | Binance data directory
    QUANT_DATA_DIR,  # 量化数据存放目录 | Quant data directory
)  # 路径配置导入结束 | End of path config import
from app.core.base_model import MappedBase  # 导入 ORM 声明基类 | Import the ORM declarative base
from app.core.base_schema import AuthSchema  # 导入认证信息模型 | Import the auth schema
from app.core.exceptions import CustomException  # 导入业务异常类 | Import the business exception class
from app.utils.import_util import ImportUtil  # 导入模型自动发现工具 | Import the model auto-discovery utility

# 注册全部 ORM 模型，确保 UserMixin.creator 等跨模块 relationship 可解析
# （CLI 单独运行时不会经过应用工厂的自动发现，需在此显式触发）
# Register all ORM models so cross-module relationships like UserMixin.creator resolve;
# (CLI runs standalone without the app factory's auto-discovery, so trigger it explicitly here)
ImportUtil.find_models(MappedBase)  # 扫描并注册所有 ORM 模型 | Scan and register all ORM models

from ..model import QuantDatasetModel  # 导入数据集 ORM 模型（放在模型注册之后）| Import the dataset ORM model (after model registration)
from ..schema import QuantDataDownloadSchema, QuantDataImportSchema  # 导入下载与导入参数模型 | Import download and import schemas
from .binance_download import fetch_rows, merge_rows, ms_to_datetime, now_ms  # 导入下载底层函数 | Import low-level download helpers

# CSV 标准列：与 freqtrade OHLCV 约定一致 | Standard CSV columns, consistent with freqtrade's OHLCV convention
CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]  # 六列标准表头 | Six standard column headers

# 允许 API 导入的目录：防止通过 ../ 读取任意本地文件 | Directories allowed for API import, preventing arbitrary file reads via ../
ALLOWED_IMPORT_DIRS = [QUANT_DATA_DIR, BASE_DIR / "data" / "quant"]  # 白名单目录列表 | Allow-list of directories


def _naive_utc(value: datetime) -> datetime:
    """把带时区的 UTC 时间转成 naive UTC，适配 TIMESTAMP WITHOUT TIME ZONE 列 | Convert aware UTC to naive UTC for TIMESTAMP WITHOUT TIME ZONE columns.

    参数 | Args:
        value (datetime): 带时区的 datetime | A timezone-aware datetime.

    返回 | Returns:
        datetime: 去掉时区信息的 UTC 时间 | UTC datetime without timezone info.
    """
    return value.astimezone(UTC).replace(tzinfo=None)  # 先转 UTC 再去 tzinfo | Convert to UTC then strip tzinfo


def _relative_path(path: Path) -> str:
    """把绝对路径转成相对项目根目录的路径，便于跨机器迁移 | Convert an absolute path to a project-root-relative path for portability.

    参数 | Args:
        path (Path): 绝对路径或相对路径 | Absolute or relative path.

    返回 | Returns:
        str: 相对项目根目录的路径；无法转换时返回绝对路径 | Path relative to project root; absolute path if conversion fails.
    """
    try:  # 尝试计算相对路径 | Try to compute a relative path
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))  # 解析为绝对路径后取相对项目根的部分 | Resolve then express relative to the project root
    except ValueError:  # 路径不在项目根目录下 | Path is not under the project root
        return str(path.resolve())  # 兜底返回绝对路径 | Fall back to the absolute path


def _parse_time_to_ms(value: str | int | None, *, end_of_day: bool = False) -> int | None:
    """把 YYYY-MM-DD / ISO8601 / 毫秒时间戳统一转成毫秒时间戳 | Normalize YYYY-MM-DD / ISO8601 / millisecond timestamp into a millisecond timestamp.

    参数 | Args:
        value (str | int | None): 时间输入 | Time input.
        end_of_day (bool): True 时纯日期按当天 23:59:59.999 解析，便于包含结束日全天数据 | When True, a bare date resolves to end-of-day to include the whole day.

    返回 | Returns:
        int | None: 毫秒时间戳；空值返回 None | Millisecond timestamp; None for empty input.
    """
    if value is None:  # 空值直接返回 None | None input returns None
        return None  # 无时间 | No time
    if isinstance(value, int):  # 已是整数（毫秒时间戳）| Already an integer (millisecond timestamp)
        return value  # 原样返回 | Return as-is
    text = str(value).strip()  # 转字符串并去首尾空白 | Convert to string and strip whitespace
    if not text:  # 空字符串 | Empty string
        return None  # 视为无时间 | Treat as no time
    if text.isdigit():  # 纯数字字符串，视为毫秒时间戳 | All digits, treat as a millisecond timestamp
        return int(text)  # 转整数返回 | Convert to int and return
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):  # 纯日期格式 YYYY-MM-DD | Bare date format
        parsed = datetime.fromisoformat(text).replace(tzinfo=UTC)  # 解析日期并设为 UTC 零点 | Parse date and set to UTC midnight
        if end_of_day:  # 需要包含整天 | Need to include the whole day
            return int((parsed + timedelta(days=1)).timestamp() * 1000) - 1  # 次日零点减 1 毫秒 = 当天 23:59:59.999 | Next midnight minus 1ms = 23:59:59.999
        return int(parsed.timestamp() * 1000)  # 返回当天零点的毫秒时间戳 | Return midnight millisecond timestamp
    if text.endswith("Z"):  # ISO8601 的 Z 后缀（UTC）| ISO8601 Z suffix (UTC)
        text = f"{text[:-1]}+00:00"  # 替换为 +00:00 以便 fromisoformat 解析 | Replace with +00:00 for fromisoformat compatibility
    parsed = datetime.fromisoformat(text)  # 解析 ISO8601 字符串 | Parse the ISO8601 string
    if parsed.tzinfo is None:  # 没有时区信息 | No timezone info
        parsed = parsed.replace(tzinfo=UTC)  # 默认按 UTC 处理 | Assume UTC
    return int(parsed.timestamp() * 1000)  # 转成毫秒时间戳 | Convert to millisecond timestamp


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    """读取并校验 CSV；表头缺列或数值非法时直接抛业务异常 | Read and validate a CSV; raise business errors on missing columns or invalid values.

    参数 | Args:
        path (Path): CSV 文件路径 | CSV file path.

    返回 | Returns:
        list[dict[str, Any]]: OHLCV 行字典列表；文件不存在时返回空列表 | List of OHLCV row dicts; empty list if the file doesn't exist.

    异常 | Raises:
        CustomException: 缺列或数值非法时抛出 | Raised on missing columns or invalid numbers.
    """
    if not path.exists():  # 文件不存在 | File does not exist
        return []  # 返回空列表（新建场景）| Return an empty list (new-file scenario)
    rows: list[dict[str, Any]] = []  # 存放解析后的行 | Accumulator for parsed rows
    with path.open("r", encoding="utf-8-sig", newline="") as file:  # 以 utf-8-sig 读取兼容 BOM | Read with utf-8-sig to tolerate BOM
        reader = csv.DictReader(file)  # 按表头映射每行 | Map each row by header
        headers = set(reader.fieldnames or [])  # 表头集合，便于成员判断 | Set of headers for membership checks
        missing = [column for column in CSV_COLUMNS if column not in headers]  # 找出缺失的必需列 | Find missing required columns
        if missing:  # 有缺列 | Some columns are missing
            raise CustomException(msg=f"CSV 缺少必需列：{', '.join(missing)}")  # 报错列出缺列 | Raise listing the missing columns
        for line_no, raw in enumerate(reader, start=2):  # 逐行解析，行号从 2 开始 | Parse rows; line numbers start at 2
            try:  # 捕获单行转换错误 | Catch per-row conversion errors
                rows.append(  # 构造标准化行字典并追加 | Build a normalized row dict and append
                    {  # 行字典 | Row dict
                        "date": str(raw["date"]).strip(),  # 日期字符串去空白 | Date string stripped
                        "open": float(raw["open"]),  # 开盘价转浮点 | Open as float
                        "high": float(raw["high"]),  # 最高价转浮点 | High as float
                        "low": float(raw["low"]),  # 最低价转浮点 | Low as float
                        "close": float(raw["close"]),  # 收盘价转浮点 | Close as float
                        "volume": float(raw["volume"]),  # 成交量转浮点 | Volume as float
                    }  # 行字典结束 | End of row dict
                )  # append 结束 | End of append
            except (TypeError, ValueError) as exc:  # 数值格式非法 | Invalid numeric format
                raise CustomException(msg=f"CSV 第 {line_no} 行数值格式无效：{exc}") from exc  # 带行号抛异常 | Raise with the line number
    return rows  # 返回所有行 | Return all rows


def _write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """写入标准 OHLCV CSV | Write a standard OHLCV CSV.

    参数 | Args:
        path (Path): 目标文件路径 | Target file path.
        rows (list[dict[str, Any]]): 要写入的行字典列表 | Row dicts to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)  # 递归创建父目录（已存在则不报错）| Create parent directories recursively (no error if existing)
    with path.open("w", encoding="utf-8", newline="") as file:  # 以写模式打开 | Open in write mode
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)  # 按标准列顺序写 | Write using the standard column order
        writer.writeheader()  # 写表头 | Write the header row
        writer.writerows(rows)  # 写所有数据行 | Write all data rows


def _dataset_name(source: str, symbol: str, interval: str, market: str = "spot") -> str:
    """生成稳定的数据集名称 | Generate a stable dataset name.

    现货名称保持不变（兼容历史数据）；合约在来源未带 futures 标识时追加后缀，
    避免同一 source 下的现货/合约同名冲突。
    Spot names stay unchanged (for legacy compatibility); futures append a suffix when the source
    lacks a "futures" marker, avoiding same-name conflicts between spot and futures.

    参数 | Args:
        source (str): 数据来源 | Data source.
        symbol (str): 交易对 | Trading pair.
        interval (str): K 线周期 | Candle interval.
        market (str): 市场类型，默认 spot | Market type, defaults to spot.

    返回 | Returns:
        str: 规范化的数据集名称 | Normalized dataset name.
    """
    source_part = source if market != "futures" or "futures" in source else f"{source}_futures"  # 合约且来源未标 futures 时追加后缀 | Append suffix for futures if source lacks the marker
    return f"{source_part}_{symbol}_{interval}".lower().replace("/", "")  # 拼接、转小写、去掉 / | Join, lowercase and remove /


def _market_from_path(path: Path) -> str:
    """按路径中的 futures 目录推断市场（兼容 Windows 反斜杠）| Infer market from a "futures" path segment (Windows backslash compatible).

    参数 | Args:
        path (Path): 文件路径 | File path.

    返回 | Returns:
        str: "futures" 或 "spot" | Either "futures" or "spot".
    """
    return "futures" if "futures" in {part.lower() for part in path.parts} else "spot"  # 任一路径段为 futures 则判为合约 | If any path segment is "futures", it's futures


async def upsert_dataset(  # 新增或更新数据集元数据 | Create or update dataset metadata
    db: AsyncSession,  # 异步数据库会话 | Async database session
    *,  # 之后的参数必须关键字传参 | Following arguments are keyword-only
    source: str,  # 数据来源 | Data source
    symbol: str,  # 交易对 | Trading pair
    interval: str,  # K 线周期 | Candle interval
    file_path: Path,  # CSV 文件路径 | CSV file path
    rows: list[dict[str, Any]],  # K 线行列表（用于统计起止时间与数量）| Candle rows (for start/end time and count)
    market: str = "spot",  # 市场类型，默认现货 | Market type, defaults to spot
    description: str | None = None,  # 数据集备注 | Dataset description
    auth: AuthSchema | None = None,  # 当前用户，用于审计字段 | Current user for audit fields
) -> QuantDatasetModel:  # 返回数据集 ORM 对象 | Returns the dataset ORM object
    """新增或更新数据集元数据 | Create or update dataset metadata.

    参数 | Args:
        db (AsyncSession): 异步数据库会话 | Async database session.
        source (str): 数据来源 | Data source.
        symbol (str): 交易对 | Trading pair.
        interval (str): K 线周期 | Candle interval.
        file_path (Path): CSV 文件路径 | CSV file path.
        rows (list[dict]): K 线行列表 | Candle rows.
        market (str): 市场类型 | Market type.
        description (str | None): 备注 | Description.
        auth (AuthSchema | None): 当前用户 | Current user.

    返回 | Returns:
        QuantDatasetModel: 数据集对象 | The dataset object.
    """
    name = _dataset_name(source, symbol, interval, market)  # 生成稳定的数据集名称 | Generate a stable dataset name
    result = await db.execute(select(QuantDatasetModel).where(QuantDatasetModel.name == name))  # 按名称查询是否已存在 | Query by name to check existence
    obj = result.scalar_one_or_none()  # 取唯一结果或 None | Get the single result or None
    user_id = getattr(getattr(auth, "user", None), "id", None)  # 安全提取用户 ID | Safely extract the user ID

    values = {  # 要写入/更新的字段字典 | Dict of fields to write/update
        "symbol": symbol,  # 交易对 | Trading pair
        "interval": interval,  # K 线周期 | Candle interval
        "source": source,  # 数据来源 | Data source
        "market": market,  # 市场类型 | Market type
        "file_path": _relative_path(file_path),  # CSV 相对路径 | CSV relative path
        "row_count": len(rows),  # K 线数量 | Candle count
        "start_time": _naive_utc(ms_to_datetime(int(datetime.fromisoformat(rows[0]["date"]).timestamp() * 1000))) if rows else None,  # 首行日期转 naive UTC；无行则 None | First row date to naive UTC; None if no rows
        "end_time": _naive_utc(ms_to_datetime(int(datetime.fromisoformat(rows[-1]["date"]).timestamp() * 1000))) if rows else None,  # 末行日期转 naive UTC；无行则 None | Last row date to naive UTC; None if no rows
        "description": description,  # 备注 | Description
    }  # 字段字典结束 | End of values dict
    if obj is None:  # 数据集不存在，新建 | Dataset doesn't exist, create it
        obj = QuantDatasetModel(name=name, status=1, created_id=user_id, **values)  # 构造新对象，状态=启用 | Build a new object, status=enabled
        db.add(obj)  # 加入会话 | Add to session
    else:  # 已存在，更新字段 | Exists, update fields
        for key, value in values.items():  # 遍历字段字典 | Iterate over the values dict
            setattr(obj, key, value)  # 逐个赋值 | Set each attribute
        if user_id is not None:  # 有用户信息时记录更新人 | Record updater when user info is available
            obj.updated_id = user_id  # 设置更新人 ID | Set the updater ID

    await db.commit()  # 提交事务 | Commit the transaction
    await db.refresh(obj)  # 刷新对象 | Refresh the object
    return obj  # 返回数据集对象 | Return the dataset object


async def download_binance_data(  # 从币安下载 K 线并落库 | Download candles from Binance and persist
    params: QuantDataDownloadSchema,  # 下载参数 | Download parameters
    *,  # 之后的参数必须关键字传参 | Following arguments are keyword-only
    db: AsyncSession | None = None,  # 数据库会话，CLI 纯下载可为 None | DB session; may be None for CLI-only download
    auth: AuthSchema | None = None,  # 当前用户，用于审计 | Current user for audit
) -> dict[str, Any]:  # 返回下载摘要 | Returns a download summary
    """从币安下载 K 线并合并写入本地 CSV，同时登记数据集 | Download candles from Binance, merge into a local CSV and register the dataset.

    参数 | Args:
        params (QuantDataDownloadSchema): 下载参数（交易对、周期、时间范围等）| Download parameters.
        db (AsyncSession | None): 数据库会话；CLI 可传入，纯文件下载场景可为 None | DB session; optional for pure file download.
        auth (AuthSchema | None): 当前用户，仅用于审计字段 | Current user, only for audit fields.

    返回 | Returns:
        dict[str, Any]: 数据集摘要与写入文件路径 | Dataset summary and written file path.
    """
    symbol = params.symbol  # 交易对 | Trading pair
    interval = params.interval  # K 线周期 | Candle interval
    current_ms = now_ms()  # 当前毫秒时间戳 | Current millisecond timestamp
    if params.days is not None:  # 指定了最近 N 天 | "Last N days" was specified
        # --days 优先：从当前时间往前推 N 天 | --days takes priority: go back N days from now
        end_ms = current_ms  # 结束时间 = 当前 | End time = now
        start_ms = end_ms - params.days * 24 * 60 * 60 * 1000  # 起始时间 = 结束 - N 天（毫秒）| Start = end - N days in ms
    else:  # 使用显式的起止时间 | Use explicit start/end times
        end_ms = _parse_time_to_ms(params.end_time, end_of_day=True)  # 解析结束时间（含当天）| Parse end time (include the whole day)
        start_ms = _parse_time_to_ms(params.start_time)  # 解析起始时间 | Parse start time
        if end_ms is None:  # 未给结束时间 | No end time given
            end_ms = current_ms  # 默认当前时间 | Default to now
        if start_ms is None:  # 未给起始时间 | No start time given
            # 只给结束时间时，从结束时间往前推 30 天，避免旧时间点与默认值冲突 | With only an end time, go back 30 days to avoid conflicts with defaults
            start_ms = end_ms - 30 * 24 * 60 * 60 * 1000  # 往前推 30 天 | Go back 30 days
    if start_ms >= end_ms:  # 起始不早于结束，范围非法 | Start not before end, invalid range
        raise CustomException(msg="起始时间必须早于结束时间")  # 抛业务异常 | Raise a business exception

    fetched = await fetch_rows(  # 调用底层下载函数拉取数据 | Call the low-level downloader to fetch rows
        source=params.source,  # 下载源 | Download source
        market=params.market,  # 市场类型 | Market type
        symbol=symbol,  # 交易对 | Trading pair
        interval=interval,  # K 线周期 | Candle interval
        start_ms=start_ms,  # 起始毫秒时间戳 | Start timestamp in ms
        end_ms=end_ms,  # 结束毫秒时间戳 | End timestamp in ms
        limit=params.limit,  # 单次请求条数 | Rows per request
        fill_recent=params.fill_recent,  # 是否补齐最近数据 | Whether to fill recent data
    )  # 下载结束 | End of fetch

    file_path = _download_target_path(symbol, interval, params.market)  # 计算目标 CSV 路径 | Compute the target CSV path
    existing = [] if params.erase else _read_csv_rows(file_path)  # 按 erase 决定清空还是读取已有数据 | Clear or read existing rows based on erase
    merged = merge_rows(existing, fetched.rows)  # 合并新旧数据（去重排序）| Merge old and new rows (dedupe and sort)
    _write_csv_rows(file_path, merged)  # 写回 CSV | Write back to CSV

    dataset_name_source = "binance_futures" if params.market == "futures" else "binance"  # 合约用带 futures 的来源名 | Use a futures-tagged source name for futures
    dataset = None  # 数据集对象，CLI 场景保持 None | Dataset object; stays None in CLI-only scenario
    if db is not None:  # 有数据库会话才登记数据集 | Only register the dataset when a DB session is present
        dataset = await upsert_dataset(  # 新增或更新数据集元数据 | Create or update dataset metadata
            db,  # 数据库会话 | DB session
            source=dataset_name_source,  # 数据来源标识 | Source tag
            symbol=symbol,  # 交易对 | Trading pair
            interval=interval,  # K 线周期 | Candle interval
            market=params.market,  # 市场类型 | Market type
            file_path=file_path,  # CSV 路径 | CSV path
            rows=merged,  # 合并后的行 | Merged rows
            description=params.description,  # 备注 | Description
            auth=auth,  # 当前用户 | Current user
        )  # upsert 结束 | End of upsert

    return {  # 组装下载结果摘要 | Assemble the download summary
        "name": _dataset_name(dataset_name_source, symbol, interval, params.market),  # 数据集名称 | Dataset name
        "symbol": symbol,  # 交易对 | Trading pair
        "interval": interval,  # K 线周期 | Candle interval
        "source": params.source,  # 下载方式 | Download source
        "market": params.market,  # 市场类型 | Market type
        "file_path": _relative_path(file_path),  # CSV 相对路径 | CSV relative path
        "downloaded": len(fetched.rows),  # 本次下载条数 | Number of rows downloaded
        "row_count": len(merged),  # 合并后总条数 | Total rows after merge
        "dataset_id": dataset.id if dataset else None,  # 数据集 ID（无 DB 时为 None）| Dataset ID (None when no DB)
        "requests": fetched.requests,  # 请求次数统计 | Request count stats
        "archive_files": fetched.archive_files,  # 归档文件列表 | Archive file list
        "missing_periods": fetched.missing_periods,  # 缺失时间段 | Missing periods
        "warnings": fetched.warnings,  # 警告信息 | Warnings
    }  # 结果摘要结束 | End of summary


def _download_target_path(symbol: str, interval: str, market: str) -> Path:
    """现货写入 binance/，合约写入 binance/futures/，避免不同市场数据互相覆盖 | Write spot data to binance/ and futures to binance/futures/ to avoid overwrites.

    参数 | Args:
        symbol (str): 交易对 | Trading pair.
        interval (str): K 线周期 | Candle interval.
        market (str): 市场类型 | Market type.

    返回 | Returns:
        Path: 目标 CSV 路径 | Target CSV path.
    """
    base = QUANT_BINANCE_DIR if market == "spot" else QUANT_BINANCE_DIR / "futures"  # 按市场选择基础目录 | Choose base directory by market
    return base / f"{symbol}_{interval}.csv"  # 拼接文件名：交易对_周期.csv | Build filename: pair_interval.csv


def _resolve_import_path(file_path: str) -> Path:
    """解析并校验待导入 CSV 路径，必须位于允许目录内 | Resolve and validate the import CSV path; it must be within allowed directories.

    参数 | Args:
        file_path (str): 用户提供的文件路径 | User-supplied file path.

    返回 | Returns:
        Path: 校验通过的绝对路径 | The validated absolute path.

    异常 | Raises:
        CustomException: 路径不在允许目录内时抛出 | Raised when the path is outside allowed directories.
    """
    raw = Path(file_path).expanduser()  # 展开 ~ 为用户目录 | Expand ~ to the user's home directory
    candidates = [raw] if raw.is_absolute() else [PROJECT_ROOT / raw, QUANT_DATA_DIR / raw]  # 绝对路径直接用；相对路径尝试两个基准 | Use absolute directly; for relative, try two bases
    for candidate in candidates:  # 逐个候选路径尝试 | Try each candidate path
        resolved = candidate.resolve()  # 解析为绝对路径 | Resolve to an absolute path
        for allowed in ALLOWED_IMPORT_DIRS:  # 逐个白名单目录检查 | Check against each allowed directory
            try:  # 尝试计算相对路径，成功说明在白名单内 | Try computing a relative path; success means it's inside the allow-list
                resolved.relative_to(allowed.resolve())  # 若在 allowed 内则不抛异常 | No exception means it's within allowed
                return resolved  # 返回该合法路径 | Return the valid path
            except ValueError:  # 不在该白名单目录内 | Not within this allowed directory
                continue  # 检查下一个 | Check the next one
    allowed_text = ", ".join(str(path) for path in ALLOWED_IMPORT_DIRS)  # 拼接白名单文本用于报错 | Join allowed dirs for the error message
    raise CustomException(msg=f"文件必须位于允许目录内：{allowed_text}")  # 所有候选都不合法，抛异常 | No candidate is valid, raise an exception


async def import_local_csv(  # 导入本地 CSV 并登记数据集 | Import a local CSV and register the dataset
    params: QuantDataImportSchema,  # 导入参数 | Import parameters
    *,  # 之后的参数必须关键字传参 | Following arguments are keyword-only
    db: AsyncSession | None = None,  # 数据库会话，CLI 可为 None | DB session; may be None for CLI
    auth: AuthSchema | None = None,  # 当前用户，用于审计 | Current user for audit
) -> dict[str, Any]:  # 返回导入摘要 | Returns an import summary
    """导入本地 CSV，归一化后写入 user_data/data 并登记数据集 | Import a local CSV, normalize it into user_data/data and register the dataset.

    参数 | Args:
        params (QuantDataImportSchema): 导入参数 | Import parameters.
        db (AsyncSession | None): 数据库会话 | DB session.
        auth (AuthSchema | None): 当前用户 | Current user.

    返回 | Returns:
        dict[str, Any]: 导入摘要 | Import summary.
    """
    source_path = _resolve_import_path(params.file_path)  # 解析并校验源路径 | Resolve and validate the source path
    rows = _read_csv_rows(source_path)  # 读取并校验 CSV 行 | Read and validate CSV rows
    if not rows:  # CSV 无有效数据 | No valid data in CSV
        raise CustomException(msg="CSV 文件为空")  # 抛异常 | Raise an exception

    target_path = QUANT_DATA_DIR / f"{params.source}_{params.symbol}_{params.interval}.csv"  # 计算目标路径 | Compute the target path
    merged = merge_rows(_read_csv_rows(target_path), rows)  # 与目标已有数据合并去重 | Merge with existing target data and dedupe
    _write_csv_rows(target_path, merged)  # 写回目标 CSV | Write back to the target CSV

    dataset = None  # 数据集对象，CLI 场景保持 None | Dataset object; stays None for CLI
    if db is not None:  # 有数据库会话才登记 | Only register when a DB session is present
        dataset = await upsert_dataset(  # 新增或更新数据集元数据 | Create or update dataset metadata
            db,  # 数据库会话 | DB session
            source=params.source,  # 数据来源 | Data source
            symbol=params.symbol,  # 交易对 | Trading pair
            interval=params.interval,  # K 线周期 | Candle interval
            market=params.market,  # 市场类型 | Market type
            file_path=target_path,  # CSV 路径 | CSV path
            rows=merged,  # 合并后的行 | Merged rows
            description=params.description,  # 备注 | Description
            auth=auth,  # 当前用户 | Current user
        )  # upsert 结束 | End of upsert

    return {  # 组装导入摘要 | Assemble the import summary
        "name": _dataset_name(params.source, params.symbol, params.interval, params.market),  # 数据集名称 | Dataset name
        "symbol": params.symbol,  # 交易对 | Trading pair
        "interval": params.interval,  # K 线周期 | Candle interval
        "market": params.market,  # 市场类型 | Market type
        "file_path": _relative_path(target_path),  # CSV 相对路径 | CSV relative path
        "imported": len(rows),  # 本次导入条数 | Number of rows imported
        "row_count": len(merged),  # 合并后总条数 | Total rows after merge
        "dataset_id": dataset.id if dataset else None,  # 数据集 ID（无 DB 时为 None）| Dataset ID (None when no DB)
    }  # 结果摘要结束 | End of summary


async def list_datasets(  # 查询数据集列表 | List datasets
    db: AsyncSession,  # 异步数据库会话 | Async database session
    *,  # 之后的参数必须关键字传参 | Following arguments are keyword-only
    symbol: str | None = None,  # 按交易对筛选 | Filter by trading pair
    interval: str | None = None,  # 按周期筛选 | Filter by candle interval
    market: str | None = None,  # 按市场筛选 | Filter by market type
    limit: int = 100,  # 返回条数上限 | Maximum number of rows
) -> list[QuantDatasetModel]:  # 返回数据集列表 | Returns a list of datasets
    """查询数据集列表 | List datasets.

    参数 | Args:
        db (AsyncSession): 异步数据库会话 | Async database session.
        symbol (str | None): 交易对筛选 | Pair filter.
        interval (str | None): 周期筛选 | Interval filter.
        market (str | None): 市场筛选 | Market filter.
        limit (int): 返回条数上限 | Row limit.

    返回 | Returns:
        list[QuantDatasetModel]: 按 ID 倒序的数据集列表 | Datasets ordered by ID descending.
    """
    stmt = select(QuantDatasetModel).where(QuantDatasetModel.is_deleted.is_(False))  # 基础查询：未删除的数据集 | Base query: non-deleted datasets
    if symbol:  # 提供了交易对筛选 | Pair filter specified
        stmt = stmt.where(QuantDatasetModel.symbol == symbol.replace("/", "").upper())  # 规范化交易对后精确匹配 | Normalize pair then exact match
    if interval:  # 提供了周期筛选 | Interval filter specified
        stmt = stmt.where(QuantDatasetModel.interval == interval)  # 按周期精确匹配 | Exact match on interval
    if market:  # 提供了市场筛选 | Market filter specified
        stmt = stmt.where(QuantDatasetModel.market == market)  # 按市场精确匹配 | Exact match on market
    stmt = stmt.order_by(QuantDatasetModel.id.desc()).limit(limit)  # 按 ID 倒序并限制条数 | Order by ID descending and limit
    return list((await db.execute(stmt)).scalars().all())  # 执行查询并转列表 | Execute query and convert to list


async def get_dataset(db: AsyncSession, dataset_id: int) -> QuantDatasetModel:
    """按 ID 查询数据集，不存在则抛业务异常 | Get a dataset by ID, raising a business error if missing.

    参数 | Args:
        db (AsyncSession): 异步数据库会话 | Async database session.
        dataset_id (int): 数据集 ID | Dataset ID.

    返回 | Returns:
        QuantDatasetModel: 找到的数据集 | The found dataset.

    异常 | Raises:
        CustomException: 记录不存在或已删除时抛出 | Raised when missing or deleted.
    """
    obj = await db.get(QuantDatasetModel, dataset_id)  # 按主键查询 | Query by primary key
    if obj is None or obj.is_deleted:  # 不存在或已软删除 | Missing or soft-deleted
        raise CustomException(msg="数据集不存在")  # 抛业务异常 | Raise a business exception
    return obj  # 返回数据集 | Return the dataset


async def read_dataset_candles(  # 读取数据集 K 线供图表展示 | Read dataset candles for chart display
    db: AsyncSession,  # 异步数据库会话 | Async database session
    dataset_id: int,  # 数据集 ID | Dataset ID
    *,  # 之后的参数必须关键字传参 | Following arguments are keyword-only
    limit: int = 2000,  # 返回条数上限 | Row limit
    tail: bool = True,  # True 取最后 limit 根，False 取最前 | True = last limit rows, False = first
) -> dict[str, Any]:  # 返回 K 线与元信息 | Returns candles and metadata
    """读取数据集 CSV 的 K 线，供图表展示 | Read dataset CSV candles for chart display.

    大文件默认只取最后 limit 根（tail=True），避免一次把多年数据塞给前端。
    Large files return only the last limit rows by default (tail=True) to avoid sending years of data to the frontend.

    参数 | Args:
        db (AsyncSession): 异步数据库会话 | Async database session.
        dataset_id (int): 数据集 ID | Dataset ID.
        limit (int): 返回条数上限 | Row limit.
        tail (bool): True 取末尾，False 取开头 | True = tail, False = head.

    返回 | Returns:
        dict[str, Any]: K 线与元信息 | Candles and metadata.
    """
    dataset = await get_dataset(db, dataset_id)  # 查询数据集（不存在会抛异常）| Fetch the dataset (raises if missing)
    csv_path = resolve_dataset_csv(dataset.file_path)  # 解析 CSV 绝对路径 | Resolve the CSV absolute path
    rows = _read_csv_rows(csv_path)  # 读取所有 K 线行 | Read all candle rows
    total = len(rows)  # 总行数 | Total row count
    if tail and total > limit:  # 取末尾且超出上限 | Tail mode and over the limit
        rows = rows[-limit:]  # 只保留最后 limit 根 | Keep only the last limit rows
    return {  # 组装响应 | Assemble the response
        "dataset_id": dataset.id,  # 数据集 ID | Dataset ID
        "name": dataset.name,  # 数据集名称 | Dataset name
        "symbol": dataset.symbol,  # 交易对 | Trading pair
        "interval": dataset.interval,  # K 线周期 | Candle interval
        "total": total,  # CSV 中总 K 线数 | Total candles in the CSV
        "candles": rows,  # K 线列表（可能截断）| Candle list (possibly truncated)
    }  # 响应结束 | End of response


def list_local_csv_files() -> list[dict[str, Any]]:
    """列出允许目录下的 CSV 文件，供页面选择导入 | List CSV files in allowed directories for the UI import picker.

    返回 | Returns:
        list[dict[str, Any]]: 文件信息列表，按修改时间倒序 | File info list sorted by modification time descending.
    """
    result: list[dict[str, Any]] = []  # 收集文件信息 | Accumulator for file info
    for root in ALLOWED_IMPORT_DIRS:  # 遍历每个允许目录 | Iterate over each allowed directory
        if not root.exists():  # 目录不存在 | Directory doesn't exist
            continue  # 跳过 | Skip
        for path in root.rglob("*.csv"):  # 递归查找所有 CSV 文件 | Recursively find all CSV files
            if not path.is_file():  # 只处理文件（排除同名目录）| Only process files (exclude directories)
                continue  # 跳过 | Skip
            stat = path.stat()  # 获取文件状态（大小/修改时间）| Get file stat (size/mtime)
            result.append(  # 追加文件信息字典 | Append the file info dict
                {  # 文件信息 | File info
                    "path": _relative_path(path),  # 相对路径 | Relative path
                    "name": path.name,  # 文件名 | File name
                    "size": stat.st_size,  # 字节大小 | Size in bytes
                    "modified_time": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),  # 修改时间 ISO8601 | Modification time in ISO8601
                    "market": _market_from_path(path),  # 按路径推断市场 | Infer market from path
                }  # 文件信息结束 | End of file info
            )  # append 结束 | End of append
    return sorted(result, key=lambda item: item["modified_time"], reverse=True)  # 按修改时间倒序返回 | Return sorted by modification time descending


def resolve_dataset_csv(file_path: str) -> Path:
    """把数据集相对路径解析为绝对路径并校验存在 | Resolve a dataset's relative path to absolute and verify existence.

    参数 | Args:
        file_path (str): 数据集文件路径（相对或绝对）| Dataset file path (relative or absolute).

    返回 | Returns:
        Path: 校验存在的绝对路径 | The validated absolute path.

    异常 | Raises:
        CustomException: 文件不存在时抛出 | Raised when the file doesn't exist.
    """
    raw = Path(file_path).expanduser()  # 展开 ~ | Expand ~
    path = raw if raw.is_absolute() else PROJECT_ROOT / raw  # 相对路径拼接项目根 | Join project root for relative paths
    resolved = path.resolve()  # 解析为绝对路径 | Resolve to absolute path
    if not resolved.is_file():  # 文件不存在 | File doesn't exist
        raise CustomException(msg=f"数据文件不存在：{resolved}")  # 抛业务异常 | Raise a business exception
    return resolved  # 返回绝对路径 | Return the absolute path
