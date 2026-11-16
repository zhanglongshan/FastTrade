"""策略统一交易接口。

回测 / 模拟盘 / 实盘三种模式向策略暴露完全相同的 :class:`StrategyAPI`，
策略只需调用 ``api.buy() / api.sell() / api.sellshort() / api.buycover() /
api.close_all()``，无需关心订单最终如何成交。差异封装在 ``submit`` 回调中：

- 回测：把请求排队，下一根开盘统一撮合（见 backtest.engine 的 TradeAPI）；
- 模拟盘：回调用 AccountLedger 按最新价即时虚拟撮合；
- 实盘：回调调用币安下单接口，成交后再更新账本。
"""

from collections.abc import Callable
from typing import Any

from ..strategy.api import Candle
from ..strategy.indicators import ema, rsi, sma
from .base import AccountLedger, OrderRequest


class StrategyAPI:
    """策略统一交易接口（三种运行模式共用）。"""

    def __init__(
        self,
        ledger: AccountLedger,
        *,
        submit: Callable[[OrderRequest], bool],
        candles: list[Candle] | None = None,
        index: int = 0,
        timestamp: str = "",
        allow_short: bool = True,
    ) -> None:
        """初始化策略接口。

        参数:
        - ledger: 账户账本。
        - submit: 下单回调，接收 OrderRequest，返回是否受理成功。
        - candles: 供指标计算与价格基准的 K 线窗口。
        - index: 当前 K 线下标。
        - timestamp: 日志/成交时间标签。
        - allow_short: 是否允许做空（实盘现货默认关闭）。
        """
        self.ledger = ledger
        self._submit = submit
        self.candles = candles or []
        self._index = 0
        self.live_price = 0.0
        self.symbol = ""
        self.timestamp = timestamp
        self.allow_short = allow_short
        self.logs: list[str] = []
        self.rejected_orders: list[str] = []
        if self.candles:
            self.set_index(index)

    def set_index(self, index: int) -> None:
        """设置当前 K 线下标。"""
        if self.candles:
            self._index = max(0, min(index, len(self.candles) - 1))

    @property
    def current_candle(self) -> Candle | None:
        """当前 K 线；无 K 线窗口时返回 None。"""
        return self.candles[self._index] if self.candles else None

    @property
    def current_price(self) -> float:
        """当前价格：优先用实时行情价，其次用当前 K 线收盘价。"""
        live = getattr(self, "live_price", 0.0)
        if live and live > 0:
            return float(live)
        candle = self.current_candle
        return candle.close if candle else 0.0

    @property
    def equity(self) -> float:
        """账户净值。"""
        return self.ledger.equity_at(self.current_price)

    @property
    def cash(self) -> float:
        """可用现金。"""
        return self.ledger.cash

    @property
    def position(self) -> dict[str, float]:
        """持仓快照。"""
        return self.ledger.position()

    def log(self, message: str) -> None:
        """记录策略日志。"""
        prefix = f"[{self.timestamp}]" if self.timestamp else ""
        self.logs.append(f"{prefix} {message}")

    def _closes(self) -> list[float]:
        """当前及之前的收盘价，防止策略偷看未来数据。"""
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

    def _validate_ratio(self, ratio: float) -> bool:
        """校验仓位比例。"""
        if not 0 < ratio <= 1:
            self.log(f"仓位比例无效：{ratio}，应在 (0, 1] 范围内")
            return False
        return True

    def _emit(self, side: str, qty: float, signal_price: float, reason: str) -> bool:
        """构造订单请求并交给执行器。"""
        if qty <= 0:
            self.log(f"{side} 数量必须大于 0")
            return False
        request = OrderRequest(
            side=side,
            qty=qty,
            signal_price=float(signal_price),
            reason=reason,
            symbol=getattr(self, "symbol", ""),
        )
        accepted = bool(self._submit(request))
        if not accepted:
            self.rejected_orders.append(f"{side}@{signal_price:.8g}")
        return accepted

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
        qty = (
            self.cash * ratio / (signal_price * (1 + self.ledger.fee_rate + self.ledger.slippage))
            if amount is None
            else float(amount)
        )
        return self._emit("buy", qty, signal_price, reason)

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
        if self.ledger.long_qty <= 0:
            self.log("当前没有多头持仓，忽略卖出")
            return False
        qty = self.ledger.long_qty * ratio if amount is None else min(float(amount), self.ledger.long_qty)
        return self._emit("sell", qty, float(price or self.current_price), reason)

    def sellshort(
        self,
        price: float | None = None,
        amount: float | None = None,
        ratio: float = 0.5,
        reason: str = "",
    ) -> bool:
        """开空。未指定数量时按账户净值的 ratio 比例开空。"""
        if not self.allow_short:
            self.log("当前模式不支持做空（实盘现货默认关闭）")
            return False
        if not self._validate_ratio(ratio):
            return False
        signal_price = float(price or self.current_price)
        if signal_price <= 0:
            self.log("开空价格必须大于 0")
            return False
        qty = (
            self.equity * ratio / (signal_price * (1 + self.ledger.fee_rate + self.ledger.slippage))
            if amount is None
            else float(amount)
        )
        return self._emit("sellshort", qty, signal_price, reason)

    def buycover(
        self,
        price: float | None = None,
        amount: float | None = None,
        ratio: float = 1.0,
        reason: str = "",
    ) -> bool:
        """平空。默认买入平掉全部空头持仓。"""
        if not self.allow_short:
            self.log("当前模式不支持做空（实盘现货默认关闭）")
            return False
        if not self._validate_ratio(ratio):
            return False
        if self.ledger.short_qty <= 0:
            self.log("当前没有空头持仓，忽略平空")
            return False
        qty = self.ledger.short_qty * ratio if amount is None else min(float(amount), self.ledger.short_qty)
        return self._emit("buycover", qty, float(price or self.current_price), reason)

    def close_all(self, reason: str = "close_all") -> bool:
        """同时平掉多头和空头全部持仓。"""
        has_position = False
        if self.ledger.long_qty > 0:
            has_position = self.sell(reason=reason) or has_position
        if self.ledger.short_qty > 0:
            has_position = self.buycover(reason=reason) or has_position
        if not has_position:
            self.log("当前没有持仓，无需平仓")
        return has_position

    def set_live_price(self, price: float) -> None:
        """注入实时行情价，供下单与净值计算使用。"""
        self.live_price = float(price)

    def state(self) -> dict[str, Any]:
        """导出接口状态快照（用于调试与持久化）。"""
        return {
            "price": self.current_price,
            "equity": self.equity,
            "position": self.position,
            "logs": list(self.logs),
            "rejected_orders": list(self.rejected_orders),
        }
