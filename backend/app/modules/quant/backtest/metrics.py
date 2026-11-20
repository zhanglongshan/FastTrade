"""回测增强指标计算：Sharpe / Sortino / Calmar / Profit Factor / VaR / CVaR。

纯函数模块，不依赖 pandas 与数据库；输入为净值曲线与成交明细，
缺失数据返回 0.0 而非抛异常，保证既有回测结果向后兼容。
"""

from __future__ import annotations

import math
from typing import Any

# 年化因子：日线用 365；本模块按"每根 K 线的时间跨度"换算，见 _periods_per_year
TRADING_DAYS_PER_YEAR = 365


def _equity_values(curve: list[dict[str, Any]]) -> list[float]:
    """从净值曲线提取净值序列（过滤非法值）。"""
    return [float(p["equity"]) for p in curve if p.get("equity") is not None]


def _period_returns(curve: list[dict[str, Any]]) -> list[float]:
    """从净值曲线计算逐笔收益率序列。

    首根作为基准不产生收益；非法/非正的前值跳过（无法计算收益率）。
    """
    values = _equity_values(curve)
    returns: list[float] = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev > 0:
            returns.append(values[i] / prev - 1.0)
    return returns


def _periods_per_year(curve: list[dict[str, Any]], interval: str) -> float:
    """根据 K 线周期估算年化期数；未知周期按日线 365 处理。"""
    mapping = {
        "1m": 365 * 24 * 60,
        "3m": 365 * 24 * 20,
        "5m": 365 * 24 * 12,
        "15m": 365 * 24 * 4,
        "30m": 365 * 24 * 2,
        "1h": 365 * 24,
        "4h": 365 * 6,
        "1d": 365,
        "1w": 52,
    }
    return float(mapping.get(interval, TRADING_DAYS_PER_YEAR))


def _std(values: list[float]) -> float:
    """样本标准差（n-1），不足 2 个样本返回 0。"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def calculate_enhanced_metrics(
    curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    interval: str = "1d",
    risk_free_rate: float = 0.0,
    max_drawdown: float = 0.0,
) -> dict[str, float]:
    """计算增强型风险收益指标。

    参数:
        curve: 净值曲线，每个点含 ``equity`` 键。
        trades: 成交明细，每笔含 ``pnl``（已平仓）键。
        interval: K 线周期，用于年化换算。
        risk_free_rate: 年化无风险利率（默认 0）。
        max_drawdown: 最大回撤比例（由引擎已算出，用于 Calmar）。

    返回:
        dict[str, float]: sharpe / sortino / calmar_ratio / profit_factor /
            var_95 / cvar_95 / expectancy。数据不足时对应值为 0.0。
    """
    result = {
        "sharpe": 0.0,
        "sortino": 0.0,
        "calmar_ratio": 0.0,
        "profit_factor": 0.0,
        "var_95": 0.0,
        "cvar_95": 0.0,
        "expectancy": 0.0,
    }

    returns = _period_returns(curve)
    if not returns:
        return result

    periods = _periods_per_year(curve, interval)
    # 年化无风险收益折算到单期
    rf_per_period = risk_free_rate / periods if periods > 0 else 0.0
    excess = [r - rf_per_period for r in returns]

    # Sharpe：超额收益均值 / 标准差 × sqrt(年化期数)
    std = _std(excess)
    if std > 0:
        result["sharpe"] = round((sum(excess) / len(excess)) / std * math.sqrt(periods), 6)

    # Sortino：只用下行标准差（低于目标收益的偏差）
    downside = [min(0.0, r) for r in excess]
    downside_std = _std(downside)
    if downside_std > 0:
        result["sortino"] = round((sum(excess) / len(excess)) / downside_std * math.sqrt(periods), 6)

    # Calmar：年化收益 / 最大回撤
    if max_drawdown > 0:
        total = 1.0
        for r in returns:
            total *= 1.0 + r
        annual = total ** (periods / len(returns)) - 1 if total > 0 else -1.0
        result["calmar_ratio"] = round(annual / max_drawdown, 6)

    # Profit Factor：总盈利 / 总亏损（绝对值）
    pnls = [float(t["pnl"]) for t in trades if t.get("pnl") is not None]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss > 0:
        result["profit_factor"] = round(gross_profit / gross_loss, 6)
    elif gross_profit > 0:
        result["profit_factor"] = 0.0  # 无亏损样本时不给出无穷大，按 0 处理并在文档说明

    # 单期 VaR 95%（历史模拟法）：取收益分布的 5% 分位；CVaR 为尾部均值
    sorted_returns = sorted(returns)
    index = max(0, int(len(sorted_returns) * 0.05) - 1)
    var = sorted_returns[index]
    result["var_95"] = round(var, 6)
    tail = sorted_returns[: index + 1]
    if tail:
        result["cvar_95"] = round(sum(tail) / len(tail), 6)

    # Expectancy：平均每笔已平仓盈亏
    if pnls:
        result["expectancy"] = round(sum(pnls) / len(pnls), 6)

    return result
