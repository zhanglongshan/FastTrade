"""轻量技术指标：仅依赖标准库，返回与输入等长的列表（未成熟位置为 None）。| Lightweight technical indicators: stdlib-only, returning lists aligned with input (immature positions are None).

目的 | Purpose:
    提供回测策略常用的三种技术指标（SMA / EMA / RSI），不引入第三方依赖，
    并对每个输入值返回同长度的结果，未成熟（数据不足）的位置用 None 填充。
    Provide three common backtesting indicators (SMA / EMA / RSI) without third-party dependencies,
    returning one output per input value with None at immature (insufficient-data) positions.

约定 | Convention:
    - 所有函数校验 period > 0，否则抛 ValueError | All functions require period > 0, else raise ValueError.
    - 返回值与输入等长，前置未成熟位为 None | Return values aligned with input; leading immature positions are None.
"""

# 说明：本模块对指标周期做统一的正数校验，避免除零或无效窗口 | Note: this module uniformly validates that period is positive to avoid division by zero or invalid windows.


def sma(values: list[float], period: int) -> list[float | None]:  # 简单移动平均 | Simple moving average
    """简单移动平均 | Simple moving average (SMA).

    Purpose:
        用滚动窗口计算简单移动平均，窗口未填满时结果为 None。
        Compute SMA over a rolling window; result is None until the window is filled.
    Parameters:
        - values: 输入数值序列（通常为收盘价）| Input numeric series (usually closing prices).
        - period: 窗口周期，必须大于 0 | Window period, must be > 0.
    Returns:
        list[float | None]: 与输入等长的 SMA 序列 | SMA series aligned with the input.
    Flow:
        校验周期 -> 用 total 累加当前值并减去滑出窗口的值 -> 窗口成熟后求均值 | Validate period -> accumulate total adding current and subtracting outgoing value -> average once window matures.
    """
    if period <= 0:  # 校验周期为正 | Validate period is positive
        raise ValueError("period 必须大于 0")  # 周期非法抛异常 | Raise error for invalid period
    result: list[float | None] = []  # 结果序列，逐点追加 | Result series, appended point by point
    total = 0.0  # 滚动窗口累加和 | Rolling window sum
    for index, value in enumerate(values):  # 遍历输入序列 | Iterate over the input series
        total += value  # 将当前值加入窗口和 | Add current value to window sum
        if index >= period:  # 窗口已满，需剔除滑出的最旧值 | Window full, remove the oldest outgoing value
            total -= values[index - period]  # 减去滑出窗口的值 | Subtract the outgoing value
        result.append(total / period if index >= period - 1 else None)  # 窗口成熟则求均值，否则 None | Average if window mature, else None
    return result  # 返回 SMA 序列 | Return the SMA series


def ema(values: list[float], period: int) -> list[float | None]:  # 指数移动平均 | Exponential moving average
    """指数移动平均，首值使用第一根 K 线初始化。| Exponential moving average (EMA), initialized with the first valid value.

    Purpose:
        用指数加权方式计算移动平均，首值以窗口首个有效值初始化。
        Compute a weighted exponential moving average, initialized with the first valid value in the window.
    Parameters:
        - values: 输入数值序列 | Input numeric series.
        - period: 周期，必须大于 0 | Period, must be > 0.
    Returns:
        list[float | None]: 与输入等长的 EMA 序列 | EMA series aligned with the input.
    Flow:
        校验周期 -> 计算平滑系数 alpha -> 逐点递推，窗口未满填 None | Validate period -> compute smoothing factor alpha -> recurse point by point, filling None before window matures.
    """
    if period <= 0:  # 校验周期为正 | Validate period is positive
        raise ValueError("period 必须大于 0")  # 周期非法抛异常 | Raise error for invalid period
    alpha = 2 / (period + 1)  # 平滑系数 | Smoothing factor
    result: list[float | None] = []  # 结果序列 | Result series
    previous: float | None = None  # 上一期 EMA 值，初始 None | Previous EMA value, initially None
    for index, value in enumerate(values):  # 遍历输入序列 | Iterate over the input series
        if index < period - 1:  # 窗口未成熟 | Window not yet mature
            result.append(None)  # 填充 None | Append None
            continue  # 进入下一轮 | Move to next iteration
        if previous is None:  # 首个有效值，直接初始化 | First valid value, initialize directly
            previous = value  # 用当前值作为初始 EMA | Use current value as initial EMA
        else:  # 已有上一期值，按公式递推 | Previous exists, recurse by formula
            previous = value * alpha + previous * (1 - alpha)  # EMA 递推公式 | EMA recursion formula
        result.append(previous)  # 追加当前 EMA | Append current EMA
    return result  # 返回 EMA 序列 | Return the EMA series


def rsi(values: list[float], period: int = 14) -> list[float | None]:  # 相对强弱指标 | Relative strength index
    """相对强弱指标（Wilder 平滑）。| Relative strength index (Wilder's smoothing).

    Purpose:
        用 Wilder 平滑计算 RSI，衡量涨跌动能，取值 0~100。
        Compute RSI with Wilder's smoothing to measure momentum, ranging from 0 to 100.
    Parameters:
        - values: 输入数值序列（通常为收盘价）| Input numeric series (usually closing prices).
        - period: 周期，默认 14，必须大于 0 | Period, default 14, must be > 0.
    Returns:
        list[float | None]: 与输入等长的 RSI 序列 | RSI series aligned with the input.
    Flow:
        校验周期 -> 用全 None 初始化结果 -> 计算首个平均涨跌并求首值 -> 逐点用 Wilder 平滑更新 | Validate period -> init result with all None -> compute initial average gains/losses and first value -> update each point with Wilder smoothing.
    """
    if period <= 0:  # 校验周期为正 | Validate period is positive
        raise ValueError("period 必须大于 0")  # 周期非法抛异常 | Raise error for invalid period
    result: list[float | None] = [None] * len(values)  # 用 None 初始化等长结果 | Init equal-length result with None
    if len(values) <= period:  # 数据不足一个周期 | Not enough data for one period
        return result  # 直接返回全 None 结果 | Return all-None result directly

    gains = 0.0  # 初始窗口涨幅累计 | Cumulative gains in initial window
    losses = 0.0  # 初始窗口跌幅累计 | Cumulative losses in initial window
    for index in range(1, period + 1):  # 遍历初始窗口（含第 period 项）| Iterate initial window (inclusive of index period)
        delta = values[index] - values[index - 1]  # 相邻差值 | Consecutive difference
        gains += max(delta, 0.0)  # 累加上涨幅度 | Accumulate upward move
        losses += max(-delta, 0.0)  # 累加下跌幅度 | Accumulate downward move
    avg_gain = gains / period  # 初始平均涨幅 | Initial average gain
    avg_loss = losses / period  # 初始平均跌幅 | Initial average loss
    result[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)  # 计算首个 RSI（无跌幅时为 100）| Compute first RSI (100 when no losses)

    for index in range(period + 1, len(values)):  # 逐点更新后续 RSI | Update subsequent RSI point by point
        delta = values[index] - values[index - 1]  # 相邻差值 | Consecutive difference
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period  # Wilder 平滑平均涨幅 | Wilder-smoothed average gain
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period  # Wilder 平滑平均跌幅 | Wilder-smoothed average loss
        result[index] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)  # 计算并写入当前 RSI | Compute and write current RSI
    return result  # 返回 RSI 序列 | Return the RSI series
