"""统一交易执行层的公共数据结构与账户账本 | Common data structures and account ledger for the unified execution layer.

目的：为回测/模拟盘/实盘三种模式定义共享的数据结构（运行模式、订单方向、成交、
下单请求、运行统计）与统一的账户账本 :class:`AccountLedger`，保证三种模式的盈亏、
手续费、滑点口径完全一致。
Purpose: Define shared data structures (run mode, order side, fill, order request,
runtime stats) and a unified :class:`AccountLedger` so that PnL, fees, and slippage
are computed identically across backtest/paper/live modes.

主要流程 | Main flow:
- 三种模式共用同一套 :class:`~.api.StrategyAPI` 下单接口，差异仅在"订单如何成交"，
  由不同执行器（Executor）决定：
  All modes share the same StrategyAPI; only "how orders fill" differs by executor:
  - 回测：在下一根 K 线开盘按历史价撮合（见 backtest.engine）；
    Backtest: match at next bar open using historical price.
  - 模拟盘：按行情快照价即时虚拟撮合（见 execution.paper）；
    Paper: virtually fill instantly at the snapshot price.
  - 实盘：按币安实时价即时撮合（见 execution.live）。
    Live: fill instantly at Binance live price.
- 资金与持仓的记账规则集中在 :class:`AccountLedger`，撮合数学与回测 TradeAPI 一致。
  All cash/position bookkeeping is centralized in AccountLedger, matching backtest math.
"""

from dataclasses import asdict, dataclass, field  # 导入 dataclass 工具 | Import dataclass helpers for data containers
from enum import Enum  # 导入 Enum 基类 | Import Enum base class for enumerations
from typing import Any  # 导入 Any 类型 | Import Any for generic dict/value typing


class RunMode(str, Enum):  # 策略运行模式枚举 | Strategy run-mode enumeration
    """策略运行模式 | Strategy run modes.

    列出系统支持的三种运行模式：回测、模拟盘、实盘。
    Lists the three supported run modes: backtest, paper, and live.

    关键属性 | Key attributes:
    - BACKTEST: 回测模式 | Backtest mode.
    - PAPER: 模拟盘模式 | Paper-trading mode.
    - LIVE: 实盘模式 | Live-trading mode.
    """

    BACKTEST = "backtest"  # 回测模式 | Backtest mode
    PAPER = "paper"  # 模拟盘模式 | Paper-trading mode
    LIVE = "live"  # 实盘模式 | Live-trading mode


class OrderSide(str, Enum):  # 订单方向枚举 | Order-side enumeration
    """订单方向，与策略 API 方法一一对应 | Order sides, one-to-one with strategy API methods.

    列出四种交易动作方向，与 StrategyAPI 的四个下单方法一一对应。
    Lists four trading actions corresponding to the four StrategyAPI order methods.

    关键属性 | Key attributes:
    - BUY: 开多/加多 | Open/increase long.
    - SELL: 平多/减多 | Close/reduce long.
    - SELLSHORT: 开空 | Open short.
    - BUYCOVER: 平空 | Cover short.
    """

    BUY = "buy"  # 买入开多方向 | Buy/open-long side
    SELL = "sell"  # 卖出平多方向 | Sell/close-long side
    SELLSHORT = "sellshort"  # 卖出开空方向 | Sell/open-short side
    BUYCOVER = "buycover"  # 买入平空方向 | Buy/cover-short side


@dataclass  # 声明数据类装饰器 | Declare dataclass decorator
class Fill:  # 一笔成交记录 | A single fill record
    """一笔成交 | A single fill.

    记录一次订单撮合的结果，包含方向、数量、成交价、手续费、盈亏、原因等。
    Records the result of one order fill: side, quantity, price, fee, PnL, reason, etc.

    关键属性 | Key attributes:
    - side: 成交方向 | Fill side.
    - qty: 成交数量 | Filled quantity.
    - price: 含滑点的成交价 | Fill price including slippage.
    - fee: 手续费 | Fee paid.
    - pnl: 已实现盈亏（开仓为 None）| Realized PnL (None when opening).
    - reason: 下单原因 | Order reason.
    - signal_price: 策略信号价 | Strategy signal price.
    - timestamp: 成交时间标签 | Fill timestamp label.
    """

    side: str  # 成交方向（buy/sell/sellshort/buycover）| Fill side
    qty: float  # 成交数量 | Filled quantity
    price: float  # 含滑点的成交价格 | Fill price including slippage
    fee: float  # 本笔成交手续费 | Fee for this fill
    pnl: float | None  # 已实现盈亏，开仓为 None | Realized PnL, None on opening
    reason: str  # 下单原因 | Order reason
    signal_price: float  # 策略发出信号时的价格 | Strategy signal price
    timestamp: str = ""  # 成交时间标签，默认为空串 | Fill timestamp label, default empty


@dataclass  # 声明数据类装饰器 | Declare dataclass decorator
class OrderRequest:  # 执行器收到的下单请求 | Order request received by executor
    """执行器收到的下单请求 | Order request received by an executor.

    封装策略意图，由 StrategyAPI 构造并交给执行器撮合。
    Encapsulates strategy intent, built by StrategyAPI and handed to an executor.

    关键属性 | Key attributes:
    - side: 交易方向 | Order side.
    - qty: 请求数量 | Requested quantity.
    - signal_price: 信号价格 | Signal price.
    - reason: 下单原因 | Order reason.
    - symbol: 交易对 | Trading symbol.
    - client_order_id: 自定义订单号（幂等）| Custom order id for idempotency.
    """

    side: str  # 交易方向 | Order side
    qty: float  # 请求数量 | Requested quantity
    signal_price: float  # 策略信号价格 | Strategy signal price
    reason: str = ""  # 下单原因，默认为空串 | Order reason, default empty
    symbol: str = ""  # 交易对，默认为空串 | Trading symbol, default empty
    client_order_id: str = ""  # 自定义订单号，默认为空串 | Custom order id, default empty


class AccountLedger:  # 账户账本：现金/持仓/成本/盈亏 | Account ledger: cash/positions/cost/PnL
    """账户账本：现金、多空持仓、成本与已实现盈亏 | Account ledger: cash, long/short positions, cost and realized PnL.

    目的：集中管理资金与持仓记账，撮合数学与回测的 TradeAPI 完全一致，
    使回测/模拟/实盘结果可比。
    Purpose: Centralize cash and position bookkeeping; matching math is identical to
    the backtest TradeAPI so results across modes are comparable.

    关键属性 | Key attributes:
    - initial_cash/cash: 初始现金与当前现金 | Initial and current cash.
    - fee_rate/slippage: 手续费率与滑点比例 | Fee rate and slippage ratio.
    - long_qty/short_qty: 多头/空头持仓数量 | Long/short position quantities.
    - long_cost/short_proceeds: 多头均价成本/空头均价所得 | Average long cost / short proceeds.
    - realized_pnl/total_fee: 累计已实现盈亏与累计手续费 | Cumulative realized PnL and total fees.
    """

    def __init__(  # 初始化账本 | Initialize ledger
        self,  # 实例自身引用 | Instance reference
        initial_cash: float,  # 初始现金 | Initial cash
        fee_rate: float = 0.001,  # 单边手续费率 | One-way fee rate
        slippage: float = 0.0,  # 滑点比例 | Slippage ratio
    ) -> None:  # 构造器无返回值 | Constructor returns None
        """初始化账本 | Initialize the ledger.

        参数 | Parameters:
        - initial_cash (float): 初始现金 | Initial cash.
        - fee_rate (float): 单边手续费率，0.001 表示 0.1% | One-way fee rate, 0.001 = 0.1%.
        - slippage (float): 滑点比例，0.001 表示 0.1% | Slippage ratio, 0.001 = 0.1%.

        返回 | Returns:
        - None: 构造器不返回显式值 | Constructor returns no explicit value.
        """
        self.initial_cash = float(initial_cash)  # 保存初始现金并转 float | Store initial cash as float
        self.cash = float(initial_cash)  # 当前现金初始化为初始现金 | Init current cash to initial cash
        self.fee_rate = float(fee_rate)  # 保存手续费率并转 float | Store fee rate as float
        self.slippage = float(slippage)  # 保存滑点比例并转 float | Store slippage ratio as float
        self.long_qty = 0.0  # 初始化多头持仓数量为 0 | Init long position to 0
        self.short_qty = 0.0  # 初始化空头持仓数量为 0 | Init short position to 0
        self.long_cost = 0.0  # 初始化多头均价成本为 0 | Init average long cost to 0
        self.short_proceeds = 0.0  # 初始化空头均价所得为 0 | Init average short proceeds to 0
        self.realized_pnl = 0.0  # 初始化累计已实现盈亏为 0 | Init cumulative realized PnL to 0
        self.total_fee = 0.0  # 初始化累计手续费为 0 | Init cumulative total fee to 0

    def equity_at(self, price: float) -> float:  # 按给定价格计算账户净值 | Compute equity at given price
        """按给定价格计算账户净值 = 现金 + 多头市值 - 空头市值 | Compute equity = cash + long value - short value.

        参数 | Parameters:
        - price (float): 用于估值标的的价格 | Price used to value positions.

        返回 | Returns:
        - float: 账户净值 | Account equity.
        """
        return self.cash + self.long_qty * price - self.short_qty * price  # 现金加多头市值减空头市值 | Cash plus long value minus short value

    def position(self) -> dict[str, float]:  # 当前持仓快照 | Current position snapshot
        """当前持仓快照 | Current position snapshot.

        返回 | Returns:
        - dict[str, float]: 含多空数量与成本/所得字典 | Dict with long/short qty and cost/proceeds.
        """
        return {  # 构造持仓快照字典 | Build the position snapshot dict
            "long_qty": self.long_qty,  # 多头持仓数量 | Long position quantity
            "short_qty": self.short_qty,  # 空头持仓数量 | Short position quantity
            "long_cost": self.long_cost,  # 多头均价成本 | Average long cost
            "short_proceeds": self.short_proceeds,  # 空头均价所得 | Average short proceeds
        }

    def snapshot(self, price: float = 0.0) -> dict[str, float]:  # 导出可持久化账户快照 | Export persistable snapshot
        """导出可持久化的账户快照 | Export a persistable account snapshot.

        参数 | Parameters:
        - price (float): 用于计算净值的价格，<=0 时净值为 0 | Price for equity, equity is 0 when <=0.

        返回 | Returns:
        - dict[str, float]: 账户各状态字段字典 | Dict of account state fields.
        """
        return {  # 构造账户快照字典 | Build the account snapshot dict
            "initial_cash": self.initial_cash,  # 初始现金 | Initial cash
            "cash": self.cash,  # 当前现金 | Current cash
            "long_qty": self.long_qty,  # 多头持仓数量 | Long position quantity
            "short_qty": self.short_qty,  # 空头持仓数量 | Short position quantity
            "long_cost": self.long_cost,  # 多头均价成本 | Average long cost
            "short_proceeds": self.short_proceeds,  # 空头均价所得 | Average short proceeds
            "realized_pnl": self.realized_pnl,  # 累计已实现盈亏 | Cumulative realized PnL
            "total_fee": self.total_fee,  # 累计手续费 | Cumulative total fee
            "equity": self.equity_at(price) if price > 0 else 0.0,  # 价格有效时算净值，否则 0 | Equity when price valid, else 0
        }

    @classmethod  # 声明类方法装饰器 | Declare classmethod decorator
    def from_snapshot(  # 从快照恢复账本 | Restore ledger from snapshot
        cls,  # 类本身引用 | Reference to the class itself
        snapshot: dict[str, Any] | None,  # 持久化快照字典，可为 None | Persisted snapshot dict, may be None
        fee_rate: float = 0.001,  # 单边手续费率 | One-way fee rate
        slippage: float = 0.0,  # 滑点比例 | Slippage ratio
    ) -> "AccountLedger":  # 返回恢复的账本实例 | Return restored ledger instance
        """从持久化快照恢复账本 | Restore a ledger from a persisted snapshot.

        参数 | Parameters:
        - snapshot (dict[str, Any] | None): 快照字典，None 时按空字典处理 | Snapshot dict, treated as empty when None.
        - fee_rate (float): 手续费率 | Fee rate.
        - slippage (float): 滑点比例 | Slippage ratio.

        返回 | Returns:
        - AccountLedger: 按快照字段恢复的账本实例 | Ledger restored from snapshot fields.
        """
        data = snapshot or {}  # 快照为 None 时退化为空字典 | Fall back to empty dict when snapshot is None
        ledger = cls(  # 用初始现金等构造新账本 | Build a new ledger with initial cash etc.
            initial_cash=float(data.get("initial_cash") or 10000.0),  # 读取初始现金，缺失回退 10000 | Read initial cash, default 10000
            fee_rate=fee_rate,  # 设置手续费率 | Set fee rate
            slippage=slippage,  # 设置滑点比例 | Set slippage ratio
        )
        ledger.cash = float(data.get("cash", ledger.cash))  # 恢复当前现金，缺失保留现值 | Restore cash, keep default
        ledger.long_qty = float(data.get("long_qty", 0.0))  # 恢复多头持仓，缺失为 0 | Restore long qty, default 0
        ledger.short_qty = float(data.get("short_qty", 0.0))  # 恢复空头持仓，缺失为 0 | Restore short qty, default 0
        ledger.long_cost = float(data.get("long_cost", 0.0))  # 恢复多头成本，缺失为 0 | Restore long cost, default 0
        ledger.short_proceeds = float(data.get("short_proceeds", 0.0))  # 恢复空头所得，缺失为 0 | Restore short proceeds, default 0
        ledger.realized_pnl = float(data.get("realized_pnl", 0.0))  # 恢复已实现盈亏，缺失为 0 | Restore realized PnL, default 0
        ledger.total_fee = float(data.get("total_fee", 0.0))  # 恢复累计手续费，缺失为 0 | Restore total fee, default 0
        return ledger  # 返回恢复后的账本实例 | Return the restored ledger

    def execute(self, order: OrderRequest, raw_price: float, timestamp: str = "") -> Fill | None:  # 按原始价撮合一笔订单 | Fill one order at raw price
        """按原始市价撮合一笔订单，应用滑点与手续费后更新账本 | Fill an order at raw market price, apply slippage/fee, update ledger.

        数量会按可用现金/持仓自动裁剪（与回测一致）；无法成交时返回 None。
        Quantity is auto-capped by available cash/holdings (like backtest); returns None if unfillable.

        参数 | Parameters:
        - order (OrderRequest): 下单请求 | The order request.
        - raw_price (float): 执行器提供的原始价格（模拟盘/实盘为实时价）| Raw price from executor.
        - timestamp (str): 成交时间标签，默认空串 | Fill timestamp label, default empty.

        返回 | Returns:
        - Fill | None: 成交记录；价格无效或数量不足时为 None | Fill record, or None if invalid price/qty.

        流程 | Flow:
        校验价格 → 计算滑点价 → 按方向裁剪数量 → 计算金额与手续费 →
        按方向更新现金/持仓/成本/盈亏 → 累计盈亏与手续费 → 返回 Fill。
        Validate price → apply slippage → cap qty by side → compute notional/fee →
        update cash/positions/cost/PnL by side → accumulate → return Fill.
        """
        if raw_price <= 0:  # 原始价格非正时无法成交 | Cannot fill when raw price is non-positive
            return None  # 返回 None 表示未成交 | Return None for no fill
        is_buy_side = order.side in {OrderSide.BUY.value, OrderSide.BUYCOVER.value}  # 判断是否为买入方向（含平空）| Determine if side is buy-type (incl. buycover)
        price = raw_price * (1 + self.slippage) if is_buy_side else raw_price * (1 - self.slippage)  # 买加滑点卖减滑点 | Add slippage on buys, subtract on sells
        if price <= 0:  # 滑点后价格非正时无法成交 | Cannot fill if slipped price is non-positive
            return None  # 返回 None 表示未成交 | Return None for no fill

        qty = float(order.qty)  # 读取请求数量并转 float | Read requested quantity as float
        if order.side == OrderSide.BUY.value:  # 买入开多：按可用现金裁剪数量 | Buy/open-long: cap qty by available cash
            affordable = self.cash / (price * (1 + self.fee_rate)) if price > 0 else 0.0  # 计算现金可承担数量（含手续费）| Compute affordable qty including fee
            qty = min(qty, affordable)  # 取请求量与可承担量的较小值 | Take min of requested and affordable
        elif order.side == OrderSide.SELL.value:  # 卖出平多：按多头持仓裁剪 | Sell/close-long: cap by long holdings
            qty = min(qty, self.long_qty)  # 数量不超过多头持仓 | Cap qty by long holdings
        elif order.side == OrderSide.BUYCOVER.value:  # 买入平空：受空头持仓与现金双重约束 | Buy/cover: capped by short holdings and cash
            qty = min(qty, self.short_qty)  # 先按空头持仓裁剪 | First cap by short holdings
            affordable = self.cash / (price * (1 + self.fee_rate)) if price > 0 else 0.0  # 计算现金可承担数量（含手续费）| Compute affordable qty including fee
            qty = min(qty, affordable)  # 再按可承担量裁剪 | Then cap by affordable
        # sellshort 开空：不产生即时现金支出，也不按现有空头持仓削减
        # Sell/open-short: no immediate cash outlay and not capped by existing short holdings

        if qty <= 1e-12:  # 数量极小（近似 0）时视为无法成交 | Treat near-zero qty as unfillable
            return None  # 返回 None 表示未成交 | Return None for no fill

        notional = qty * price  # 计算成交金额（数量 × 价格）| Compute notional (qty × price)
        fee = notional * self.fee_rate  # 计算手续费（金额 × 费率）| Compute fee (notional × rate)
        pnl: float | None = None  # 初始化盈亏为 None，仅平仓时赋值 | Init PnL to None, set only on closing

        if order.side == OrderSide.BUY.value:  # 买入开多分支 | Buy/open-long branch
            self.cash -= notional + fee  # 扣减现金（金额 + 手续费）| Deduct cash by notional plus fee
            total_cost = self.long_cost * self.long_qty + notional  # 计算新的多头总成本 | Compute new total long cost
            self.long_qty += qty  # 增加多头持仓数量 | Increase long position
            self.long_cost = total_cost / self.long_qty  # 更新多头均价成本 | Update average long cost
        elif order.side == OrderSide.SELL.value:  # 卖出平多分支 | Sell/close-long branch
            self.cash += notional - fee  # 增加现金（金额 - 手续费）| Add cash by notional minus fee
            pnl = (price - self.long_cost) * qty - fee  # 计算平多盈亏 | Compute realized PnL for long close
            self.long_qty -= qty  # 减少多头持仓数量 | Reduce long position
            if self.long_qty <= 1e-12:  # 多头持仓清零时重置成本 | Reset cost when long position is wiped out
                self.long_qty = 0.0  # 将多头持仓归零 | Zero out long quantity
                self.long_cost = 0.0  # 将多头成本归零 | Zero out long cost
        elif order.side == OrderSide.SELLSHORT.value:  # 卖出开空分支 | Sell/open-short branch
            self.cash += notional - fee  # 增加现金（金额 - 手续费）| Add cash by notional minus fee
            total_proceeds = self.short_proceeds * self.short_qty + notional  # 计算新的空头总所得 | Compute new total short proceeds
            self.short_qty += qty  # 增加空头持仓数量 | Increase short position
            self.short_proceeds = total_proceeds / self.short_qty  # 更新空头均价所得 | Update average short proceeds
        else:  # buycover  # 其余情况视为买入平空 | Treat remaining case as buy/cover
            self.cash -= notional + fee  # 扣减现金（金额 + 手续费）| Deduct cash by notional plus fee
            pnl = (self.short_proceeds - price) * qty - fee  # 计算平空盈亏 | Compute realized PnL for short cover
            self.short_qty -= qty  # 减少空头持仓数量 | Reduce short position
            if self.short_qty <= 1e-12:  # 空头持仓清零时重置所得 | Reset proceeds when short position is wiped out
                self.short_qty = 0.0  # 将空头持仓归零 | Zero out short quantity
                self.short_proceeds = 0.0  # 将空头所得归零 | Zero out short proceeds

        if pnl is not None:  # 仅平仓产生的盈亏需要累计 | Only accumulate PnL produced by closing
            self.realized_pnl += pnl  # 累加本笔已实现盈亏 | Accumulate realized PnL
        self.total_fee += fee  # 累加本笔手续费 | Accumulate total fee

        return Fill(  # 构造并返回成交记录 | Build and return the fill record
            side=order.side,  # 填充成交方向 | Fill side
            qty=qty,  # 填充实际成交数量 | Fill actual quantity
            price=price,  # 填充含滑点成交价 | Fill slipped price
            fee=fee,  # 填充手续费 | Fill fee
            pnl=pnl,  # 填充盈亏（开仓为 None）| Fill PnL (None on opening)
            reason=order.reason,  # 填充下单原因 | Fill order reason
            signal_price=order.signal_price,  # 填充信号价格 | Fill signal price
            timestamp=timestamp,  # 填充成交时间标签 | Fill timestamp
        )


def fill_to_dict(fill: Fill) -> dict[str, Any]:  # 成交记录转字典 | Convert fill to dict
    """成交记录转字典，便于落库与 JSON 序列化 | Convert a Fill to a dict for persistence and JSON serialization.

    参数 | Parameters:
    - fill (Fill): 成交记录对象 | The fill record.

    返回 | Returns:
    - dict[str, Any]: 成交记录的字典形式 | Dict form of the fill.
    """
    return asdict(fill)  # 使用 dataclasses.asdict 递归转字典 | Use asdict to recursively convert


@dataclass  # 声明数据类装饰器 | Declare dataclass decorator
class RuntimeStats:  # 实时运行实例的运行统计 | Runtime stats of a live running instance
    """实时运行实例的运行统计 | Runtime statistics of a live running instance.

    记录模拟盘/实盘实例的运行模式、tick 数、最新价、最近错误与日志。
    Records the run mode, tick count, latest price, last error, and logs of a live instance.

    关键属性 | Key attributes:
    - mode: 运行模式，默认 paper | Run mode, default paper.
    - ticks: 已执行的 tick 数 | Number of ticks executed.
    - last_price: 最新价格 | Latest price.
    - last_error: 最近错误信息 | Last error message.
    - logs: 运行日志列表 | Runtime log list.
    """

    mode: str = RunMode.PAPER.value  # 运行模式，默认模拟盘 | Run mode, default paper
    ticks: int = 0  # 已执行 tick 数，默认 0 | Executed tick count, default 0
    last_price: float = 0.0  # 最新价格，默认 0 | Latest price, default 0
    last_error: str | None = None  # 最近错误，默认 None | Last error, default None
    logs: list[str] = field(default_factory=list)  # 日志列表，使用工厂函数避免可变默认值共享 | Log list via factory to avoid shared mutable default
