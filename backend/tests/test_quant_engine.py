"""量化回测引擎单元测试。"""

from pathlib import Path

from app.modules.quant.backtest.engine import load_candles, run_backtest
from app.modules.quant.strategy.api import Strategy


class AlwaysBuyOnce(Strategy):
    """只在第 5 根 K 线买入、第 10 根 K 线卖出的测试策略。"""

    name = "测试策略"
    startup_candle_count = 3

    def on_bar(self, api, candles, index):
        """产生一次完整多头交易。"""
        if index == 5:
            api.buy(ratio=0.8, reason="test_buy")
        elif index == 10:
            api.sell(reason="test_sell")


class SignalOnce(Strategy):
    """信号模式测试策略。"""

    name = "信号策略"

    def populate_entry_trend(self, candles, metadata):
        """最后一根 K 线开多。"""
        return [0] * (len(candles) - 1) + [1]

    def populate_exit_trend(self, candles, metadata):
        """无平仓信号。"""
        return [0] * len(candles)


def _write_csv(path: Path, count: int = 20) -> None:
    """写入先涨后跌的确定性 CSV。"""
    lines = ["date,open,high,low,close,volume"]
    for index in range(count):
        price = 100.0 + index if index < 10 else 100.0 + (19 - index)
        lines.append(f"2024-01-{index + 1:02d}T00:00:00+00:00,{price},{price + 1},{price - 1},{price},1000")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_load_and_run_event_strategy(tmp_path: Path) -> None:
    """事件模式回测产生成交与净值曲线。"""
    csv_path = tmp_path / "test.csv"
    _write_csv(csv_path)
    candles = load_candles(csv_path)

    result = run_backtest(
        candles,
        AlwaysBuyOnce,
        mode="event",
        symbol="BTC/USDT",
        interval="1d",
        initial_cash=10000.0,
        fee_rate=0.001,
    )

    assert result["candle_count"] == 20
    assert len(result["equity_curve"]) == 20
    assert result["trade_count"] == 2
    assert result["closed_trade_count"] == 1
    assert result["final_equity"] > 0


def test_signal_mode_strategy(tmp_path: Path) -> None:
    """信号模式最后一根 K 线产生开多订单。"""
    csv_path = tmp_path / "signal.csv"
    _write_csv(csv_path)
    candles = load_candles(csv_path)

    result = run_backtest(
        candles,
        SignalOnce,
        mode="signal",
        symbol="BTC/USDT",
        interval="1d",
        initial_cash=10000.0,
        fee_rate=0.0,
    )

    assert result["strategy"] == "SignalOnce"
    assert result["trade_count"] >= 1
