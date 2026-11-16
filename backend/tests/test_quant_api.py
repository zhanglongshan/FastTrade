"""量化交易 API 封装单元测试。"""

from app.modules.quant.strategy.api import Candle, TradeAPI


def _candles(count: int = 8) -> list[Candle]:
    """构造确定性 K 线。"""
    return [
        Candle(
            date=f"2024-01-{index + 1:02d}T00:00:00+00:00",
            open=100.0 + index,
            high=102.0 + index,
            low=98.0 + index,
            close=100.5 + index,
            volume=1000.0,
        )
        for index in range(count)
    ]


def test_buy_and_sell_execute_at_next_open() -> None:
    """买单在下一根开盘成交，卖出后产生已实现盈亏。"""
    api = TradeAPI(_candles(), initial_cash=10000.0, fee_rate=0.001, slippage=0.0)

    api.set_index(1)
    assert api.buy() is True
    assert api.long_qty == 0.0  # 订单尚未成交

    api.execute_pending(2)
    assert api.long_qty > 0.0
    assert api.cash < 10000.0

    api.set_index(4)
    assert api.sell() is True
    api.execute_pending(5)
    assert api.long_qty == 0.0
    assert api.trades[-1].pnl is not None


def test_sellshort_and_buycover() -> None:
    """做空与平空接口成对可用。"""
    api = TradeAPI(_candles(), initial_cash=10000.0, fee_rate=0.001, slippage=0.0)

    api.set_index(1)
    assert api.sellshort(ratio=0.3) is True
    api.execute_pending(2)
    assert api.short_qty > 0.0

    api.set_index(3)
    assert api.buycover() is True
    api.execute_pending(4)
    assert api.short_qty == 0.0
    assert any(trade.side == "buycover" for trade in api.trades)


def test_close_all_closes_both_sides() -> None:
    """close_all 同时平掉多头与空头。"""
    api = TradeAPI(_candles(), initial_cash=10000.0, fee_rate=0.0, slippage=0.0)
    api.set_index(1)
    api.buy(ratio=0.3)
    api.sellshort(ratio=0.3)
    api.execute_pending(2)
    assert api.long_qty > 0.0 and api.short_qty > 0.0

    api.set_index(3)
    assert api.close_all() is True
    api.execute_pending(4)
    assert api.long_qty == 0.0
    assert api.short_qty == 0.0


def test_indicators_do_not_look_ahead() -> None:
    """指标接口只使用当前及之前的 K 线。"""
    api = TradeAPI(_candles(), initial_cash=10000.0)
    api.set_index(2)
    assert len(api.sma(3)) == 3
    assert len(api.ema(3)) == 3
    assert len(api.rsi(3)) == 3
