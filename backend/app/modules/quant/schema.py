"""量化模块 Pydantic 模型 | Pydantic models for the quant module.

定义下载、导入、数据集查询、回测与实时策略的请求/响应结构 | Defines request/response schemas for download, import,
dataset queries, backtesting and live strategies.
"""

from typing import Any, Literal  # 导入 Any（宽松类型）与 Literal（固定取值集合）| Import Any (loose type) and Literal (fixed value sets)

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator  # 导入 Pydantic 基础模型、配置、字段与校验器 | Import Pydantic base model, config, fields and validators

from app.core.validator import DateTimeStr  # 导入日期时间字符串校验类型 | Import the datetime string validator type

# 币安现货 K 线合法周期 | Valid Binance spot candle intervals
BINANCE_INTERVALS = {  # 周期集合（集合结构便于 O(1) 成员判断）| A set structure for O(1) membership checks
    "1m",  # 1 分钟 | 1 minute
    "3m",  # 3 分钟 | 3 minutes
    "5m",  # 5 分钟 | 5 minutes
    "15m",  # 15 分钟 | 15 minutes
    "30m",  # 30 分钟 | 30 minutes
    "1h",  # 1 小时 | 1 hour
    "2h",  # 2 小时 | 2 hours
    "4h",  # 4 小时 | 4 hours
    "6h",  # 6 小时 | 6 hours
    "8h",  # 8 小时 | 8 hours
    "12h",  # 12 小时 | 12 hours
    "1d",  # 1 天 | 1 day
    "3d",  # 3 天 | 3 days
    "1w",  # 1 周 | 1 week
    "1M",  # 1 月 | 1 month
}  # 周期集合结束 | End of interval set


class QuantIntervalValidatorMixin(BaseModel):
    """周期校验混入类 | Mixin that validates candle interval and normalizes symbol.

    供下载、导入、创建实时策略等模型复用 | Reused by download, import and live-strategy creation schemas.
    """

    @field_validator("interval", check_fields=False)  # 字段校验器：校验 interval；check_fields=False 允许混入类自身无该字段 | Field validator for interval; check_fields=False allows the mixin to lack the field
    @classmethod  # 校验器是类方法 | Validators are class methods
    def validate_interval(cls, value: str) -> str:
        """校验 K 线周期必须是币安支持的周期 | Validate that the interval is supported by Binance.

        参数 | Args:
            value (str): 待校验的周期字符串 | Interval string to validate.

        返回 | Returns:
            str: 校验通过的周期 | The validated interval.

        异常 | Raises:
            ValueError: 周期不在合法集合中时抛出 | Raised when the interval is not in the allowed set.
        """
        if value not in BINANCE_INTERVALS:  # 周期不在合法集合中 | Interval is not in the allowed set
            raise ValueError(f"不支持的 K 线周期 {value}，可选：{sorted(BINANCE_INTERVALS)}")  # 报错并列出可选周期 | Raise listing the allowed intervals
        return value  # 返回合法的周期 | Return the valid interval

    @field_validator("symbol", check_fields=False)  # 字段校验器：规范化交易对 | Field validator that normalizes the trading pair
    @classmethod  # 校验器是类方法 | Validators are class methods
    def validate_symbol(cls, value: str) -> str:
        """校验并规范化交易对：BTC/USDT 与 BTCUSDT 均接受 | Validate and normalize symbol: both BTC/USDT and BTCUSDT are accepted.

        参数 | Args:
            value (str): 原始交易对字符串 | Raw symbol string.

        返回 | Returns:
            str: 规范化后的大写无分隔符交易对 | Normalized uppercase symbol without separators.

        异常 | Raises:
            ValueError: 长度或字符非法时抛出 | Raised when length or characters are invalid.
        """
        normalized = value.replace("/", "").replace("_", "").strip().upper()  # 去掉 / 和 _，去空白并转大写 | Remove / and _, strip whitespace and uppercase
        if not 4 <= len(normalized) <= 30 or not normalized.isalnum():  # 长度需 4~30 且全为字母数字 | Length must be 4~30 and alphanumeric
            raise ValueError("交易对格式无效，示例：BTC/USDT 或 BTCUSDT")  # 交易对格式非法 | Invalid symbol format
        return normalized  # 返回规范化交易对 | Return the normalized symbol


class QuantDataDownloadSchema(QuantIntervalValidatorMixin):
    """币安数据下载参数 | Binance data download parameters.

    source 支持三种方式 | Three download sources are supported:
    - api：币安官方 REST，现货与 U 本位合约均可，适合近期数据 | Official Binance REST; spot and USDT-m futures; recent data.
    - mirror：公开 REST 镜像（data-api.binance.vision），仅现货 | Public REST mirror; spot only.
    - archive：币安历史归档 ZIP（data.binance.vision），适合长历史，支持现货与合约 | Historical ZIP archives; long history; spot and futures.
    """

    symbol: str = Field(description="交易对，如 BTC/USDT")  # 交易对（必填）| Trading pair (required)
    interval: str = Field(description="K线周期，如 1h")  # K 线周期（必填）| Candle interval (required)
    start_time: str | None = Field(default=None, description="起始时间：YYYY-MM-DD、ISO8601 或毫秒时间戳")  # 起始时间，可空 | Start time, optional
    end_time: str | None = Field(default=None, description="结束时间：YYYY-MM-DD、ISO8601 或毫秒时间戳")  # 结束时间，可空 | End time, optional
    days: int | None = Field(default=None, ge=1, le=3650, description="下载最近 N 天，设置后优先于起止时间")  # 下载最近 N 天，1~3650，优先于起止时间 | Download last N days (1~3650), overrides start/end
    source: Literal["api", "mirror", "archive"] = Field(default="api", description="下载方式：api / mirror / archive")  # 下载方式 | Download source
    market: Literal["spot", "futures"] = Field(default="spot", description="市场：spot-现货，futures-U 本位合约")  # 市场类型 | Market type
    limit: int = Field(default=1000, ge=100, le=1000, description="REST 单次请求条数，币安上限 1000")  # REST 单次请求条数，100~1000 | Rows per REST request, 100~1000
    erase: bool = Field(default=False, description="清空本地已有数据后重新写入")  # 是否清空已有数据再写入 | Whether to erase existing data before writing
    fill_recent: bool = Field(default=True, description="归档方式是否用公开镜像补齐最近数据")  # 归档方式是否用镜像补齐最近数据 | Whether to fill recent data via mirror in archive mode
    description: str | None = Field(default=None, description="数据集备注")  # 数据集备注 | Dataset description


class QuantDataImportSchema(QuantIntervalValidatorMixin):
    """本地 CSV 导入参数 | Local CSV import parameters."""

    file_path: str = Field(description="本地 CSV 路径，需在允许的数据目录内")  # 待导入 CSV 路径，须在允许目录内 | CSV path, must be within allowed directories
    symbol: str = Field(description="交易对，如 BTC/USDT")  # 交易对 | Trading pair
    interval: str = Field(description="K线周期，如 1h")  # K 线周期 | Candle interval
    source: str = Field(default="local", description="数据来源标识")  # 数据来源标识，默认 local | Source tag, defaults to local
    market: Literal["spot", "futures"] = Field(default="spot", description="市场：spot-现货，futures-合约")  # 市场类型 | Market type
    description: str | None = Field(default=None, description="数据集备注")  # 数据集备注 | Dataset description


class QuantDatasetOutSchema(BaseModel):
    """数据集响应模型 | Dataset response schema."""

    model_config = ConfigDict(from_attributes=True)  # 允许从 ORM 对象属性填充（支持 model_validate(obj)）| Allow population from ORM object attributes

    id: int = Field(description="主键ID")  # 主键 ID | Primary key
    name: str = Field(description="数据集名称")  # 数据集名称 | Dataset name
    symbol: str = Field(description="交易对")  # 交易对 | Trading pair
    interval: str = Field(description="K线周期")  # K 线周期 | Candle interval
    source: str = Field(description="数据来源")  # 数据来源 | Data source
    market: str = Field(default="spot", description="市场：spot-现货，futures-合约")  # 市场类型 | Market type
    file_path: str = Field(description="本地 CSV 相对路径")  # CSV 相对路径 | CSV relative path
    row_count: int = Field(description="K线数量")  # K 线数量 | Candle count
    start_time: DateTimeStr | None = Field(default=None, description="数据起始时间")  # 数据起始时间 | Data start time
    end_time: DateTimeStr | None = Field(default=None, description="数据结束时间")  # 数据结束时间 | Data end time
    status: int = Field(description="状态：0=禁用，1=启用")  # 状态 | Status
    description: str | None = Field(default=None, description="备注")  # 备注 | Description
    created_time: DateTimeStr | None = Field(default=None, description="创建时间")  # 创建时间 | Creation time


class QuantCandleSchema(BaseModel):
    """单根 K 线 | A single candle."""

    date: str = Field(description="UTC ISO8601 时间")  # UTC ISO8601 时间 | UTC ISO8601 timestamp
    open: float = Field(description="开盘价")  # 开盘价 | Open price
    high: float = Field(description="最高价")  # 最高价 | High price
    low: float = Field(description="最低价")  # 最低价 | Low price
    close: float = Field(description="收盘价")  # 收盘价 | Close price
    volume: float = Field(description="成交量")  # 成交量 | Volume


class QuantCandlesOutSchema(BaseModel):
    """数据集 K 线响应 | Dataset candles response."""

    dataset_id: int = Field(description="数据集ID")  # 数据集 ID | Dataset ID
    name: str = Field(description="数据集名称")  # 数据集名称 | Dataset name
    symbol: str = Field(description="交易对")  # 交易对 | Trading pair
    interval: str = Field(description="K线周期")  # K 线周期 | Candle interval
    total: int = Field(description="CSV 中总 K 线数")  # CSV 中 K 线总数 | Total candles in the CSV
    candles: list[QuantCandleSchema] = Field(description="K 线列表（可能截断）")  # K 线列表（可能被截断）| Candle list (possibly truncated)


class QuantLocalFileSchema(BaseModel):
    """可导入的本地文件 | An importable local file."""

    path: str = Field(description="相对项目根目录的路径")  # 相对项目根目录的路径 | Path relative to project root
    name: str = Field(description="文件名")  # 文件名 | File name
    size: int = Field(description="文件字节数")  # 文件字节数 | File size in bytes
    modified_time: str = Field(description="修改时间 ISO8601")  # 修改时间（ISO8601）| Modification time (ISO8601)
    market: str = Field(default="spot", description="根据路径推断的市场：spot/futures")  # 按路径推断的市场 | Market inferred from path


class QuantStrategyOutSchema(BaseModel):
    """策略信息 | Strategy information."""

    name: str = Field(description="策略类名")  # 策略类名 | Strategy class name
    file: str = Field(description="策略文件相对路径")  # 策略文件相对路径 | Strategy file relative path
    mode: str = Field(description="策略模式：event-事件回调，signal-信号列")  # 策略模式 | Strategy mode
    timeframe: str = Field(default="", description="策略推荐周期")  # 策略推荐周期 | Recommended timeframe
    doc: str = Field(default="", description="策略类文档说明")  # 策略类文档说明 | Strategy docstring


class QuantBacktestCreateSchema(BaseModel):
    """创建回测任务参数 | Backtest creation parameters.

    dataset_ids 支持一次选择多个数据集（每个数据集 = 交易对 + 周期 + 市场），
    dataset_id 为旧版单数据集接口兼容字段，两者会合并去重。
    dataset_ids supports selecting multiple datasets at once (each = pair + interval + market);
    dataset_id is a legacy single-dataset field; both are merged and de-duplicated.
    """

    dataset_ids: list[int] = Field(default_factory=list, description="数据集ID列表，支持多选")  # 数据集 ID 列表，支持多选 | Dataset ID list, supports multiple
    dataset_id: int | None = Field(default=None, description="单个数据集ID（兼容旧调用）")  # 单数据集 ID（兼容旧调用）| Single dataset ID (legacy compatibility)
    strategy_name: str = Field(description="策略类名，必须与策略文件中的类名一致")  # 策略类名 | Strategy class name
    name: str | None = Field(default=None, description="回测名称，为空时自动生成")  # 回测名称，空则自动生成 | Backtest name, auto-generated when empty
    initial_cash: float = Field(default=10000.0, gt=0, description="初始资金")  # 初始资金，必须 > 0 | Initial capital, must be > 0
    fee_rate: float = Field(default=0.001, ge=0, le=0.1, description="单边手续费率，0.001=0.1%")  # 单边手续费率，0~0.1 | One-way fee rate, 0~0.1
    slippage: float = Field(default=0.0, ge=0, le=0.1, description="滑点比例，0.001=0.1%")  # 滑点比例，0~0.1 | Slippage ratio, 0~0.1
    leverage: float = Field(default=1.0, ge=1, le=125, description="杠杆倍数，1=现货全额，10=10 倍杠杆（仅合约数据集生效）")  # 杠杆倍数 | Leverage multiple
    description: str | None = Field(default=None, description="备注")  # 备注 | Description

    @model_validator(mode="after")  # 模型级校验器，在所有字段校验之后运行 | Model-level validator, runs after all field validators
    def normalize_dataset_ids(self) -> "QuantBacktestCreateSchema":
        """合并新旧字段、去重并校验数量 | Merge new/legacy fields, de-duplicate and validate count.

        返回 | Returns:
            QuantBacktestCreateSchema: 规范化后的模型自身 | The normalized model instance itself.

        异常 | Raises:
            ValueError: 未选数据集或超过 50 个时抛出 | Raised when no dataset is selected or more than 50 are given.
        """
        ids = list(dict.fromkeys([*self.dataset_ids, *([self.dataset_id] if self.dataset_id else [])]))  # 合并两个字段并按出现顺序去重 | Merge both fields and de-duplicate preserving order
        if not ids:  # 一个数据集都没选 | No dataset selected
            raise ValueError("请至少选择一个数据集")  # 至少选一个 | Must select at least one
        if len(ids) > 50:  # 单次最多 50 个数据集 | At most 50 datasets per call
            raise ValueError("单次最多回测 50 个数据集")  # 超出数量上限 | Exceeded the count limit
        self.dataset_ids = ids  # 写回规范化后的 ID 列表 | Write back the normalized ID list
        return self  # 返回模型自身 | Return the model itself


class QuantBacktestOutSchema(BaseModel):
    """回测记录响应模型 | Backtest record response schema."""

    model_config = ConfigDict(from_attributes=True)  # 允许从 ORM 对象属性填充 | Allow population from ORM object attributes

    id: int = Field(description="主键ID")  # 主键 ID | Primary key
    name: str = Field(description="回测名称")  # 回测名称 | Backtest name
    strategy_name: str = Field(description="策略类名")  # 策略类名 | Strategy class name
    symbol: str = Field(description="交易对")  # 交易对 | Trading pair
    interval: str = Field(description="K线周期")  # K 线周期 | Candle interval
    market: str = Field(default="spot", description="市场：spot-现货，futures-合约")  # 市场类型 | Market type
    initial_cash: float = Field(description="初始资金")  # 初始资金 | Initial capital
    fee_rate: float = Field(description="手续费率")  # 手续费率 | Fee rate
    leverage: float = Field(default=1.0, description="回测使用的杠杆倍数（现货恒为 1）")  # 杠杆倍数 | Leverage used
    status: int = Field(description="状态：0=运行中，1=成功，2=失败")  # 回测状态 | Backtest status
    result: dict[str, Any] | None = Field(default=None, description="回测结果")  # 回测结果，可空 | Backtest result, optional
    error: str | None = Field(default=None, description="失败原因")  # 失败原因 | Error message
    started_time: DateTimeStr | None = Field(default=None, description="开始时间")  # 开始时间 | Start time
    finished_time: DateTimeStr | None = Field(default=None, description="结束时间")  # 结束时间 | Finish time
    created_time: DateTimeStr | None = Field(default=None, description="创建时间")  # 创建时间 | Creation time


class QuantLiveStrategyCreateSchema(QuantIntervalValidatorMixin):
    """创建实时策略实例参数（模拟盘/实盘共用）| Live strategy creation parameters (paper/live shared)."""

    name: str = Field(min_length=1, max_length=150, description="实例名称")  # 实例名称，长度 1~150 | Instance name, length 1~150
    mode: str = Field(default="paper", description="运行模式：paper-模拟盘，live-实盘")  # 运行模式 | Run mode
    strategy_name: str = Field(description="策略类名，必须与策略文件中的类名一致")  # 策略类名 | Strategy class name
    symbol: str = Field(description="交易对，如 BTC/USDT")  # 交易对 | Trading pair
    interval: str = Field(description="K线周期，如 1h")  # K 线周期 | Candle interval
    initial_cash: float = Field(default=10000.0, gt=0, description="初始/虚拟资金")  # 初始/虚拟资金，> 0 | Initial/virtual capital, > 0
    fee_rate: float = Field(default=0.001, ge=0, le=0.1, description="单边手续费率")  # 单边手续费率 | One-way fee rate
    slippage: float = Field(default=0.0, ge=0, le=0.1, description="滑点比例")  # 滑点比例 | Slippage ratio
    leverage: float = Field(default=1.0, ge=1, le=125, description="杠杆倍数，1=现货，>1=合约保证金交易（净值跌破维持保证金会被强平）")  # 杠杆倍数 | Leverage multiple
    dry_run: bool = Field(default=True, description="实盘是否模拟运行，True 不下真实单")  # 是否模拟运行 | Simulated run flag
    allow_short: bool = Field(default=True, description="是否允许做空，实盘现货建议 False")  # 是否允许做空 | Whether shorting is allowed
    api_key: str | None = Field(default=None, description="币安 API Key（仅实盘真实下单需要，加密存储）")  # API Key（加密存储）| Encrypted API key
    api_secret: str | None = Field(default=None, description="币安 API Secret（仅实盘真实下单需要，加密存储）")  # API Secret（加密存储）| Encrypted API secret
    description: str | None = Field(default=None, description="备注")  # 备注 | Description

    @field_validator("mode")  # 字段校验器：校验运行模式 | Field validator for run mode
    @classmethod  # 校验器是类方法 | Validators are class methods
    def validate_mode(cls, value: str) -> str:
        """校验运行模式取值 | Validate the run mode value.

        参数 | Args:
            value (str): 待校验的模式 | Mode string to validate.

        返回 | Returns:
            str: 合法的模式 | The valid mode.

        异常 | Raises:
            ValueError: 模式非 paper/live 时抛出 | Raised when mode is not paper/live.
        """
        if value not in {"paper", "live"}:  # 模式必须是 paper 或 live | Mode must be paper or live
            raise ValueError("mode 只能是 paper 或 live")  # 模式取值非法 | Invalid mode value
        return value  # 返回合法模式 | Return the valid mode


class QuantLiveStrategyUpdateSchema(BaseModel):
    """更新实时策略实例参数（仅允许修改安全字段）| Live strategy update parameters (only safe fields are editable)."""

    name: str | None = Field(default=None, max_length=150, description="实例名称")  # 实例名称，可空表示不改 | Instance name, None means no change
    fee_rate: float | None = Field(default=None, ge=0, le=0.1, description="单边手续费率")  # 手续费率 | Fee rate
    slippage: float | None = Field(default=None, ge=0, le=0.1, description="滑点比例")  # 滑点比例 | Slippage ratio
    dry_run: bool | None = Field(default=None, description="实盘是否模拟运行")  # 是否模拟运行 | Simulated run flag
    allow_short: bool | None = Field(default=None, description="是否允许做空")  # 是否允许做空 | Whether shorting is allowed
    api_key: str | None = Field(default=None, description="币安 API Key，留空表示不修改")  # API Key，留空不改 | API key, empty means no change
    api_secret: str | None = Field(default=None, description="币安 API Secret，留空表示不修改")  # API Secret，留空不改 | API secret, empty means no change
    description: str | None = Field(default=None, description="备注")  # 备注 | Description


class QuantLiveStrategyOutSchema(BaseModel):
    """实时策略实例响应模型（永不返回密钥明文）| Live strategy response schema (never returns plaintext credentials)."""

    model_config = ConfigDict(from_attributes=True)  # 允许从 ORM 对象属性填充 | Allow population from ORM object attributes

    id: int = Field(description="主键ID")  # 主键 ID | Primary key
    name: str = Field(description="实例名称")  # 实例名称 | Instance name
    mode: str = Field(description="运行模式：paper/live")  # 运行模式 | Run mode
    strategy_name: str = Field(description="策略类名")  # 策略类名 | Strategy class name
    symbol: str = Field(description="交易对")  # 交易对 | Trading pair
    interval: str = Field(description="K线周期")  # K 线周期 | Candle interval
    initial_cash: float = Field(description="初始/虚拟资金")  # 初始/虚拟资金 | Initial/virtual capital
    fee_rate: float = Field(description="手续费率")  # 手续费率 | Fee rate
    slippage: float = Field(description="滑点比例")  # 滑点比例 | Slippage ratio
    leverage: float = Field(default=1.0, description="杠杆倍数，1 表示现货全额")  # 杠杆倍数 | Leverage multiple
    dry_run: bool = Field(description="是否模拟运行")  # 是否模拟运行 | Simulated run flag
    allow_short: bool = Field(description="是否允许做空")  # 是否允许做空 | Whether shorting is allowed
    status: int = Field(description="状态：0=停止，1=运行，2=异常")  # 实例状态 | Instance status
    account: dict[str, Any] | None = Field(default=None, description="账户账本快照")  # 账户账本快照 | Account ledger snapshot
    has_api_key: bool = Field(default=False, description="是否已配置 API Key（不返回明文）")  # 是否已配置密钥（不返回明文）| Whether a key is configured (never plaintext)
    last_result: dict[str, Any] | None = Field(default=None, description="最近一次 tick 结果")  # 最近一次 tick 结果 | Latest tick result
    last_error: str | None = Field(default=None, description="最近错误")  # 最近错误 | Latest error
    last_tick_time: DateTimeStr | None = Field(default=None, description="最近运行时间")  # 最近运行时间 | Last run time
    description: str | None = Field(default=None, description="备注")  # 备注 | Description
    created_time: DateTimeStr | None = Field(default=None, description="创建时间")  # 创建时间 | Creation time


class QuantTradeRecordOutSchema(BaseModel):
    """实时成交流水响应模型 | Real-time trade record response schema."""

    model_config = ConfigDict(from_attributes=True)  # 允许从 ORM 对象属性填充 | Allow population from ORM object attributes

    id: int = Field(description="主键ID")  # 主键 ID | Primary key
    strategy_id: int = Field(description="实时策略实例ID")  # 关联策略实例 ID | Related strategy instance ID
    mode: str = Field(description="运行模式：paper/live")  # 运行模式 | Run mode
    symbol: str = Field(description="交易对")  # 交易对 | Trading pair
    side: str = Field(description="方向")  # 交易方向 | Trade direction
    qty: float = Field(description="成交数量")  # 成交数量 | Filled quantity
    price: float = Field(description="成交价格")  # 成交价格 | Filled price
    fee: float = Field(description="手续费")  # 手续费 | Fee
    pnl: float | None = Field(default=None, description="已实现盈亏")  # 已实现盈亏 | Realized pnl
    reason: str | None = Field(default=None, description="触发原因")  # 触发原因 | Trigger reason
    trade_time: DateTimeStr | None = Field(default=None, description="成交时间")  # 成交时间 | Trade timestamp
