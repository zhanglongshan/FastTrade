"""轻量技术指标：仅依赖标准库，返回与输入等长的列表（未成熟位置为 None）。"""


def sma(values: list[float], period: int) -> list[float | None]:
    """简单移动平均。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    result: list[float | None] = []
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= period:
            total -= values[index - period]
        result.append(total / period if index >= period - 1 else None)
    return result


def ema(values: list[float], period: int) -> list[float | None]:
    """指数移动平均，首值使用第一根 K 线初始化。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    alpha = 2 / (period + 1)
    result: list[float | None] = []
    previous: float | None = None
    for index, value in enumerate(values):
        if index < period - 1:
            result.append(None)
            continue
        if previous is None:
            previous = value
        else:
            previous = value * alpha + previous * (1 - alpha)
        result.append(previous)
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """相对强弱指标（Wilder 平滑）。"""
    if period <= 0:
        raise ValueError("period 必须大于 0")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result

    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        delta = values[index] - values[index - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    result[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    for index in range(period + 1, len(values)):
        delta = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        result[index] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return result
