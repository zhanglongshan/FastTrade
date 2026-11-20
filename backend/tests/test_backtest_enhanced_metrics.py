"""增强型回测指标单元测试（纯函数，不依赖 conftest/Flask app）。"""

from __future__ import annotations

import math

from app.modules.quant.backtest.metrics import calculate_enhanced_metrics


def _curve(values: list[float]) -> list[dict]:
    return [{"equity": v} for v in values]


def test_empty_curve_returns_zero_metrics() -> None:
    result = calculate_enhanced_metrics([], [], interval="1d")
    for key in ("sharpe", "sortino", "calmar_ratio", "profit_factor", "var_95", "cvar_95", "expectancy"):
        assert result[key] == 0.0


def test_single_point_curve_returns_zero_metrics() -> None:
    result = calculate_enhanced_metrics(_curve([10000.0]), [], interval="1d")
    assert result["sharpe"] == 0.0
    assert result["var_95"] == 0.0


def test_profit_factor_uses_gross_profit_over_gross_loss() -> None:
    trades = [{"pnl": 100.0}, {"pnl": 50.0}, {"pnl": -30.0}]
    result = calculate_enhanced_metrics(_curve([100.0, 110.0, 115.0]), trades, interval="1d")
    # 总盈利 150 / 总亏损 30 = 5
    assert result["profit_factor"] == 5.0


def test_profit_factor_with_no_loss_is_zero_not_infinity() -> None:
    trades = [{"pnl": 100.0}]
    result = calculate_enhanced_metrics(_curve([100.0, 110.0]), trades, interval="1d")
    assert result["profit_factor"] == 0.0


def test_expectancy_is_average_pnl() -> None:
    trades = [{"pnl": 100.0}, {"pnl": -50.0}, {"pnl": None}]
    result = calculate_enhanced_metrics(_curve([100.0, 110.0, 105.0]), trades, interval="1d")
    assert result["expectancy"] == 25.0


def test_var_and_cvar_use_lower_tail() -> None:
    # 收益率序列：-10%, -5%, +5%, +10%  → 5% 分位落在最小收益附近
    curve = _curve([100.0, 90.0, 85.5, 89.775, 98.75])
    result = calculate_enhanced_metrics(curve, [], interval="1d")
    assert result["var_95"] < 0  # VaR 为负收益
    assert result["cvar_95"] <= result["var_95"]  # CVaR 不优于 VaR


def test_sharpe_positive_for_monotonic_growth() -> None:
    curve = _curve([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    result = calculate_enhanced_metrics(curve, [], interval="1d")
    assert result["sharpe"] > 0
    assert math.isfinite(result["sharpe"])


def test_sharpe_zero_for_flat_curve() -> None:
    curve = _curve([100.0] * 10)
    result = calculate_enhanced_metrics(curve, [], interval="1d")
    assert result["sharpe"] == 0.0
    assert result["sortino"] == 0.0


def test_calmar_uses_annualized_return_over_drawdown() -> None:
    curve = _curve([100.0, 110.0, 105.0])
    result = calculate_enhanced_metrics(curve, [], interval="1d", max_drawdown=0.1)
    assert result["calmar_ratio"] > 0


def test_calmar_zero_when_no_drawdown() -> None:
    curve = _curve([100.0, 110.0, 120.0])
    result = calculate_enhanced_metrics(curve, [], interval="1d", max_drawdown=0.0)
    assert result["calmar_ratio"] == 0.0
