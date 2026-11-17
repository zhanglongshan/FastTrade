"""A股/期货量化模块的 Pydantic 模型。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MarketImportSchema(BaseModel):
    """导入本地 CSV 到指定市场。"""

    file_path: str = Field(description="本地 CSV 路径（需在允许目录内）")
    symbol: str = Field(description="证券/合约代码，如 600519.SH 或 rb2410.SHFE")
    interval: str = Field(description="K线周期，如 1d")
    source: str = Field(default="local", description="数据来源标识")
    description: str | None = Field(default=None, description="备注")


class MarketDatasetOutSchema(BaseModel):
    """数据集响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="主键ID")
    name: str = Field(description="数据集名称")
    symbol: str = Field(description="证券/合约代码")
    interval: str = Field(description="K线周期")
    source: str = Field(description="数据来源")
    file_path: str = Field(description="本地 CSV 相对路径")
    row_count: int = Field(description="K线数量")
    start_time: str | None = Field(default=None, description="起始时间")
    end_time: str | None = Field(default=None, description="结束时间")
    status: int = Field(description="状态")
    description: str | None = Field(default=None, description="备注")
    created_time: str | None = Field(default=None, description="创建时间")


class MarketLocalFileSchema(BaseModel):
    """可导入的本地 CSV 文件。"""

    path: str
    name: str
    size: int
    modified_time: str | None = None


class MarketCandleSchema(BaseModel):
    """单根 K 线。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketCandlesOutSchema(BaseModel):
    """K 线响应。"""

    dataset_id: int
    name: str
    symbol: str
    interval: str
    total: int
    candles: list[MarketCandleSchema]


class MarketBacktestCreateSchema(BaseModel):
    """创建回测。"""

    dataset_id: int = Field(description="数据集ID")
    strategy_name: str = Field(description="策略类名")
    name: str | None = Field(default=None, description="回测名称")
    initial_cash: float = Field(default=10000.0, gt=0, description="初始资金")
    fee_rate: float = Field(default=0.001, ge=0, le=0.1, description="单边手续费率")
    slippage: float = Field(default=0.0, ge=0, le=0.1, description="滑点比例")
    description: str | None = Field(default=None, description="备注")


class MarketBacktestOutSchema(BaseModel):
    """回测记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    strategy_name: str
    symbol: str
    interval: str
    initial_cash: float
    fee_rate: float
    status: int
    result: dict[str, Any] | None = None
    error: str | None = None
    started_time: str | None = None
    finished_time: str | None = None
    created_time: str | None = None


class MarketLiveCreateSchema(BaseModel):
    """创建实时策略实例（模拟盘/实盘共用）。"""

    name: str = Field(min_length=1, max_length=150, description="实例名称")
    mode: Literal["paper", "live"] = Field(default="paper", description="运行模式")
    strategy_name: str = Field(description="策略类名")
    symbol: str = Field(description="证券/合约代码")
    interval: str = Field(description="K线周期")
    initial_cash: float = Field(default=10000.0, gt=0, description="初始/虚拟资金")
    fee_rate: float = Field(default=0.001, ge=0, le=0.1, description="手续费率")
    slippage: float = Field(default=0.0, ge=0, le=0.1, description="滑点比例")
    dry_run: bool = Field(default=True, description="实盘是否模拟运行")
    allow_short: bool = Field(default=False, description="是否允许做空")
    broker_config: dict[str, Any] | None = Field(default=None, description="柜台配置（如 prices 初始价）")
    description: str | None = Field(default=None, description="备注")


class MarketLiveOutSchema(BaseModel):
    """实时策略实例响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    mode: str
    strategy_name: str
    symbol: str
    interval: str
    initial_cash: float
    fee_rate: float
    slippage: float
    dry_run: bool
    allow_short: bool
    status: int
    account: dict[str, Any] | None = None
    last_result: dict[str, Any] | None = None
    last_error: str | None = None
    last_tick_time: str | None = None
    description: str | None = None
    created_time: str | None = None


class MarketTickSchema(BaseModel):
    """手动 tick 参数。"""

    price: float | None = Field(default=None, gt=0, description="当前价格；不传则用柜台最新价")
    candles_file: str | None = Field(default=None, description="离线 CSV 路径（相对项目根）")
    reason: str | None = Field(default=None, description="触发原因")
