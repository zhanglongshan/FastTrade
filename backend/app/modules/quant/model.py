"""量化模块 ORM 模型 | ORM models for the quant module.

定义数据集、回测记录、实时策略实例与成交流水四张表的映射 | Defines mappings for four tables:
datasets, backtest records, live strategy instances and trade records.
"""

from datetime import datetime  # 导入 datetime 类型，用于时间字段的类型标注 | Import datetime type for annotating time columns

from sqlalchemy import Float, Integer, String, Text  # 导入 SQLAlchemy 列类型：浮点/整数/字符串/长文本 | Import SQLAlchemy column types: float/int/string/text
from sqlalchemy.orm import Mapped, mapped_column  # 导入 ORM 类型标注与列构造器 | Import ORM type annotation and column constructor

from app.core.base_model import JSONType, ModelMixin, UserMixin  # 导入 JSON 列类型与基础混入（主键/时间戳/审计字段）| Import JSON column type and base mixins (pk/timestamps/audit fields)


class QuantDatasetModel(ModelMixin, UserMixin):
    """量化数据集：记录本地 CSV 文件及下载范围 | Quant dataset: records a local CSV file and its download range.

    关键属性 | Key attributes:
        name (str): 唯一的数据集名称 | Unique dataset name.
        symbol (str): 交易对，如 BTC/USDT | Trading pair, e.g. BTC/USDT.
        interval (str): K 线周期，如 1h | Candle interval, e.g. 1h.
        file_path (str): 本地 CSV 相对路径 | Local CSV relative path.
        row_count (int): K 线数量 | Number of candles.
        start_time/end_time: 数据起止时间 | Data start/end time.
    """

    __tablename__: str = "quant_web3_dataset"  # 数据库表名 | Database table name
    __table_args__: dict[str, str] = {"comment": "量化数据集表"}  # 表注释元数据 | Table comment metadata

    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, comment="数据集名称")  # 数据集名称，唯一且非空 | Dataset name, unique and non-null
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="交易对，如 BTC/USDT")  # 交易对 | Trading pair
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期，如 1h")  # K 线周期 | Candle interval
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="binance", comment="数据来源")  # 数据来源，默认 binance | Data source, defaults to binance
    market: Mapped[str] = mapped_column(  # 市场类型字段（现货/合约）| Market type field (spot/futures)
        String(10), nullable=False, default="spot", server_default="spot", comment="市场：spot-现货，futures-合约"  # 默认现货，服务端也设默认值 | Defaults to spot, also set as server default
    )  # market 字段结束 | End of market field
    file_path: Mapped[str] = mapped_column(String(500), nullable=False, comment="本地 CSV 相对路径")  # 数据集 CSV 的相对路径 | Relative path to the dataset CSV
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="K线数量")  # K 线行数，默认 0 | Candle row count, defaults to 0
    start_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="数据起始时间")  # 数据起始时间，可空 | Data start time, nullable
    end_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="数据结束时间")  # 数据结束时间，可空 | Data end time, nullable
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="状态：0=禁用，1=启用")  # 状态：0 禁用 1 启用，默认启用 | Status: 0 disabled, 1 enabled; defaults to enabled
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")  # 数据集备注，可空 | Dataset description, nullable


class QuantBacktestModel(ModelMixin, UserMixin):
    """量化回测记录：保存回测参数、状态与结果摘要 | Backtest record: stores parameters, status and result summary.

    关键属性 | Key attributes:
        strategy_name (str): 策略类名 | Strategy class name.
        status (int): 0=运行中 1=成功 2=失败 | 0=running, 1=success, 2=failed.
        result (dict): 回测结果 JSON | Backtest result JSON.
        error (str): 失败原因 | Failure reason.
    """

    __tablename__: str = "quant_web3_backtest"  # 数据库表名 | Database table name
    __table_args__: dict[str, str] = {"comment": "量化回测记录表"}  # 表注释元数据 | Table comment metadata

    name: Mapped[str] = mapped_column(String(150), nullable=False, comment="回测名称")  # 回测名称 | Backtest name
    strategy_name: Mapped[str] = mapped_column(String(150), nullable=False, comment="策略类名")  # 使用的策略类名 | Strategy class name used
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="交易对")  # 交易对 | Trading pair
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期")  # K 线周期 | Candle interval
    market: Mapped[str] = mapped_column(  # 市场类型字段 | Market type field
        String(10), nullable=False, default="spot", server_default="spot", comment="市场：spot-现货，futures-合约"  # 默认现货 | Defaults to spot
    )  # market 字段结束 | End of market field
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0, comment="初始资金")  # 初始资金，默认 10000 | Initial capital, defaults to 10000
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.001, comment="单边手续费率")  # 单边手续费率，默认 0.1% | One-way fee rate, defaults to 0.1%
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="状态：0=运行中，1=成功，2=失败")  # 回测状态，默认运行中 | Backtest status, defaults to running
    result: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="回测结果 JSON")  # 回测结果 JSON，可空 | Backtest result JSON, nullable
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="失败原因")  # 失败原因文本，可空 | Failure reason text, nullable
    started_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="开始时间")  # 回测开始时间 | Backtest start time
    finished_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="结束时间")  # 回测结束时间 | Backtest finish time
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")  # 回测备注 | Backtest description


class QuantLiveStrategyModel(ModelMixin, UserMixin):
    """实时策略实例：模拟盘(paper)与实盘(live)共用，仅执行器不同 | Live strategy instance: paper and live share this model; only the executor differs.

    关键属性 | Key attributes:
        mode (str): paper-模拟盘, live-实盘 | paper=simulated, live=real.
        account (dict): 账户账本快照 | Account ledger snapshot.
        api_key/api_secret (str): 加密存储的币安密钥 | Encrypted Binance credentials.
        last_result/last_error/last_tick_time: 最近一次运行状态 | Latest run status.
    """

    __tablename__: str = "quant_web3_live_strategy"  # 数据库表名 | Database table name
    __table_args__: dict[str, str] = {"comment": "量化实时策略实例表"}  # 表注释元数据 | Table comment metadata

    name: Mapped[str] = mapped_column(String(150), nullable=False, comment="实例名称")  # 实例名称 | Instance name
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default="paper", comment="运行模式：paper-模拟盘，live-实盘")  # 运行模式，默认模拟盘 | Run mode, defaults to paper
    strategy_name: Mapped[str] = mapped_column(String(150), nullable=False, comment="策略类名")  # 策略类名 | Strategy class name
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="交易对，如 BTC/USDT")  # 交易对 | Trading pair
    interval: Mapped[str] = mapped_column(String(10), nullable=False, comment="K线周期，如 1h")  # K 线周期 | Candle interval
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0, comment="初始/虚拟资金")  # 初始或虚拟资金 | Initial/virtual capital
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.001, comment="单边手续费率")  # 单边手续费率 | One-way fee rate
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="滑点比例")  # 滑点比例，默认 0 | Slippage ratio, defaults to 0
    dry_run: Mapped[bool] = mapped_column(nullable=False, default=True, comment="实盘是否模拟运行：True 不下真实单")  # 是否模拟运行，True 不下真实单 | Simulated run; True means no real orders
    allow_short: Mapped[bool] = mapped_column(nullable=False, default=True, comment="是否允许做空（实盘现货建议关闭）")  # 是否允许做空 | Whether shorting is allowed
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="状态：0=停止，1=运行，2=异常")  # 实例状态，默认停止 | Instance status, defaults to stopped
    account: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="账户账本快照 JSON")  # 账户账本快照 | Account ledger snapshot
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="币安 API Key（Fernet 加密）")  # 加密存储的 API Key | Encrypted API key
    api_secret: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="币安 API Secret（Fernet 加密）")  # 加密存储的 API Secret | Encrypted API secret
    last_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True, default=None, comment="最近一次 tick 结果 JSON")  # 最近一次 tick 结果 | Result of the latest tick
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="最近错误")  # 最近一次错误信息 | Latest error message
    last_tick_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="最近运行时间")  # 最近运行时间 | Last run timestamp
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None, comment="备注")  # 实例备注 | Instance description


class QuantTradeRecordModel(ModelMixin, UserMixin):
    """实时成交流水：模拟盘/实盘每次 tick 产生的成交 | Trade record: trades generated by each tick in paper/live mode.

    关键属性 | Key attributes:
        strategy_id (int): 关联的实时策略实例 ID | Related live strategy instance ID.
        side (str): buy/sell/sellshort/buycover | Trade direction.
        qty/price/fee: 数量/价格/手续费 | Quantity/price/fee.
        pnl (float): 已实现盈亏（仅平仓单）| Realized pnl (closed trades only).
    """

    __tablename__: str = "quant_web3_trade_record"  # 数据库表名 | Database table name
    __table_args__: dict[str, str] = {"comment": "量化实时成交流水表"}  # 表注释元数据 | Table comment metadata

    strategy_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="实时策略实例ID")  # 关联策略实例 ID，建索引加速查询 | Related strategy instance ID, indexed for fast queries
    mode: Mapped[str] = mapped_column(String(10), nullable=False, comment="运行模式：paper/live")  # 运行模式 | Run mode
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, comment="交易对")  # 交易对 | Trading pair
    side: Mapped[str] = mapped_column(String(20), nullable=False, comment="方向：buy/sell/sellshort/buycover")  # 交易方向 | Trade direction
    qty: Mapped[float] = mapped_column(Float, nullable=False, comment="成交数量")  # 成交数量 | Filled quantity
    price: Mapped[float] = mapped_column(Float, nullable=False, comment="成交价格")  # 成交价格 | Filled price
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="手续费")  # 手续费，默认 0 | Fee, defaults to 0
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True, default=None, comment="已实现盈亏（平仓单）")  # 已实现盈亏，仅平仓单有值 | Realized pnl, only set for closing trades
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None, comment="触发原因")  # 触发原因（如 entry_long）| Trigger reason (e.g. entry_long)
    trade_time: Mapped[datetime | None] = mapped_column(nullable=True, comment="成交时间")  # 成交时间 | Trade timestamp
