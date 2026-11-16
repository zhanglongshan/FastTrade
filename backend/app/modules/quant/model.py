"""量化模块 ORM 模型。"""

from datetime import datetime

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base_model import JSONType, ModelMixin, UserMixin


class QuantDatasetModel(ModelMixin, UserMixin):
    """量化数据集：记录本地 CSV 文件及下载范围。"""

    __tablename__: str = "quant_dataset"
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

    __tablename__: str = "quant_backtest"
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
