"""统一交易执行层单元测试：验证回测/模拟/实盘共用一套策略接口。"""

import pytest

from app.modules.quant.execution.api import StrategyAPI
from app.modules.quant.execution.base import AccountLedger, OrderRequest
from app.modules.quant.execution.runner import LiveExecutor, PaperExecutor, run_tick
from app.modules.quant.strategy.api import Strategy


class AlwaysBuy(Strategy):
    """每次都尝试开多、随后可用的简单策略。"""

    name = "统一接口测试策略"
    startup_candle_count = 2

    def on_bar(self, api, candles, index):
        """第 3 根开多，之后不再操作。"""
        if index == 2:
            api.buy(ratio=0.5, reason="test_entry")


def _make_ledger() -> AccountLedger:
    """构造确定性账本。"""
    return AccountLedger(10000.0, fee_rate=0.001, slippage=0.0)


def test_ledger_buy_sell_roundtrip() -> None:
    """账本开多再平多产生已实现盈亏。"""
    ledger = _make_ledger()
    buy = ledger.execute(OrderRequest(side="buy", qty=10, signal_price=100, symbol="BTCUSDT"), 100)
    assert buy is not None and ledger.long_qty == 10
    assert ledger.cash > 0 and ledger.cash < 10000

    sell = ledger.execute(OrderRequest(side="sell", qty=10, signal_price=110, symbol="BTCUSDT"), 110)
    assert sell is not None and sell.pnl is not None
    assert ledger.long_qty == 0 and ledger.realized_pnl > 0


def test_ledger_short_cover() -> None:
    """账本开空再平空，空头数量正确归零。"""
    ledger = _make_ledger()
    short = ledger.execute(OrderRequest(side="sellshort", qty=5, signal_price=100), 100)
    assert short is not None and ledger.short_qty > 0
    cover = ledger.execute(OrderRequest(side="buycover", qty=5, signal_price=90), 90)
    assert cover is not None and cover.pnl is not None
    assert ledger.short_qty == 0


def test_strategy_api_shared_interface() -> None:
    """策略接口 buy/sell/sellshort/buycover/close_all 均可用。"""
    ledger = _make_ledger()
    seen: list[OrderRequest] = []
    api = StrategyAPI(ledger, submit=lambda order: seen.append(order) or True)
    api.set_live_price(100)
    assert api.buy(ratio=0.5) is True
    assert api.sellshort(ratio=0.2) is True
    assert len(seen) == 2
    assert {order.side for order in seen} == {"buy", "sellshort"}
    assert api.current_price == 100


def test_paper_executor_marks_fill() -> None:
    """模拟执行器即时虚拟撮合，产生成交。"""
    ledger = _make_ledger()
    executor = PaperExecutor(ledger)
    fill = __import__("asyncio").run(
        executor.execute(OrderRequest(side="buy", qty=1, signal_price=100), 100)
    )
    assert fill is not None and fill.price == 100 and ledger.long_qty == 1


def test_live_executor_dry_run_uses_local_ledger() -> None:
    """实盘 dry_run 不下真实单，仅本地撮合。"""
    ledger = _make_ledger()
    executor = LiveExecutor(ledger, dry_run=True)
    fill = __import__("asyncio").run(
        executor.execute(OrderRequest(side="buy", qty=1, signal_price=100), 100)
    )
    assert fill is not None and ledger.long_qty == 1


def test_live_executor_rejects_short_in_real_mode() -> None:
    """实盘真实模式拒绝做空（现货不支持）。"""
    ledger = _make_ledger()
    executor = LiveExecutor(ledger, api_key="k", api_secret="s", dry_run=False)
    with pytest.raises(Exception):
        __import__("asyncio").run(
            executor.execute(OrderRequest(side="sellshort", qty=1, signal_price=100), 100)
        )


def test_run_tick_with_paper_executor_uses_candles() -> None:
    """统一 tick 用同一策略在本地 K 线上运行并成交。"""
    from app.modules.quant.strategy.api import Candle

    candles = [
        Candle(date=f"2024-01-{i + 1:02d}T00:00:00+00:00", open=100 + i, high=102 + i, low=98 + i, close=100.5 + i, volume=1000)
        for i in range(30)
    ]
    ledger = _make_ledger()
    executor = PaperExecutor(ledger)
    result = __import__("asyncio").run(
        run_tick(
            mode="paper",
            executor=executor,
            ledger=ledger,
            strategy_name="SmaCrossStrategy",
            symbol="BTCUSDT",
            interval="1d",
            candles=candles,
        )
    )
    assert result.mode == "paper" and result.strategy and result.equity > 0
