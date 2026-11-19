"""量化策略 API 封装 | Quantitative strategy API wrapper.

目的 | Purpose:
    提供策略开发所需的全部接口：K 线/成交/订单数据结构、策略基类，以及供策略下单、
    查询账户与调用指标的 TradeAPI。策略通过 api.buy() / api.sell() / api.sellshort() /
    api.buycover() / api.close_all() 下单。
    Provides everything needed for strategy development: candle/trade/order data structures,
    the strategy base class, and TradeAPI for placing orders, querying the account and calling
    indicators. Strategies place orders via api.buy() / api.sell() / api.sellshort() /
    api.buycover() / api.close_all().

成交时序 | Fill timing:
    订单在当前 K 线收盘产生，于下一根 K 线开盘成交（最后一根 K 线按收盘价成交）。
    Orders are generated at the current candle's close and filled at the next candle's open
    (the last candle is filled at its close).

记账口径 | Accounting:
    TradeAPI 自身不再维护现金/持仓，全部委托给 :class:`~app.modules.quant.execution.base.AccountLedger`，
    与模拟盘/实盘共用同一份撮合数学，避免两套实现漂移。
    TradeAPI no longer keeps its own cash/positions; everything is delegated to AccountLedger,
    which is also used by paper/live, so both modes share one matching implementation.
"""

from dataclasses import dataclass  # 导入 dataclass 装饰器 | Import dataclass decorator for immutable data containers
from typing import Any  # 导入 Any 类型 | Import Any type for loosely-typed dictionaries

from ..execution.base import AccountLedger, Fill, OrderRequest  # 导入统一账本/成交/订单请求 | Import the shared ledger, fill and order request
from ..execution.risk import RiskSettings  # 导入风控配置 | Import risk settings
from .indicators import ema, rsi, sma  # 导入 SMA/EMA/RSI 指标 | Import SMA/EMA/RSI indicator functions
from .trading_base import can_add_position, sizing_qty  # 导入共享决策基元 | Import shared trading primitives


@dataclass(frozen=True)  # 冻结数据类：创建后不可变，保证 K 线不被策略篡改 | Frozen dataclass: immutable after creation to prevent strategies from mutating candles
class Candle:
    """单根 K 线（OHLCV）| A single OHLCV candlestick.

    目的 | Purpose:
        以不可变对象表示一根 K 线的行情数据，含日期、开高低收与成交量。
        Represent one candle's market data as an immutable object: date, open/high/low/close and volume.

    关键属性 | Key attributes:
        - date: 日期字符串 | Date string.
        - open/high/low/close: 开/高/低/收盘价 | Open/high/low/close price.
        - volume: 成交量 | Trading volume.
    """

    date: str  # 日期标签 | Date label
    open: float  # 开盘价 | Opening price
    high: float  # 最高价 | Highest price
    low: float  # 最低价 | Lowest price
    close: float  # 收盘价 | Closing price
    volume: float  # 成交量 | Trading volume


@dataclass  # 可变数据类：订单在撮合前可能被处理 | Mutable dataclass: orders may be processed before matching
class Order:
    """待执行订单 | A pending order.

    目的 | Purpose:
        记录策略产生的下单意图，等待下一根 K 线撮合执行。
        Record an order intent produced by a strategy, awaiting execution on the next candle.

    关键属性 | Key attributes:
        - side: 买卖方向 | Order side.
        - qty: 数量 | Quantity.
        - signal_price: 发单时基准价（当前收盘价）| Signal price (current close) at order time.
        - reason: 下单原因 | Reason for the order.
    """

    side: str  # 交易方向：buy/sell/sellshort/buycover | Trade direction: buy/sell/sellshort/buycover
    qty: float  # 委托数量（按信号价预估，开仓单会按成交价重算）| Ordered quantity (pre-estimated at signal price; entry orders are re-sized at fill price)
    signal_price: float  # 信号基准价 | Signal reference price
    reason: str = ""  # 下单原因（默认为空）| Order reason (defaults to empty)
    # 开仓资金比例：>0 表示"按比例开仓"，撮合时会用成交价重算数量，
    # 使回测与实盘的仓位规模采用同一计价基准（成交价），避免回测超配。
    # Entry ratio: when >0 the quantity is re-sized at the fill price, so the backtest
    # and the live engine size positions on the same basis (the actual fill price).
    qty_ratio: float = 0.0  # 开仓比例，0 表示使用固定 qty | Entry ratio; 0 means use the fixed qty


@dataclass  # 可变数据类：成交记录逐笔追加 | Mutable dataclass: trade records are appended one by one
class Trade:
    """成交记录 | A filled trade record.

    目的 | Purpose:
        保存一笔已撮合成交的完整信息，含价格、费用与已实现盈亏。
        Store full information of a matched fill, including price, fee and realized PnL.

    关键属性 | Key attributes:
        - date: 成交日期 | Fill date.
        - side: 交易方向 | Trade side.
        - qty: 成交数量 | Filled quantity.
        - price: 实际成交价（含滑点）| Actual fill price (with slippage).
        - fee: 手续费 | Transaction fee.
        - pnl: 已实现盈亏（开仓为 None）| Realized PnL (None when opening a position).
        - reason: 成交原因 | Fill reason.
        - signal_price: 发单基准价 | Signal reference price.
    """

    date: str  # 成交日期 | Fill date
    side: str  # 交易方向 | Trade side
    qty: float  # 成交数量 | Filled quantity
    price: float  # 成交价格 | Fill price
    fee: float  # 手续费金额 | Fee amount
    pnl: float | None  # 已实现盈亏，开仓为 None | Realized PnL, None for position-opening trades
    reason: str  # 成交原因 | Fill reason
    signal_price: float  # 发单时的信号价格 | Signal price at order time


class Strategy:
    """策略基类 | Strategy base class.

    目的 | Purpose:
        所有用户策略的父类，定义子类需实现的回调契约与元信息字段。
        Base class for all user strategies, defining the callback contract and metadata fields
        that subclasses should implement.

    两种用法 | Two usage modes:
        - 事件模式 | Event mode: 子类实现 on_bar(api, candles, index) | Subclass implements on_bar(api, candles, index).
        - 信号模式 | Signal mode: 子类实现 populate_entry_trend / populate_exit_trend，参考 freqtrade 的信号列思想 | Subclass implements populate_entry_trend / populate_exit_trend, following freqtrade's signal-column idea.

    关键属性 | Key attributes:
        - name: 策略名称 | Strategy name.
        - timeframe: 周期 | Timeframe.
        - startup_candle_count: 预热 K 线数量 | Number of warm-up candles.
        - stoploss: 固定止损比例，0.05 表示 -5%，0 表示关闭 | Fixed stoploss ratio, 0 disables it.
        - trailing_stop: 移动止损比例，0 表示关闭 | Trailing stop ratio, 0 disables it.
        - max_drawdown: 净值回撤熔断阈值，0 表示关闭 | Equity drawdown breaker, 0 disables it.
        - allow_position_add: 是否允许同一信号反复加仓，默认 False | Whether pyramiding is allowed, default False.
    """

    name: str = ""  # 策略名称（默认为空）| Strategy name (defaults to empty)
    timeframe: str = ""  # 交易周期（默认为空）| Trading timeframe (defaults to empty)
    startup_candle_count: int = 0  # 指标预热所需 K 线数 | Candles needed for indicator warm-up
    # 风控契约：由引擎读取，回测与模拟盘/实盘自动生效，无需改数据库
    # Risk contract: read by the engine and applied identically in backtest and paper/live.
    stoploss: float = 0.0  # 固定止损比例，0 关闭 | Fixed stoploss ratio, 0 disables
    trailing_stop: float = 0.0  # 移动止损比例，0 关闭 | Trailing stop ratio, 0 disables
    max_drawdown: float = 0.0  # 净值回撤熔断阈值，0 关闭 | Drawdown breaker threshold, 0 disables
    allow_position_add: bool = False  # 是否允许重复开仓加仓 | Whether repeated entries are allowed

    def on_bar(self, api: "TradeAPI", candles: list[Candle], index: int) -> None:  # 每根 K 线回调 | Per-candle callback
        """逐根 K 线回调；默认空实现 | Per-candle callback; default no-op.

        Purpose:
            事件模式下由回测引擎在每根 K 线收盘时调用，子类在此下单。
            Called by the backtest engine at each candle's close in event mode; subclasses place orders here.
        Parameters:
            - api: 交易 API 实例 | Trading API instance.
            - candles: 截至当前的 K 线列表 | Candle list up to the current bar.
            - index: 当前 K 线下标 | Current candle index.
        Returns:
            None。
        Flow:
            默认直接返回 None，子类覆盖以编写策略逻辑 | Default returns None; subclasses override with strategy logic.
        """
        return None  # 空实现，返回 None | No-op implementation, returns None

    def populate_entry_trend(self, candles: list[Candle], metadata: dict[str, Any]) -> list[int]:  # 生成开仓信号 | Generate entry signals
        """生成开仓信号列表（1=开多，-1=开空，0=无信号）；默认空实现。| Generate entry signal list (1=long, -1=short, 0=none); default no-op.

        Purpose:
            信号模式下由引擎调用，为每根 K 线给出开仓信号。
            Called by the engine in signal mode to give an entry signal for each candle.
        Parameters:
            - candles: 完整 K 线列表 | Full candle list.
            - metadata: 附加元数据字典 | Additional metadata dictionary.
        Returns:
            list[int]: 与 K 线等长的信号列表 | Signal list aligned with candles.
        Flow:
            默认返回空列表，子类覆盖填充信号 | Default returns an empty list; subclasses override to fill signals.
        """
        return []  # 空实现，返回空列表 | No-op implementation, returns an empty list

    def populate_exit_trend(self, candles: list[Candle], metadata: dict[str, Any]) -> list[int]:  # 生成平仓信号 | Generate exit signals
        """生成平仓信号列表（1=平多，-1=平空，0=无信号）；默认空实现。| Generate exit signal list (1=exit long, -1=exit short, 0=none); default no-op.

        Purpose:
            信号模式下由引擎调用，为每根 K 线给出平仓信号。
            Called by the engine in signal mode to give an exit signal for each candle.
        Parameters:
            - candles: 完整 K 线列表 | Full candle list.
            - metadata: 附加元数据字典 | Additional metadata dictionary.
        Returns:
            list[int]: 与 K 线等长的信号列表 | Signal list aligned with candles.
        Flow:
            默认返回空列表，子类覆盖填充信号 | Default returns an empty list; subclasses override to fill signals.
        """
        return []  # 空实现，返回空列表 | No-op implementation, returns an empty list


class TradeAPI:
    """回测用交易与指标接口（撮合延迟到下一根开盘）| Backtest trading/indicator API (fills deferred to next open).

    目的 | Purpose:
        向上给策略提供与实盘一致的 buy/sell/sellshort/buycover/close_all 接口；向下把
        撮合与记账全部委托给 :class:`~app.modules.quant.execution.base.AccountLedger`，
        从而使回测与模拟盘/实盘的成交数学完全一致。
        Expose the same buy/sell/sellshort/buycover/close_all interface as live mode, while
        delegating all matching and bookkeeping to AccountLedger so backtest and paper/live
        share identical fill math.

    关键属性 | Key attributes:
        - cash/long_qty/short_qty/long_cost/short_proceeds: 账本状态的只读代理 | Read-only proxies of ledger state.
        - trades/logs/equity_curve: 成交、日志与净值曲线 | Trades, logs and equity curve.
        - _pending: 待下一根 K 线撮合的订单队列 | Orders awaiting the next candle open.
        - _index: 当前 K 线下标 | Current candle index.
        - allow_add: 是否允许同一信号重复开仓 | Whether repeated entries are allowed.
    """

    def __init__(
        self,  # 实例自身 | The instance itself
        candles: list[Candle],  # 完整 K 线序列 | Full candle series
        initial_cash: float,  # 初始资金 | Initial capital
        fee_rate: float = 0.001,  # 单边手续费率，0.001=0.1% | One-way fee rate, 0.001 = 0.1%
        slippage: float = 0.0,  # 滑点比例，0.001=0.1% | Slippage ratio, 0.001 = 0.1%
        allow_short: bool = True,  # 是否允许开空 | Whether shorting is allowed
        *,  # 强制后续参数为关键字参数 | Force following params to be keyword-only
        risk: RiskSettings | None = None,  # 风控配置 | Risk settings
        allow_add: bool = False,  # 是否允许重复开仓加仓 | Whether repeated entries are allowed
        leverage: float = 1.0,  # 杠杆倍数，1 表示现货全额，>1 表示合约保证金 | Leverage; 1 is spot, >1 is margin
    ) -> None:
        """初始化账户 | Initialize the account.

        Purpose:
            设置账户初始资金、费率与 K 线数据，并初始化持仓、成交、日志等状态。
            Set initial capital, fee rates and candle data, and initialize position, trade and log state.
        Parameters:
            - candles: 完整 K 线序列 | Full candle series.
            - initial_cash: 初始资金 | Initial capital.
            - fee_rate: 单边手续费率，0.001 表示 0.1% | One-way fee rate, 0.001 means 0.1%.
            - slippage: 滑点比例，0.001 表示 0.1% | Slippage ratio, 0.001 means 0.1%.
            - allow_short: 是否允许开空；现货回测传 False，策略调用 sellshort 会被拒绝 | Whether shorting is allowed; pass False for spot backtests and sellshort calls will be rejected.
            - risk: 风控配置，None 表示关闭 | Risk settings, None disables risk.
            - allow_add: 是否允许同一信号重复开仓 | Whether repeated entries are allowed.
            - leverage: 杠杆倍数；1 表示全额（现货口径），>1 时按保证金记账并可强平
              | Leverage; 1 means fully funded (spot), >1 books margin and enables liquidation.
        Returns:
            None。
        Flow:
            保存配置 → 构造统一账本 → 初始化成交/日志/净值/待办队列 | Save config → build the shared ledger → init trades/logs/equity/pending.
        """
        self.candles = candles  # 保存完整 K 线序列 | Store full candle series
        self.initial_cash = initial_cash  # 保存初始资金（用于绩效计算）| Store initial capital for performance calc
        self.allow_short = allow_short  # 是否允许做空标志 | Shorting-allowed flag
        self.allow_add = allow_add  # 是否允许加仓标志 | Pyramiding flag
        # 统一账本：现金/持仓/成本/盈亏/风控/杠杆状态全部由它维护
        # Shared ledger owning cash, positions, cost, PnL, risk state and leverage.
        self.ledger = AccountLedger(initial_cash, fee_rate=fee_rate, slippage=slippage, risk=risk, leverage=leverage)  # 构造统一账本 | Build the shared ledger

        self.trades: list[Trade] = []  # 成交记录列表 | List of filled trades
        self.logs: list[str] = []  # 策略日志列表 | List of strategy log messages
        self.equity_curve: list[dict[str, Any]] = []  # 净值曲线（每根 K 线一个快照）| Equity curve (one snapshot per candle)
        self._pending: list[Order] = []  # 待下一根 K 线撮合的订单队列 | Order queue awaiting next-candle matching
        self._index = 0  # 当前 K 线下标，初始为 0 | Current candle index, initially 0
        # 交易所最小名义金额：回测默认 0（不约束），由服务层按交易对过滤器注入。
        # 设成 0 时不改变任何既有结果；设成真实 minNotional 后，回测才能复现实盘"小单被拒"。
        # Exchange minimum notional: 0 (unconstrained) by default, injected by the service layer from
        # the symbol filters. Zero keeps legacy results untouched; the real value reproduces the
        # live venue rejecting dust orders.
        self.min_notional = 0.0  # 最小名义金额（计价币）| Minimum notional in the quote asset

    # ------------------------------------------------------------------
    # 账本状态只读代理：保持 api.cash / api.long_qty 等既有用法不变
    # Read-only ledger proxies so existing api.cash / api.long_qty usage keeps working.
    # ------------------------------------------------------------------
    @property  # 只读属性 | Read-only property
    def cash(self) -> float:  # 可用现金 | Available cash
        """可用现金（代理账本）| Available cash (ledger proxy)."""
        return self.ledger.cash  # 读取账本现金 | Read ledger cash

    @property  # 只读属性 | Read-only property
    def long_qty(self) -> float:  # 多头持仓数量 | Long position quantity
        """多头持仓数量（代理账本）| Long quantity (ledger proxy)."""
        return self.ledger.long_qty  # 读取账本多头持仓 | Read ledger long quantity

    @property  # 只读属性 | Read-only property
    def short_qty(self) -> float:  # 空头持仓数量 | Short position quantity
        """空头持仓数量（代理账本）| Short quantity (ledger proxy)."""
        return self.ledger.short_qty  # 读取账本空头持仓 | Read ledger short quantity

    @property  # 只读属性 | Read-only property
    def long_cost(self) -> float:  # 多头均价成本 | Average long cost
        """多头均价成本（代理账本）| Average long cost (ledger proxy)."""
        return self.ledger.long_cost  # 读取账本多头成本 | Read ledger long cost

    @property  # 只读属性 | Read-only property
    def short_proceeds(self) -> float:  # 空头均价所得 | Average short proceeds
        """空头均价所得（代理账本）| Average short proceeds (ledger proxy)."""
        return self.ledger.short_proceeds  # 读取账本空头所得 | Read ledger short proceeds

    @property  # 只读属性 | Read-only property
    def fee_rate(self) -> float:  # 手续费率 | Fee rate
        """单边手续费率（代理账本）| One-way fee rate (ledger proxy)."""
        return self.ledger.fee_rate  # 读取账本费率 | Read ledger fee rate

    @property  # 只读属性 | Read-only property
    def slippage(self) -> float:  # 滑点比例 | Slippage ratio
        """滑点比例（代理账本）| Slippage ratio (ledger proxy)."""
        return self.ledger.slippage  # 读取账本滑点 | Read ledger slippage

    @property  # 只读属性 | Read-only property
    def halted(self) -> bool:  # 是否已触发回撤熔断 | Whether the drawdown breaker tripped
        """是否已触发回撤熔断（代理账本）| Whether halted by the drawdown breaker."""
        return self.ledger.halted  # 读取账本熔断标志 | Read the ledger halt flag

    @property  # 只读属性 | Read-only property
    def leverage(self) -> float:  # 杠杆倍数 | Leverage multiple
        """当前杠杆倍数（代理账本，1 表示现货）| Current leverage (ledger proxy; 1 means spot)."""
        return self.ledger.leverage  # 读取账本杠杆 | Read the ledger leverage

    @property  # 只读属性 | Read-only property
    def margin_used(self) -> float:  # 已占用保证金 | Used margin
        """已占用保证金（代理账本）| Locked margin (ledger proxy)."""
        return self.ledger.margin_used()  # 名义本金除以杠杆 | Notional divided by leverage

    @property  # 只读属性 | Read-only property
    def liquidation_price(self) -> float:  # 理论强平价 | Theoretical liquidation price
        """理论强平价，0 表示不适用（现货或无持仓）| Liquidation price, 0 when not applicable.

        目的 | Purpose:
            策略可据此提前减仓，避免被强平。
            Strategies can de-risk before the position is force-closed.
        """
        return self.ledger.liquidation_price()  # 委托账本计算 | Delegate to the ledger

    def set_index(self, index: int) -> None:  # 设置当前 K 线下标 | Set current candle index
        """设置当前 K 线下标 | Set the current candle index.

        Purpose:
            将引擎的当前 K 线下标写入 API，限制在合法范围内防止越界。
            Store the engine's current candle index into the API, clamped to a valid range.
        Parameters:
            - index: 目标下标 | Target index.
        Returns:
            None。
        Flow:
            用 min 限制上界、max 限制下界后赋值 | Clamp upper bound with min and lower bound with max before assigning.
        """
        # 将下标裁剪到 [0, len(candles)-1] 区间 | Clamp index into [0, len(candles)-1]
        self._index = max(0, min(index, len(self.candles) - 1))  # 写入裁剪后的下标 | Assign the clamped index

    @property  # 只读属性 | Read-only property
    def current_candle(self) -> Candle:  # 当前 K 线 | Current candle
        """当前 K 线 | Current candle.

        Purpose:
            返回下标 _index 对应的 K 线对象。
            Return the candle object at index _index.
        Parameters:
            无 | None.
        Returns:
            Candle: 当前 K 线 | The current candle.
        Flow:
            直接按 _index 索引 candles | Directly index candles by _index.
        """
        return self.candles[self._index]  # 返回当前下标的 K 线 | Return candle at current index

    @property  # 只读属性 | Read-only property
    def current_price(self) -> float:  # 当前收盘价 | Current closing price
        """当前收盘价，作为下单定价基准 | Current closing price, used as the pricing baseline for orders.

        Purpose:
            提供策略下单时的信号价格基准。
            Provide the signal price baseline when strategies place orders.
        Parameters:
            无 | None.
        Returns:
            float: 当前 K 线收盘价 | Current candle's close price.
        Flow:
            读取当前 K 线的 close 字段 | Read the close field of the current candle.
        """
        return self.current_candle.close  # 返回当前 K 线收盘价 | Return current candle's close price

    @property  # 只读属性 | Read-only property
    def bar_close(self) -> float:  # 信号 K 线收盘价 | Signal bar close
        """当前信号 K 线收盘价（与实盘 ``bar_close`` 同义）| Current signal bar close.

        目的 | Purpose:
            实盘的 ``current_price`` 是成交口径价（下一根开盘/盘口），本属性在两种模式下
            都返回"信号 K 线收盘价"，策略用它判断信号即可保证回测与实盘同义。
            Live's ``current_price`` is the fill basis (next open / book price); this property always
            returns the signal bar's close, so a strategy reading it behaves identically in both modes.

        Returns:
            float: 当前 K 线收盘价 | Current candle close.
        """
        return self.current_candle.close  # 与 current_price 在回测中完全一致 | Identical to current_price in backtest

    @property  # 只读属性 | Read-only property
    def equity(self) -> float:  # 账户净值 | Account equity
        """账户净值 = 现金 + 多头市值 - 空头市值 | Account equity = cash + long market value - short market value.

        Purpose:
            按当前价格计算账户总净值。
            Compute total account equity at the current price.
        Parameters:
            无 | None.
        Returns:
            float: 账户净值 | Account equity value.
        Flow:
            委托账本按当前收盘价估值 | Delegate valuation to the ledger at the current close.
        """
        return self.ledger.equity_at(self.current_price)  # 按当前价估值 | Value at the current price

    @property  # 只读属性 | Read-only property
    def position(self) -> dict[str, float]:  # 当前持仓快照 | Current position snapshot
        """当前持仓快照 | Current position snapshot.

        Purpose:
            以字典形式返回多空持仓数量与成本，供策略查询。
            Return long/short quantities and costs as a dict for strategy queries.
        Parameters:
            无 | None.
        Returns:
            dict[str, float]: 持仓字段映射 | Mapping of position fields.
        Flow:
            委托账本返回持仓快照 | Delegate to the ledger snapshot.
        """
        return self.ledger.position()  # 返回账本持仓快照 | Return the ledger position snapshot

    def log(self, message: str) -> None:  # 记录策略日志 | Record a strategy log
        """记录策略日志 | Record a strategy log message.

        Purpose:
            将带日期前缀的日志追加到 logs 列表。
            Append a date-prefixed message to the logs list.
        Parameters:
            - message: 日志正文 | Log message body.
        Returns:
            None。
        Flow:
            用 f-string 拼接当前 K 线日期后追加 | Prefix with current candle date via f-string then append.
        """
        self.logs.append(f"[{self.current_candle.date}] {message}")  # 追加带日期的日志 | Append dated log entry

    def _closes(self) -> list[float]:  # 获取截至当前的收盘价 | Get closes up to current bar
        """仅返回当前及之前的收盘价，避免策略偷看未来数据。| Return only closes up to and including the current bar to prevent look-ahead.

        Purpose:
            为指标计算提供无未来函数的数据切片。
            Provide a look-ahead-free data slice for indicator computation.
        Parameters:
            无 | None.
        Returns:
            list[float]: 截至当前 K 线的收盘价列表 | Closing prices up to the current candle.
        Flow:
            对 candles 切片到 _index+1 并取 close 字段 | Slice candles to _index+1 and extract close fields.
        """
        return [candle.close for candle in self.candles[: self._index + 1]]  # 列表推导取收盘价 | List comprehension extracting close prices

    def sma(self, period: int) -> list[float | None]:  # 简单移动平均 | Simple moving average
        """简单移动平均序列 | Simple moving average series.

        Purpose:
            基于无未来数据的收盘价计算 SMA，供策略调用。
            Compute SMA from look-ahead-free closes for strategy use.
        Parameters:
            - period: 均线周期 | Moving average period.
        Returns:
            list[float | None]: SMA 序列 | SMA series.
        Flow:
            取 _closes() 结果传给 indicators.sma | Pass _closes() result to indicators.sma.
        """
        return sma(self._closes(), period)  # 调用 SMA 指标函数 | Call the SMA indicator function

    def ema(self, period: int) -> list[float | None]:  # 指数移动平均 | Exponential moving average
        """指数移动平均序列 | Exponential moving average series.

        Purpose:
            基于无未来数据的收盘价计算 EMA，供策略调用。
            Compute EMA from look-ahead-free closes for strategy use.
        Parameters:
            - period: 均线周期 | Moving average period.
        Returns:
            list[float | None]: EMA 序列 | EMA series.
        Flow:
            取 _closes() 结果传给 indicators.ema | Pass _closes() result to indicators.ema.
        """
        return ema(self._closes(), period)  # 调用 EMA 指标函数 | Call the EMA indicator function

    def rsi(self, period: int = 14) -> list[float | None]:  # 相对强弱指标 | Relative strength index
        """RSI 序列 | RSI series.

        Purpose:
            基于无未来数据的收盘价计算 RSI，供策略调用。
            Compute RSI from look-ahead-free closes for strategy use.
        Parameters:
            - period: RSI 周期，默认 14 | RSI period, default 14.
        Returns:
            list[float | None]: RSI 序列 | RSI series.
        Flow:
            取 _closes() 结果传给 indicators.rsi | Pass _closes() result to indicators.rsi.
        """
        return rsi(self._closes(), period)  # 调用 RSI 指标函数 | Call the RSI indicator function

    def _last(self, values: list[float | None]) -> float | None:  # 取序列最后一个值 | Take last value of series
        """取序列最后一个有效值 | Take the last value of a series.

        Purpose:
            便捷获取指标序列的最新值，空序列返回 None。
            Conveniently fetch the latest indicator value; returns None for an empty series.
        Parameters:
            - values: 指标值序列 | Indicator value series.
        Returns:
            float | None: 最后一个值或 None | Last value or None.
        Flow:
            若列表非空取 [-1]，否则返回 None | Take [-1] if non-empty, otherwise return None.
        """
        return values[-1] if values else None  # 非空取末值否则 None | Last element if non-empty else None

    def _validate_ratio(self, ratio: float) -> bool:  # 校验仓位比例 | Validate position ratio
        """校验仓位比例 | Validate the position ratio.

        Purpose:
            确保下单比例落在 (0, 1] 区间，否则记日志并拒绝。
            Ensure the order ratio lies in (0, 1]; otherwise log and reject.
        Parameters:
            - ratio: 待校验的仓位比例 | Ratio to validate.
        Returns:
            bool: 合法返回 True，否则 False | True if valid, else False.
        Flow:
            判断 0 < ratio <= 1，不满足则记录日志并返回 False | Check 0 < ratio <= 1; if not, log and return False.
        """
        if not 0 < ratio <= 1:  # 判断比例是否越界 | Check whether ratio is out of range
            self.log(f"仓位比例无效：{ratio}，应在 (0, 1] 范围内")  # 记录无效比例日志 | Log invalid ratio message
            return False  # 校验失败返回 False | Return False on validation failure
        return True  # 校验通过返回 True | Return True on validation success

    def _pending_qty(self, side: str) -> float:  # 统计某方向的挂单数量 | Sum pending quantity for a side
        """统计某方向尚未成交的挂单数量 | Sum the still-pending quantity of one side.

        Purpose:
            开仓门控需要把"已挂单但未成交"的数量也算进去，否则同一根 K 线内会重复下单。
            The entry gate must count pending (unfilled) quantity, otherwise the same bar
            could queue duplicate entries.
        Parameters:
            - side: 订单方向 | Order side.
        Returns:
            float: 该方向挂单总量 | Total pending quantity for the side.
        """
        return sum(order.qty for order in self._pending if order.side == side)  # 累加同方向挂单数量 | Sum pending quantities of the side

    def _available_qty(self, entry_side: str, current_qty: float) -> float:  # 扣减待平仓量后的可用持仓 | Position net of pending closes
        """计算开仓门控应看到的同向持仓（扣除本根已挂出的平仓单）| Same-side position net of queued closes.

        目的 | Purpose:
            反手信号（先平多再开空、或先平空再开多）在同一根 K 线上会先 sell/buycover 入队，
            此时账本持仓尚未变化，若门控直接看原始持仓会把随后的开仓判为"重复开仓"而拒绝，
            导致反手永远要等下一根。扣掉待平仓量后，反手可以在同一根内完成。
            A reversal queues its close first, so the ledger position has not changed yet; gating on
            the raw position rejects the follow-up entry and forces every reversal to wait a bar.
            Subtracting the queued close lets the reversal complete on the same bar.

        参数 | Parameters:
            - entry_side: 开仓方向 buy/sellshort | Entry side.
            - current_qty: 账本当前同向持仓 | Current same-side position.

        返回 | Returns:
            - float: 成交后的同向持仓（不小于 0）| Same-side position after the close, floored at 0.
        """
        close_side = "sell" if entry_side == "buy" else "buycover"  # 对应的平仓方向 | Matching close side
        closing = sum(order.qty for order in self._pending if order.side == close_side)  # 待成交平仓量 | Queued closing quantity
        return max(0.0, float(current_qty) - closing)  # 扣减并避免负数 | Subtract and floor at zero

    @property  # 只读属性 | Read-only property
    def pending_count(self) -> int:  # 待办订单笔数 | Number of queued orders
        """当前待下一根 K 线撮合的订单笔数 | How many orders await the next candle.

        Purpose:
            末根 K 线无法等到下一根开盘，严格口径下需要知道有多少笔信号被放弃。
            Under the strict basis the last bar has no next open, so callers need the count of
            signals that will be dropped.

        Returns:
            int: 待办订单笔数 | Number of pending orders.
        """
        return len(self._pending)  # 返回队列长度 | Return the queue length

    def buy(
        self,  # 实例自身 | The instance itself
        price: float | None = None,  # 指定价格，None 时用当前价 | Specified price, None uses current price
        amount: float | None = None,  # 指定数量，None 时按资金比例计算 | Specified quantity, None computes from cash ratio
        ratio: float = 0.95,  # 可用现金买入比例 | Fraction of available cash to spend
        reason: str = "",  # 下单原因 | Order reason
    ) -> bool:
        """开多/加多。未指定数量时按可用现金的 ratio 比例买入。| Open/add long. When quantity is unspecified, buy using the ratio of available cash.

        Purpose:
            生成一张买入（开多）订单加入待办队列；默认在已有持仓或挂单时拒绝重复开仓。
            Create a buy (open-long) order and add it to the pending queue; by default it
            rejects a second entry when a position or pending order already exists.
        Parameters:
            - price: 指定信号价，None 用当前收盘价 | Specified signal price, None uses current close.
            - amount: 指定数量，None 按现金比例计算 | Specified quantity, None computes from cash ratio.
            - ratio: 使用现金比例，默认 0.95 | Fraction of cash to use, default 0.95.
            - reason: 下单原因 | Order reason.
        Returns:
            bool: 下单请求是否被接受（入队）| Whether the order request was accepted (queued).
        Flow:
            校验比例与价格 → 开仓门控 → 计算数量 → 入队 | Validate ratio/price → entry gate → size → enqueue.
        """
        if not self._validate_ratio(ratio):  # 先校验比例 | Validate ratio first
            return False  # 比例非法，拒绝 | Reject invalid ratio
        if self.ledger.halted:  # 熔断后禁止开新仓 | No new entries after a drawdown halt
            self.log("已触发最大回撤熔断，忽略开仓信号")  # 记录熔断拦截 | Log the halt rejection
            return False  # 拒绝开仓 | Reject the entry
        if not can_add_position(self._available_qty("buy", self.long_qty), self._pending_qty("buy"), self.allow_add):  # 开仓门控（扣减待平仓量）| Entry gate net of pending closes
            self.log("已有多头持仓或挂单，忽略重复开仓信号")  # 记录重复开仓拦截 | Log the duplicate-entry rejection
            return False  # 拒绝重复开仓 | Reject the duplicate entry
        signal_price = float(price or self.current_price)  # 确定信号价格（指定价或当前价）| Determine signal price (specified or current)
        if signal_price <= 0:  # 校验价格为正 | Check price is positive
            self.log("买入价格必须大于 0")  # 记录价格错误 | Log price error
            return False  # 拒绝非法价格 | Reject invalid price
        if amount is None:  # 未指定数量则按购买力比例计算 | If no quantity, compute from the budget ratio
            # 购买力口径：1 倍杠杆等于可用现金，>1 倍时等于可用保证金×杠杆（名义本金）
            # Budget basis: 1x equals available cash, above 1x it is free margin × leverage.
            qty = sizing_qty(self.ledger.entry_budget(signal_price, side="buy"), ratio, signal_price, self.fee_rate, self.slippage)  # 共享数量换算 | Shared sizing helper
        else:  # 指定了数量 | Quantity specified
            qty = float(amount)  # 使用指定数量 | Use specified quantity
        if qty <= 0:  # 校验数量为正 | Check quantity is positive
            self.log("买入数量必须大于 0")  # 记录数量错误 | Log quantity error
            return False  # 拒绝非法数量 | Reject invalid quantity
        self._pending.append(  # 构造买入订单入队 | Build buy order and enqueue
            Order(  # 买入订单 | Buy order
                side="buy",  # 方向为买入 | Side is buy
                qty=qty,  # 预估数量（按信号价）| Pre-estimated quantity at the signal price
                signal_price=signal_price,  # 信号基准价 | Signal reference price
                reason=reason,  # 下单原因 | Order reason
                qty_ratio=ratio if amount is None else 0.0,  # 按比例开仓时记录比例，撮合时按成交价重算 | Record the ratio so the fill re-sizes at the fill price
            )
        )  # 入队结束 | Enqueue done
        return True  # 下单请求被接受 | Order request accepted

    def sell(
        self,  # 实例自身 | The instance itself
        price: float | None = None,  # 指定价格，None 时用当前价 | Specified price, None uses current price
        amount: float | None = None,  # 指定数量，None 时按比例平多 | Specified quantity, None exits long by ratio
        ratio: float = 1.0,  # 平多比例，默认全部 | Fraction of long position to sell, default all
        reason: str = "",  # 下单原因 | Order reason
    ) -> bool:
        """平多/减多。默认卖出全部多头持仓。| Close/reduce long. By default sells the entire long position.

        Purpose:
            生成一张卖出（平多）订单加入待办队列。
            Create a sell (close-long) order and add it to the pending queue.
        Parameters:
            - price: 指定信号价，None 用当前收盘价 | Specified signal price, None uses current close.
            - amount: 指定数量，None 按持仓比例计算 | Specified quantity, None computed by position ratio.
            - ratio: 平多比例，默认 1.0 全部 | Fraction to sell, default 1.0 (all).
            - reason: 下单原因 | Order reason.
        Returns:
            bool: 下单请求是否被接受 | Whether the order request was accepted.
        Flow:
            校验比例与持仓 → 计算卖出数量 → 入队 | Validate ratio and position → compute sell quantity → enqueue.
        """
        if not self._validate_ratio(ratio):  # 先校验比例 | Validate ratio first
            return False  # 比例非法，拒绝 | Reject invalid ratio
        if self.long_qty <= 0:  # 检查是否有多头持仓 | Check for any long position
            self.log("当前没有多头持仓，忽略卖出")  # 记录无持仓提示 | Log no-position notice
            return False  # 无持仓，忽略 | No position, ignore
        qty = self.long_qty * ratio if amount is None else min(float(amount), self.long_qty)  # 计算卖出量，不超过持仓 | Compute sell qty, capped by position
        if qty <= 0:  # 校验数量 | Validate quantity
            return False  # 数量非法，拒绝 | Reject invalid quantity
        self._pending.append(Order(side="sell", qty=qty, signal_price=float(price or self.current_price), reason=reason))  # 构造卖出订单入队 | Build sell order and enqueue
        return True  # 下单请求被接受 | Order request accepted

    def sellshort(
        self,  # 实例自身 | The instance itself
        price: float | None = None,  # 指定价格，None 时用当前价 | Specified price, None uses current price
        amount: float | None = None,  # 指定数量，None 时按净值比例计算 | Specified quantity, None computes from equity ratio
        ratio: float = 0.5,  # 开空占净值比例，默认 0.5 | Fraction of equity to short, default 0.5
        reason: str = "",  # 下单原因 | Order reason
    ) -> bool:
        """开空。未指定数量时按账户净值的 ratio 比例开空。| Open short. When quantity is unspecified, short using the ratio of account equity.

        Purpose:
            生成一张开空订单加入待办队列，仅在允许做空时有效。
            Create a short-opening order and enqueue it, only when shorting is allowed.
        Parameters:
            - price: 指定信号价，None 用当前收盘价 | Specified signal price, None uses current close.
            - amount: 指定数量，None 按净值比例计算 | Specified quantity, None computed from equity ratio.
            - ratio: 占净值比例，默认 0.5 | Fraction of equity, default 0.5.
            - reason: 下单原因 | Order reason.
        Returns:
            bool: 下单请求是否被接受 | Whether the order request was accepted.
        Flow:
            校验做空许可与比例 → 开仓门控 → 确定价格与数量 → 入队 | Check permission/ratio → entry gate → price and size → enqueue.
        """
        if not self.allow_short:  # 检查是否允许做空 | Check whether shorting is allowed
            self.log("当前为现货市场，忽略开空信号")  # 记录现货禁空提示 | Log spot-market rejection
            return False  # 不允许做空，拒绝 | Shorting disabled, reject
        if not self._validate_ratio(ratio):  # 校验比例 | Validate ratio
            return False  # 比例非法，拒绝 | Reject invalid ratio
        if self.ledger.halted:  # 熔断后禁止开新仓 | No new entries after a drawdown halt
            self.log("已触发最大回撤熔断，忽略开空信号")  # 记录熔断拦截 | Log the halt rejection
            return False  # 拒绝开仓 | Reject the entry
        if not can_add_position(self._available_qty("sellshort", self.short_qty), self._pending_qty("sellshort"), self.allow_add):  # 开仓门控（扣减待平仓量）| Entry gate net of pending closes
            self.log("已有空头持仓或挂单，忽略重复开空信号")  # 记录重复开空拦截 | Log the duplicate-short rejection
            return False  # 拒绝重复开仓 | Reject the duplicate entry
        signal_price = float(price or self.current_price)  # 确定信号价格 | Determine signal price
        if signal_price <= 0:  # 校验价格 | Validate price
            self.log("开空价格必须大于 0")  # 记录价格错误 | Log price error
            return False  # 拒绝非法价格 | Reject invalid price
        if amount is None:  # 未指定数量按净值比例计算 | If no quantity, compute from equity ratio
            # 1 倍杠杆时退化为"净值×比例"，与旧口径一致；>1 倍时按可用保证金×杠杆
            # At 1x this degrades to equity × ratio (legacy); above 1x it uses free margin × leverage.
            qty = sizing_qty(self.ledger.entry_budget(signal_price, side="sellshort"), ratio, signal_price, self.fee_rate, self.slippage)  # 共享数量换算 | Shared sizing helper
        else:  # 指定了数量 | Quantity specified
            qty = float(amount)  # 使用指定数量 | Use specified quantity
        if qty <= 0:  # 校验数量 | Validate quantity
            self.log("开空数量必须大于 0")  # 记录数量错误 | Log quantity error
            return False  # 拒绝非法数量 | Reject invalid quantity
        self._pending.append(  # 构造开空订单入队 | Build short order and enqueue
            Order(  # 开空订单 | Short order
                side="sellshort",  # 方向为开空 | Side is short
                qty=qty,  # 预估数量（按信号价）| Pre-estimated quantity at the signal price
                signal_price=signal_price,  # 信号基准价 | Signal reference price
                reason=reason,  # 下单原因 | Order reason
                qty_ratio=ratio if amount is None else 0.0,  # 按比例开仓时记录比例，撮合时按成交价重算 | Record the ratio so the fill re-sizes at the fill price
            )
        )  # 入队结束 | Enqueue done
        return True  # 下单请求被接受 | Order request accepted

    def buycover(
        self,  # 实例自身 | The instance itself
        price: float | None = None,  # 指定价格，None 时用当前价 | Specified price, None uses current price
        amount: float | None = None,  # 指定数量，None 时按比例平空 | Specified quantity, None covers short by ratio
        ratio: float = 1.0,  # 平空比例，默认全部 | Fraction of short position to cover, default all
        reason: str = "",  # 下单原因 | Order reason
    ) -> bool:
        """平空。默认买入平掉全部空头持仓。| Cover short. By default buys back the entire short position.

        Purpose:
            生成一张买入平空订单加入待办队列。
            Create a buy-to-cover order and add it to the pending queue.
        Parameters:
            - price: 指定信号价，None 用当前收盘价 | Specified signal price, None uses current close.
            - amount: 指定数量，None 按持仓比例计算 | Specified quantity, None computed by position ratio.
            - ratio: 平空比例，默认 1.0 全部 | Fraction to cover, default 1.0 (all).
            - reason: 下单原因 | Order reason.
        Returns:
            bool: 下单请求是否被接受 | Whether the order request was accepted.
        Flow:
            校验比例与空头持仓 → 计算平空数量 → 入队 | Validate ratio and short position → compute cover quantity → enqueue.
        """
        if not self._validate_ratio(ratio):  # 先校验比例 | Validate ratio first
            return False  # 比例非法，拒绝 | Reject invalid ratio
        if self.short_qty <= 0:  # 检查是否有空头持仓 | Check for any short position
            self.log("当前没有空头持仓，忽略平空")  # 记录无持仓提示 | Log no-position notice
            return False  # 无持仓，忽略 | No position, ignore
        qty = self.short_qty * ratio if amount is None else min(float(amount), self.short_qty)  # 计算平空量，不超过持仓 | Compute cover qty, capped by position
        if qty <= 0:  # 校验数量 | Validate quantity
            return False  # 数量非法，拒绝 | Reject invalid quantity
        self._pending.append(Order(side="buycover", qty=qty, signal_price=float(price or self.current_price), reason=reason))  # 构造平空订单入队 | Build cover order and enqueue
        return True  # 下单请求被接受 | Order request accepted

    def close_all(self, reason: str = "close_all") -> bool:  # 一键平掉所有持仓 | Close all positions at once
        """同时平掉多头和空头全部持仓。| Close both long and short positions entirely.

        Purpose:
            对当前存在的多头与空头持仓分别发出平仓订单。
            Issue close orders for any existing long and short positions.
        Parameters:
            - reason: 平仓原因，默认 "close_all" | Close reason, default "close_all".
        Returns:
            bool: 是否至少发起了一笔平仓 | Whether at least one close order was issued.
        Flow:
            有多头则 sell、有空头则 buycover，汇总是否下过单 | Sell if long exists, buycover if short exists; aggregate whether any order placed.
        """
        has_position = False  # 标记是否下过平仓单 | Flag whether any close order was placed
        if self.long_qty > 0:  # 有多头持仓时平多 | Close long if a long position exists
            has_position = self.sell(reason=reason) or has_position  # 调用 sell 并累积标记 | Call sell and accumulate flag
        if self.short_qty > 0:  # 有空头持仓时平空 | Cover short if a short position exists
            has_position = self.buycover(reason=reason) or has_position  # 调用 buycover 并累积标记 | Call buycover and accumulate flag
        if not has_position:  # 完全无持仓时记录提示 | Log notice when no position existed
            self.log("当前没有持仓，无需平仓")  # 记录无需平仓 | Log nothing-to-close message
        return has_position  # 返回是否有平仓动作 | Return whether a close action occurred

    def _record_fill(self, fill: Fill, date: str) -> None:  # 把账本成交转成回测成交记录 | Convert a ledger fill into a backtest trade record
        """把统一账本的成交记录转成回测对外暴露的 Trade | Convert a ledger fill into the Trade exposed by the backtest.

        Purpose:
            保持 trades 的数据结构（带日期）不变，同时复用账本的撮合数学。
            Keep the date-carrying Trade structure while reusing the ledger's fill math.
        Parameters:
            - fill: 账本成交记录 | Ledger fill.
            - date: 成交对应的 K 线日期 | Candle date of the fill.
        Returns:
            None。
        """
        self.trades.append(  # 追加成交记录 | Append the trade record
            Trade(  # 构造回测成交对象 | Build the backtest trade object
                date=date,  # 成交日期 | Fill date
                side=fill.side,  # 交易方向 | Trade side
                qty=fill.qty,  # 实际成交数量 | Actual filled quantity
                price=fill.price,  # 实际成交价格 | Actual fill price
                fee=fill.fee,  # 手续费 | Fee
                pnl=fill.pnl,  # 已实现盈亏 | Realized PnL
                reason=fill.reason,  # 下单原因 | Order reason
                signal_price=fill.signal_price,  # 信号基准价 | Signal reference price
            )  # 成交对象构造结束 | End of Trade construction
        )  # 追加结束 | End of append

    def execute_pending(self, index: int, final: bool = False, drop: bool = False) -> None:  # 撮合上一根 K 线的订单 | Execute orders from the previous candle
        """执行上一根 K 线产生的订单。| Execute orders generated on the previous candle.

        Purpose:
            在下一根 K 线开盘（最后一根按收盘）按滑点定价撮合所有待办订单，更新账户并记成交。
            Match all pending orders at the next candle's open (last candle uses close) with slippage,
            update the account and record trades.
        Parameters:
            - index: 当前 K 线下标，用于取成交价 | Current candle index used to get the fill price.
            - final: 是否为最后一根 K 线，True 时按收盘价成交 | Whether this is the last candle; if True fill at close.
            - drop: 只清空队列、不撮合。用于"末根信号没有下一根可成交"的严格口径
              | Only clear the queue. Used by the strict basis where the last bar has no next open.
        Returns:
            None。
        Flow:
            取开盘价 → 卖出类先成交 → 逐单委托账本撮合 → 记录成交 | Get open price → fill sells first → delegate each fill → record trades.
        """
        if not self._pending:  # 无待办订单则直接返回 | Return early if no pending orders
            return  # 提前结束 | Early exit
        candle = self.candles[index]  # 取当前 K 线（用于日期与价格）| Get current candle (for date and price)
        open_price = candle.close if final else candle.open  # 最后一根按收盘价，其余按开盘价 | Use close for final candle, otherwise open
        orders = self._pending  # 取出待办订单列表 | Take the pending order list
        self._pending = []  # 清空待办队列 | Clear the pending queue
        if drop:  # 丢弃口径：不撮合，只记录 | Drop basis: record without filling
            for item in orders:  # 逐笔记录被放弃的信号 | Log every dropped signal
                self.log(f"末根 K 线信号无法成交，已放弃：{item.side}")  # 记录放弃原因 | Log the reason
            return  # 直接结束 | Finish here
        # 平仓类订单优先成交：保证"先卖后买"的现金可用性，避免同根 K 线内买入挤占卖出所需资金
        # Fill closing orders first so cash freed by exits is available to same-bar entries.
        ordered = sorted(orders, key=lambda item: 0 if item.side in {"sell", "buycover"} else 1)  # 卖类优先排序 | Sort closing orders first
        for order in ordered:  # 逐笔处理订单 | Process each order
            # 按比例开仓的单据在成交价上重算数量：回测的信号价（收盘）与成交价（次根开盘）
            # 往往不同，若沿用信号价预估的数量会造成实际占用资金超过设定比例，
            # 与实盘（按成交价计算）产生系统性偏差。
            # Ratio-sized entries are re-sized at the fill price: the backtest's signal price
            # (close) usually differs from the fill price (next open), so reusing the pre-estimated
            # quantity over-allocates capital and diverges from the live engine, which sizes at the
            # actual fill price.
            fill_qty = order.qty  # 默认沿用预估数量（固定数量单）| Default to the pre-estimated qty (fixed-size orders)
            if order.qty_ratio > 0 and open_price > 0:  # 仅按比例开仓且价格有效时重算 | Only re-size ratio entries with a valid price
                # 重算同样走购买力口径：1 倍杠杆等价于旧实现（买入按现金、开空按净值），
                # >1 倍时按可用保证金×杠杆，避免杠杆下单量被"现金"口径压回 1 倍。
                # Re-sizing also uses the buying-power basis: identical to the legacy formula at
                # 1x (cash for buys, equity for shorts) and margin × leverage above 1x, so a
                # leveraged entry is not silently capped back to 1x.
                fill_qty = sizing_qty(  # 成交价口径的数量 | Quantity at the fill price
                    self.ledger.entry_budget(open_price, side=order.side),  # 该方向的购买力 | Buying power for the side
                    order.qty_ratio,  # 开仓比例 | Entry ratio
                    open_price,  # 成交价 | Fill price
                    self.fee_rate,  # 手续费率 | Fee rate
                    self.slippage,  # 滑点比例 | Slippage ratio
                )  # 数量换算结束 | End of sizing
            # 最小名义金额：实盘会被交易所过滤器拒绝的碎单，回测里同样不成交，
            # 否则小资金/小比例策略在回测中"能成交"、实盘全部报错，绩效不可比。
            # Minimum notional: dust orders the venue would reject must not fill in the backtest
            # either, otherwise small-capital runs look tradeable while every live order errors out.
            if self.min_notional > 0 and fill_qty * open_price < self.min_notional:  # 低于交易所最小额 | Below the venue minimum
                self.log(f"订单名义金额低于最小额 {self.min_notional}，取消：{order.side}")  # 记录取消原因 | Log the cancellation
                continue  # 跳过该订单 | Skip this order
            fill = self.ledger.execute(  # 委托统一账本撮合 | Delegate the fill to the shared ledger
                OrderRequest(  # 构造统一订单请求 | Build the shared order request
                    side=order.side,  # 交易方向 | Order side
                    qty=fill_qty,  # 成交价口径的数量 | Quantity at the fill price
                    signal_price=order.signal_price,  # 信号价格 | Signal price
                    reason=order.reason,  # 下单原因 | Order reason
                ),  # 订单请求构造结束 | End of order request
                open_price,  # 按开盘价撮合 | Fill at the open
            )  # 撮合结束 | End of fill
            if fill is None:  # 未成交（资金/持仓不足或价格无效）| Not filled (insufficient cash/position or bad price)
                self.log(f"可用资金或持仓不足，取消订单：{order.side}")  # 记录取消日志 | Log cancellation
                continue  # 跳过该订单 | Skip this order
            self._record_fill(fill, candle.date)  # 记录成交 | Record the fill

    def execute_now(self, order: OrderRequest, price: float) -> None:  # 立即按指定价撮合（风控强平用）| Fill immediately at a given price (risk exits)
        """立即按给定价格撮合一笔订单，不进入下一根队列 | Fill an order immediately at the given price, bypassing the next-bar queue.

        Purpose:
            止损/熔断属于"盘中触发"，必须当根立即成交，不能等到下一根开盘。
            Stops and breakers trigger intrabar and must fill on the same bar, not at the next open.
        Parameters:
            - order: 要执行的风控订单 | Risk order to execute.
            - price: 成交价（已含跳空处理）| Fill price (gap-adjusted).
        Returns:
            None。
        """
        fill = self.ledger.execute(order, price)  # 委托账本立即撮合 | Delegate an immediate fill
        if fill is None:  # 无法成交时记录日志 | Log when unfillable
            self.log(f"风控平仓失败：{order.side}")  # 记录风控失败 | Log the risk-exit failure
            return  # 直接返回 | Return early
        self._record_fill(fill, self.current_candle.date)  # 按当前 K 线日期记录成交 | Record with the current candle date

    def mark(self, price: float, low: float | None = None, high: float | None = None) -> None:  # 更新风控状态 | Update risk state
        """用本根行情更新账本的移动止损极值与净值峰值 | Update trailing extremes and the equity peak with this bar.

        Parameters:
            - price: 收盘价 | Close price.
            - low: 最低价 | Bar low.
            - high: 最高价 | Bar high.
        Returns:
            None。
        """
        self.ledger.mark(price, low=low, high=high)  # 委托账本更新 | Delegate to the ledger

    def risk_orders(self, open_price: float, low: float | None = None, high: float | None = None) -> list[tuple[OrderRequest, float]]:  # 取风控强平单 | Get forced-exit orders
        """评估止损/熔断并返回需要立即成交的订单 | Evaluate stops and return forced exits.

        Parameters:
            - open_price: 本根开盘价（跳空定价用）| Bar open for gap pricing.
            - low: 最低价 | Bar low.
            - high: 最高价 | Bar high.
        Returns:
            list[tuple[OrderRequest, float]]: (订单, 成交价) 列表 | (order, fill price) pairs.
        """
        return self.ledger.risk_orders(open_price, low=low, high=high)  # 委托账本评估 | Delegate evaluation to the ledger

    def record_equity(self, index: int) -> None:  # 记录净值点 | Record an equity point
        """记录当前净值点。| Record the current equity point.

        Purpose:
            在指定 K 线处计算并追加一条净值快照，用于绘制净值曲线。
            Compute and append an equity snapshot at the given candle for the equity curve.
        Parameters:
            - index: 当前 K 线下标 | Current candle index.
        Returns:
            None。
        Flow:
            取收盘价估值 → 四舍五入各字段 → 追加快照字典 | Value at close -> round fields -> append snapshot dict.
        """
        candle = self.candles[index]  # 取指定下标的 K 线 | Get candle at the given index
        price = candle.close  # 以收盘价估值 | Value using the close price
        self.equity_curve.append(  # 追加净值快照 | Append equity snapshot
            {  # 快照字典开始 | Start of snapshot dict
                "date": candle.date,  # 日期 | Date
                "equity": round(self.ledger.equity_at(price), 6),  # 净值（保留 6 位）| Equity (6 decimals)
                "cash": round(self.cash, 6),  # 现金（保留 6 位）| Cash (6 decimals)
                "long_qty": round(self.long_qty, 8),  # 多头数量（保留 8 位）| Long quantity (8 decimals)
                "short_qty": round(self.short_qty, 8),  # 空头数量（保留 8 位）| Short quantity (8 decimals)
            }  # 快照字典结束 | End of snapshot dict
        )  # 追加结束 | End of append
