"""统一策略运行时：一次 tick 拉行情、跑策略、执行订单、产出结果。

回测之外的模拟盘/实盘没有"完整数据集"，因此这里以滚动 K 线窗口驱动策略：
每次 tick 拉取最新 N 根 K 线，把最后一根当作当前 K 线，调用一次策略回调，
策略通过统一的 :class:`StrategyAPI` 下单，订单由 :class:`Executor` 即时撮合。

三种模式共享本函数，差异仅在 ``executor``：
- PaperExecutor：本地账本虚拟撮合；
- LiveExecutor：调用币安下单后再记账（dry_run 时仍走本地撮合）。
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.exceptions import CustomException

from ..strategy.api import Candle, Strategy
from ..strategy.loader import load_strategy
from .api import StrategyAPI
from .base import AccountLedger, Fill, OrderRequest, RunMode
from .exchange import fetch_klines_async


class Executor(Protocol):
    """执行器协议：把订单请求变成成交。"""

    async def execute(self, order: OrderRequest, price: float) -> Fill | None:
        """执行一笔订单，返回成交记录；被拒绝时返回 None。"""
        ...

    def snapshot(self) -> dict[str, Any]:
        """返回执行器可持久化的状态（账本快照等）。"""
        ...


@dataclass
class TickResult:
    """一次策略运行的结果。"""

    mode: str
    symbol: str
    interval: str
    strategy: str
    price: float
    equity: float
    cash: float
    position: dict[str, float]
    fills: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    error: str | None = None
    tick_time: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为可落库/可序列化的字典。"""
        return {
            "mode": self.mode,
            "symbol": self.symbol,
            "interval": self.interval,
            "strategy": self.strategy,
            "price": self.price,
            "equity": self.equity,
            "cash": self.cash,
            "position": self.position,
            "fills": self.fills,
            "logs": self.logs,
            "rejected": self.rejected,
            "error": self.error,
            "tick_time": self.tick_time,
        }


def _last_signal(signals: list[int], index: int) -> int:
    """读取当前下标信号；越界时取最后一个。"""
    if not signals:
        return 0
    return signals[index] if len(signals) > index else signals[-1]


async def run_tick(
    *,
    mode: RunMode | str,
    executor: Executor,
    ledger: AccountLedger,
    strategy_name: str,
    symbol: str,
    interval: str,
    candles: list[Candle] | None = None,
    kline_limit: int = 300,
    allow_short: bool = True,
) -> TickResult:
    """执行一次策略 tick。

    参数:
    - mode: 运行模式（paper/live）。
    - executor: 订单执行器，决定订单如何成交。
    - ledger: 账户账本（由上层从持久化状态恢复）。
    - strategy_name: 策略类名。
    - symbol/interval: 交易对与周期。
    - candles: 可选的历史 K 线；不传则从币安拉取最新窗口。
    - kline_limit: 拉取 K 线根数。
    - allow_short: 是否允许做空（实盘现货 False）。

    返回:
    - TickResult: 本次 tick 价格、净值、持仓、成交与日志。
    """
    mode_value = mode.value if isinstance(mode, RunMode) else str(mode)
    now_iso = datetime.now(UTC).isoformat()
    info = load_strategy(strategy_name)
    strategy: Strategy = info.cls()
    resolved_symbol = symbol.replace("/", "").replace("_", "").upper()

    # 1) 行情窗口
    if candles:
        window = sorted(candles, key=lambda item: item.date)
    else:
        warmup_hint = max(0, int(getattr(info.cls, "startup_candle_count", 0) or 0))
        window = await fetch_klines_async(resolved_symbol, interval, limit=max(warmup_hint + 5, kline_limit))
    if len(window) < 2:
        raise CustomException(msg="K 线不足，至少需要 2 根才能运行策略")

    last = window[-1]
    price = last.close

    # 2) 构造统一策略接口
    pending: list[OrderRequest] = []

    def submit(order: OrderRequest) -> bool:
        """策略下单入口：排队，tick 末按最新价统一撮合。"""
        pending.append(order)
        return True

    api = StrategyAPI(
        ledger,
        submit=submit,
        candles=window,
        index=len(window) - 1,
        timestamp=now_iso,
        allow_short=allow_short,
    )
    api.symbol = resolved_symbol
    api.set_live_price(price)

    # 3) 调用策略（与回测相同的两种模式）
    index = len(window) - 1
    warmup = max(0, int(getattr(info.cls, "startup_candle_count", 0) or 0))
    try:
        if index >= warmup:
            if info.mode == "event":
                strategy.on_bar(api, window, index)
            else:
                metadata = {"pair": symbol, "interval": interval}
                entries = strategy.populate_entry_trend(window, metadata) or []
                exits = strategy.populate_exit_trend(window, metadata) or []
                exit_signal = _last_signal(exits, index)
                entry_signal = _last_signal(entries, index)
                if exit_signal == 1:
                    api.sell(reason="exit_long")
                elif exit_signal == -1:
                    api.buycover(reason="exit_short")
                if entry_signal == 1:
                    api.buy(reason="entry_long")
                elif entry_signal == -1:
                    api.sellshort(reason="entry_short")
    except CustomException:
        raise
    except Exception as exc:  # noqa: BLE001 - 用户策略异常需要回传展示
        raise CustomException(msg=f"策略执行失败：{type(exc).__name__}: {exc}") from exc

    # 4) 撮合排队的订单
    fills: list[Fill] = []
    for order in pending:
        try:
            fill = await executor.execute(order, price)
            if fill is not None:
                fills.append(fill)
        except CustomException as exc:
            api.log(f"订单执行失败({order.side})：{exc}")
        except Exception as exc:  # noqa: BLE001
            api.log(f"订单执行异常({order.side})：{type(exc).__name__}: {exc}")

    return TickResult(
        mode=mode_value,
        symbol=symbol,
        interval=interval,
        strategy=info.name,
        price=round(price, 8),
        equity=round(ledger.equity_at(price), 6),
        cash=round(ledger.cash, 6),
        position={k: round(v, 8) for k, v in ledger.position().items()},
        fills=[fill.__dict__ for fill in fills],
        logs=api.logs,
        rejected=api.rejected_orders,
        tick_time=now_iso,
    )


class PaperExecutor:
    """模拟盘执行器：按最新价在本地账本即时虚拟撮合。"""

    def __init__(self, ledger: AccountLedger, fee_rate: float = 0.001) -> None:
        """初始化模拟执行器。"""
        self.ledger = ledger
        self.fee_rate = fee_rate

    async def execute(self, order: OrderRequest, price: float) -> Fill | None:
        """本地虚拟撮合。"""
        return self.ledger.execute(order, price, timestamp=datetime.now(UTC).isoformat())

    def snapshot(self) -> dict[str, Any]:
        """返回账本快照。"""
        return self.ledger.snapshot()


class LiveExecutor:
    """实盘执行器：按最新价在币安下单，成交后记账。

    ``dry_run=True``（默认）时不下真实订单，仅用本地账本模拟撮合并标注，
    便于先用真实行情验证策略而不动用资金。
    """

    def __init__(
        self,
        ledger: AccountLedger,
        *,
        api_key: str = "",
        api_secret: str = "",
        dry_run: bool = True,
    ) -> None:
        """初始化实盘执行器。

        参数:
        - ledger: 账户账本。
        - api_key/api_secret: 解密后的币安密钥。
        - dry_run: 是否模拟运行（True 不下真实单）。
        """
        self.ledger = ledger
        self.dry_run = dry_run
        self._client = None
        if not dry_run:
            from .exchange import BinanceOrderClient

            self._client = BinanceOrderClient(api_key, api_secret)

    async def execute(self, order: OrderRequest, price: float) -> Fill | None:
        """执行订单：dry_run 走本地撮合，真实模式先下单再记账。"""
        # 现货不支持做空：即便账本允许，实盘真实下单也必须拒绝，避免误操作
        if not self.dry_run and order.side in {"sellshort", "buycover"}:
            raise CustomException(msg="币安现货不支持做空，请将策略改为仅做多，或使用模拟盘验证做空逻辑")

        if not self.dry_run and self._client is not None:
            symbol = order.symbol.replace("/", "").upper()
            result = await asyncio.to_thread(
                self._client.place_market_order,
                symbol,
                order.side,
                order.qty,
                client_order_id=order.client_order_id,
            )
            # 用实际成交均价（fills 加权）作为记账价，缺失时回落最新价
            executed_price = _extract_avg_price(result) or price
            executed_qty = float(result.get("executedQty") or order.qty) or order.qty
            real_order = OrderRequest(
                side=order.side,
                qty=executed_qty,
                signal_price=order.signal_price,
                reason=order.reason,
                symbol=symbol,
            )
            return self.ledger.execute(real_order, executed_price, timestamp=datetime.now(UTC).isoformat())

        return self.ledger.execute(order, price, timestamp=datetime.now(UTC).isoformat())

    def snapshot(self) -> dict[str, Any]:
        """返回账本快照。"""
        return self.ledger.snapshot()


def _extract_avg_price(result: dict[str, Any]) -> float:
    """从币安下单返回中提取成交均价。"""
    fills = result.get("fills")
    if not isinstance(fills, list) or not fills:
        return 0.0
    total_qty = 0.0
    total_value = 0.0
    for item in fills:
        try:
            qty = float(item.get("qty") or 0)
            px = float(item.get("price") or 0)
        except (TypeError, ValueError):
            continue
        total_qty += qty
        total_value += qty * px
    return total_value / total_qty if total_qty > 0 else 0.0
