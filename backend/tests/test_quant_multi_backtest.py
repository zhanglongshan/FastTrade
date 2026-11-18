"""web3 回测多选数据集与现货/合约约束的单元测试。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.modules.quant.backtest.service import _market_of
from app.modules.quant.schema import QuantBacktestCreateSchema
from app.modules.quant.strategy.api import Candle, TradeAPI


def _candles(count: int = 6) -> list[Candle]:
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


def test_backtest_schema_merges_and_dedupes_dataset_ids() -> None:
    """新旧字段合并去重，保持旧调用方 dataset_id 可用。"""
    params = QuantBacktestCreateSchema(dataset_id=3, dataset_ids=[1, 2, 1], strategy_name="SmaCrossStrategy")
    assert params.dataset_ids == [1, 2, 3]

    params = QuantBacktestCreateSchema(dataset_id=7, strategy_name="SmaCrossStrategy")
    assert params.dataset_ids == [7]


def test_backtest_schema_rejects_empty_or_too_many_datasets() -> None:
    """未选数据集或超过上限时给出明确校验错误。"""
    with pytest.raises(ValidationError):
        QuantBacktestCreateSchema(strategy_name="SmaCrossStrategy")
    with pytest.raises(ValidationError):
        QuantBacktestCreateSchema(dataset_ids=list(range(51)), strategy_name="SmaCrossStrategy")


def test_spot_backtest_rejects_short_and_futures_allows() -> None:
    """现货按 allow_short=False 拒绝开空，合约允许。"""
    spot = TradeAPI(_candles(), initial_cash=10000.0, allow_short=False)
    spot.set_index(1)
    assert spot.sellshort() is False
    assert "现货" in spot.logs[-1]

    futures = TradeAPI(_candles(), initial_cash=10000.0, allow_short=True)
    futures.set_index(1)
    assert futures.sellshort() is True


def test_market_of_falls_back_to_source() -> None:
    """旧数据没有 market 列时按 source 推断。"""

    class FakeDataset:
        market: str | None = None
        source = "binance_futures"

    assert _market_of(FakeDataset()) == "futures"
    FakeDataset.source = "binance"
    assert _market_of(FakeDataset()) == "spot"
    FakeDataset.market = "spot"
    assert _market_of(FakeDataset()) == "spot"


def test_naive_utc_has_no_timezone() -> None:
    """写入 TIMESTAMP WITHOUT TIME ZONE 列的时间不能带时区。"""
    from app.modules.quant.backtest.service import _naive_utc

    value = _naive_utc()
    assert value.tzinfo is None
    assert (datetime.now(UTC) - value.replace(tzinfo=UTC)).total_seconds() < 5
