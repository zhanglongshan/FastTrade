"""量化策略 API 封装。

策略通过 api.buy() / api.sell() / api.sellshort() / api.buycover() / api.close_all() 下单；
订单在当前 K 线收盘产生，于下一根 K 线开盘成交（最后一根 K 线按收盘价成交）。
"""

from dataclasses import dataclass
from typing import Any

from .indicators import ema, rsi, sma


@dataclass(frozen=True)
class Candle:
    """单根 K 线。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Order:
    """待执行订单。"""

    side: str
    qty: float
    signal_price: float
    reason: str = ""


@dataclass
class Trade:
    """成交记录。"""

    date: str
    side: str
    qty: float
    price: float
    fee: float
    pnl: float | None
    reason: str
    signal_price: float


class Strategy:
    """策略基类。

    事件模式：子类实现 on_bar(api, candles, index)。
    信号模式：子类实现 populate_entry_trend / populate_exit_trend，参考 freqtrade 的信号列思想。
    """

    name: str = ""
    timeframe: str = ""
    startup_candle_count: int = 0

    def on_bar(self, api: "TradeAPI", candles: list[Candle], index: int) -> None:
        """逐根 K 线回调；默认空实现。"""
        return None

    def populate_entry_trend(self, candles: list[Candle], metadata: dict[str, Any]) -> list[int]:
        """生成开仓信号列表（1=开多，-1=开空，0=无信号）；默认空实现。"""
        return []

    def populate_exit_trend(self, candles: list[Candle], metadata: dict[str, Any]) -> list[int]:
        """生成平仓信号列表（1=平多，-1=平空，0=无信号）；默认空实现。"""
        return []


class TradeAPI:
    """提供给策略使用的交易与指标接口。"""

    def __init__(
        self,
        candles: list[Candle],
        initial_cash: float,
        fee_rate: float = 0.001,
        slippage: float = 0.0,
    ) -> None:
        """初始化账户。

        参数:
        - candles: 完整 K 线序列。
        - initial_cash: 初始资金。
        - fee_rate: 单边手续费率，0.001 表示 0.1%。
        - slippage: 滑点比例，0.001 表示 0.1%。
        """
        self.candles = candles
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.fee_rate = fee_rate
        self.slippage = slippage

        self.long_qty = 0.0
        self.short_qty = 0.0
        self.long_cost = 0.0
        self.short_proceeds = 0.0

        self.trades: list[Trade] = []
        self.logs: list[str] = []
        self.equity_curve: list[dict[str, Any]] = []
        self._pending: list[Order] = []
        self._index = 0

    def set_index(self, index: int) -> None:
        """设置当前 K 线下标。"""
        self._index = max(0, min(index, len(self.candles) - 1))

    @property
    def current_candle(self) -> Candle:
        """当前 K 线。"""
        return self.candles[self._index]

    @property
    def current_price(self) -> float:
        """当前收盘价，作为下单定价基准。"""
        return self.current_candle.close

    @property
    def equity(self) -> float:
        """账户净值 = 现金 + 多头市值 - 空头市值。"""
        price = self.current_price
        return self.cash + self.long_qty * price - self.short_qty * price

    @property
    def position(self) -> dict[str, float]:
        """当前持仓快照。"""
        return {
            "long_qty": self.long_qty,
            "short_qty": self.short_qty,
            "long_cost": self.long_cost,
            "short_proceeds": self.short_proceeds,
        }

    def log(self, message: str) -> None:
        """记录策略日志。"""
        self.logs.append(f"[{self.current_candle.date}] {message}")

    def _closes(self) -> list[float]:
        """仅返回当前及之前的收盘价，避免策略偷看未来数据。"""
        return [candle.close for candle in self.candles[: self._index + 1]]

    def sma(self, period: int) -> list[float | None]:
        """简单移动平均序列。"""
        return sma(self._closes(), period)

    def ema(self, period: int) -> list[float | None]:
        """指数移动平均序列。"""
        return ema(self._closes(), period)

    def rsi(self, period: int = 14) -> list[float | None]:
        """RSI 序列。"""
        return rsi(self._closes(), period)

    def _last(self, values: list[float | None]) -> float | None:
        """取序列最后一个有效值。"""
        return values[-1] if values else None

    def _validate_ratio(self, ratio: float) -> bool:
        """校验仓位比例。"""
        if not 0 < ratio <= 1:
            self.log(f"仓位比例无效：{ratio}，应在 (0, 1] 范围内")
            return False
        return True

    def buy(
        self,
        price: float | None = None,
        amount: float | None = None,
        ratio: float = 0.95,
        reason: str = "",
    ) -> bool:
        """开多/加多。未指定数量时按可用现金的 ratio 比例买入。"""
        if not self._validate_ratio(ratio):
            return False
        signal_price = float(price or self.current_price)
        if signal_price <= 0:
            self.log("买入价格必须大于 0")
            return False
        if amount is None:
            qty = self.cash * ratio / (signal_price * (1 + self.fee_rate + self.slippage))
        else:
            qty = float(amount)
        if qty <= 0:
            self.log("买入数量必须大于 0")
            return False
        self._pending.append(Order(side="buy", qty=qty, signal_price=signal_price, reason=reason))
        return True

    def sell(
        self,
        price: float | None = None,
        amount: float | None = None,
        ratio: float = 1.0,
        reason: str = "",
    ) -> bool:
        """平多/减多。默认卖出全部多头持仓。"""
        if not self._validate_ratio(ratio):
            return False
        if self.long_qty <= 0:
            self.log("当前没有多头持仓，忽略卖出")
            return False
        qty = self.long_qty * ratio if amount is None else min(float(amount), self.long_qty)
        if qty <= 0:
            return False
        self._pending.append(Order(side="sell", qty=qty, signal_price=float(price or self.current_price), reason=reason))
        return True

    def sellshort(
        self,
        price: float | None = None,
        amount: float | None = None,
        ratio: float = 0.5,
        reason: str = "",
    ) -> bool:
        """开空。未指定数量时按账户净值的 ratio 比例开空。"""
        if not self._validate_ratio(ratio):
            return False
        signal_price = float(price or self.current_price)
        if signal_price <= 0:
            self.log("开空价格必须大于 0")
            return False
        if amount is None:
            qty = self.equity * ratio / (signal_price * (1 + self.fee_rate + self.slippage))
        else:
            qty = float(amount)
        if qty <= 0:
            self.log("开空数量必须大于 0")
            return False
        self._pending.append(Order(side="sellshort", qty=qty, signal_price=signal_price, reason=reason))
        return True

    def buycover(
        self,
        price: float | None = None,
        amount: float | None = None,
        ratio: float = 1.0,
        reason: str = "",
    ) -> bool:
        """平空。默认买入平掉全部空头持仓。"""
        if not self._validate_ratio(ratio):
            return False
        if self.short_qty <= 0:
            self.log("当前没有空头持仓，忽略平空")
            return False
        qty = self.short_qty * ratio if amount is None else min(float(amount), self.short_qty)
        if qty <= 0:
            return False
        self._pending.append(Order(side="buycover", qty=qty, signal_price=float(price or self.current_price), reason=reason))
        return True

    def close_all(self, reason: str = "close_all") -> bool:
        """同时平掉多头和空头全部持仓。"""
        has_position = False
        if self.long_qty > 0:
            has_position = self.sell(reason=reason) or has_position
        if self.short_qty > 0:
            has_position = self.buycover(reason=reason) or has_position
        if not has_position:
            self.log("当前没有持仓，无需平仓")
        return has_position

    def execute_pending(self, index: int, final: bool = False) -> None:
        """执行上一根 K 线产生的订单。"""
        if not self._pending:
            return
        candle = self.candles[index]
        open_price = candle.close if final else candle.open
        orders = self._pending
        self._pending = []
        for order in orders:
            if order.side in {"buy", "buycover"}:
                price = open_price * (1 + self.slippage)
            else:
                price = open_price * (1 - self.slippage)
            if price <= 0:
                self.log(f"成交价格无效，取消订单：{order.side}")
                continue

            qty = order.qty
            if order.side == "buy":
                affordable = self.cash / (price * (1 + self.fee_rate))
                qty = min(qty, affordable)
            elif order.side == "buycover":
                affordable = self.cash / (price * (1 + self.fee_rate))
                qty = min(qty, affordable)
            elif order.side == "sell":
                qty = min(qty, self.long_qty)
            else:
                qty = min(qty, self.short_qty)

            if qty <= 1e-12:
                self.log(f"可用资金或持仓不足，取消订单：{order.side}")
                continue

            notional = qty * price
            fee = notional * self.fee_rate
            pnl: float | None = None

            if order.side == "buy":
                self.cash -= notional + fee
                total_cost = self.long_cost * self.long_qty + notional
                self.long_qty += qty
                self.long_cost = total_cost / self.long_qty
            elif order.side == "sell":
                self.cash += notional - fee
                pnl = (price - self.long_cost) * qty - fee
                self.long_qty -= qty
                if self.long_qty <= 1e-12:
                    self.long_qty = 0.0
                    self.long_cost = 0.0
            elif order.side == "sellshort":
                self.cash += notional - fee
                total_proceeds = self.short_proceeds * self.short_qty + notional
                self.short_qty += qty
                self.short_proceeds = total_proceeds / self.short_qty
            else:
                self.cash -= notional + fee
                pnl = (self.short_proceeds - price) * qty - fee
                self.short_qty -= qty
                if self.short_qty <= 1e-12:
                    self.short_qty = 0.0
                    self.short_proceeds = 0.0

            self.trades.append(
                Trade(
                    date=candle.date,
                    side=order.side,
                    qty=qty,
                    price=price,
                    fee=fee,
                    pnl=pnl,
                    reason=order.reason,
                    signal_price=order.signal_price,
                )
            )

    def record_equity(self, index: int) -> None:
        """记录当前净值点。"""
        candle = self.candles[index]
        price = candle.close
        self.equity_curve.append(
            {
                "date": candle.date,
                "equity": round(self.cash + self.long_qty * price - self.short_qty * price, 6),
                "cash": round(self.cash, 6),
                "long_qty": round(self.long_qty, 8),
                "short_qty": round(self.short_qty, 8),
            }
        )
