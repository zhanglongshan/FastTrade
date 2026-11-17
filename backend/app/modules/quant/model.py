"""量化模块 ORM 模型。"""

from datetime import datetime

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import JSONType, ModelMixin, UserMixin


class QuantDatasetModel(ModelMixin, UserMixin):
    """量化数据集：记录本地 CSV 文件及下载范围。"""

    __tablename__: str = "quant_web3_dataset"
    __table_args__: dict[str, str] = {"comment": "量化数据集表"}

    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, comment="数据集名称")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="交易对，如 BTC/USDT")
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期，如 1h")
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="binance", comment="数据来源")
    file_path: Mapped[str] = mapped_column(String(500), nullable=False, comment="本地 CSV 相对路径")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="K线数量")
    start_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="数据起始时间")
    end_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="数据结束时间")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="状态：0=禁用，1=启用")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")


class QuantBacktestModel(ModelMixin, UserMixin):
    """量化回测记录：保存回测参数、状态与结果摘要。"""

    __tablename__: str = "quant_web3_backtest"
    __table_args__: dict[str, str] = {"comment": "量化回测记录表"}

    name: Mapped[str] = mapped_column(String(150), nullable=False, comment="回测名称")
    strategy_name: Mapped[str] = mapped_column(String(150), nullable=False, comment="策略类名")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="交易对")
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期")
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0, comment="初始资金")
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.001, comment="单边手续费率")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="状态：0=运行中，1=成功，2=失败")
    result: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="回测结果 JSON")
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="失败原因")
    started_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="开始时间")
    finished_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="结束时间")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")


class QuantLiveStrategyModel(ModelMixin, UserMixin):
    """实时策略实例：模拟盘(paper)与实盘(live)共用，仅执行器不同。"""

    __tablename__: str = "quant_web3_live_strategy"
    __table_args__: dict[str, str] = {"comment": "量化实时策略实例表"}

    name: Mapped[str] = mapped_column(String(150), nullable=False, comment="实例名称")
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default="paper", comment="运行模式：paper-模拟盘，live-实盘")
    strategy_name: Mapped[str] = mapped_column(String(150), nullable=False, comment="策略类名")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="交易对，如 BTC/USDT")
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期，如 1h")
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0, comment="初始/虚拟资金")
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.001, comment="单边手续费率")
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="滑点比例")
    dry_run: Mapped[bool] = mapped_column(nullable=False, default=True, comment="实盘是否模拟运行：True 不下真实单")
    allow_short: Mapped[bool] = mapped_column(nullable=False, default=True, comment="是否允许做空（实盘现货建议关闭）")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="状态：0=停止，1=运行，2=异常")
    account: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="账户账本快照 JSON")
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="币安 API Key（Fernet 加密）")
    api_secret: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="币安 API Secret（Fernet 加密）")
    last_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="最近一次 tick 结果 JSON")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="最近错误")
    last_tick_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="最近运行时间")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")


class QuantTradeRecordModel(ModelMixin, UserMixin):
    """实时成交流水：模拟盘/实盘每次 tick 产生的成交。"""

    __tablename__: str = "quant_web3_trade_record"
    __table_args__: dict[str, str] = {"comment": "量化实时成交流水表"}

    strategy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="实时策略实例ID")
    mode: Mapped[str] = mapped_column(String(10), nullable=False, comment="运行模式：paper/live")
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="交易对")
    side: Mapped[str] = mapped_column(String(20), nullable=False, comment="方向：buy/sell/sellshort/buycover")
    qty: Mapped[float] = mapped_column(Float, nullable=False, comment="成交数量")
    price: Mapped[float] = mapped_column(Float, nullable=False, comment="成交价格")
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="手续费")
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True, default=None, comment="已实现盈亏（平仓单）")
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None, comment="触发原因")
    trade_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="成交时间")
