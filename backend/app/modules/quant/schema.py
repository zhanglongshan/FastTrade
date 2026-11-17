"""量化模块 Pydantic 模型。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.validator import DateTimeStr

# 币安现货 K 线合法周期
BINANCE_INTERVALS = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
}


class QuantIntervalValidatorMixin(BaseModel):
    """周期校验混入类。"""

    @field_validator("interval", check_fields=False)
    @classmethod
    def validate_interval(cls, value: str) -> str:
        """校验 K 线周期必须是币安支持的周期。"""
        if value not in BINANCE_INTERVALS:
            raise ValueError(f"不支持的 K 线周期 {value}，可选：{sorted(BINANCE_INTERVALS)}")
        return value

    @field_validator("symbol", check_fields=False)
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        """校验并规范化交易对：BTC/USDT 与 BTCUSDT 均接受。"""
        normalized = value.replace("/", "").replace("_", "").strip().upper()
        if not 4 <= len(normalized) <= 30 or not normalized.isalnum():
            raise ValueError("交易对格式无效，示例：BTC/USDT 或 BTCUSDT")
        return normalized


class QuantDataDownloadSchema(QuantIntervalValidatorMixin):
    """币安数据下载参数。

    source 支持三种方式：
    - api：币安官方 REST，现货与 U 本位合约均可，适合近期数据；
    - mirror：公开 REST 镜像（data-api.binance.vision），仅现货；
    - archive：币安历史归档 ZIP（data.binance.vision），适合长历史，支持现货与合约。
    """

    symbol: str = Field(description="交易对，如 BTC/USDT")
    interval: str = Field(description="K线周期，如 1h")
    start_time: str | None = Field(default=None, description="起始时间：YYYY-MM-DD、ISO8601 或毫秒时间戳")
    end_time: str | None = Field(default=None, description="结束时间：YYYY-MM-DD、ISO8601 或毫秒时间戳")
    days: int | None = Field(default=None, ge=1, le=3650, description="下载最近 N 天，设置后优先于起止时间")
    source: Literal["api", "mirror", "archive"] = Field(default="api", description="下载方式：api / mirror / archive")
    market: Literal["spot", "futures"] = Field(default="spot", description="市场：spot-现货，futures-U 本位合约")
    limit: int = Field(default=1000, ge=100, le=1000, description="REST 单次请求条数，币安上限 1000")
    erase: bool = Field(default=False, description="清空本地已有数据后重新写入")
    fill_recent: bool = Field(default=True, description="归档方式是否用公开镜像补齐最近数据")
    description: str | None = Field(default=None, description="数据集备注")


class QuantDataImportSchema(QuantIntervalValidatorMixin):
    """本地 CSV 导入参数。"""

    file_path: str = Field(description="本地 CSV 路径，需在允许的数据目录内")
    symbol: str = Field(description="交易对，如 BTC/USDT")
    interval: str = Field(description="K线周期，如 1h")
    source: str = Field(default="local", description="数据来源标识")
    description: str | None = Field(default=None, description="数据集备注")


class QuantDatasetOutSchema(BaseModel):
    """数据集响应模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键ID")
    name: str = Field(description="数据集名称")
    symbol: str = Field(description="交易对")
    interval: str = Field(description="K线周期")
    source: str = Field(description="数据来源")
    file_path: str = Field(description="本地 CSV 相对路径")
    row_count: int = Field(description="K线数量")
    start_time: DateTimeStr | None = Field(default=None, description="数据起始时间")
    end_time: DateTimeStr | None = Field(default=None, description="数据结束时间")
    status: int = Field(description="状态：0=禁用，1=启用")
    description: str | None = Field(default=None, description="备注")
    created_time: DateTimeStr | None = Field(default=None, description="创建时间")


class QuantLocalFileSchema(BaseModel):
    """可导入的本地文件。"""

    path: str = Field(description="相对项目根目录的路径")
    name: str = Field(description="文件名")
    size: int = Field(description="文件字节数")
    modified_time: str = Field(description="修改时间 ISO8601")


class QuantStrategyOutSchema(BaseModel):
    """策略信息。"""

    name: str = Field(description="策略类名")
    file: str = Field(description="策略文件相对路径")
    mode: str = Field(description="策略模式：event-事件回调，signal-信号列")
    timeframe: str = Field(default="", description="策略推荐周期")
    doc: str = Field(default="", description="策略类文档说明")


class QuantBacktestCreateSchema(BaseModel):
    """创建回测任务参数。"""

    dataset_id: int = Field(description="数据集ID")
    strategy_name: str = Field(description="策略类名，必须与策略文件中的类名一致")
    name: str | None = Field(default=None, description="回测名称，为空时自动生成")
    initial_cash: float = Field(default=10000.0, gt=0, description="初始资金")
    fee_rate: float = Field(default=0.001, ge=0, le=0.1, description="单边手续费率，0.001=0.1%")
    slippage: float = Field(default=0.0, ge=0, le=0.1, description="滑点比例，0.001=0.1%")
    description: str | None = Field(default=None, description="备注")


class QuantBacktestOutSchema(BaseModel):
    """回测记录响应模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键ID")
    name: str = Field(description="回测名称")
    strategy_name: str = Field(description="策略类名")
    symbol: str = Field(description="交易对")
    interval: str = Field(description="K线周期")
    initial_cash: float = Field(description="初始资金")
    fee_rate: float = Field(description="手续费率")
    status: int = Field(description="状态：0=运行中，1=成功，2=失败")
    result: dict[str, Any] | None = Field(default=None, description="回测结果")
    error: str | None = Field(default=None, description="失败原因")
    started_time: DateTimeStr | None = Field(default=None, description="开始时间")
    finished_time: DateTimeStr | None = Field(default=None, description="结束时间")
    created_time: DateTimeStr | None = Field(default=None, description="创建时间")


class QuantLiveStrategyCreateSchema(QuantIntervalValidatorMixin):
    """创建实时策略实例参数（模拟盘/实盘共用）。"""

    name: str = Field(min_length=1, max_length=150, description="实例名称")
    mode: str = Field(default="paper", description="运行模式：paper-模拟盘，live-实盘")
    strategy_name: str = Field(description="策略类名，必须与策略文件中的类名一致")
    symbol: str = Field(description="交易对，如 BTC/USDT")
    interval: str = Field(description="K线周期，如 1h")
    initial_cash: float = Field(default=10000.0, gt=0, description="初始/虚拟资金")
    fee_rate: float = Field(default=0.001, ge=0, le=0.1, description="单边手续费率")
    slippage: float = Field(default=0.0, ge=0, le=0.1, description="滑点比例")
    dry_run: bool = Field(default=True, description="实盘是否模拟运行，True 不下真实单")
    allow_short: bool = Field(default=True, description="是否允许做空，实盘现货建议 False")
    api_key: str | None = Field(default=None, description="币安 API Key（仅实盘真实下单需要，加密存储）")
    api_secret: str | None = Field(default=None, description="币安 API Secret（仅实盘真实下单需要，加密存储）")
    description: str | None = Field(default=None, description="备注")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        """校验运行模式取值。"""
        if value not in {"paper", "live"}:
            raise ValueError("mode 只能是 paper 或 live")
        return value


class QuantLiveStrategyUpdateSchema(BaseModel):
    """更新实时策略实例参数（仅允许修改安全字段）。"""

    name: str | None = Field(default=None, max_length=150, description="实例名称")
    fee_rate: float | None = Field(default=None, ge=0, le=0.1, description="单边手续费率")
    slippage: float | None = Field(default=None, ge=0, le=0.1, description="滑点比例")
    dry_run: bool | None = Field(default=None, description="实盘是否模拟运行")
    allow_short: bool | None = Field(default=None, description="是否允许做空")
    api_key: str | None = Field(default=None, description="币安 API Key，留空表示不修改")
    api_secret: str | None = Field(default=None, description="币安 API Secret，留空表示不修改")
    description: str | None = Field(default=None, description="备注")


class QuantLiveStrategyOutSchema(BaseModel):
    """实时策略实例响应模型（永不返回密钥明文）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键ID")
    name: str = Field(description="实例名称")
    mode: str = Field(description="运行模式：paper/live")
    strategy_name: str = Field(description="策略类名")
    symbol: str = Field(description="交易对")
    interval: str = Field(description="K线周期")
    initial_cash: float = Field(description="初始/虚拟资金")
    fee_rate: float = Field(description="手续费率")
    slippage: float = Field(description="滑点比例")
    dry_run: bool = Field(description="是否模拟运行")
    allow_short: bool = Field(description="是否允许做空")
    status: int = Field(description="状态：0=停止，1=运行，2=异常")
    account: dict[str, Any] | None = Field(default=None, description="账户账本快照")
    has_api_key: bool = Field(default=False, description="是否已配置 API Key（不返回明文）")
    last_result: dict[str, Any] | None = Field(default=None, description="最近一次 tick 结果")
    last_error: str | None = Field(default=None, description="最近错误")
    last_tick_time: DateTimeStr | None = Field(default=None, description="最近运行时间")
    description: str | None = Field(default=None, description="备注")
    created_time: DateTimeStr | None = Field(default=None, description="创建时间")


class QuantTradeRecordOutSchema(BaseModel):
    """实时成交流水响应模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键ID")
    strategy_id: int = Field(description="实时策略实例ID")
    mode: str = Field(description="运行模式：paper/live")
    symbol: str = Field(description="交易对")
    side: str = Field(description="方向")
    qty: float = Field(description="成交数量")
    price: float = Field(description="成交价格")
    fee: float = Field(description="手续费")
    pnl: float | None = Field(default=None, description="已实现盈亏")
    reason: str | None = Field(default=None, description="触发原因")
    trade_time: DateTimeStr | None = Field(default=None, description="成交时间")
