"""策略统一交易接口 | Unified strategy trading interface.

目的：向策略暴露与运行模式无关的同一套下单 API，屏蔽回测/模拟盘/实盘三种模式在
"订单如何成交"上的差异，使策略代码只需调用 buy/sell/sellshort/buycover/close_all
而不必关心底层撮合细节。
Purpose: Expose one mode-agnostic trading API to strategies, hiding the differences
in "how orders are filled" across backtest/paper/live modes, so strategy code only
calls buy/sell/sellshort/buycover/close_all without caring about low-level execution.

主要流程 | Main flow:
- 策略通过 :class:`StrategyAPI` 的方法构造 :class:`~.base.OrderRequest` 下单请求；
  Strategies build OrderRequest objects through StrategyAPI methods.
- 请求交给 ``submit`` 回调；差异封装在该回调中：
  Requests go to the ``submit`` callback; mode differences are encapsulated there:
  - 回测：请求排队，下一根开盘统一撮合（见 backtest.engine 的 TradeAPI）；
    Backtest: queue requests and match them all at next bar open.
  - 模拟盘：回调用 AccountLedger 按最新价即时虚拟撮合；
    Paper: callback uses AccountLedger to virtually fill at the latest price.
  - 实盘：回调调用币安下单接口，成交后再更新账本。
    Live: callback calls Binance order API, then updates the ledger after fill.
"""

from collections.abc import Callable  # 导入 Callable 类型 | Import Callable for typing callback signatures
from typing import Any  # 导入 Any 类型 | Import Any for generic dict/value typing

from ..strategy.api import Candle  # 导入 Candle K 线类型 | Import Candle candle data type
from ..strategy.indicators import ema, rsi, sma  # 导入指标函数 | Import ema/rsi/sma indicator functions
from .base import AccountLedger, OrderRequest  # 导入账本与订单请求 | Import AccountLedger and OrderRequest


class StrategyAPI:
    """策略统一交易接口（三种运行模式共用） | Unified strategy trading API shared by all run modes.

    目的：为策略提供买入、卖出、开空、平空、查询账户等统一入口，并把下单动作
    委托给构造时注入的 ``submit`` 回调，从而与具体执行器解耦。
    Purpose: Provide a single entry point for buy/sell/short/cover/account queries and
    delegate order placement to an injected ``submit`` callback, decoupling from executors.

    关键属性 | Key attributes:
    - ledger: 账户账本，负责资金/持仓记账 | Account ledger for cash/position bookkeeping.
    - _submit: 下单回调，真正决定订单如何成交 | Submission callback deciding how orders fill.
    - candles/_index: K 线窗口与当前下标，供指标与价格基准使用 | Candle window and current index.
    - live_price: 实时行情价，优先于 K 线收盘价 | Live price, preferred over candle close.
    - allow_short: 是否允许做空 | Whether short selling is allowed.
    - logs/rejected_orders: 策略日志与被拒订单记录 | Strategy logs and rejected-order records.
    """

    def __init__(
        self,  # 实例自身引用 | Reference to the instance itself
        ledger: AccountLedger,  # 账户账本对象 | Account ledger object
        *,  # 强制后续参数为关键字参数 | Force following params to be keyword-only
        submit: Callable[[OrderRequest], bool],  # 下单回调，返回是否受理 | Submission callback returning acceptance
        candles: list[Candle] | None = None,  # 可选 K 线窗口 | Optional candle window
        index: int = 0,  # 当前 K 线下标 | Current candle index
        timestamp: str = "",  # 日志/成交时间标签 | Log/fill timestamp label
        allow_short: bool = True,  # 是否允许做空 | Whether shorting is allowed
    ) -> None:  # 构造器无返回值 | Constructor returns None
        """初始化策略接口 | Initialize the strategy API.

        目的：保存账本与下单回调，建立 K 线窗口、实时价、日志等内部状态，并在
        提供 K 线时用 ``set_index`` 校正当前下标。
        Purpose: Store ledger and submit callback, set up candle window/live price/log
        state, and normalize the current index via ``set_index`` when candles exist.

        参数 | Parameters:
        - ledger (AccountLedger): 账户账本，管理现金与多空持仓 | Account ledger for cash/positions.
        - submit (Callable[[OrderRequest], bool]): 下单回调，接收 OrderRequest，返回是否受理成功
          | Submission callback taking OrderRequest, returning whether accepted.
        - candles (list[Candle] | None): 供指标计算与价格基准的 K 线窗口，默认 None
          | Candle window for indicators and price baseline, default None.
        - index (int): 当前 K 线下标，默认 0 | Current candle index, default 0.
        - timestamp (str): 日志/成交时间标签，默认空串 | Log/fill timestamp label, default empty.
        - allow_short (bool): 是否允许做空，实盘现货默认关闭 | Whether shorting allowed.

        返回 | Returns:
        - None: 构造器不返回显式值 | Constructor returns no explicit value.

        流程 | Flow:
        保存依赖 → 初始化空窗口与默认价 → 有 K 线则调用 ``set_index`` 裁剪下标。
        Save dependencies → init empty window/default price → call ``set_index`` if candles exist.
        """
        self.ledger = ledger  # 保存账户账本引用 | Store the account ledger reference
        self._submit = submit  # 保存下单回调引用 | Store the submission callback reference
        self.candles = candles or []  # 保存 K 线窗口，None 时退化为空列表 | Store window, fallback to empty list
        self._index = 0  # 初始化当前 K 线下标为 0 | Initialize current candle index to 0
        self.live_price = 0.0  # 初始化实时行情价为 0（表示暂无实时价）| Init live price to 0 (no live price yet)
        self.symbol = ""  # 初始化交易对字符串为空 | Initialize trading symbol to empty string
        self.timestamp = timestamp  # 保存时间标签 | Store the timestamp label
        self.allow_short = allow_short  # 保存是否允许做空的开关 | Store the shorting-allowed flag
        self.logs: list[str] = []  # 初始化策略日志列表 | Initialize strategy log list
        self.rejected_orders: list[str] = []  # 初始化被拒订单记录列表 | Initialize rejected-order list
        if self.candles:  # 仅当存在 K 线窗口时才校正下标 | Only normalize index when a candle window exists
            self.set_index(index)  # 调用 set_index 设置并裁剪当前下标 | Call set_index to set/clamp current index

    def set_index(self, index: int) -> None:  # 设置当前 K 线下标 | Set the current candle index
        """设置当前 K 线下标 | Set the current candle index.

        目的：把下标限制在 [0, len(candles)-1] 区间，防止越界。
        Purpose: Clamp the index into [0, len(candles)-1] to avoid out-of-range access.

        参数 | Parameters:
        - index (int): 期望设置的下标 | Desired index to set.

        返回 | Returns:
        - None: 无显式返回值 | No explicit return value.
        """
        if self.candles:  # 有 K 线窗口时才执行裁剪 | Only clamp when a candle window exists
            self._index = max(0, min(index, len(self.candles) - 1))  # 将下标裁剪到合法范围 | Clamp index to valid range

    @property  # 声明属性装饰器 | Declare property decorator
    def current_candle(self) -> Candle | None:  # 当前 K 线访问器 | Current candle accessor
        """当前 K 线；无 K 线窗口时返回 None | Current candle, or None when no window.

        返回 | Returns:
        - Candle | None: 下标处的 K 线；无窗口时为 None | Candle at index, or None if no window.
        """
        return self.candles[self._index] if self.candles else None  # 有窗口则取下标处 K 线，否则 None | Return candle at index or None

    @property  # 声明属性装饰器 | Declare property decorator
    def current_price(self) -> float:  # 当前价格访问器 | Current price accessor
        """当前价格：优先用实时行情价，其次用当前 K 线收盘价 | Current price: live price first, then candle close.

        返回 | Returns:
        - float: 有效价格；无实时价且无 K 线时返回 0.0 | Valid price, or 0.0 when none available.
        """
        live = getattr(self, "live_price", 0.0)  # 读取实时价属性，缺失时回退 0.0 | Read live price attr, fallback 0.0
        if live and live > 0:  # 实时价存在且为正时优先采用 | Prefer live price when present and positive
            return float(live)  # 返回转换为 float 的实时价 | Return live price as float
        candle = self.current_candle  # 回退读取当前 K 线 | Fall back to current candle
        return candle.close if candle else 0.0  # 有 K 线返回收盘价，否则 0.0 | Return close or 0.0

    @property  # 声明属性装饰器 | Declare property decorator
    def equity(self) -> float:  # 账户净值访问器 | Account equity accessor
        """账户净值 | Account equity.

        返回 | Returns:
        - float: 按当前价格计算的账户净值 | Equity computed at the current price.
        """
        return self.ledger.equity_at(self.current_price)  # 委托账本按当前价计算净值 | Delegate to ledger for equity

    @property  # 声明属性装饰器 | Declare property decorator
    def cash(self) -> float:  # 可用现金访问器 | Available cash accessor
        """可用现金 | Available cash.

        返回 | Returns:
        - float: 账本当前现金余额 | Current cash balance from ledger.
        """
        return self.ledger.cash  # 返回账本现金余额 | Return ledger cash balance

    @property  # 声明属性装饰器 | Declare property decorator
    def position(self) -> dict[str, float]:  # 持仓快照访问器 | Position snapshot accessor
        """持仓快照 | Position snapshot.

        返回 | Returns:
        - dict[str, float]: 账本当前多空持仓字典 | Current long/short position dict from ledger.
        """
        return self.ledger.position()  # 委托账本返回持仓快照 | Delegate to ledger for position snapshot

    def log(self, message: str) -> None:  # 记录策略日志 | Record a strategy log
        """记录策略日志 | Record a strategy log.

        参数 | Parameters:
        - message (str): 日志正文 | Log message body.

        返回 | Returns:
        - None: 无显式返回值 | No explicit return value.
        """
        prefix = f"[{self.timestamp}]" if self.timestamp else ""  # 有时间标签则包裹成方括号前缀 | Wrap timestamp in brackets if present
        self.logs.append(f"{prefix} {message}")  # 拼接前缀与正文后追加到日志列表 | Append prefixed message to log list

    def _closes(self) -> list[float]:  # 取当前及之前的收盘价 | Get closes up to current index
        """当前及之前的收盘价，防止策略偷看未来数据 | Closes up to current index, preventing look-ahead.

        返回 | Returns:
        - list[float]: 从首根到当前下标的收盘价列表 | Close prices from first bar to current index.
        """
        return [candle.close for candle in self.candles[: self._index + 1]]  # 仅切片到当前下标（含）以防止未来函数 | Slice up to and including current index to avoid look-ahead

    def sma(self, period: int) -> list[float | None]:  # 简单移动平均序列 | Simple moving average series
        """简单移动平均序列 | Simple moving average series.

        参数 | Parameters:
        - period (int): 均线周期 | Moving average period.

        返回 | Returns:
        - list[float | None]: SMA 序列，前期不足周期为 None | SMA values, None before enough data.
        """
        return sma(self._closes(), period)  # 用截至当前的收盘价计算 SMA | Compute SMA from closes up to current

    def ema(self, period: int) -> list[float | None]:  # 指数移动平均序列 | Exponential moving average series
        """指数移动平均序列 | Exponential moving average series.

        参数 | Parameters:
        - period (int): 均线周期 | Moving average period.

        返回 | Returns:
        - list[float | None]: EMA 序列 | EMA values.
        """
        return ema(self._closes(), period)  # 用截至当前的收盘价计算 EMA | Compute EMA from closes up to current

    def rsi(self, period: int = 14) -> list[float | None]:  # RSI 序列 | RSI series
        """RSI 序列 | RSI series.

        参数 | Parameters:
        - period (int): RSI 周期，默认 14 | RSI period, default 14.

        返回 | Returns:
        - list[float | None]: RSI 序列 | RSI values.
        """
        return rsi(self._closes(), period)  # 用截至当前的收盘价计算 RSI | Compute RSI from closes up to current

    def _validate_ratio(self, ratio: float) -> bool:  # 校验仓位比例 | Validate position ratio
        """校验仓位比例 | Validate position ratio.

        目的：确保比例落在 (0, 1] 区间，否则记录日志并判定无效。
        Purpose: Ensure ratio lies in (0, 1], otherwise log and mark invalid.

        参数 | Parameters:
        - ratio (float): 待校验的仓位比例 | Position ratio to validate.

        返回 | Returns:
        - bool: 比例有效返回 True，否则 False | True if valid, False otherwise.
        """
        if not 0 < ratio <= 1:  # 比例不在 (0, 1] 区间时为非法 | Invalid when ratio not in (0, 1]
            self.log(f"仓位比例无效：{ratio}，应在 (0, 1] 范围内")  # 记录非法比例日志 | Log invalid ratio message
            return False  # 返回校验失败 | Return validation failure
        return True  # 返回校验通过 | Return validation success

    def _emit(self, side: str, qty: float, signal_price: float, reason: str) -> bool:  # 构造订单并交给执行器 | Build order and submit
        """构造订单请求并交给执行器 | Build an order request and hand it to the executor.

        目的：校验数量、封装 OrderRequest、调用 ``submit`` 回调并记录被拒订单。
        Purpose: Validate quantity, wrap an OrderRequest, call ``submit``, and record rejections.

        参数 | Parameters:
        - side (str): 交易方向 | Order side.
        - qty (float): 数量 | Quantity.
        - signal_price (float): 信号价格 | Signal price.
        - reason (str): 下单原因 | Reason for the order.

        返回 | Returns:
        - bool: 订单是否被受理 | Whether the order was accepted.

        流程 | Flow:
        数量校验 → 构造请求 → 调用回调 → 被拒则记录 → 返回受理结果。
        Validate qty → build request → call callback → record rejection → return result.
        """
        if qty <= 0:  # 数量非正时拒绝下单 | Reject when quantity is not positive
            self.log(f"{side} 数量必须大于 0")  # 记录数量非法日志 | Log invalid quantity message
            return False  # 返回下单失败 | Return submission failure
        request = OrderRequest(  # 构造订单请求对象 | Build the order request object
            side=side,  # 设置交易方向 | Set order side
            qty=qty,  # 设置数量 | Set quantity
            signal_price=float(signal_price),  # 设置并转换信号价格为 float | Set and convert signal price to float
            reason=reason,  # 设置下单原因 | Set order reason
            symbol=getattr(self, "symbol", ""),  # 读取交易对，缺失回退空串 | Read symbol, fallback to empty
        )
        accepted = bool(self._submit(request))  # 调用下单回调并转为布尔受理结果 | Invoke callback and coerce to bool
        if not accepted:  # 订单被拒绝时记录 | Record when the order is rejected
            self.rejected_orders.append(f"{side}@{signal_price:.8g}")  # 追加被拒订单摘要 | Append rejected-order summary
        return accepted  # 返回受理结果 | Return acceptance result

    def buy(  # 开多/加多 | Open/increase long
        self,  # 实例自身引用 | Instance reference
        price: float | None = None,  # 指定价格，None 时用当前价 | Explicit price, else current price
        amount: float | None = None,  # 指定数量，None 时按现金比例计算 | Explicit qty, else compute from cash ratio
        ratio: float = 0.95,  # 可用现金下单比例 | Ratio of available cash to use
        reason: str = "",  # 下单原因 | Order reason
    ) -> bool:  # 返回是否受理 | Return whether accepted
        """开多/加多。未指定数量时按可用现金的 ratio 比例买入 | Open/increase long; size by cash ratio when amount omitted.

        参数 | Parameters:
        - price (float | None): 指定信号价格，None 表示用当前价 | Explicit signal price, None = current price.
        - amount (float | None): 指定数量，None 表示按比例计算 | Explicit quantity, None = compute by ratio.
        - ratio (float): 可用现金下单比例，默认 0.95 | Fraction of cash to use, default 0.95.
        - reason (str): 下单原因，默认空串 | Order reason, default empty.

        返回 | Returns:
        - bool: 订单是否被受理 | Whether the order was accepted.
        """
        if not self._validate_ratio(ratio):  # 校验仓位比例，失败则中止 | Validate ratio, abort on failure
            return False  # 返回下单失败 | Return submission failure
        signal_price = float(price or self.current_price)  # 确定信号价：显式价格或当前价并转 float | Resolve signal price
        if signal_price <= 0:  # 价格非正时拒绝 | Reject non-positive price
            self.log("买入价格必须大于 0")  # 记录价格非法日志 | Log invalid price message
            return False  # 返回下单失败 | Return submission failure
        qty = (  # 计算买入数量 | Compute buy quantity
            self.cash * ratio / (signal_price * (1 + self.ledger.fee_rate + self.ledger.slippage))  # 按现金比例折算可买数量（含手续费与滑点）| Size from cash ratio including fee and slippage
            if amount is None  # 未指定数量时走比例计算分支 | Branch when amount not specified
            else float(amount)  # 指定数量时直接转换使用 | Branch when amount specified
        )
        return self._emit("buy", qty, signal_price, reason)  # 构造并提交买入订单 | Build and submit buy order

    def sell(  # 平多/减多 | Close/reduce long
        self,  # 实例自身引用 | Instance reference
        price: float | None = None,  # 指定价格，None 时用当前价 | Explicit price, else current price
        amount: float | None = None,  # 指定数量，None 时用持仓比例 | Explicit qty, else use holding ratio
        ratio: float = 1.0,  # 多头持仓卖出比例 | Ratio of long holdings to sell
        reason: str = "",  # 下单原因 | Order reason
    ) -> bool:  # 返回是否受理 | Return whether accepted
        """平多/减多。默认卖出全部多头持仓 | Close/reduce long; sell all long holdings by default.

        参数 | Parameters:
        - price (float | None): 指定信号价格，None 表示用当前价 | Explicit signal price, None = current price.
        - amount (float | None): 指定数量，None 表示按持仓比例 | Explicit quantity, None = by holding ratio.
        - ratio (float): 多头持仓卖出比例，默认 1.0 | Fraction of long holdings, default 1.0.
        - reason (str): 下单原因，默认空串 | Order reason, default empty.

        返回 | Returns:
        - bool: 订单是否被受理 | Whether the order was accepted.
        """
        if not self._validate_ratio(ratio):  # 校验仓位比例，失败则中止 | Validate ratio, abort on failure
            return False  # 返回下单失败 | Return submission failure
        if self.ledger.long_qty <= 0:  # 无多头持仓时忽略卖出 | Ignore sell when no long position
            self.log("当前没有多头持仓，忽略卖出")  # 记录无持仓日志 | Log no-position message
            return False  # 返回下单失败 | Return submission failure
        qty = self.ledger.long_qty * ratio if amount is None else min(float(amount), self.ledger.long_qty)  # 按比例或指定量计算卖出数量，且不超过持仓 | Compute sell qty by ratio or amount, capped by holdings
        return self._emit("sell", qty, float(price or self.current_price), reason)  # 构造并提交卖出订单 | Build and submit sell order

    def sellshort(  # 开空 | Open short
        self,  # 实例自身引用 | Instance reference
        price: float | None = None,  # 指定价格，None 时用当前价 | Explicit price, else current price
        amount: float | None = None,  # 指定数量，None 时按净值比例计算 | Explicit qty, else compute from equity ratio
        ratio: float = 0.5,  # 账户净值开空比例 | Ratio of equity to short with
        reason: str = "",  # 下单原因 | Order reason
    ) -> bool:  # 返回是否受理 | Return whether accepted
        """开空。未指定数量时按账户净值的 ratio 比例开空 | Open short; size by equity ratio when amount omitted.

        参数 | Parameters:
        - price (float | None): 指定信号价格，None 表示用当前价 | Explicit signal price, None = current price.
        - amount (float | None): 指定数量，None 表示按比例计算 | Explicit quantity, None = compute by ratio.
        - ratio (float): 账户净值开空比例，默认 0.5 | Fraction of equity to short, default 0.5.
        - reason (str): 下单原因，默认空串 | Order reason, default empty.

        返回 | Returns:
        - bool: 订单是否被受理 | Whether the order was accepted.
        """
        if not self.allow_short:  # 不允许做空时拒绝 | Reject when shorting disabled
            self.log("当前模式不支持做空（实盘现货默认关闭）")  # 记录不支持做空日志 | Log shorting-unsupported message
            return False  # 返回下单失败 | Return submission failure
        if not self._validate_ratio(ratio):  # 校验仓位比例，失败则中止 | Validate ratio, abort on failure
            return False  # 返回下单失败 | Return submission failure
        signal_price = float(price or self.current_price)  # 确定信号价：显式价格或当前价并转 float | Resolve signal price
        if signal_price <= 0:  # 价格非正时拒绝 | Reject non-positive price
            self.log("开空价格必须大于 0")  # 记录价格非法日志 | Log invalid price message
            return False  # 返回下单失败 | Return submission failure
        qty = (  # 计算开空数量 | Compute short quantity
            self.equity * ratio / (signal_price * (1 + self.ledger.fee_rate + self.ledger.slippage))  # 按净值比例折算可开空数量（含手续费与滑点）| Size from equity ratio including fee and slippage
            if amount is None  # 未指定数量时走比例计算分支 | Branch when amount not specified
            else float(amount)  # 指定数量时直接转换使用 | Branch when amount specified
        )
        return self._emit("sellshort", qty, signal_price, reason)  # 构造并提交开空订单 | Build and submit short order

    def buycover(  # 平空 | Cover short
        self,  # 实例自身引用 | Instance reference
        price: float | None = None,  # 指定价格，None 时用当前价 | Explicit price, else current price
        amount: float | None = None,  # 指定数量，None 时用持仓比例 | Explicit qty, else use holding ratio
        ratio: float = 1.0,  # 空头持仓平仓比例 | Ratio of short holdings to cover
        reason: str = "",  # 下单原因 | Order reason
    ) -> bool:  # 返回是否受理 | Return whether accepted
        """平空。默认买入平掉全部空头持仓 | Cover short; buy to cover all short holdings by default.

        参数 | Parameters:
        - price (float | None): 指定信号价格，None 表示用当前价 | Explicit signal price, None = current price.
        - amount (float | None): 指定数量，None 表示按持仓比例 | Explicit quantity, None = by holding ratio.
        - ratio (float): 空头持仓平仓比例，默认 1.0 | Fraction of short holdings, default 1.0.
        - reason (str): 下单原因，默认空串 | Order reason, default empty.

        返回 | Returns:
        - bool: 订单是否被受理 | Whether the order was accepted.
        """
        if not self.allow_short:  # 不允许做空时拒绝（平空同样受限）| Reject when shorting disabled (cover also restricted)
            self.log("当前模式不支持做空（实盘现货默认关闭）")  # 记录不支持做空日志 | Log shorting-unsupported message
            return False  # 返回下单失败 | Return submission failure
        if not self._validate_ratio(ratio):  # 校验仓位比例，失败则中止 | Validate ratio, abort on failure
            return False  # 返回下单失败 | Return submission failure
        if self.ledger.short_qty <= 0:  # 无空头持仓时忽略平空 | Ignore cover when no short position
            self.log("当前没有空头持仓，忽略平空")  # 记录无持仓日志 | Log no-position message
            return False  # 返回下单失败 | Return submission failure
        qty = self.ledger.short_qty * ratio if amount is None else min(float(amount), self.ledger.short_qty)  # 按比例或指定量计算平空数量，且不超过空头持仓 | Compute cover qty by ratio or amount, capped by short holdings
        return self._emit("buycover", qty, float(price or self.current_price), reason)  # 构造并提交平空订单 | Build and submit cover order

    def close_all(self, reason: str = "close_all") -> bool:  # 一键平掉所有持仓 | Close all positions
        """同时平掉多头和空头全部持仓 | Close all long and short positions.

        参数 | Parameters:
        - reason (str): 下单原因，默认 "close_all" | Order reason, default "close_all".

        返回 | Returns:
        - bool: 是否至少有一笔平仓被受理 | Whether at least one close order was accepted.
        """
        has_position = False  # 标记是否存在持仓/是否有平仓被受理 | Flag for position existence / accepted close
        if self.ledger.long_qty > 0:  # 有多头持仓时先平多 | Close long first when long position exists
            has_position = self.sell(reason=reason) or has_position  # 调用 sell 平多并累积受理标记 | Call sell and accumulate accepted flag
        if self.ledger.short_qty > 0:  # 有空头持仓时再平空 | Cover short when short position exists
            has_position = self.buycover(reason=reason) or has_position  # 调用 buycover 平空并累积受理标记 | Call buycover and accumulate accepted flag
        if not has_position:  # 没有任何持仓需要平时记录日志 | Log when there was nothing to close
            self.log("当前没有持仓，无需平仓")  # 记录无需平仓日志 | Log no-position-to-close message
        return has_position  # 返回是否有平仓被受理 | Return whether any close was accepted

    def set_live_price(self, price: float) -> None:  # 注入实时行情价 | Inject live market price
        """注入实时行情价，供下单与净值计算使用 | Inject live market price for ordering and equity calc.

        参数 | Parameters:
        - price (float): 实时价格 | Live price.

        返回 | Returns:
        - None: 无显式返回值 | No explicit return value.
        """
        self.live_price = float(price)  # 将价格转换为 float 后写入实时价属性 | Convert price to float and store

    def state(self) -> dict[str, Any]:  # 导出接口状态快照 | Export API state snapshot
        """导出接口状态快照（用于调试与持久化） | Export API state snapshot for debugging and persistence.

        返回 | Returns:
        - dict[str, Any]: 含价格、净值、持仓、日志、被拒订单的字典 | Dict with price/equity/position/logs/rejections.
        """
        return {  # 构造状态快照字典 | Build the state snapshot dict
            "price": self.current_price,  # 当前价格 | Current price
            "equity": self.equity,  # 账户净值 | Account equity
            "position": self.position,  # 持仓快照 | Position snapshot
            "logs": list(self.logs),  # 日志列表副本，避免外部改动内部状态 | Copy of logs to avoid external mutation
            "rejected_orders": list(self.rejected_orders),  # 被拒订单列表副本 | Copy of rejected orders
        }
