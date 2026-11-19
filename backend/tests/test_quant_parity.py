"""回测与模拟盘/实盘的一致性回归测试 | Regression tests for backtest vs paper/live parity.

覆盖本轮的四个修复点：
    1. 成交时点对齐：模拟盘按"下一根开盘"成交，与回测完全一致
    2. 同一信号不重复开仓（可用 allow_position_add 显式开启加仓）
    3. 止损/移动止损的跳空最坏情况定价
    4. 最大回撤熔断后停止开仓
Covers the four fixes:
    1. Fill timing parity: paper fills at the next bar open, exactly like the backtest.
    2. One entry per signal (pyramiding only when allow_position_add is set).
    3. Worst-case gap pricing for stoploss/trailing stops.
    4. The drawdown breaker stops further entries.
"""

import asyncio  # 导入异步运行时（驱动 run_tick）| Import asyncio to drive run_tick
from datetime import UTC, datetime, timedelta  # 导入时间工具 | Import datetime helpers

import pytest  # 导入测试框架 | Import pytest

from app.modules.quant.backtest.engine import run_backtest  # 导入回测入口 | Import the backtest entry point
from app.modules.quant.execution.base import AccountLedger  # 导入统一账本 | Import the shared ledger
from app.modules.quant.execution.runner import PaperExecutor, _bar_is_closed, _resolve_signal_and_price, run_tick  # 导入运行时与内部辅助 | Import runtime helpers
from app.modules.quant.strategy.api import Candle, Strategy  # 导入 K 线与策略基类 | Import Candle and Strategy


class FixedEntry(Strategy):  # 固定 K 线开仓的测试策略 | Test strategy entering at a fixed bar
    """在第 1 根 K 线开多的测试策略 | Enter long at bar 1."""

    name = "固定开仓"  # 策略名 | Strategy name
    startup_candle_count = 0  # 无需预热 | No warmup

    def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
        """第 1 根开多 | Open long at bar 1."""
        if index == 1:  # 命中目标 K 线 | Target bar reached
            api.buy(ratio=0.9, reason="entry")  # 按 90% 资金开多 | Open long with 90% cash


class StreakEntry(Strategy):  # 连续信号测试策略 | Consecutive-signal test strategy
    """连续 5 根 K 线都给开多信号 | Emit an entry signal on five consecutive bars."""

    name = "连续信号"  # 策略名 | Strategy name

    def populate_entry_trend(self, candles, metadata):  # 生成入场信号 | Generate entry signals
        """窗口最后 5 根给信号 | Signal on the last five bars of the window."""
        signals = [0] * len(candles)  # 初始化全零信号 | Init zero signals
        if 10 <= len(candles) - 1 <= 14:  # 只在 10~14 根窗口给信号 | Signal only for windows of 10..14 bars
            signals[-1] = 1  # 最后一根置为开多 | Mark the last bar as long
        return signals  # 返回信号列 | Return the signal column


class StreakAdd(StreakEntry):  # 显式允许加仓的版本 | Variant that explicitly allows pyramiding
    """允许加仓的连续信号策略 | Consecutive-signal strategy with pyramiding."""

    name = "连续信号加仓"  # 策略名 | Strategy name
    allow_position_add = True  # 显式开启加仓 | Enable pyramiding explicitly


def _candles(prices: list[float], start: datetime | None = None) -> list[Candle]:  # 构造确定性 K 线 | Build deterministic candles
    """按价格序列构造 K 线（开盘=收盘=价格，高低价 ±2%）| Build candles from prices (open=close=price, high/low ±2%)."""
    base = start or datetime(2024, 1, 1)  # 起始时间 | Start time
    return [  # 逐根构造 | Build candle by candle
        Candle(  # 单根 K 线 | A single candle
            (base + timedelta(hours=index)).isoformat(),  # 时间戳 | Timestamp
            price,  # 开盘价 | Open
            price * 1.02,  # 最高价 | High
            price * 0.98,  # 最低价 | Low
            price,  # 收盘价 | Close
            1000,  # 成交量 | Volume
        )  # K 线构造结束 | End of candle
        for index, price in enumerate(prices)  # 遍历价格 | Iterate prices
    ]


def test_bar_is_closed_detects_forming_candle() -> None:  # 未收盘 K 线识别 | Detect an unclosed candle
    """当前整点的 K 线应判为未收盘，历史 K 线应判为已收盘 | The current-hour bar is unclosed, historical bars are closed."""
    now_open = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)  # 当前整点 | Current hour start
    forming = Candle(now_open.isoformat(), 100, 101, 99, 100, 1)  # 正在形成的 K 线 | Forming candle
    assert _bar_is_closed(forming, "1h") is False  # 未收盘 | Not closed
    old = Candle("2024-01-01T00:00:00+00:00", 100, 101, 99, 100, 1)  # 历史 K 线 | Historical candle
    assert _bar_is_closed(old, "1h") is True  # 已收盘 | Closed


def test_resolve_drops_forming_bar_and_uses_its_open() -> None:  # 丢弃未收盘根并按开盘成交 | Drop the forming bar and fill at its open
    """丢弃未收盘 K 线后，成交价应等于该根开盘价 | After dropping the forming bar the fill price is its open."""
    now_open = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)  # 当前整点 | Current hour start
    closed = _candles([100.0, 101.0], start=now_open - timedelta(hours=2))  # 两根已收盘 K 线 | Two closed candles
    forming = Candle(now_open.isoformat(), 105.0, 106.0, 104.0, 105.5, 10)  # 正在形成的 K 线 | Forming candle
    window, price, _ref, basis = _resolve_signal_and_price(closed + [forming], "1h", True)  # 解析口径 | Resolve the basis
    assert len(window) == 2  # 未收盘 K 线被丢弃 | The forming bar was dropped
    assert price == pytest.approx(105.0)  # 成交价取该根开盘价 | Fill price is its open
    assert basis == "next_open"  # 口径正确 | Correct basis


def test_paper_fill_matches_backtest_next_open() -> None:  # 模拟盘成交价与回测一致 | Paper fill matches the backtest
    """同一信号下，模拟盘成交价应等于回测的"下一根开盘价" | For the same signal, paper fills at the backtest's next open."""
    prices = [120 - 18 * __import__("math").sin(index / 9.0) for index in range(60)]  # 构造能产生金叉的行情 | Build a series producing a golden cross
    candles = _candles(prices)  # 全量 K 线 | Full candle series
    from app.modules.quant.strategy.loader import load_strategy  # 延迟导入避免测试收集期副作用 | Lazy import to avoid import-time effects

    info = load_strategy("SmaCrossStrategy")  # 加载示例策略 | Load the sample strategy
    result = run_backtest(candles, info.cls, mode="event", symbol="BTCUSDT", interval="1h", fee_rate=0.0, slippage=0.0)  # 跑回测 | Run the backtest
    buys = [trade for trade in result["trades"] if trade["side"] == "buy"]  # 取买入成交 | Collect buy fills
    assert buys  # 至少一笔买入 | At least one buy
    entry_date = buys[0]["date"]  # 回测成交时间 | Backtest fill date
    fill_index = next(index for index, candle in enumerate(candles) if candle.date == entry_date)  # 成交 K 线下标 | Fill bar index
    assert buys[0]["price"] == pytest.approx(candles[fill_index].open)  # 成交价等于该根开盘 | Fill equals that bar's open

    # 模拟盘：把"成交 K 线"作为正在形成的一根喂进去，应当复刻同样价格
    # Paper: feed the fill bar as the forming candle and reproduce the same price.
    now_open = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)  # 当前整点 | Current hour start
    history = _candles(prices[:fill_index], start=now_open - timedelta(hours=fill_index))  # 信号之前的已收盘 K 线 | Closed bars before the signal
    forming = Candle(now_open.isoformat(), candles[fill_index].open, candles[fill_index].high, candles[fill_index].low, candles[fill_index].close, 1)  # 正在形成的成交根 | Forming fill bar
    ledger = AccountLedger(10000.0, fee_rate=0.0, slippage=0.0)  # 构造账本 | Build the ledger
    tick = asyncio.run(  # 驱动一次 tick | Drive one tick
        run_tick(  # 调用统一运行时 | Call the unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name="SmaCrossStrategy",  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=history + [forming],  # 历史 + 未收盘根 | History plus the forming bar
        )
    )
    assert tick.fill_basis == "next_open"  # 使用下一根开盘口径 | Next-open basis
    assert tick.fills and tick.fills[0]["price"] == pytest.approx(buys[0]["price"])  # 成交价与回测一致 | Fill price matches the backtest


def test_repeated_ticks_do_not_pyramid() -> None:  # 重复 tick 不加仓 | Repeated ticks must not pyramid
    """同一信号连续 tick 三次只应产生一笔成交 | Three ticks on the same signal produce exactly one fill."""
    now_open = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)  # 当前整点 | Current hour start
    prices = [120 - 18 * __import__("math").sin(index / 9.0) for index in range(23)]  # 23 根已收盘 K 线 | 23 closed bars
    history = _candles(prices, start=now_open - timedelta(hours=23))  # 构造历史 | Build history
    forming = Candle(now_open.isoformat(), 110.0, 111.0, 108.0, 109.0, 1)  # 正在形成的 K 线 | Forming bar
    ledger = AccountLedger(10000.0, fee_rate=0.001, slippage=0.0)  # 构造账本 | Build the ledger
    fills: list[dict] = []  # 汇总成交 | Collect fills
    for _ in range(3):  # 连续三次 tick | Three consecutive ticks
        tick = asyncio.run(  # 驱动 tick | Drive a tick
            run_tick(  # 统一运行时 | Unified runtime
                mode="paper",  # 模拟盘 | Paper mode
                executor=PaperExecutor(ledger, fee_rate=0.001),  # 本地撮合 | Local fills
                ledger=ledger,  # 账本 | Ledger
                strategy_name="SmaCrossStrategy",  # 策略 | Strategy
                symbol="BTC/USDT",  # 交易对 | Symbol
                interval="1h",  # 周期 | Interval
                candles=history + [forming],  # 行情窗口 | Market window
            )
        )
        fills.extend(tick.fills)  # 累计成交 | Accumulate fills
    assert len(fills) == 1  # 只成交一次 | Exactly one fill
    assert ledger.long_qty > 0  # 已建立多头 | A long position exists


def test_backtest_entry_gate_and_opt_in_pyramiding() -> None:  # 回测开仓门控 | Backtest entry gate
    """默认连续信号只开一笔，显式 allow_position_add 才逐根加仓 | One entry by default, pyramiding only when opted in."""
    candles = _candles([100.0 + index for index in range(30)])  # 单调上涨行情 | Monotonically rising series
    single = run_backtest(candles, StreakEntry, mode="signal", symbol="X", interval="1h", fee_rate=0.0, slippage=0.0)  # 默认门控 | Default gate
    assert len(single["trades"]) == 1  # 只开一笔 | Exactly one entry
    adding = run_backtest(candles, StreakAdd, mode="signal", symbol="X", interval="1h", fee_rate=0.0, slippage=0.0)  # 开启加仓 | Pyramiding enabled
    assert len(adding["trades"]) == 5  # 五根信号各加一笔 | One entry per signal bar


def test_stoploss_fills_at_gap_open() -> None:  # 止损跳空按开盘成交 | Stop fills at the gap open
    """止损价 90 但跳空开盘 80 时应按 80 成交 | A stop at 90 with an 80 open must fill at 80."""

    class Stop(Strategy):  # 带止损的策略 | Strategy with a stop
        """10% 固定止损 | 10% fixed stoploss."""

        name = "止损"  # 策略名 | Strategy name
        startup_candle_count = 0  # 无预热 | No warmup
        stoploss = 0.10  # 10% 止损 | 10% stop

        def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
            """第 1 根开多 | Open long at bar 1."""
            if index == 1:  # 目标 K 线 | Target bar
                api.buy(ratio=0.9, reason="entry")  # 开多 | Open long

    candles = _candles([100.0, 100.0, 100.0, 100.0, 80.0])  # 第 4 根跳空到 80 | Bar 4 gaps down to 80
    result = run_backtest(candles, Stop, mode="event", symbol="X", interval="1h", fee_rate=0.0, slippage=0.0)  # 跑回测 | Run the backtest
    exits = [trade for trade in result["trades"] if trade["side"] == "sell"]  # 取平仓成交 | Collect exits
    assert exits and exits[0]["reason"] == "stop_loss"  # 由止损触发 | Triggered by the stop
    assert exits[0]["price"] == pytest.approx(80.0)  # 按跳空开盘价成交 | Filled at the gap open


def test_trailing_stop_exits_from_peak() -> None:  # 移动止损从峰值回撤触发 | Trailing stop triggers from the peak
    """峰值 120、回撤 5% 时应在 114 以下触发 | With a 120 peak, a 5% trail triggers below 114."""

    class Trail(Strategy):  # 带移动止损的策略 | Strategy with a trailing stop
        """5% 移动止损 | 5% trailing stop."""

        name = "移动止损"  # 策略名 | Strategy name
        startup_candle_count = 0  # 无预热 | No warmup
        trailing_stop = 0.05  # 5% 移动止损 | 5% trailing stop

        def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
            """第 1 根开多 | Open long at bar 1."""
            if index == 1:  # 目标 K 线 | Target bar
                api.buy(ratio=0.9, reason="entry")  # 开多 | Open long

    candles = _candles([100.0, 100.0, 110.0, 120.0, 110.0])  # 先涨到 120 再回落 | Rise to 120 then fall back
    result = run_backtest(candles, Trail, mode="event", symbol="X", interval="1h", fee_rate=0.0, slippage=0.0)  # 跑回测 | Run the backtest
    exits = [trade for trade in result["trades"] if trade["side"] == "sell"]  # 取平仓成交 | Collect exits
    assert exits and exits[0]["reason"] == "stop_loss"  # 由移动止损触发 | Triggered by the trailing stop
    assert exits[0]["price"] == pytest.approx(110.0)  # 跳空/回落按开盘 110 成交 | Filled at the 110 open


def test_repeated_bar_is_skipped_by_last_signal_bar() -> None:  # 重复 K 线跳过信号 | A repeated bar skips signal evaluation
    """传入上次已处理的 bar 时应跳过信号计算（幂等）| Skip signal evaluation when the bar was already processed."""
    now_open = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)  # 当前整点 | Current hour start
    prices = [120 - 18 * __import__("math").sin(index / 9.0) for index in range(23)]  # 构造金叉行情 | Build a golden-cross series
    history = _candles(prices, start=now_open - timedelta(hours=23))  # 历史已收盘 K 线 | Closed history
    forming = Candle(now_open.isoformat(), 110.0, 111.0, 108.0, 109.0, 1)  # 正在形成的 K 线 | Forming bar
    ledger = AccountLedger(10000.0, fee_rate=0.001, slippage=0.0)  # 构造账本 | Build the ledger
    tick = asyncio.run(  # 驱动 tick | Drive a tick
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.001),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name="SmaCrossStrategy",  # 策略 | Strategy
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=history + [forming],  # 行情窗口 | Market window
            last_signal_bar=history[-1].date,  # 该根已处理过 | Already processed
        )
    )
    assert tick.fills == []  # 不产生任何成交 | No fills
    assert ledger.long_qty == 0  # 未开仓 | No position opened
    assert any("已处理过" in line for line in tick.logs)  # 日志说明了跳过原因 | The log explains the skip


def test_max_drawdown_halts_further_entries() -> None:  # 回撤熔断后禁止开仓 | The drawdown breaker blocks entries
    """回撤超过阈值后应全部平仓，且后续信号不再开仓 | After the breaker trips it flattens and blocks new entries."""

    class Breaker(Strategy):  # 带回撤熔断的策略 | Strategy with a drawdown breaker
        """15% 净值回撤熔断 | 15% equity drawdown breaker."""

        name = "熔断"  # 策略名 | Strategy name
        startup_candle_count = 0  # 无预热 | No warmup
        max_drawdown = 0.15  # 15% 熔断 | 15% breaker

        def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
            """多次尝试开仓 | Attempt entries repeatedly."""
            if index in {1, 4, 6}:  # 三个尝试点 | Three entry attempts
                api.buy(ratio=0.9, reason=f"entry{index}")  # 尝试开多 | Attempt a long

    candles = _candles([100.0, 100.0, 100.0, 85.0, 85.0, 80.0, 80.0, 80.0])  # 开仓后大幅下跌 | Sharp drop after entry
    result = run_backtest(candles, Breaker, mode="event", symbol="X", interval="1h", fee_rate=0.0, slippage=0.0)  # 跑回测 | Run the backtest
    reasons = [trade["reason"] for trade in result["trades"]]  # 取所有成交原因 | Collect fill reasons
    assert "max_drawdown" in reasons  # 触发过熔断 | The breaker tripped
    assert result["equity_curve"][-1]["long_qty"] == pytest.approx(0.0)  # 最终空仓 | Ends flat
    assert reasons.count("max_drawdown") == 1  # 只平仓一次 | Flattened exactly once


def test_position_sizing_parity_between_backtest_and_paper() -> None:  # 仓位口径与成交价对齐 | Sizing parity on the fill price
    """回测与模拟盘应按同一成交价基准定仓，成交数量与最终净值应完全一致。

    Regression guard: the backtest used to size at the signal close but fill at the
    next open, over-allocating capital and diverging from the live engine, which
    sizes at the actual fill price. After the fix both must agree exactly.
    """
    import math  # 局部导入数学函数 | Local import of math

    from app.modules.quant.strategy.loader import load_strategy  # 延迟导入策略加载器 | Lazy-import the strategy loader

    info = load_strategy("SmaCrossStrategy")  # 加载示例策略 | Load the sample strategy
    prices = [120 - 18 * math.sin(index / 9.0) for index in range(120)]  # 能产生多次交叉的行情 | Series with several crosses
    candles = _candles(prices)  # 全量 K 线 | Full candle series
    fee, slip = 0.001, 0.0005  # 与实盘一致的费率与滑点 | Same fee and slippage as live

    result = run_backtest(candles, info.cls, mode=info.mode, symbol="X", interval="1h", fee_rate=fee, slippage=slip)  # 跑回测 | Run the backtest
    backtest_trades = [(trade["side"], round(trade["price"], 6), round(trade["qty"], 6)) for trade in result["trades"]]  # 回测成交三元组 | Backtest (side, price, qty)

    async def drive() -> tuple[list[tuple[str, float, float]], AccountLedger]:  # 逐根驱动模拟盘 tick | Drive paper ticks bar by bar
        ledger = AccountLedger(10000.0, fee_rate=fee, slippage=slip)  # 构造账本 | Build the ledger
        last_bar, fills = "", []  # 已处理 bar 与成交汇总 | Last processed bar and collected fills
        for index in range(1, len(candles) - 1):  # 遍历每个已收盘信号点 | Walk every closed signal bar
            forming = Candle(  # 把下一根当作正在形成的一根 | Treat the next bar as the forming one
                datetime.now(UTC).isoformat(),  # 用当前时间使其判为未收盘 | Current time so it reads as unclosed
                candles[index + 1].open,  # 开盘价即回测成交价 | Its open is the backtest fill price
                candles[index + 1].high,  # 最高价 | High
                candles[index + 1].low,  # 最低价 | Low
                candles[index + 1].close,  # 收盘价 | Close
                1,  # 成交量 | Volume
            )
            tick = await run_tick(  # 驱动一次 tick | Drive one tick
                mode="paper",  # 模拟盘 | Paper mode
                executor=PaperExecutor(ledger, fee_rate=fee),  # 本地撮合 | Local fills
                ledger=ledger,  # 账本 | Ledger
                strategy_name="SmaCrossStrategy",  # 策略名 | Strategy name
                symbol="BTC/USDT",  # 交易对 | Symbol
                interval="1h",  # 周期 | Interval
                candles=candles[: index + 1] + [forming],  # 已收盘窗口 + 正在形成根 | Closed window plus the forming bar
                drop_unclosed=True,  # 与实盘同口径 | Same basis as live
                last_signal_bar=last_bar,  # 幂等 | Idempotency
            )
            last_bar = tick.signal_bar  # 记录已处理 bar | Record the processed bar
            for fill in tick.fills:  # 收集成交 | Collect fills
                fills.append((fill["side"], round(fill["price"], 6), round(fill["qty"], 6)))  # 模拟盘成交三元组 | Paper (side, price, qty)
        return fills, ledger  # 返回成交与账本 | Return fills and ledger

    paper_trades, ledger = asyncio.run(drive())  # 执行驱动循环 | Run the driving loop
    assert backtest_trades  # 行情应产生成交 | The series must trade
    assert len(paper_trades) == len(backtest_trades)  # 成交笔数一致 | Same number of fills
    for expected, actual in zip(backtest_trades, paper_trades, strict=True):  # 逐笔比对 | Compare fill by fill
        assert expected[0] == actual[0]  # 方向一致 | Same side
        assert expected[1] == pytest.approx(actual[1])  # 成交价一致 | Same fill price
        assert expected[2] == pytest.approx(actual[2])  # 数量一致（本次修复点）| Same quantity (the fix under test)
    assert ledger.equity_at(prices[-1]) == pytest.approx(result["final_equity"])  # 最终净值一致 | Same final equity


def test_live_stop_triggers_from_closed_bar_range(monkeypatch: pytest.MonkeyPatch) -> None:  # 实盘用已收盘区间触发止损 | Live stops use the closed bar's range
    """实盘的止损必须用最近一根已收盘 K 线的完整区间判定，不能只看开盘是否已破位。

    回归背景：此前实盘把"正在形成那根"的 low/high 交给风控，而它刚开盘时
    low≈high≈open，等于只检查了跳空；盘中触及止损会被静默漏掉。本用例构造
    "收盘后 low 已破 10% 止损"的 K 线，要求实盘能照常止损。
    Regression: live used to feed the forming bar's low/high to the risk layer, which right
    after the open is roughly open-only, so only gaps were caught and intra-bar breaches were
    silently dropped. This test builds a closed bar whose low breaks a 10% stop and expects
    the live path to exit.
    """

    class Stop(Strategy):  # 带固定止损的策略 | Strategy with a fixed stop
        """10% 固定止损 | 10% fixed stoploss."""

        name = "实盘止损"  # 策略名 | Strategy name
        startup_candle_count = 0  # 无预热 | No warmup
        stoploss = 0.10  # 10% 止损 | 10% stop

        def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
            """第 1 根开多 | Open long at bar 1."""
            if index == 1:  # 目标 K 线 | Target bar
                api.buy(ratio=0.9, reason="entry")  # 开多 | Open long

    from types import SimpleNamespace  # 局部导入轻量命名空间 | Local import of a lightweight namespace

    # 测试策略是本地类，未注册到 user_data/strategies，这里直接替换加载器返回它，
    # 避免为了一个用例去往磁盘写策略文件。
    # The test strategy is a local class that is not registered under user_data/strategies;
    # patch the loader to return it instead of writing a strategy file for one test.
    monkeypatch.setattr(  # 替换策略加载函数 | Patch the strategy loader
        "app.modules.quant.execution.runner.load_strategy",  # 目标函数路径 | Target function path
        lambda _name, *args, **kwargs: SimpleNamespace(name="实盘止损", mode="event", cls=Stop),  # 返回测试策略 | Return the test strategy
    )

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)  # 当前整点 | Current hour start
    bar0 = Candle((now - timedelta(hours=3)).isoformat(), 100.0, 100.0, 100.0, 100.0, 1)  # 平静 K 线 | Quiet bar
    bar1 = Candle((now - timedelta(hours=2)).isoformat(), 100.0, 100.0, 100.0, 100.0, 1)  # 平静 K 线 | Quiet bar
    bar2 = Candle((now - timedelta(hours=1)).isoformat(), 100.0, 101.0, 85.0, 88.0, 1)  # low 85 破 90 止损 | Low 85 breaks the 90 stop
    from app.modules.quant.execution.risk import RiskSettings  # 局部导入风控配置 | Local import of risk settings

    ledger = AccountLedger(10000.0, fee_rate=0.0, slippage=0.0, risk=RiskSettings.from_strategy(Stop))  # 注入策略的风控配置 | Inject the strategy's risk config

    first_forming = Candle(now.isoformat(), 100.0, 100.0, 100.0, 100.0, 1)  # 首根正在形成（成交价 100）| First forming bar (fills at 100)
    entry_tick = asyncio.run(  # 第一次 tick：开仓 | First tick: open the position
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=Stop.name,  # 策略名（仅用于解析信息）| Strategy name (only for resolution)
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=[bar0, bar1, first_forming],  # 两根已收盘 + 正在形成 | Two closed bars plus the forming bar
        )
    )
    assert entry_tick.fills and ledger.long_qty > 0  # 已建仓 | Position opened
    assert ledger.long_cost == pytest.approx(100.0)  # 成本为 100 | Cost is 100

    # 关键：正在形成那根的现价（95）在止损（90）之上，只靠它的区间无法发现破位；
    # 只有回头看已收盘 bar2 的最低点（85）才能判定止损被触及。这正是本用例的区分点。
    # Key: the forming bar's price (95) is above the stop (90), so its own range cannot reveal
    # the breach; only bar2's closed low (85) does. That is exactly what this test distinguishes.
    second_forming = Candle(now.isoformat(), 95.0, 95.0, 95.0, 95.0, 1)  # 下一根正在形成（现价 95）| Next forming bar (price 95)
    exit_tick = asyncio.run(  # 第二次 tick：应触发止损 | Second tick: the stop must fire
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=Stop.name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=[bar0, bar1, bar2, second_forming],  # bar2 已收盘且 low 破位 | bar2 is closed with a breaching low
            last_signal_bar=entry_tick.signal_bar,  # 幂等标记 | Idempotency marker
        )
    )
    stops = [fill for fill in exit_tick.fills if fill.get("reason") == "stop_loss"]  # 取止损成交 | Collect stop fills
    assert stops, "已收盘 K 线的最低点跌破止损时应平仓 | A closed bar breaching the stop must exit"
    # 成交价口径：实盘/模拟盘只能在收盘后发现破位、按当时的市价下市价单，
    # 因此真实成交价是 95（当前价）而不是 90（止损价）。按 90 记账等于虚构一个
    # 只在交易所侧止损单才存在的成交价，会让账本与交易所余额长期偏离。
    # Fill basis: paper/live can only detect the breach at the close and send a market order at the
    # price then available, so the real fill is 95, not the 90 stop. Booking 90 would invent a price
    # that only an exchange-side stop order could achieve and keep the ledger off the real balance.
    assert stops[0]["price"] == pytest.approx(95.0)  # 止损按发现时的市价成交 | Stops fill at the price seen on detection
    assert ledger.long_qty == pytest.approx(0.0)  # 已清仓 | Position flattened
