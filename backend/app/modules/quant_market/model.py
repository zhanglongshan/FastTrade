"""量化市场 ORM 模型：A股（quant_astock_*）与期货（quant_futures_*）。

两套表结构与 web3 量化一致，仅表名前缀不同。字段直接声明而非动态生成，
便于 Alembic autogenerate 与 IDE 类型识别。
"""

from datetime import datetime

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import JSONType, ModelMixin, UserMixin


class AStockDatasetModel(ModelMixin, UserMixin):
    """A 股数据集。"""

    __tablename__: str = "quant_astock_dataset"
    __table_args__: dict[str, str] = {"comment": "A股量化数据集表"}

    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, comment="数据集名称")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="证券代码，如 600519.SH")
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期")
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="local", comment="数据来源")
    file_path: Mapped[str] = mapped_column(String(500), nullable=False, comment="本地 CSV 相对路径")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="K线数量")
    start_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="数据起始时间")
    end_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="数据结束时间")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="状态：0=禁用，1=启用")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")


class AStockBacktestModel(ModelMixin, UserMixin):
    """A 股回测记录。"""

    __tablename__: str = "quant_astock_backtest"
    __table_args__: dict[str, str] = {"comment": "A股量化回测记录表"}

    name: Mapped[str] = mapped_column(String(150), nullable=False, comment="回测名称")
    strategy_name: Mapped[str] = mapped_column(String(150), nullable=False, comment="策略类名")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="证券代码")
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期")
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0, comment="初始资金")
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.001, comment="单边手续费率")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="状态：0=运行中，1=成功，2=失败")
    result: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="回测结果 JSON")
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="失败原因")
    started_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="开始时间")
    finished_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="结束时间")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")


class AStockLiveStrategyModel(ModelMixin, UserMixin):
    """A 股实时策略实例（模拟盘/实盘共用）。"""

    __tablename__: str = "quant_astock_live_strategy"
    __table_args__: dict[str, str] = {"comment": "A股量化实时策略实例表"}

    name: Mapped[str] = mapped_column(String(150), nullable=False, comment="实例名称")
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default="paper", comment="运行模式：paper/live")
    strategy_name: Mapped[str] = mapped_column(String(150), nullable=False, comment="策略类名")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="证券代码")
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期")
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0, comment="初始/虚拟资金")
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.001, comment="单边手续费率")
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="滑点比例")
    dry_run: Mapped[bool] = mapped_column(nullable=False, default=True, comment="实盘是否模拟运行")
    allow_short: Mapped[bool] = mapped_column(nullable=False, default=False, comment="是否允许做空（A股默认关闭）")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="状态：0=停止，1=运行，2=异常")
    account: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="账户账本快照")
    broker_config: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="柜台配置（XTP 参数，非密钥明文）")
    last_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="最近一次 tick 结果")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="最近错误")
    last_tick_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="最近运行时间")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")


class AStockTradeRecordModel(ModelMixin, UserMixin):
    """A 股实时成交流水。"""

    __tablename__: str = "quant_astock_trade_record"
    __table_args__: dict[str, str] = {"comment": "A股量化成交流水表"}

    strategy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="实时策略实例ID")
    mode: Mapped[str] = mapped_column(String(10), nullable=False, comment="运行模式：paper/live")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="证券代码")
    side: Mapped[str] = mapped_column(String(20), nullable=False, comment="方向")
    qty: Mapped[float] = mapped_column(Float, nullable=False, comment="成交数量")
    price: Mapped[float] = mapped_column(Float, nullable=False, comment="成交价格")
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="手续费")
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True, default=None, comment="已实现盈亏")
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="触发原因")
    trade_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="成交时间")


class FuturesDatasetModel(ModelMixin, UserMixin):
    """期货数据集。"""

    __tablename__: str = "quant_futures_dataset"
    __table_args__: dict[str, str] = {"comment": "期货量化数据集表"}

    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, comment="数据集名称")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="合约代码，如 rb2410.SHFE")
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期")
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="local", comment="数据来源")
    file_path: Mapped[str] = mapped_column(String(500), nullable=False, comment="本地 CSV 相对路径")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="K线数量")
    start_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="数据起始时间")
    end_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="数据结束时间")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="状态：0=禁用，1=启用")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")


class FuturesBacktestModel(ModelMixin, UserMixin):
    """期货回测记录。"""

    __tablename__: str = "quant_futures_backtest"
    __table_args__: dict[str, str] = {"comment": "期货量化回测记录表"}

    name: Mapped[str] = mapped_column(String(150), nullable=False, comment="回测名称")
    strategy_name: Mapped[str] = mapped_column(String(150), nullable=False, comment="策略类名")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="合约代码")
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期")
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0, comment="初始资金")
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.001, comment="单边手续费率")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="状态：0=运行中，1=成功，2=失败")
    result: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="回测结果 JSON")
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="失败原因")
    started_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="开始时间")
    finished_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="结束时间")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")


class FuturesLiveStrategyModel(ModelMixin, UserMixin):
    """期货实时策略实例（模拟盘/实盘共用）。"""

    __tablename__: str = "quant_futures_live_strategy"
    __table_args__: dict[str, str] = {"comment": "期货量化实时策略实例表"}

    name: Mapped[str] = mapped_column(String(150), nullable=False, comment="实例名称")
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default="paper", comment="运行模式：paper/live")
    strategy_name: Mapped[str] = mapped_column(String(150), nullable=False, comment="策略类名")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="合约代码")
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期")
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0, comment="初始/虚拟资金")
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.001, comment="单边手续费率")
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="滑点比例")
    dry_run: Mapped[bool] = mapped_column(nullable=False, default=True, comment="实盘是否模拟运行")
    allow_short: Mapped[bool] = mapped_column(nullable=False, default=True, comment="是否允许做空（期货默认允许）")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="状态：0=停止，1=运行，2=异常")
    account: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="账户账本快照")
    broker_config: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="柜台配置（CTP 参数）")
    last_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="最近一次 tick 结果")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="最近错误")
    last_tick_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="最近运行时间")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")


class FuturesTradeRecordModel(ModelMixin, UserMixin):
    """期货实时成交流水。"""

    __tablename__: str = "quant_futures_trade_record"
    __table_args__: dict[str, str] = {"comment": "期货量化成交流水表"}

    strategy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="实时策略实例ID")
    mode: Mapped[str] = mapped_column(String(10), nullable=False, comment="运行模式：paper/live")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="合约代码")
    side: Mapped[str] = mapped_column(String(20), nullable=False, comment="方向")
    qty: Mapped[float] = mapped_column(Float, nullable=False, comment="成交数量")
    price: Mapped[float] = mapped_column(Float, nullable=False, comment="成交价格")
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="手续费")
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True, default=None, comment="已实现盈亏")
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="触发原因")
    trade_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="成交时间")


MARKET_MODELS = {
    "astock": {
        "dataset": AStockDatasetModel,
        "backtest": AStockBacktestModel,
        "live": AStockLiveStrategyModel,
        "trade": AStockTradeRecordModel,
    },
    "futures": {
        "dataset": FuturesDatasetModel,
        "backtest": FuturesBacktestModel,
        "live": FuturesLiveStrategyModel,
        "trade": FuturesTradeRecordModel,
    },
}
