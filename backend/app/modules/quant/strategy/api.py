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
"""

from dataclasses import dataclass  # 导入 dataclass 装饰器 | Import dataclass decorator for immutable data containers
from typing import Any  # 导入 Any 类型 | Import Any type for loosely-typed dictionaries

from .indicators import ema, rsi, sma  # 导入 SMA/EMA/RSI 指标 | Import SMA/EMA/RSI indicator functions


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
    qty: float  # 委托数量 | Ordered quantity
    signal_price: float  # 信号基准价 | Signal reference price
    reason: str = ""  # 下单原因（默认为空）| Order reason (defaults to empty)


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
    """

    name: str = ""  # 策略名称（默认为空）| Strategy name (defaults to empty)
    timeframe: str = ""  # 交易周期（默认为空）| Trading timeframe (defaults to empty)
    startup_candle_count: int = 0  # 指标预热所需 K 线数 | Candles needed for indicator warm-up

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
    """提供给策略使用的交易与指标接口 | Trading and indicator API exposed to strategies.

    目的 | Purpose:
        封装回测账户状态（现金、多头、空头、净值），提供下单、撮合、日志与指标查询能力。
        Encapsulate backtest account state (cash, long, short, equity) and provide order placement,
        matching, logging and indicator query capabilities.

    关键属性 | Key attributes:
        - cash: 可用现金 | Available cash.
        - long_qty / short_qty: 多/空持仓数量 | Long/short position quantities.
        - long_cost / short_proceeds: 多头平均成本 / 空头平均收入 | Average long cost / short proceeds.
        - trades / logs / equity_curve: 成交、日志与净值曲线 | Trades, logs and equity curve.
        - _pending: 待撮合订单队列 | Pending order queue.
        - _index: 当前 K 线下标 | Current candle index.
    """

    def __init__(
        self,  # 实例自身 | The instance itself
        candles: list[Candle],  # 完整 K 线序列 | Full candle series
        initial_cash: float,  # 初始资金 | Initial capital
        fee_rate: float = 0.001,  # 单边手续费率，0.001=0.1% | One-way fee rate, 0.001 = 0.1%
        slippage: float = 0.0,  # 滑点比例，0.001=0.1% | Slippage ratio, 0.001 = 0.1%
        allow_short: bool = True,  # 是否允许开空 | Whether shorting is allowed
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
        Returns:
            None。
        Flow:
            先保存配置参数，再初始化持仓为零，最后准备成交/日志/净值/待办队列 | Save config first, then zero positions, then prepare trades/logs/equity/pending queue.
        """
        self.candles = candles  # 保存完整 K 线序列 | Store full candle series
        self.initial_cash = initial_cash  # 保存初始资金（用于绩效计算）| Store initial capital for performance calc
        self.cash = initial_cash  # 当前可用现金，初始等于初始资金 | Current available cash, initially equal to initial capital
        self.fee_rate = fee_rate  # 单边手续费率 | One-way fee rate
        self.slippage = slippage  # 滑点比例 | Slippage ratio
        self.allow_short = allow_short  # 是否允许做空标志 | Shorting-allowed flag

        self.long_qty = 0.0  # 多头持仓数量，初始为 0 | Long position quantity, initially 0
        self.short_qty = 0.0  # 空头持仓数量，初始为 0 | Short position quantity, initially 0
        self.long_cost = 0.0  # 多头平均成本价，初始为 0 | Average long cost price, initially 0
        self.short_proceeds = 0.0  # 空头平均卖出收入价，初始为 0 | Average short sale proceeds price, initially 0

        self.trades: list[Trade] = []  # 成交记录列表 | List of filled trades
        self.logs: list[str] = []  # 策略日志列表 | List of strategy log messages
        self.equity_curve: list[dict[str, Any]] = []  # 净值曲线（每根 K 线一个快照）| Equity curve (one snapshot per candle)
        self._pending: list[Order] = []  # 待下一根 K 线撮合的订单队列 | Order queue awaiting next-candle matching
        self._index = 0  # 当前 K 线下标，初始为 0 | Current candle index, initially 0

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
            现金加上多头市值，减去空头负债市值 | Cash plus long market value minus short liability value.
        """
        price = self.current_price  # 取当前价格作为估值基准 | Get current price as valuation baseline
        return self.cash + self.long_qty * price - self.short_qty * price  # 计算并返回净值 | Compute and return equity

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
            构造包含四个持仓字段的字典并返回 | Build a dict with four position fields and return it.
        """
        return {  # 返回持仓快照字典 | Return the position snapshot dict
            "long_qty": self.long_qty,  # 多头数量 | Long quantity
            "short_qty": self.short_qty,  # 空头数量 | Short quantity
            "long_cost": self.long_cost,  # 多头平均成本 | Average long cost
            "short_proceeds": self.short_proceeds,  # 空头平均收入 | Average short proceeds
        }

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

    def buy(
        self,  # 实例自身 | The instance itself
        price: float | None = None,  # 指定价格，None 时用当前价 | Specified price, None uses current price
        amount: float | None = None,  # 指定数量，None 时按资金比例计算 | Specified quantity, None computes from cash ratio
        ratio: float = 0.95,  # 可用现金买入比例 | Fraction of available cash to spend
        reason: str = "",  # 下单原因 | Order reason
    ) -> bool:
        """开多/加多。未指定数量时按可用现金的 ratio 比例买入。| Open/add long. When quantity is unspecified, buy using the ratio of available cash.

        Purpose:
            生成一张买入（开多）订单加入待办队列。
            Create a buy (open-long) order and add it to the pending queue.
        Parameters:
            - price: 指定信号价，None 用当前收盘价 | Specified signal price, None uses current close.
            - amount: 指定数量，None 按现金比例计算 | Specified quantity, None computes from cash ratio.
            - ratio: 使用现金比例，默认 0.95 | Fraction of cash to use, default 0.95.
            - reason: 下单原因 | Order reason.
        Returns:
            bool: 下单请求是否被接受（入队）| Whether the order request was accepted (queued).
        Flow:
            校验比例与价格 -> 计算数量 -> 校验数量 -> 入队 | Validate ratio and price -> compute quantity -> validate quantity -> enqueue.
        """
        if not self._validate_ratio(ratio):  # 先校验比例 | Validate ratio first
            return False  # 比例非法，拒绝 | Reject invalid ratio
        signal_price = float(price or self.current_price)  # 确定信号价格（指定价或当前价）| Determine signal price (specified or current)
        if signal_price <= 0:  # 校验价格为正 | Check price is positive
            self.log("买入价格必须大于 0")  # 记录价格错误 | Log price error
            return False  # 拒绝非法价格 | Reject invalid price
        if amount is None:  # 未指定数量则按现金比例计算 | If no quantity, compute from cash ratio
            qty = self.cash * ratio / (signal_price * (1 + self.fee_rate + self.slippage))  # 按可用现金与费率算数量 | Compute qty from cash and rates
        else:  # 指定了数量 | Quantity specified
            qty = float(amount)  # 使用指定数量 | Use specified quantity
        if qty <= 0:  # 校验数量为正 | Check quantity is positive
            self.log("买入数量必须大于 0")  # 记录数量错误 | Log quantity error
            return False  # 拒绝非法数量 | Reject invalid quantity
        self._pending.append(Order(side="buy", qty=qty, signal_price=signal_price, reason=reason))  # 构造买入订单入队 | Build buy order and enqueue
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
            校验比例与持仓 -> 计算卖出数量 -> 入队 | Validate ratio and position -> compute sell quantity -> enqueue.
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
            校验做空许可与比例 -> 确定价格与数量 -> 入队 | Check shorting permission and ratio -> determine price and quantity -> enqueue.
        """
        if not self.allow_short:  # 检查是否允许做空 | Check whether shorting is allowed
            self.log("当前为现货市场，忽略开空信号")  # 记录现货禁空提示 | Log spot-market rejection
            return False  # 不允许做空，拒绝 | Shorting disabled, reject
        if not self._validate_ratio(ratio):  # 校验比例 | Validate ratio
            return False  # 比例非法，拒绝 | Reject invalid ratio
        signal_price = float(price or self.current_price)  # 确定信号价格 | Determine signal price
        if signal_price <= 0:  # 校验价格 | Validate price
            self.log("开空价格必须大于 0")  # 记录价格错误 | Log price error
            return False  # 拒绝非法价格 | Reject invalid price
        if amount is None:  # 未指定数量按净值比例计算 | If no quantity, compute from equity ratio
            qty = self.equity * ratio / (signal_price * (1 + self.fee_rate + self.slippage))  # 按净值与费率算数量 | Compute qty from equity and rates
        else:  # 指定了数量 | Quantity specified
            qty = float(amount)  # 使用指定数量 | Use specified quantity
        if qty <= 0:  # 校验数量 | Validate quantity
            self.log("开空数量必须大于 0")  # 记录数量错误 | Log quantity error
            return False  # 拒绝非法数量 | Reject invalid quantity
        self._pending.append(Order(side="sellshort", qty=qty, signal_price=signal_price, reason=reason))  # 构造开空订单入队 | Build short order and enqueue
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
            校验比例与空头持仓 -> 计算平空数量 -> 入队 | Validate ratio and short position -> compute cover quantity -> enqueue.
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

    def execute_pending(self, index: int, final: bool = False) -> None:  # 撮合上一根 K 线的订单 | Execute orders from the previous candle
        """执行上一根 K 线产生的订单。| Execute orders generated on the previous candle.

        Purpose:
            在下一根 K 线开盘（最后一根按收盘）按滑点定价撮合所有待办订单，更新账户并记成交。
            Match all pending orders at the next candle's open (last candle uses close) with slippage,
            update the account and record trades.
        Parameters:
            - index: 当前 K 线下标，用于取成交价 | Current candle index used to get the fill price.
            - final: 是否为最后一根 K 线，True 时按收盘价成交 | Whether this is the last candle; if True fill at close.
        Returns:
            None。
        Flow:
            取开盘价 -> 逐单定价 -> 按方向约束数量 -> 更新现金/持仓/成本 -> 记录成交 | Get open price -> price each order -> cap quantity by side -> update cash/position/cost -> record trade.
        """
        if not self._pending:  # 无待办订单则直接返回 | Return early if no pending orders
            return  # 提前结束 | Early exit
        candle = self.candles[index]  # 取当前 K 线（用于日期与价格）| Get current candle (for date and price)
        open_price = candle.close if final else candle.open  # 最后一根按收盘价，其余按开盘价 | Use close for final candle, otherwise open
        orders = self._pending  # 取出待办订单列表 | Take the pending order list
        self._pending = []  # 清空待办队列 | Clear the pending queue
        for order in orders:  # 逐笔处理订单 | Process each order
            if order.side in {"buy", "buycover"}:  # 买入类订单需加上滑点 | Buy-side orders add slippage
                price = open_price * (1 + self.slippage)  # 买价上浮滑点 | Raise buy price by slippage
            else:  # 卖出类订单扣减滑点 | Sell-side orders subtract slippage
                price = open_price * (1 - self.slippage)  # 卖价下浮滑点 | Lower sell price by slippage
            if price <= 0:  # 校验成交价有效性 | Validate fill price
                self.log(f"成交价格无效，取消订单：{order.side}")  # 记录取消日志 | Log cancellation
                continue  # 跳过该订单 | Skip this order

            qty = order.qty  # 以委托数量起步 | Start from the ordered quantity
            if order.side == "buy":  # 开多分支 | Open-long branch
                # 开多：受可用现金约束 | Open long: constrained by available cash
                affordable = self.cash / (price * (1 + self.fee_rate))  # 按现金与费率算可买上限 | Max affordable qty from cash and fee
                qty = min(qty, affordable)  # 取较小值 | Take the smaller quantity
            elif order.side == "sell":  # 平多分支 | Close-long branch
                # 平多：受多头持仓约束 | Close long: constrained by long position
                qty = min(qty, self.long_qty)  # 不超过多头持仓 | Cap by long position
            elif order.side == "buycover":  # 平空分支 | Cover-short branch
                # 平空：受空头持仓与可用现金双重约束 | Cover short: constrained by both short position and cash
                qty = min(qty, self.short_qty)  # 先受空头持仓约束 | First cap by short position
                affordable = self.cash / (price * (1 + self.fee_rate))  # 再按现金算可买上限 | Then compute affordable qty from cash
                qty = min(qty, affordable)  # 取两者较小值 | Take the smaller of the two
            else:  # sellshort 开空分支 | Sell-short branch
                # sellshort 开空：不产生即时现金支出，且开空时 short_qty 尚为 0，
                # 不能按现有空头持仓削减数量，否则永远无法建立首笔空单。
                # Sell-short produces no immediate cash outlay, and short_qty is still 0 when opening,
                # so we must not cap by existing short holdings or the first short could never be opened.
                qty = max(qty, 0.0)  # 仅保证非负 | Only ensure non-negative

            if qty <= 1e-12:  # 数量过小视为无效 | Treat tiny quantity as invalid
                self.log(f"可用资金或持仓不足，取消订单：{order.side}")  # 记录取消日志 | Log cancellation
                continue  # 跳过该订单 | Skip this order

            notional = qty * price  # 名义金额 = 数量 × 价格 | Notional = quantity × price
            fee = notional * self.fee_rate  # 手续费 = 名义金额 × 费率 | Fee = notional × fee rate
            pnl: float | None = None  # 已实现盈亏，开仓保持 None | Realized PnL, stays None for opening trades

            if order.side == "buy":  # 开多成交处理 | Handle open-long fill
                self.cash -= notional + fee  # 扣减现金（含手续费）| Deduct cash (including fee)
                total_cost = self.long_cost * self.long_qty + notional  # 累计多头成本 | Accumulate long cost
                self.long_qty += qty  # 增加多头持仓 | Increase long position
                self.long_cost = total_cost / self.long_qty  # 更新多头平均成本 | Update average long cost
            elif order.side == "sell":  # 平多成交处理 | Handle close-long fill
                self.cash += notional - fee  # 增加现金（扣手续费）| Add cash (minus fee)
                pnl = (price - self.long_cost) * qty - fee  # 计算已实现盈亏 | Compute realized PnL
                self.long_qty -= qty  # 减少多头持仓 | Reduce long position
                if self.long_qty <= 1e-12:  # 持仓清零判定 | Detect position fully closed
                    self.long_qty = 0.0  # 归零多头数量 | Zero out long quantity
                    self.long_cost = 0.0  # 归零多头成本 | Zero out long cost
            elif order.side == "sellshort":  # 开空成交处理 | Handle open-short fill
                self.cash += notional - fee  # 增加现金（扣手续费）| Add cash (minus fee)
                total_proceeds = self.short_proceeds * self.short_qty + notional  # 累计空头收入 | Accumulate short proceeds
                self.short_qty += qty  # 增加空头持仓 | Increase short position
                self.short_proceeds = total_proceeds / self.short_qty  # 更新空头平均收入价 | Update average short proceeds
            else:  # buycover 平空成交处理 | Handle cover-short fill
                self.cash -= notional + fee  # 扣减现金（含手续费）| Deduct cash (including fee)
                pnl = (self.short_proceeds - price) * qty - fee  # 计算已实现盈亏 | Compute realized PnL
                self.short_qty -= qty  # 减少空头持仓 | Reduce short position
                if self.short_qty <= 1e-12:  # 持仓清零判定 | Detect position fully closed
                    self.short_qty = 0.0  # 归零空头数量 | Zero out short quantity
                    self.short_proceeds = 0.0  # 归零空头收入 | Zero out short proceeds

            self.trades.append(  # 追加成交记录 | Append trade record
                Trade(  # 构造成交对象 | Construct the Trade object
                    date=candle.date,  # 成交日期 | Fill date
                    side=order.side,  # 交易方向 | Trade side
                    qty=qty,  # 实际成交数量 | Actual filled quantity
                    price=price,  # 实际成交价格 | Actual fill price
                    fee=fee,  # 手续费 | Fee
                    pnl=pnl,  # 已实现盈亏 | Realized PnL
                    reason=order.reason,  # 下单原因 | Order reason
                    signal_price=order.signal_price,  # 信号基准价 | Signal reference price
                )  # 成交对象构造结束 | End of Trade construction
            )  # 追加结束 | End of append

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
            取收盘价估值 -> 四舍五入各字段 -> 追加快照字典 | Value at close -> round fields -> append snapshot dict.
        """
        candle = self.candles[index]  # 取指定下标的 K 线 | Get candle at the given index
        price = candle.close  # 以收盘价估值 | Value using the close price
        self.equity_curve.append(  # 追加净值快照 | Append equity snapshot
            {  # 快照字典开始 | Start of snapshot dict
                "date": candle.date,  # 日期 | Date
                "equity": round(self.cash + self.long_qty * price - self.short_qty * price, 6),  # 净值（保留 6 位）| Equity (6 decimals)
                "cash": round(self.cash, 6),  # 现金（保留 6 位）| Cash (6 decimals)
                "long_qty": round(self.long_qty, 8),  # 多头数量（保留 8 位）| Long quantity (8 decimals)
                "short_qty": round(self.short_qty, 8),  # 空头数量（保留 8 位）| Short quantity (8 decimals)
            }  # 快照字典结束 | End of snapshot dict
        )  # 追加结束 | End of append
