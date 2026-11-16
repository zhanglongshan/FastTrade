"""统一交易执行层的公共数据结构与账户账本。

回测 / 模拟盘 / 实盘三种模式共用同一套 :class:`StrategyAPI` 下单接口，
差异仅在"订单如何成交"——由不同的执行器（Executor）决定：

- 回测：在下一根 K 线开盘按历史价撮合（见 backtest.engine）。
- 模拟盘：按行情快照价即时虚拟撮合（见 execution.paper）。
- 实盘：按币安实时价即时撮合（见 execution.live）。

账户资金与持仓的记账规则集中在本模块的 :class:`AccountLedger`，
保证三种模式的盈亏、手续费、滑点口径完全一致。
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class RunMode(str, Enum):
    """策略运行模式。"""

    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class OrderSide(str, Enum):
    """订单方向，与策略 API 方法一一对应。"""

    BUY = "buy"
    SELL = "sell"
    SELLSHORT = "sellshort"
    BUYCOVER = "buycover"


@dataclass
class Fill:
    """一笔成交。"""

    side: str
    qty: float
    price: float
    fee: float
    pnl: float | None
    reason: str
    signal_price: float
    timestamp: str = ""


@dataclass
class OrderRequest:
    """执行器收到的下单请求。"""

    side: str
    qty: float
    signal_price: float
    reason: str = ""
    symbol: str = ""
    client_order_id: str = ""


class AccountLedger:
    """账户账本：现金、多空持仓、成本与已实现盈亏。

    撮合数学与 backtest 的 TradeAPI 完全一致，便于回测/模拟/实盘结果可比。
    """

    def __init__(
        self,
        initial_cash: float,
        fee_rate: float = 0.001,
        slippage: float = 0.0,
    ) -> None:
        """初始化账本。

        参数:
        - initial_cash: 初始现金。
        - fee_rate: 单边手续费率，0.001 表示 0.1%。
        - slippage: 滑点比例，0.001 表示 0.1%。
        """
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.fee_rate = float(fee_rate)
        self.slippage = float(slippage)
        self.long_qty = 0.0
        self.short_qty = 0.0
        self.long_cost = 0.0
        self.short_proceeds = 0.0
        self.realized_pnl = 0.0
        self.total_fee = 0.0

    def equity_at(self, price: float) -> float:
        """按给定价格计算账户净值 = 现金 + 多头市值 - 空头市值。"""
        return self.cash + self.long_qty * price - self.short_qty * price

    def position(self) -> dict[str, float]:
        """当前持仓快照。"""
        return {
            "long_qty": self.long_qty,
            "short_qty": self.short_qty,
            "long_cost": self.long_cost,
            "short_proceeds": self.short_proceeds,
        }

    def snapshot(self, price: float = 0.0) -> dict[str, float]:
        """导出可持久化的账户快照。"""
        return {
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "long_qty": self.long_qty,
            "short_qty": self.short_qty,
            "long_cost": self.long_cost,
            "short_proceeds": self.short_proceeds,
            "realized_pnl": self.realized_pnl,
            "total_fee": self.total_fee,
            "equity": self.equity_at(price) if price > 0 else 0.0,
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any] | None,
        fee_rate: float = 0.001,
        slippage: float = 0.0,
    ) -> "AccountLedger":
        """从持久化快照恢复账本。"""
        data = snapshot or {}
        ledger = cls(
            initial_cash=float(data.get("initial_cash") or 10000.0),
            fee_rate=fee_rate,
            slippage=slippage,
        )
        ledger.cash = float(data.get("cash", ledger.cash))
        ledger.long_qty = float(data.get("long_qty", 0.0))
        ledger.short_qty = float(data.get("short_qty", 0.0))
        ledger.long_cost = float(data.get("long_cost", 0.0))
        ledger.short_proceeds = float(data.get("short_proceeds", 0.0))
        ledger.realized_pnl = float(data.get("realized_pnl", 0.0))
        ledger.total_fee = float(data.get("total_fee", 0.0))
        return ledger

    def execute(self, order: OrderRequest, raw_price: float, timestamp: str = "") -> Fill | None:
        """按原始市价撮合一笔订单，应用滑点与手续费后更新账本。

        数量会按可用现金/持仓自动裁剪（与回测一致）；无法成交时返回 None。

        参数:
        - order: 下单请求。
        - raw_price: 执行器提供的原始价格（模拟盘/实盘为实时价）。
        - timestamp: 成交时间标签。

        返回:
        - Fill | None: 成交记录，数量不足时返回 None。
        """
        if raw_price <= 0:
            return None
        is_buy_side = order.side in {OrderSide.BUY.value, OrderSide.BUYCOVER.value}
        price = raw_price * (1 + self.slippage) if is_buy_side else raw_price * (1 - self.slippage)
        if price <= 0:
            return None

        qty = float(order.qty)
        if order.side == OrderSide.BUY.value:
            affordable = self.cash / (price * (1 + self.fee_rate)) if price > 0 else 0.0
            qty = min(qty, affordable)
        elif order.side == OrderSide.SELL.value:
            qty = min(qty, self.long_qty)
        elif order.side == OrderSide.BUYCOVER.value:
            qty = min(qty, self.short_qty)
            affordable = self.cash / (price * (1 + self.fee_rate)) if price > 0 else 0.0
            qty = min(qty, affordable)
        # sellshort 开空：不产生即时现金支出，也不按现有空头持仓削减

        if qty <= 1e-12:
            return None

        notional = qty * price
        fee = notional * self.fee_rate
        pnl: float | None = None

        if order.side == OrderSide.BUY.value:
            self.cash -= notional + fee
            total_cost = self.long_cost * self.long_qty + notional
            self.long_qty += qty
            self.long_cost = total_cost / self.long_qty
        elif order.side == OrderSide.SELL.value:
            self.cash += notional - fee
            pnl = (price - self.long_cost) * qty - fee
            self.long_qty -= qty
            if self.long_qty <= 1e-12:
                self.long_qty = 0.0
                self.long_cost = 0.0
        elif order.side == OrderSide.SELLSHORT.value:
            self.cash += notional - fee
            total_proceeds = self.short_proceeds * self.short_qty + notional
            self.short_qty += qty
            self.short_proceeds = total_proceeds / self.short_qty
        else:  # buycover
            self.cash -= notional + fee
            pnl = (self.short_proceeds - price) * qty - fee
            self.short_qty -= qty
            if self.short_qty <= 1e-12:
                self.short_qty = 0.0
                self.short_proceeds = 0.0

        if pnl is not None:
            self.realized_pnl += pnl
        self.total_fee += fee

        return Fill(
            side=order.side,
            qty=qty,
            price=price,
            fee=fee,
            pnl=pnl,
            reason=order.reason,
            signal_price=order.signal_price,
            timestamp=timestamp,
        )


def fill_to_dict(fill: Fill) -> dict[str, Any]:
    """成交记录转字典，便于落库与 JSON 序列化。"""
    return asdict(fill)


@dataclass
class RuntimeStats:
    """实时运行实例的运行统计。"""

    mode: str = RunMode.PAPER.value
    ticks: int = 0
    last_price: float = 0.0
    last_error: str | None = None
    logs: list[str] = field(default_factory=list)
