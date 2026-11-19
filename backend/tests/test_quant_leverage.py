"""杠杆（合约）记账回归测试 | Regression tests for leverage accounting.

覆盖点 | Coverage:
1. 保证金口径：开仓只占用名义本金/杠杆，净值在开仓瞬间只减少手续费；
   Opening only locks notional/leverage, so equity drops by the fee alone.
2. 盈亏放大：杠杆倍数线性放大价格变动带来的净值变化；
   Leverage amplifies equity changes linearly with the price move.
3. 1 倍等价现货：leverage=1 时现金流与旧口径逐项一致（回归保护）；
   leverage=1 must reproduce the legacy spot cash flows exactly.
4. 强平：净值跌破维持保证金时强平并停止交易；
   Liquidation triggers once equity falls below maintenance margin.
5. 回测：run_backtest(leverage=N) 的收益率约为 1 倍时的 N 倍。
   run_backtest with leverage N scales returns by about N.
"""

import pytest  # 导入测试框架 | Import pytest

from app.modules.quant.backtest.engine import run_backtest  # 导入回测入口 | Import the backtest entry point
from app.modules.quant.execution.base import AccountLedger, OrderRequest  # 导入账本与订单请求 | Import ledger and order request
from app.modules.quant.strategy.api import Candle, Strategy  # 导入 K 线与策略基类 | Import Candle and Strategy


class AllInLong(Strategy):
    """第 1 根满仓开多，之后不再操作 | Go all-in long on bar 1 and never act again."""

    name = "满仓开多测试策略"  # 策略名 | Strategy name
    startup_candle_count = 0  # 无需预热 | No warmup

    def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
        """第 1 根按 100% 购买力开多 | Enter long with 100% buying power on bar 1."""
        if index == 1:  # 命中目标 K 线 | Target bar reached
            api.buy(ratio=1.0, reason="entry")  # 满仓开多 | Go all-in


def _candles(prices: list[float]) -> list[Candle]:  # 构造确定性 K 线 | Build deterministic candles
    """按价格序列构造 K 线（开=高=低=收=价格，零成交量）| Build candles where OHLC all equal the price."""
    return [  # 列表推导生成 K 线 | Generate candles with a comprehension
        Candle(  # 单根 K 线 | Single candle
            date=f"2024-01-{index + 1:02d}T00:00:00+00:00",  # 递增日期 | Incrementing date
            open=price,  # 开盘价 | Open
            high=price,  # 最高价 | High
            low=price,  # 最低价 | Low
            close=price,  # 收盘价 | Close
            volume=0.0,  # 成交量（回测不使用）| Volume (unused by the backtest)
        )
        for index, price in enumerate(prices)  # 遍历价格 | Iterate prices
    ]


def test_leverage_margin_and_equity_amplification() -> None:
    """10 倍杠杆开仓只占用 1/10 名义本金，价格 +10% 时净值 +100%。"""
    ledger = AccountLedger(1000.0, fee_rate=0.0, leverage=10.0)  # 1000 本金、10 倍杠杆 | 1000 capital at 10x
    fill = ledger.execute(OrderRequest(side="buy", qty=100, signal_price=100), 100)  # 开多 100 个 @100 | Buy 100 at 100
    assert fill is not None  # 必须成交 | Must fill
    assert ledger.long_qty == pytest.approx(100.0)  # 名义本金 10000 | Notional 10000
    assert ledger.cash == pytest.approx(0.0)  # 保证金恰好 1000 | Margin equals 1000
    assert ledger.equity_at(100) == pytest.approx(1000.0)  # 开仓瞬间净值不变 | Equity unchanged at entry
    assert ledger.equity_at(110) == pytest.approx(2000.0)  # +10% 价格 → +100% 净值 | +10% price -> +100% equity
    assert ledger.margin_used() == pytest.approx(1000.0)  # 占用保证金 10000/10 | Used margin 10000/10
    assert ledger.free_margin(110) == pytest.approx(1000.0)  # 浮盈不占用保证金 | Unrealized profit stays free


def test_leverage_short_equity_amplification() -> None:
    """10 倍杠杆开空：价格 -10% 时净值 +100%，平仓后现金回到放大后的净值。"""
    ledger = AccountLedger(1000.0, fee_rate=0.0, leverage=10.0)  # 1000 本金、10 倍杠杆 | 1000 capital at 10x
    fill = ledger.execute(OrderRequest(side="sellshort", qty=100, signal_price=100), 100)  # 开空 100 个 @100 | Short 100 at 100
    assert fill is not None  # 必须成交 | Must fill
    # 开空只记入保证金（10000/10），借币所得不进可用现金；1 倍杠杆时才等于全额所得
    # Only the margin (10000/10) is credited; the borrow proceeds stay out of free cash (full at 1x).
    assert ledger.cash == pytest.approx(2000.0)  # 初始 1000 + 保证金 1000 | Initial 1000 plus margin 1000
    assert ledger.free_margin(100) == pytest.approx(0.0)  # 保证金已全部占用 | Margin fully in use
    assert ledger.equity_at(100) == pytest.approx(1000.0)  # 开仓瞬间净值不变 | Equity unchanged at entry
    assert ledger.equity_at(90) == pytest.approx(2000.0)  # -10% 价格 → +100% 净值 | -10% price -> +100% equity
    cover = ledger.execute(OrderRequest(side="buycover", qty=100, signal_price=90), 90)  # 全部平空 | Cover everything
    assert cover is not None  # 必须成交 | Must fill
    assert ledger.short_qty == pytest.approx(0.0)  # 空头归零 | Short is flat
    assert ledger.cash == pytest.approx(2000.0)  # 现金等于放大后的净值 | Cash equals the amplified equity


def test_leverage_one_matches_spot_cash_flow() -> None:
    """leverage=1 时现金/持仓与现货口径逐项一致。"""
    spot = AccountLedger(10000.0, fee_rate=0.001)  # 不传杠杆（默认 1）| Default leverage of 1
    lev1 = AccountLedger(10000.0, fee_rate=0.001, leverage=1.0)  # 显式 1 倍 | Explicit 1x
    for ledger in (spot, lev1):  # 两者执行同一笔订单 | Both execute the same order
        fill = ledger.execute(OrderRequest(side="buy", qty=10, signal_price=100), 100)  # 买入 10 个 @100 | Buy 10 at 100
        assert fill is not None  # 必须成交 | Must fill
    assert spot.cash == pytest.approx(lev1.cash)  # 现金一致 | Identical cash
    assert spot.cash == pytest.approx(10000.0 - 1000.0 - 1.0)  # 全额扣款 + 0.1% 手续费 | Full price plus 0.1% fee
    assert spot.equity_at(110) == pytest.approx(lev1.equity_at(110))  # 净值一致 | Identical equity
    assert spot.liquidation_price() == 0.0  # 现货没有强平价 | No liquidation price on spot


def test_leverage_liquidation_flattens_and_halts() -> None:
    """净值跌破维持保证金时强平并置 halted。"""
    ledger = AccountLedger(1000.0, fee_rate=0.0, leverage=10.0)  # 1000 本金、10 倍杠杆 | 1000 capital at 10x
    ledger.execute(OrderRequest(side="buy", qty=100, signal_price=100), 100)  # 开多 100 个 @100 | Buy 100 at 100
    liq_price = ledger.liquidation_price()  # 理论强平价约 90.45 | Theoretical liquidation price ~90.45
    assert 89.0 < liq_price < 91.0  # 大致落在 90 附近 | Roughly around 90
    assert ledger.risk_orders(liq_price - 1.0, low=liq_price - 1.0)  # 跌破强平价应产生强平单 | Breach must produce a liquidation order
    orders = ledger.risk_orders(liq_price - 1.0, low=liq_price - 1.0)  # 取强平订单（幂等重算）| Read the liquidation order
    assert orders  # 非空 | Non-empty
    order, fill_price = orders[0]  # 拆解订单与成交价 | Unpack the order and price
    assert order.reason == "liquidation"  # 原因标记为强平 | Reason is liquidation
    assert order.side == "sell"  # 强平多头用卖出 | Liquidating a long sells
    assert order.qty == pytest.approx(ledger.long_qty)  # 全部强平 | Flatten everything
    assert fill_price == pytest.approx(liq_price - 1.0)  # 按跳空后的更差价格成交 | Fill at the gapped, worse price
    assert ledger.halted is True  # 强平后停止交易 | Trading halts after liquidation


def test_spot_never_liquidates() -> None:
    """1 倍杠杆（现货）不触发强平，净值可以为负也只是亏损。"""
    ledger = AccountLedger(1000.0, fee_rate=0.0, leverage=1.0)  # 现货 1 倍 | Spot at 1x
    ledger.execute(OrderRequest(side="sellshort", qty=5, signal_price=100), 100)  # 开空（模拟做空）| Open a simulated short
    assert ledger.liquidation_price() == 0.0  # 无强平价 | No liquidation price
    assert ledger.risk_orders(1000.0, high=1000.0) == []  # 即使价格翻十倍也不强平 | No liquidation even at 10x price


def test_backtest_leverage_scales_return() -> None:
    """回测杠杆参数按倍数放大收益，并在结果中回写生效杠杆。"""
    candles = _candles([100.0, 100.0, 100.0, 110.0, 110.0, 110.0])  # 第 2 根成交，随后涨 10% | Fill on bar 2, then +10%
    spot = run_backtest(candles, AllInLong, symbol="BTC/USDT", interval="1d", initial_cash=10000.0, fee_rate=0.0, slippage=0.0, leverage=1.0)  # 1 倍 | 1x
    lev10 = run_backtest(candles, AllInLong, symbol="BTC/USDT", interval="1d", initial_cash=10000.0, fee_rate=0.0, slippage=0.0, leverage=10.0)  # 10 倍 | 10x
    assert spot["leverage"] == pytest.approx(1.0)  # 结果回写 1 倍 | Result echoes 1x
    assert lev10["leverage"] == pytest.approx(10.0)  # 结果回写 10 倍 | Result echoes 10x
    assert spot["total_return"] == pytest.approx(0.1, rel=1e-6)  # 1 倍涨 10% | 10% at 1x
    assert lev10["total_return"] == pytest.approx(1.0, rel=1e-6)  # 10 倍涨 100% | 100% at 10x
    assert lev10["final_equity"] == pytest.approx(20000.0, rel=1e-6)  # 净值翻倍 | Equity doubles


def test_leverage_snapshot_roundtrip() -> None:
    """杠杆随快照持久化，恢复后保证金口径不变；实例配置优先于快照。"""
    ledger = AccountLedger(1000.0, fee_rate=0.0, leverage=5.0)  # 5 倍杠杆 | 5x leverage
    ledger.execute(OrderRequest(side="buy", qty=50, signal_price=100), 100)  # 名义本金 5000 | Notional 5000
    snapshot = ledger.snapshot(100.0)  # 导出快照 | Export a snapshot
    assert snapshot["leverage"] == pytest.approx(5.0)  # 快照记录杠杆 | Snapshot keeps the leverage
    assert snapshot["margin_used"] == pytest.approx(1000.0)  # 占用保证金 5000/5 | Used margin 5000/5

    restored = AccountLedger.from_snapshot(snapshot, fee_rate=0.0)  # 从快照恢复 | Restore from the snapshot
    assert restored.leverage == pytest.approx(5.0)  # 杠杆被恢复 | Leverage restored
    assert restored.equity_at(100) == pytest.approx(ledger.equity_at(100))  # 净值一致 | Same equity

    overridden = AccountLedger.from_snapshot(snapshot, fee_rate=0.0, leverage=2.0)  # 实例配置覆盖快照 | Instance config wins
    assert overridden.leverage == pytest.approx(2.0)  # 以参数为准 | Argument wins
    assert overridden.margin_used() == pytest.approx(2500.0)  # 保证金按 2 倍重算 | Margin recomputed at 2x


def test_leverage_all_in_blocks_extra_entry() -> None:
    """满杠杆后没有可用保证金，再加仓会被数量裁剪为 0（无法成交）。"""
    ledger = AccountLedger(1000.0, fee_rate=0.0, leverage=10.0)  # 10 倍杠杆 | 10x leverage
    first = ledger.execute(OrderRequest(side="buy", qty=100, signal_price=100), 100)  # 满仓开多 | Go all-in
    assert first is not None  # 首笔成交 | First fill succeeds
    second = ledger.execute(OrderRequest(side="buy", qty=1, signal_price=100), 100)  # 再次开多 | Try another entry
    assert second is None  # 保证金耗尽，无法成交 | No margin left, no fill


def test_schema_validates_leverage_range() -> None:
    """接口层限制杠杆在 1~125 倍之间。"""
    from pydantic import ValidationError  # 导入校验异常 | Import the validation error

    from app.modules.quant.schema import QuantBacktestCreateSchema, QuantLiveStrategyCreateSchema  # 导入请求模型 | Import request schemas

    base = {"dataset_ids": [1], "strategy_name": "Demo"}  # 回测最小必填字段 | Minimal backtest payload
    assert QuantBacktestCreateSchema(**base, leverage=10).leverage == pytest.approx(10.0)  # 10 倍合法 | 10x is valid
    with pytest.raises(ValidationError):  # 0.5 倍非法 | 0.5x is invalid
        QuantBacktestCreateSchema(**base, leverage=0.5)  # 低于下限 | Below the lower bound
    with pytest.raises(ValidationError):  # 200 倍非法 | 200x is invalid
        QuantBacktestCreateSchema(**base, leverage=200)  # 高于上限 | Above the upper bound

    live = {"name": "t", "strategy_name": "Demo", "symbol": "BTC/USDT", "interval": "1h"}  # 实时实例最小必填字段 | Minimal live payload
    assert QuantLiveStrategyCreateSchema(**live).leverage == pytest.approx(1.0)  # 默认 1 倍 | Defaults to 1x
    assert QuantLiveStrategyCreateSchema(**live, leverage=20).leverage == pytest.approx(20.0)  # 20 倍合法 | 20x is valid
