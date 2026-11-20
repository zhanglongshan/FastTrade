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
  - 模拟盘：按"下一根开盘价"虚拟撮合，并受 L1 盘口深度约束（见 execution.runner）；
    Paper: virtually fill at the next bar open, capped by L1 book depth.
  - 实盘：调用币安下单，回查确认真实成交后按真实成交量/均价/手续费记账（见 execution.runner）。
    Live: place on Binance, confirm the fill, then book the real quantity/price/fee.
- 资金与持仓的记账规则集中在 :class:`AccountLedger`，撮合数学与回测 TradeAPI 一致。
  All cash/position bookkeeping is centralized in AccountLedger, matching backtest math.
"""

from dataclasses import asdict, dataclass, field  # 导入 dataclass 工具 | Import dataclass helpers for data containers
from enum import Enum  # 导入 Enum 基类 | Import Enum base class for enumerations
from typing import Any  # 导入 Any 类型 | Import Any for generic dict/value typing

from ..strategy.trading_base import apply_slippage as _apply_slippage  # 复用共享滑点定价函数 | Reuse the shared slippage pricing helper
from .risk import RiskSettings, gap_fill_price, long_stop_price, short_stop_price  # 导入风控规则 | Import risk rules

# 杠杆取值范围：1 倍等价于现货全额，125 倍是币安 U 本位合约的常见上限
# Leverage bounds: 1x is spot-equivalent, 125x is a typical Binance USD-M ceiling.
MIN_LEVERAGE = 1.0  # 最小杠杆（现货）| Minimum leverage (spot)
MAX_LEVERAGE = 125.0  # 最大杠杆 | Maximum leverage
DEFAULT_MAINTENANCE_MARGIN_RATE = 0.005  # 默认维持保证金率 0.5% | Default maintenance margin rate (0.5%)


def normalize_leverage(value: Any) -> float:  # 归一化杠杆倍数 | Normalize a leverage value
    """把任意输入兜底成合法的杠杆倍数 | Coerce any input into a valid leverage value.

    目的 | Purpose:
        杠杆来自前端表单、数据库旧行与快照三处，可能是 None、字符串或越界值；
        统一在这里裁剪，避免 0 或负数导致除零、也避免超出交易所上限。
        Leverage arrives from forms, legacy DB rows and snapshots, possibly None, a string or
        out of range. Clamping here prevents a divide-by-zero and keeps values within exchange limits.

    参数 | Parameters:
        value (Any): 原始值 | Raw value.

    返回 | Returns:
        float: 落在 [MIN_LEVERAGE, MAX_LEVERAGE] 的杠杆倍数 | Leverage clamped into the allowed range.
    """
    try:  # 兼容字符串与 None | Tolerate strings and None
        number = float(value or MIN_LEVERAGE)  # 转 float，空值按 1 倍 | Coerce to float, empty means 1x
    except (TypeError, ValueError):  # 无法转换时按现货处理 | Fall back to spot on unconvertible input
        return MIN_LEVERAGE  # 回退 1 倍 | Fall back to 1x
    return min(max(number, MIN_LEVERAGE), MAX_LEVERAGE)  # 裁剪到合法区间 | Clamp into the allowed range


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
    - qty_ratio: 按比例开仓时的比例；>0 表示撮合时必须用成交价重算数量
      | Ratio for ratio-sized entries; >0 means the fill must re-size at the fill price.
    """

    side: str  # 交易方向 | Order side
    qty: float  # 请求数量（按比例开仓时为信号价口径的预估值）| Requested quantity
    signal_price: float  # 策略信号价格 | Strategy signal price
    reason: str = ""  # 下单原因，默认为空串 | Order reason, default empty
    symbol: str = ""  # 交易对，默认为空串 | Trading symbol, default empty
    client_order_id: str = ""  # 自定义订单号，默认为空串 | Custom order id, default empty
    qty_ratio: float = 0.0  # 按比例开仓的比例，0 表示固定数量 | Entry ratio, 0 means a fixed quantity


class AccountLedger:  # 账户账本：现金/持仓/成本/盈亏 | Account ledger: cash/positions/cost/PnL
    """账户账本：现金、多空持仓、成本与已实现盈亏 | Account ledger: cash, long/short positions, cost and realized PnL.

    目的：集中管理资金与持仓记账，撮合数学与回测的 TradeAPI 完全一致，
    使回测/模拟/实盘结果可比。
    Purpose: Centralize cash and position bookkeeping; matching math is identical to
    the backtest TradeAPI so results across modes are comparable.

    关键属性 | Key attributes:
    - initial_cash/cash: 初始现金与当前现金 | Initial and current cash.
    - fee_rate/slippage: 手续费率与滑点比例 | Fee rate and slippage ratio.
    - leverage: 杠杆倍数，1 表示现货全额，>1 表示合约保证金交易 | Leverage; 1 is spot, >1 is margin trading.
    - long_qty/short_qty: 多头/空头持仓数量 | Long/short position quantities.
    - long_cost/short_proceeds: 多头均价成本/空头均价所得 | Average long cost / short proceeds.
    - realized_pnl/total_fee: 累计已实现盈亏与累计手续费 | Cumulative realized PnL and total fees.
    """

    def __init__(  # 初始化账本 | Initialize ledger
        self,  # 实例自身引用 | Instance reference
        initial_cash: float,  # 初始现金 | Initial cash
        fee_rate: float = 0.001,  # 单边手续费率 | One-way fee rate
        slippage: float = 0.0,  # 滑点比例 | Slippage ratio
        *,  # 强制后续参数为关键字参数 | Force following params to be keyword-only
        risk: RiskSettings | None = None,  # 风控配置，None 表示关闭 | Risk settings, None disables risk
        leverage: float = MIN_LEVERAGE,  # 杠杆倍数，默认 1 倍（现货）| Leverage, default 1x (spot)
        maintenance_margin_rate: float = DEFAULT_MAINTENANCE_MARGIN_RATE,  # 维持保证金率 | Maintenance margin rate
    ) -> None:  # 构造器无返回值 | Constructor returns None
        """初始化账本 | Initialize the ledger.

        参数 | Parameters:
        - initial_cash (float): 初始现金 | Initial cash.
        - fee_rate (float): 单边手续费率，0.001 表示 0.1% | One-way fee rate, 0.001 = 0.1%.
        - slippage (float): 滑点比例，0.001 表示 0.1% | Slippage ratio, 0.001 = 0.1%.
        - risk (RiskSettings | None): 风控配置，None 时全部关闭 | Risk settings, all disabled when None.
        - leverage (float): 杠杆倍数，1 表示现货全额，>1 表示合约保证金交易 | Leverage; 1 is spot, >1 is margin.
        - maintenance_margin_rate (float): 维持保证金率，用于强平判定 | Maintenance margin rate used for liquidation.

        返回 | Returns:
        - None: 构造器不返回显式值 | Constructor returns no explicit value.
        """
        self.initial_cash = float(initial_cash)  # 保存初始现金并转 float | Store initial cash as float
        self.cash = float(initial_cash)  # 当前现金初始化为初始现金 | Init current cash to initial cash
        self.fee_rate = float(fee_rate)  # 保存手续费率并转 float | Store fee rate as float
        self.slippage = float(slippage)  # 保存滑点比例并转 float | Store slippage ratio as float
        self.leverage = normalize_leverage(leverage)  # 归一化并保存杠杆倍数 | Normalize and store leverage
        # 维持保证金率裁剪到 [0, 0.5)：过大会让强平价贴近开仓价，过小则失去保护意义
        # Clamp the maintenance rate into [0, 0.5): too large liquidates at entry, too small offers no protection.
        self.maintenance_margin_rate = min(max(float(maintenance_margin_rate), 0.0), 0.5)  # 保存维持保证金率 | Store the maintenance rate
        self.long_qty = 0.0  # 初始化多头持仓数量为 0 | Init long position to 0
        self.short_qty = 0.0  # 初始化空头持仓数量为 0 | Init short position to 0
        self.long_cost = 0.0  # 初始化多头均价成本为 0 | Init average long cost to 0
        self.short_proceeds = 0.0  # 初始化空头均价所得为 0 | Init average short proceeds to 0
        self.realized_pnl = 0.0  # 初始化累计已实现盈亏为 0 | Init cumulative realized PnL to 0
        self.total_fee = 0.0  # 初始化累计手续费为 0 | Init cumulative total fee to 0
        # 风控状态：持仓期极值用于移动止损，净值峰值用于回撤熔断
        # Risk state: holding-period extremes for trailing stops, equity peak for the drawdown breaker.
        self.risk = risk or RiskSettings()  # 保存风控配置，None 时为全关闭 | Store risk config, all disabled when None
        self.long_peak = 0.0  # 多头持仓期间最高价，0 表示无持仓 | Highest price while long; 0 means flat
        self.short_trough = 0.0  # 空头持仓期间最低价，0 表示无持仓 | Lowest price while short; 0 means flat
        self.peak_equity = float(initial_cash)  # 账户净值历史峰值，初值为初始资金 | Historical equity peak, starts at initial cash
        self.halted = False  # 回撤熔断后置 True，禁止新开仓 | Set True after the drawdown breaker trips
        self.liquidated = False  # 已被强制平仓后置 True | Set True once the position was force-liquidated
        # 盘口深度不足而未成交的剩余量：随快照持久化，下一次 tick 继续尝试撮合。
        # 不记录这部分会让模拟盘/实盘"悄悄少成交"，与回测的全量成交口径产生方向性偏差。
        # Unfilled remainders left by book-depth caps: persisted with the snapshot and retried on
        # the next tick, so paper/live cannot silently under-fill versus the backtest.
        self.remainders: list[OrderRequest] = []  # 待补成交的剩余订单 | Remainder orders to retry

    @property
    def inverse_leverage(self) -> float:  # 杠杆倒数 = 保证金率 | Inverse leverage, i.e. the margin ratio
        """杠杆倒数（保证金占名义本金的比例）| Inverse leverage, the margin share of notional.

        返回 | Returns:
            float: 1/leverage，1 倍时为 1 | 1/leverage, equal to 1 at 1x.
        """
        return 1.0 / self.leverage  # 直接取倒数 | Return the reciprocal

    def equity_at(self, price: float) -> float:  # 按给定价格计算账户净值 | Compute equity at given price
        """按给定价格计算账户净值 | Compute account equity at the given price.

        公式 | Formula:
            净值 = 现金 + 多头市值 - 多头借款 - 空头市值 + 空头已收借款
                 = 现金 + 多头数量×(价格 - 成本×(1-1/杠杆))
                        - 空头数量×(价格 - 所得×(1-1/杠杆))
            杠杆为 1 时 (1-1/杠杆)=0，退化为"现金 + 多头市值 - 空头市值"，与现货口径完全一致。
            equity = cash + long_qty×(price - cost×(1-1/L)) - short_qty×(price - proceeds×(1-1/L)).
            At L=1 the debt terms vanish and it reduces to the cash + long value - short value
            formula, byte-for-byte identical to the legacy spot accounting.

        参数 | Parameters:
        - price (float): 用于估值标的的价格 | Price used to value positions.

        返回 | Returns:
        - float: 账户净值 | Account equity.
        """
        debt = 1.0 - self.inverse_leverage  # 借款占名义本金的比例 | Borrowed share of notional
        long_value = self.long_qty * price - self.long_qty * self.long_cost * debt  # 多头市值扣除借款 | Long value net of debt
        short_value = self.short_qty * price - self.short_qty * self.short_proceeds * debt  # 空头市值扣除已收借款 | Short value net of the credit
        return self.cash + long_value - short_value  # 现金加多头减空头 | Cash plus long minus short

    def notional_at_cost(self) -> float:  # 按开仓成本计算的名义本金 | Notional at entry cost
        """按开仓成本计算的持仓名义本金 | Position notional measured at entry cost.

        目的 | Purpose:
            保证金占用按**开仓成本**计量（而不是浮动的市价），否则浮盈会让保证金占用
            凭空变化，导致可开仓额度抖动。
            Margin is measured at entry cost rather than the moving mark price, so unrealized
            PnL cannot make the used margin (and thus the buying power) jitter.

        返回 | Returns:
            float: 多空名义本金之和 | Combined long and short notional.
        """
        return self.long_qty * self.long_cost + self.short_qty * self.short_proceeds  # 多空成本名义合计 | Sum of long and short notional

    def margin_used(self) -> float:  # 已占用保证金 | Margin currently in use
        """已占用的保证金 = 名义本金 / 杠杆 | Margin in use = notional / leverage.

        返回 | Returns:
            float: 占用保证金金额 | Locked margin amount.
        """
        return self.notional_at_cost() * self.inverse_leverage  # 名义本金乘保证金率 | Notional times the margin ratio

    def free_margin(self, price: float) -> float:  # 可用保证金 | Free margin
        """可用保证金 = 净值 - 已占用保证金（不为负）| Free margin = equity - used margin, floored at zero.

        参数 | Parameters:
        - price (float): 估值价格 | Valuation price.

        返回 | Returns:
            float: 可继续开仓的保证金 | Margin still available for new positions.
        """
        return max(0.0, self.equity_at(price) - self.margin_used())  # 净值扣除占用保证金 | Equity minus used margin

    def entry_budget(self, price: float, *, side: str) -> float:  # 开仓预算（名义本金口径）| Entry budget in notional terms
        """计算某个方向可动用的开仓预算 | Compute the entry budget for one side.

        目的 | Purpose:
            买入用"可用保证金×杠杆"作为名义本金上限；开空同理。1 倍杠杆时退化为旧口径
            （买入用可用现金、开空用净值），保证现货模式逐项不变。
            Longs may use free_margin × leverage of notional and shorts the same. At 1x this
            degrades to the legacy budget (cash for buys, equity for shorts) so spot behaviour
            stays identical to the previous implementation.

        参数 | Parameters:
        - price (float): 计价价格 | Pricing price.
        - side (str): buy 或 sellshort | "buy" or "sellshort".

        返回 | Returns:
            float: 名义本金预算，非负 | Non-negative notional budget.
        """
        if self.leverage <= MIN_LEVERAGE:  # 现货口径：保持旧实现 | Spot: keep the legacy behaviour
            return max(0.0, self.cash if side == OrderSide.BUY.value else self.equity_at(price))  # 买入看现金，开空看净值 | Cash for buys, equity for shorts
        return self.free_margin(price) * self.leverage  # 合约口径：可用保证金加杠杆 | Margin trading: free margin times leverage

    def liquidation_price(self) -> float:  # 理论强平价 | Theoretical liquidation price
        """计算当前持仓的理论强平价 | Compute the theoretical liquidation price.

        推导 | Derivation:
            强平条件：净值 ≤ 维持保证金率 × 名义本金。
            多头：现金 + q×P - q×成本×(1-1/L) = mm×q×P
                  → P = (成本×(1-1/L) - 现金/q) / (1-mm)
            空头：现金 - q×P + q×所得×(1-1/L) = mm×q×P
                  → P = (现金/q + 所得×(1-1/L)) / (1+mm)
            Liquidation fires when equity ≤ mm × notional; solving for the price gives the
            two formulas above for longs and shorts respectively.

        返回 | Returns:
            float: 强平价；1 倍杠杆、无持仓或结果非正时返回 0 | Liquidation price, or 0 when on spot, flat, or non-positive.
        """
        if self.leverage <= MIN_LEVERAGE:  # 现货不会被强平 | Spot cannot be liquidated
            return 0.0  # 返回 0 表示无强平价 | Zero means no liquidation price
        debt = 1.0 - self.inverse_leverage  # 借款占名义本金比例 | Borrowed share of notional
        mm = self.maintenance_margin_rate  # 维持保证金率 | Maintenance margin rate
        price = 0.0  # 默认无强平价 | Default to no liquidation price
        if self.long_qty > 0 and mm < 1:  # 多头强平价 | Long liquidation price
            price = (self.long_cost * debt - self.cash / self.long_qty) / (1.0 - mm)  # 多头公式 | Long formula
        elif self.short_qty > 0:  # 空头强平价 | Short liquidation price
            price = (self.cash / self.short_qty + self.short_proceeds * debt) / (1.0 + mm)  # 空头公式 | Short formula
        return price if price > 0 else 0.0  # 非正价格视为不适用 | Non-positive prices do not apply

    def is_liquidatable(self, price: float) -> bool:  # 是否触发强平 | Whether liquidation is due
        """判断当前净值是否已跌破维持保证金要求 | Check whether equity fell below maintenance margin.

        参数 | Parameters:
        - price (float): 最新价 | Latest price.

        返回 | Returns:
            bool: 需要强平时返回 True | True when a liquidation is required.
        """
        if self.leverage <= MIN_LEVERAGE or (self.long_qty <= 0 and self.short_qty <= 0):  # 现货或无持仓不强平 | Spot or flat positions never liquidate
            return False  # 不触发 | No liquidation
        notional = (self.long_qty + self.short_qty) * price  # 按市价计的名义本金 | Notional at market
        return notional > 0 and self.equity_at(price) <= notional * self.maintenance_margin_rate  # 净值跌破维持保证金 | Equity below maintenance margin

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

    def snapshot(self, price: float = 0.0) -> dict[str, Any]:  # 导出可持久化账户快照 | Export persistable snapshot
        """导出可持久化的账户快照 | Export a persistable account snapshot.

        参数 | Parameters:
        - price (float): 用于计算净值的价格，<=0 时净值为 0 | Price for equity, equity is 0 when <=0.

        返回 | Returns:
        - dict[str, Any]: 账户各状态字段字典（含待补成交订单）| Account state fields, including remainders.
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
            "long_peak": self.long_peak,  # 多头持仓期最高价（移动止损用）| Highest price while long (trailing stop)
            "short_trough": self.short_trough,  # 空头持仓期最低价（移动止损用）| Lowest price while short (trailing stop)
            "peak_equity": self.peak_equity,  # 净值历史峰值（回撤熔断用）| Equity peak (drawdown breaker)
            "halted": self.halted,  # 熔断标志 | Halted flag
            "leverage": self.leverage,  # 杠杆倍数（快照回读与前端展示）| Leverage for restore and display
            "margin_used": self.margin_used(),  # 已占用保证金 | Margin in use
            "maintenance_margin_rate": self.maintenance_margin_rate,  # 维持保证金率 | Maintenance margin rate
            "liquidated": self.liquidated,  # 是否已被强平 | Whether the position was liquidated
            "remainders": [asdict(order) for order in self.remainders],  # 待补成交的剩余订单 | Remainders to retry
        }

    @classmethod  # 声明类方法装饰器 | Declare classmethod decorator
    def from_snapshot(  # 从快照恢复账本 | Restore ledger from snapshot
        cls,  # 类本身引用 | Reference to the class itself
        snapshot: dict[str, Any] | None,  # 持久化快照字典，可为 None | Persisted snapshot dict, may be None
        fee_rate: float = 0.001,  # 单边手续费率 | One-way fee rate
        slippage: float = 0.0,  # 滑点比例 | Slippage ratio
        *,  # 强制后续参数为关键字参数 | Force following params to be keyword-only
        risk: RiskSettings | None = None,  # 风控配置，None 表示关闭 | Risk settings, None disables risk
        leverage: float | None = None,  # 杠杆倍数；None 时读快照，仍缺失则按 1 倍 | Leverage; snapshot fallback, default 1x
    ) -> "AccountLedger":  # 返回恢复的账本实例 | Return restored ledger instance
        """从持久化快照恢复账本 | Restore a ledger from a persisted snapshot.

        参数 | Parameters:
        - snapshot (dict[str, Any] | None): 快照字典，None 时按空字典处理 | Snapshot dict, treated as empty when None.
        - fee_rate (float): 手续费率 | Fee rate.
        - slippage (float): 滑点比例 | Slippage ratio.
        - risk (RiskSettings | None): 风控配置 | Risk settings.
        - leverage (float | None): 杠杆倍数。实例表里的 leverage 是权威配置（允许人工调整），
          因此优先于快照内记录的历史值；两者都缺失时按现货 1 倍处理。
          | Leverage. The instance row is authoritative (it can be edited by hand), so it wins
          over the historical snapshot value; both missing means spot 1x.

        返回 | Returns:
        - AccountLedger: 按快照字段恢复的账本实例 | Ledger restored from snapshot fields.
        """
        data = snapshot or {}  # 快照为 None 时退化为空字典 | Fall back to empty dict when snapshot is None
        ledger = cls(  # 用初始现金等构造新账本 | Build a new ledger with initial cash etc.
            initial_cash=float(data.get("initial_cash") or 10000.0),  # 读取初始现金，缺失回退 10000 | Read initial cash, default 10000
            fee_rate=fee_rate,  # 设置手续费率 | Set fee rate
            slippage=slippage,  # 设置滑点比例 | Set slippage ratio
            risk=risk,  # 注入风控配置 | Inject risk settings
            leverage=normalize_leverage(leverage if leverage is not None else data.get("leverage")),  # 杠杆：参数优先，其次快照 | Leverage: argument first, then snapshot
            maintenance_margin_rate=float(data.get("maintenance_margin_rate") or DEFAULT_MAINTENANCE_MARGIN_RATE),  # 维持保证金率 | Maintenance margin rate
        )
        ledger.cash = float(data.get("cash", ledger.cash))  # 恢复当前现金，缺失保留现值 | Restore cash, keep default
        ledger.long_qty = float(data.get("long_qty", 0.0))  # 恢复多头持仓，缺失为 0 | Restore long qty, default 0
        ledger.short_qty = float(data.get("short_qty", 0.0))  # 恢复空头持仓，缺失为 0 | Restore short qty, default 0
        ledger.long_cost = float(data.get("long_cost", 0.0))  # 恢复多头成本，缺失为 0 | Restore long cost, default 0
        ledger.short_proceeds = float(data.get("short_proceeds", 0.0))  # 恢复空头所得，缺失为 0 | Restore short proceeds, default 0
        ledger.realized_pnl = float(data.get("realized_pnl", 0.0))  # 恢复已实现盈亏，缺失为 0 | Restore realized PnL, default 0
        ledger.total_fee = float(data.get("total_fee", 0.0))  # 恢复累计手续费，缺失为 0 | Restore total fee, default 0
        ledger.long_peak = float(data.get("long_peak", 0.0))  # 恢复多头持仓期最高价 | Restore the long holding peak
        ledger.short_trough = float(data.get("short_trough", 0.0))  # 恢复空头持仓期最低价 | Restore the short holding trough
        ledger.peak_equity = float(data.get("peak_equity") or data.get("initial_cash") or ledger.cash)  # 恢复净值峰值 | Restore the equity peak
        ledger.halted = bool(data.get("halted", False))  # 恢复熔断标志 | Restore the halted flag
        ledger.liquidated = bool(data.get("liquidated", False))  # 恢复强平标志 | Restore the liquidation flag
        # 待补成交订单按字段重建：旧快照没有该键时保持空列表，向前兼容
        # Rebuild remainders field by field; older snapshots simply have no key and stay empty.
        ledger.remainders = [
            OrderRequest(
                side=str(item.get("side") or ""),  # 方向 | Side
                qty=float(item.get("qty") or 0.0),  # 剩余数量 | Remaining quantity
                signal_price=float(item.get("signal_price") or 0.0),  # 信号价 | Signal price
                reason=str(item.get("reason") or ""),  # 下单原因 | Order reason
                symbol=str(item.get("symbol") or ""),  # 交易对 | Symbol
                client_order_id=str(item.get("client_order_id") or ""),  # 幂等订单号 | Client order id
                qty_ratio=float(item.get("qty_ratio") or 0.0),  # 开仓比例 | Entry ratio
            )
            for item in (data.get("remainders") or [])
            if isinstance(item, dict)
        ]
        return ledger  # 返回恢复后的账本实例 | Return the restored ledger

    def add_remainder(self, order: OrderRequest) -> None:  # 登记一笔待补成交订单 | Register an unfilled remainder
        """登记因盘口深度不足而未成交的剩余订单，供下一个 tick 继续撮合。

        目的 | Purpose:
            深度裁剪把"超出 L1 挂单量"的部分直接丢掉，等于假设这部分永远做不成；
            回测却按全量成交记账。把剩余量留下来重试，模拟盘/实盘才不会系统性少做。
            Depth capping used to discard the excess, implicitly assuming it could never fill,
            while the backtest books the full size. Retrying the remainder keeps paper/live from
            systematically under-trading.

        参数 | Parameters:
            order (OrderRequest): 剩余未成交的订单（数量为差额）| The unfilled remainder order.

        返回 | Returns:
            None: 无返回值 | No return value.
        """
        if order.qty <= 1e-12:  # 剩余量过小没有重试价值 | Too small to be worth retrying
            return  # 直接忽略 | Ignore it
        self.remainders.append(order)  # 追加到待补队列 | Append to the retry queue
        # 队列上限保护：极端行情下深度长期不足时避免无限堆积，只保留最近的若干笔
        # Bound the queue so a persistently thin book cannot grow it without limit.
        if len(self.remainders) > 5:  # 最多保留 5 笔 | Keep at most five orders
            del self.remainders[:-5]  # 丢弃最旧的 | Drop the oldest ones

    def take_remainders(self) -> list[OrderRequest]:  # 取出并清空待补订单 | Drain the remainder queue
        """取出待补成交订单并清空队列（由下一次 tick 调用）| Drain remainders for the next tick.

        返回 | Returns:
            list[OrderRequest]: 待重试的订单列表 | Orders to retry.
        """
        orders = list(self.remainders)  # 复制当前队列 | Copy the queue
        self.remainders = []  # 清空原队列 | Clear the original
        return orders  # 返回待重试订单 | Return the drained orders

    def execute(  # 按原始价撮合一笔订单 | Fill one order at raw price
        self,  # 实例自身引用 | Instance reference
        order: OrderRequest,  # 下单请求 | The order request
        raw_price: float,  # 原始成交价 | Raw fill price
        timestamp: str = "",  # 成交时间标签 | Fill timestamp label
        *,  # 强制后续参数为关键字参数 | Force following params to be keyword-only
        apply_slippage: bool = True,  # 是否再叠加滑点；真实成交价应传 False | Whether to add slippage; pass False for real fills
        fee_override: float | None = None,  # 交易所实际手续费；None 时按费率估算 | Real exchange fee; estimated from the rate when None
    ) -> Fill | None:  # 返回成交记录或 None | Return a fill or None
        """按原始市价撮合一笔订单，应用滑点与手续费后更新账本 | Fill an order at raw market price, apply slippage/fee, update ledger.

        数量会按可用现金/持仓自动裁剪（与回测一致）；无法成交时返回 None。
        Quantity is auto-capped by available cash/holdings (like backtest); returns None if unfillable.

        参数 | Parameters:
        - order (OrderRequest): 下单请求 | The order request.
        - raw_price (float): 执行器提供的原始价格（模拟盘/实盘为实时价）| Raw price from executor.
        - timestamp (str): 成交时间标签，默认空串 | Fill timestamp label, default empty.
        - apply_slippage (bool): 是否叠加滑点；实盘真实成交价已经含滑点，应传 False 避免二次计算
          | Whether to add slippage; real exchange fills already include it, so pass False.
        - fee_override (float | None): 交易所回填的真实手续费；给定后不再按费率估算。
          实盘手续费常因 VIP 等级、BNB 抵扣而与配置费率不同，用真实值才能让
          ``total_fee`` 与账户一致。
          | The real fee backfilled from the exchange. Live fees differ from the configured
          rate because of VIP tiers and BNB discounts; using the real value keeps ``total_fee``
          consistent with the account.

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
        price = _apply_slippage(raw_price, is_buy_side, self.slippage) if apply_slippage else float(raw_price)  # 买加滑点卖减滑点；真实成交价不再叠加 | Slippage by side; skip for real fills
        if price <= 0:  # 滑点后价格非正时无法成交 | Cannot fill if slipped price is non-positive
            return None  # 返回 None 表示未成交 | Return None for no fill

        qty = float(order.qty)  # 读取请求数量并转 float | Read requested quantity as float
        margin_ratio = self.inverse_leverage  # 保证金占名义本金比例（1 倍时为 1）| Margin share of notional (1 at spot)
        if order.side == OrderSide.BUY.value:  # 买入开多：按购买力裁剪数量 | Buy/open-long: cap qty by buying power
            # 购买力是名义本金口径：数量 × 价格 × 保证金率 + 手续费 必须落在可用资金内
            # Buying power is notional: qty × price × margin ratio plus the fee must fit the budget.
            budget = self.entry_budget(price, side=order.side)  # 该方向名义本金预算 | Notional budget for the side
            affordable = budget / (price * (1 + self.fee_rate * self.leverage)) if price > 0 else 0.0  # 折成数量 | Convert into quantity
            qty = min(qty, affordable)  # 取请求量与可承担量的较小值 | Take min of requested and affordable
        elif order.side == OrderSide.SELL.value:  # 卖出平多：按多头持仓裁剪 | Sell/close-long: cap by long holdings
            qty = min(qty, self.long_qty)  # 数量不超过多头持仓 | Cap qty by long holdings
        elif order.side == OrderSide.BUYCOVER.value:  # 买入平空：受空头持仓与现金双重约束 | Buy/cover: capped by short holdings and cash
            qty = min(qty, self.short_qty)  # 先按空头持仓裁剪 | First cap by short holdings
            # 平空每单位实际占用的现金 = 买回价格 - 归还借币所得中释放的部分，再加手续费
            # Cash actually spent per unit = buy-back price minus the released short credit, plus the fee.
            released = self.short_proceeds * (1 - margin_ratio)  # 每单位释放的借币所得 | Credit released per unit
            unit_cost = price - released + price * self.fee_rate  # 每单位净现金支出 | Net cash cost per unit
            if unit_cost > 0:  # 只有净支出为正时才需要限制数量 | Only cap when the net cost is positive
                qty = min(qty, max(0.0, self.cash) / unit_cost)  # 按可用现金裁剪 | Cap by available cash
        else:  # sellshort 开空 | Sell/open-short
            # 开空只需缴纳保证金：qty × 价格 × 保证金率 + 手续费 不得超过购买力
            # Opening a short only posts margin, so the same notional budget applies.
            budget = self.entry_budget(price, side=order.side)  # 该方向名义本金预算 | Notional budget for the side
            affordable = budget / (price * (1 + self.fee_rate * self.leverage)) if price > 0 else 0.0  # 折成数量 | Convert into quantity
            qty = min(qty, affordable)  # 不超过购买力可承担量 | Cap by affordable quantity

        if qty <= 1e-12:  # 数量极小（近似 0）时视为无法成交 | Treat near-zero qty as unfillable
            return None  # 返回 None 表示未成交 | Return None for no fill

        notional = qty * price  # 计算成交金额（数量 × 价格）| Compute notional (qty × price)
        if fee_override is None:  # 未提供真实手续费时按费率估算 | Estimate from the rate when no real fee is given
            fee = notional * self.fee_rate  # 计算手续费（金额 × 费率）| Compute fee (notional × rate)
        else:  # 提供了交易所回填的真实手续费 | A real exchange fee was provided
            fee = max(0.0, float(fee_override))  # 真实手续费不允许为负 | Real fees cannot be negative
        pnl: float | None = None  # 初始化盈亏为 None，仅平仓时赋值 | Init PnL to None, set only on closing

        # 平仓时释放的借款：开仓只按 1/杠杆缴纳保证金，剩余部分记为浮动借款，
        # 平仓时用"卖出所得 - 归还借款"结算，1 倍杠杆下借款为 0，现金流与旧口径一致。
        # Debt released on closing: only 1/leverage was posted as margin at entry, so the
        # remainder is settled as "proceeds minus debt repayment"; at 1x the debt is zero and
        # the cash flows match the legacy implementation exactly.
        debt_ratio = 1.0 - margin_ratio  # 借款占名义本金比例 | Borrowed share of notional

        if order.side == OrderSide.BUY.value:  # 买入开多分支 | Buy/open-long branch
            self.cash -= notional * margin_ratio + fee  # 只扣保证金与手续费 | Deduct margin and fee only
            total_cost = self.long_cost * self.long_qty + notional  # 计算新的多头总成本 | Compute new total long cost
            self.long_qty += qty  # 增加多头持仓数量 | Increase long position
            self.long_cost = total_cost / self.long_qty  # 更新多头均价成本 | Update average long cost
        elif order.side == OrderSide.SELL.value:  # 卖出平多分支 | Sell/close-long branch
            self.cash += notional - qty * self.long_cost * debt_ratio - fee  # 所得扣除借款归还 | Proceeds minus debt repayment
            pnl = (price - self.long_cost) * qty - fee  # 计算平多盈亏 | Compute realized PnL for long close
            self.long_qty -= qty  # 减少多头持仓数量 | Reduce long position
            if self.long_qty <= 1e-12:  # 多头持仓清零时重置成本 | Reset cost when long position is wiped out
                self.long_qty = 0.0  # 将多头持仓归零 | Zero out long quantity
                self.long_cost = 0.0  # 将多头成本归零 | Zero out long cost
        elif order.side == OrderSide.SELLSHORT.value:  # 卖出开空分支 | Sell/open-short branch
            self.cash += notional * margin_ratio - fee  # 只收到保证金（借币所得不计入可用现金）| Credit margin only
            total_proceeds = self.short_proceeds * self.short_qty + notional  # 计算新的空头总所得 | Compute new total short proceeds
            self.short_qty += qty  # 增加空头持仓数量 | Increase short position
            self.short_proceeds = total_proceeds / self.short_qty  # 更新空头均价所得 | Update average short proceeds
        else:  # buycover  # 其余情况视为买入平空 | Treat remaining case as buy/cover
            self.cash -= notional - qty * self.short_proceeds * debt_ratio + fee  # 买回价扣掉释放的借币所得 | Buy-back price net of released credit
            pnl = (self.short_proceeds - price) * qty - fee  # 计算平空盈亏 | Compute realized PnL for short cover
            self.short_qty -= qty  # 减少空头持仓数量 | Reduce short position
            if self.short_qty <= 1e-12:  # 空头持仓清零时重置所得 | Reset proceeds when short position is wiped out
                self.short_qty = 0.0  # 将空头持仓归零 | Zero out short quantity
                self.short_proceeds = 0.0  # 将空头所得归零 | Zero out short proceeds

        # 维护风控所需的"持仓期极值"与"净值峰值"：开仓时初始化为成交价，加仓时取更优极值，平仓后清零
        # Maintain the holding-period extremes and equity peak used by risk rules:
        # initialise at fill price, extend on adds, reset when flat.
        if self.long_qty > 0:  # 仍持有多头 | Still long
            self.long_peak = max(self.long_peak, price) if self.long_peak > 0 else price  # 扩展多头最高价 | Extend the long peak
        else:  # 已无多头 | No long position
            self.long_peak = 0.0  # 清零多头峰值 | Reset the long peak
        if self.short_qty > 0:  # 仍持有空头 | Still short
            self.short_trough = min(self.short_trough, price) if self.short_trough > 0 else price  # 扩展空头最低价 | Extend the short trough
        else:  # 已无空头 | No short position
            self.short_trough = 0.0  # 清零空头低点 | Reset the short trough
        self.peak_equity = max(self.peak_equity, self.equity_at(price))  # 更新净值历史峰值 | Update the equity peak

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


    def mark(  # 用最新行情更新风控状态 | Update risk state with the latest market data
        self,
        price: float,  # 最新价/收盘价 | Latest or close price
        *,  # 强制关键字参数 | Force keyword-only params
        low: float | None = None,  # 本根最低价（回测用）| Bar low (backtest)
        high: float | None = None,  # 本根最高价（回测用）| Bar high (backtest)
        update_equity_peak: bool = True,  # 是否更新净值峰值 | Whether to lift the equity peak
    ) -> None:  # 无返回值 | No return value
        """用最新行情更新移动止损极值与净值峰值 | Update trailing extremes and the equity peak with the latest price.

        目的 | Purpose:
            回测每根 K 线调用一次、实盘每个 tick 调用一次，保证风控状态（峰值/低点/净值峰值）
            与"当前时间点可见的行情"严格同步。
            Called once per bar in backtest and once per tick in live so risk state stays in
            sync with the market data visible at that moment.

        参数 | Parameters:
            price (float): 最新价或收盘价 | Latest or close price.
            low (float | None): 本根最低价，None 时用 price | Bar low, price when None.
            high (float | None): 本根最高价，None 时用 price | Bar high, price when None.
            update_equity_peak (bool): 是否抬升 ``peak_equity``。盘中风控心跳应按信号周期口径
                更新（传 False），否则 1 分钟粒度会捕捉到大周期内的瞬时高点，回撤熔断比回测
                更容易触发。
                | Whether to lift ``peak_equity``. Intra-bar heartbeats pass False so the peak
                follows the signal period; otherwise the one-minute granularity catches intra-bar
                highs and trips the drawdown breaker more readily than the backtest does.
        """
        if price <= 0:  # 价格无效时不做任何更新 | Do nothing on invalid prices
            return  # 直接返回 | Return early
        low_px = low if low and low > 0 else price  # 取有效最低价 | Resolve a valid low
        high_px = high if high and high > 0 else price  # 取有效最高价 | Resolve a valid high
        if self.long_qty > 0:  # 持有多头时维护最高价 | Track the peak while long
            self.long_peak = max(self.long_peak, high_px) if self.long_peak > 0 else high_px  # 扩展多头峰值 | Extend the long peak
        if self.short_qty > 0:  # 持有空头时维护最低价 | Track the trough while short
            self.short_trough = min(self.short_trough, low_px) if self.short_trough > 0 else low_px  # 扩展空头低点 | Extend the short trough
        if update_equity_peak:  # 净值峰值按信号周期口径更新 | The equity peak follows the signal period
            self.peak_equity = max(self.peak_equity, self.equity_at(price))  # 更新净值峰值 | Update the equity peak

    def risk_orders(  # 生成风控强制平仓订单 | Produce forced-exit orders from risk rules
        self,
        open_price: float,  # 本根/本次开盘价，用于跳空定价 | Bar/tick open price for gap pricing
        *,  # 强制关键字参数 | Force keyword-only params
        low: float | None = None,  # 本根最低价 | Bar low
        high: float | None = None,  # 本根最高价 | Bar high
        stop_fill_at_market: bool = False,  # 止损是否按"发现时市价"成交 | Whether stops fill at the price seen on detection
    ) -> list[tuple[OrderRequest, float]]:  # 返回 (订单, 成交价) 列表 | Return (order, fill price) pairs
        """评估止损/移动止损/回撤熔断，返回需要立即成交的平仓订单 | Evaluate stops and return forced exits.

        目的 | Purpose:
            回测与实盘共用同一判定：多头用最低价判触发、空头用最高价判触发。
            Backtest and live share this check: longs trigger on the low, shorts on the high.

        参数 | Parameters:
            open_price (float): 本根开盘价或当前 tick 价 | Bar open or current tick price.
            low (float | None): 本根最低价 | Bar low.
            high (float | None): 本根最高价 | Bar high.
            stop_fill_at_market (bool): 止损成交价口径。False（回测）按"止损价与开盘价中更差者"
                记账，等价于交易所侧止损单；True（模拟盘/实盘）按发现破位时的市价成交，
                因为这类系统只能在收盘后下市价单，真实成交价就是当时价格。
                | Stop fill basis. False (backtest) books the worse of the stop and the open, i.e.
                an exchange-side stop order; True (paper/live) books the market price seen when the
                breach was detected, because these modes can only send a market order at that time.

        返回 | Returns:
            list[tuple[OrderRequest, float]]: 需要立即执行的平仓订单及其成交价 | Forced exits with fill prices.

        流程 | Flow:
            强平优先（净值跌破维持保证金则全平并 halted）→ 回撤熔断 →
            多头止损 → 空头止损 → 返回订单列表。
            Liquidation first (flatten and halt once equity breaches maintenance margin) →
            drawdown breaker → long stop → short stop → return orders.
        """
        orders: list[tuple[OrderRequest, float]] = []  # 收集风控订单 | Collect risk orders
        if open_price <= 0:  # 价格无效时不下任何单 | Place nothing on invalid prices
            return orders  # 返回空列表 | Return empty list
        low_px = low if low and low > 0 else open_price  # 取有效最低价 | Resolve a valid low
        high_px = high if high and high > 0 else open_price  # 取有效最高价 | Resolve a valid high

        # 0) 强制平仓：合约账户在净值跌破维持保证金时必须被交易所强平，优先级高于策略风控。
        #    多头用最低价、空头用最高价判触发（与用户止损一致），跳空时按更差的开盘价成交。
        #    Liquidation is the venue-level rule and outranks strategy risk: a margin account is
        #    force-closed once equity breaches maintenance margin. Longs trigger on the low,
        #    shorts on the high, and gaps fill at the worse open, mirroring the stop logic.
        if self.leverage > MIN_LEVERAGE and (self.long_qty > 0 or self.short_qty > 0):  # 仅合约持仓需要判定 | Only margin positions can be liquidated
            liq_price = self.liquidation_price()  # 理论强平价 | Theoretical liquidation price
            if liq_price > 0 and (low_px <= liq_price if self.long_qty > 0 else high_px >= liq_price):  # 本根区间触及强平价 | The bar range breached the price
                self.liquidated = True  # 标记已强平 | Flag the liquidation
                self.halted = True  # 强平后禁止再交易 | No more trading after liquidation
                if self.long_qty > 0:  # 多头强平 | Liquidate the long
                    fill = open_price if stop_fill_at_market else gap_fill_price(open_price, liq_price, True)  # 市价口径或跳空强平价 | Market or gap-adjusted price
                    orders.append((OrderRequest(side=OrderSide.SELL.value, qty=self.long_qty, signal_price=liq_price, reason="liquidation"), fill))  # 市价全平 | Flatten at market
                if self.short_qty > 0:  # 空头强平 | Liquidate the short
                    fill = open_price if stop_fill_at_market else gap_fill_price(open_price, liq_price, False)  # 市价口径或跳空强平价 | Market or gap-adjusted price
                    orders.append((OrderRequest(side=OrderSide.BUYCOVER.value, qty=self.short_qty, signal_price=liq_price, reason="liquidation"), fill))  # 市价全平 | Flatten at market
                return orders  # 强平后不再评估其他规则 | Stop evaluating after liquidation

        # 1) 最大回撤熔断：净值从峰值回撤超阈值 → 立即全平并禁止后续开仓
        # 1) Max-drawdown breaker: flatten and halt once equity drops below the threshold from its peak.
        if self.risk.max_drawdown > 0 and self.peak_equity > 0:  # 仅在启用且有峰值时判断 | Evaluate only when enabled and a peak exists
            drawdown = 1.0 - self.equity_at(open_price) / self.peak_equity  # 当前相对峰值的回撤比例 | Drawdown from the peak
            if drawdown >= self.risk.max_drawdown:  # 达到熔断阈值 | Breaker threshold reached
                self.halted = True  # 置熔断标志 | Set the halted flag
                if self.long_qty > 0:  # 有多头则平多 | Close the long if any
                    orders.append((OrderRequest(side=OrderSide.SELL.value, qty=self.long_qty, signal_price=open_price, reason="max_drawdown"), open_price))  # 市价平多 | Flatten the long at market
                if self.short_qty > 0:  # 有空头则平空 | Cover the short if any
                    orders.append((OrderRequest(side=OrderSide.BUYCOVER.value, qty=self.short_qty, signal_price=open_price, reason="max_drawdown"), open_price))  # 市价平空 | Flatten the short at market
                return orders  # 熔断后不再做其他风控 | Stop evaluating other rules after halting

        # 2) 多头止损 / 移动止损
        # 2) Long fixed and trailing stop.
        if self.long_qty > 0:  # 仅在持有多头时判断 | Only when long
            stop = long_stop_price(self.long_cost, self.long_peak, self.risk)  # 计算多头止损价 | Compute the long stop
            if stop > 0 and low_px <= stop:  # 最低价触及止损 | The low touched the stop
                fill = open_price if stop_fill_at_market else gap_fill_price(open_price, stop, True)  # 市价口径或跳空止损价 | Market or gap-adjusted stop price
                orders.append((OrderRequest(side=OrderSide.SELL.value, qty=self.long_qty, signal_price=stop, reason="stop_loss"), fill))  # 止损平多 | Stop out the long

        # 3) 空头止损 / 移动止损
        # 3) Short fixed and trailing stop.
        if self.short_qty > 0:  # 仅在持有空头时判断 | Only when short
            stop = short_stop_price(self.short_proceeds, self.short_trough, self.risk)  # 计算空头止损价 | Compute the short stop
            if stop > 0 and high_px >= stop:  # 最高价触及止损 | The high touched the stop
                fill = open_price if stop_fill_at_market else gap_fill_price(open_price, stop, False)  # 市价口径或跳空止损价 | Market or gap-adjusted stop price
                orders.append((OrderRequest(side=OrderSide.BUYCOVER.value, qty=self.short_qty, signal_price=stop, reason="stop_loss"), fill))  # 止损平空 | Stop out the short

        return orders  # 返回风控订单列表 | Return the risk order list

    def reconcile(  # 用交易所真实状态校正账本 | Correct the ledger with the real exchange state
        self,
        *,
        cash: float | None = None,  # 真实可用计价资产 | Real free quote balance
        long_qty: float | None = None,  # 真实基础资产数量 | Real base-asset quantity
        short_qty: float | None = None,  # 真实空头数量（现货恒为 0）| Real short quantity (always 0 on spot)
        total_fee: float | None = None,  # 真实累计手续费 | Real cumulative fee
        realized_pnl: float | None = None,  # 复算的已实现盈亏 | Recomputed realized PnL
        price: float = 0.0,  # 最新价，用于成本与净值兜底 | Latest price for cost/equity fallbacks
    ) -> dict[str, float]:  # 返回修正项（空字典表示完全一致）| Return adjustments (empty when consistent)
        """用交易所真实状态覆盖账本，并返回所有修正项 | Overwrite the ledger with real exchange state and report the deltas.

        目的 | Purpose:
            仅校正现金与多头是不够的：空头残留、手续费累计偏差、已实现盈亏偏差都会
            让绩效统计长期漂移。本方法把四类状态一次校正到位，并把每项差异返回给
            调用方落日志，便于审计"账本与交易所为何不一致"。
            Correcting only cash and the long position is not enough: leftover shorts, drifted
            fee totals and wrong realized PnL all skew performance over time. This method fixes
            all four in one pass and returns every delta so the caller can audit the cause.

        参数 | Parameters:
            - cash (float | None): 真实可用资金，None 表示不校正 | Real cash, None skips the field.
            - long_qty (float | None): 真实多头持仓，None 表示不校正 | Real long position.
            - short_qty (float | None): 真实空头持仓，None 表示不校正 | Real short position.
            - total_fee (float | None): 真实累计手续费 | Real cumulative fee.
            - realized_pnl (float | None): 复算的已实现盈亏 | Recomputed realized PnL.
            - price (float): 最新价，用于成本未知时的兜底 | Latest price for cost fallbacks.

        返回 | Returns:
            - dict[str, float]: 字段名 → 修正量（新值 - 旧值）| Field -> delta (new minus old).

        流程 | Flow:
            逐字段比较 → 超阈值才覆盖 → 持仓归零时清理成本/极值 → 重算净值峰值 → 返回差异。
            Compare field by field → overwrite only past the tolerance → clear cost/extremes when
            flat → refresh the equity peak → return the deltas.
        """
        adjustments: dict[str, float] = {}  # 收集所有修正量 | Collect every adjustment
        if cash is not None:  # 校正现金 | Correct cash
            real_cash = float(cash)  # 归一化为 float | Normalize to float
            if abs(real_cash - self.cash) > 1e-8:  # 超过容差才校正 | Only adjust past the tolerance
                adjustments["cash"] = real_cash - self.cash  # 记录差异 | Record the delta
                self.cash = real_cash  # 以交易所为准 | Trust the exchange
        if long_qty is not None:  # 校正多头持仓 | Correct the long position
            real_long = max(0.0, float(long_qty))  # 持仓不允许为负 | Positions cannot be negative
            if abs(real_long - self.long_qty) > 1e-12:  # 超过容差才校正 | Only adjust past the tolerance
                adjustments["long_qty"] = real_long - self.long_qty  # 记录差异 | Record the delta
                self.long_qty = real_long  # 覆盖持仓 | Overwrite the position
                if real_long <= 0:  # 已无持仓 | Now flat
                    self.long_cost = 0.0  # 清零成本 | Reset cost
                    self.long_peak = 0.0  # 清零极值 | Reset the peak
                elif self.long_cost <= 0 and price > 0:  # 有持仓但成本未知 | Holding with an unknown cost
                    self.long_cost = float(price)  # 用最新价兜底 | Fall back to the latest price
        if short_qty is not None:  # 校正空头持仓 | Correct the short position
            real_short = max(0.0, float(short_qty))  # 持仓不允许为负 | Positions cannot be negative
            if abs(real_short - self.short_qty) > 1e-12:  # 超过容差才校正 | Only adjust past the tolerance
                adjustments["short_qty"] = real_short - self.short_qty  # 记录差异 | Record the delta
                self.short_qty = real_short  # 覆盖持仓 | Overwrite the position
                if real_short <= 0:  # 已无空头 | Now flat
                    self.short_proceeds = 0.0  # 清零所得 | Reset proceeds
                    self.short_trough = 0.0  # 清零极值 | Reset the trough
                elif self.short_proceeds <= 0 and price > 0:  # 有空头但成本未知 | Short with an unknown entry
                    self.short_proceeds = float(price)  # 用最新价兜底 | Fall back to the latest price
        if total_fee is not None:  # 校正累计手续费 | Correct the cumulative fee
            real_fee = max(0.0, float(total_fee))  # 手续费不允许为负 | Fees cannot be negative
            if abs(real_fee - self.total_fee) > 1e-9:  # 超过容差才校正 | Only adjust past the tolerance
                adjustments["total_fee"] = real_fee - self.total_fee  # 记录差异 | Record the delta
                self.total_fee = real_fee  # 覆盖累计值 | Overwrite the total
        if realized_pnl is not None:  # 校正已实现盈亏 | Correct the realized PnL
            real_pnl = float(realized_pnl)  # 盈亏可为负 | PnL may be negative
            if abs(real_pnl - self.realized_pnl) > 1e-9:  # 超过容差才校正 | Only adjust past the tolerance
                adjustments["realized_pnl"] = real_pnl - self.realized_pnl  # 记录差异 | Record the delta
                self.realized_pnl = real_pnl  # 覆盖累计值 | Overwrite the total
        if price > 0:  # 价格有效时刷新净值峰值 | Refresh the equity peak when the price is valid
            self.peak_equity = max(self.peak_equity, self.equity_at(price))  # 只升不降，避免熔断误触发 | Only raise it so the breaker cannot misfire
        return adjustments  # 返回修正项 | Return the adjustments

    def resume(self) -> None:  # 解除回撤熔断 | Clear the drawdown halt
        """解除熔断标志，并重置净值峰值为当前状态 | Clear the halt flag and re-base the equity peak.

        目的 | Purpose:
            熔断是"停手"而非"停机"，人工干预或新资金注入后需要能恢复交易。
            Halting means "stop trading", not "shut down"; allow resuming after manual intervention.

        返回 | Returns:
            None。
        """
        self.halted = False  # 清除熔断标志 | Clear the halted flag
        self.liquidated = False  # 清除强平标志，允许重新建仓 | Clear the liquidation flag so new positions are allowed
        self.peak_equity = max(self.cash, self.initial_cash * 0.0001)  # 以当前现金重置峰值 | Re-base the peak on current cash


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
