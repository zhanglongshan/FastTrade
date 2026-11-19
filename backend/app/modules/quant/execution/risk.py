"""风控规则：止损、移动止损、最大回撤熔断 | Risk rules: stoploss, trailing stop and max-drawdown breaker.

目的 | Purpose:
    回测与模拟盘/实盘此前都**没有任何风控**：策略漏写出场信号就会无限持仓。
    本模块提供一份共享的风控实现，两种模式调用同一套价格判定，保证触发点一致。
    Backtest and paper/live previously had no risk control at all: a strategy that forgot
    to emit an exit signal would hold forever. This module provides one shared risk
    implementation so both modes trigger at identical prices.

设计要点 | Design notes:
    - 风控以"策略类属性"声明，不需要改数据库：``stoploss = 0.05`` 表示相对开仓价 -5%。
      Risk is declared as strategy class attributes, so no DB migration is needed.
    - 触发价按**最坏情况**成交：跳空低开跌破止损时按开盘价成交，而不是止损价。
      Trigger prices fill at the worst case: a gap through the stop fills at the open.
    - ``max_drawdown`` 触发后置 ``halted``，本根/本次不再开新仓，避免连续亏损加仓。
      Once max_drawdown trips, ``halted`` blocks new entries for the rest of the bar/tick.
"""

from __future__ import annotations  # 前向引用类型标注 | Forward-referenced annotations

from dataclasses import dataclass  # 导入数据类装饰器 | Import dataclass decorator
from typing import Any  # 导入 Any 类型 | Import Any for loose typing


@dataclass(frozen=True)  # 冻结数据类：风控配置创建后不可变 | Frozen dataclass: risk config is immutable
class RiskSettings:  # 风控配置 | Risk configuration
    """风控配置 | Risk configuration.

    关键属性 | Key attributes:
        - stoploss: 固定止损比例，0.05 表示 -5%；0 表示关闭 | Fixed stoploss ratio; 0 disables it.
        - trailing_stop: 移动止损比例，0.03 表示从持仓最高点回撤 3% 出场 | Trailing ratio from the peak; 0 disables.
        - max_drawdown: 净值回撤熔断阈值，0.2 表示回撤 20% 平仓并停手 | Equity drawdown breaker; 0 disables.
        - allow_add: 是否允许同一信号重复开仓（金字塔加仓）| Whether pyramiding is allowed.
    """

    stoploss: float = 0.0  # 固定止损比例 | Fixed stoploss ratio
    trailing_stop: float = 0.0  # 移动止损比例 | Trailing stop ratio
    max_drawdown: float = 0.0  # 最大回撤熔断阈值 | Max drawdown breaker threshold
    allow_add: bool = False  # 是否允许加仓，默认关闭 | Pyramiding disabled by default

    @classmethod  # 声明类方法 | Declare classmethod
    def from_strategy(cls, strategy_cls: type[Any] | None) -> RiskSettings:  # 从策略类读取风控配置 | Read risk config from a strategy class
        """从策略类属性读取风控参数，缺失则关闭风控 | Read risk params from strategy class attributes; missing means disabled.

        参数 | Parameters:
            strategy_cls (type | None): 策略类，可为 None | Strategy class, may be None.

        返回 | Returns:
            RiskSettings: 归一化后的风控配置 | Normalized risk settings.

        流程 | Flow:
            逐个属性 getattr → 转正数并裁剪到 [0, 1) → 组装配置。
            getattr each attribute -> coerce to a non-negative ratio below 1 -> build the config.
        """
        def _ratio(name: str) -> float:  # 读取并归一化单个比例属性 | Read and normalize one ratio attribute
            """读取策略上的比例属性并裁剪到 [0, 1) | Read a ratio attribute and clamp it into [0, 1)."""
            raw = getattr(strategy_cls, name, 0.0) if strategy_cls is not None else 0.0  # 安全读取属性 | Safely read the attribute
            try:  # 属性可能是字符串或 None，需要容错 | The attribute may be a string or None
                value = float(raw or 0.0)  # 转 float，None/空值按 0 | Coerce to float, treating None as 0
            except (TypeError, ValueError):  # 无法转换时视为关闭 | Treat unconvertible values as disabled
                return 0.0  # 关闭该项风控 | Disable this rule
            return min(max(value, 0.0), 0.99)  # 裁剪到 [0, 0.99] | Clamp into [0, 0.99]

        return cls(  # 组装风控配置 | Assemble the risk config
            stoploss=_ratio("stoploss"),  # 固定止损 | Fixed stoploss
            trailing_stop=_ratio("trailing_stop"),  # 移动止损 | Trailing stop
            max_drawdown=_ratio("max_drawdown"),  # 回撤熔断 | Drawdown breaker
            allow_add=bool(getattr(strategy_cls, "allow_position_add", False)),  # 是否允许加仓 | Whether pyramiding is allowed
        )  # 配置组装结束 | End of config assembly

    @property  # 只读属性 | Read-only property
    def enabled(self) -> bool:  # 是否启用了任意一项风控 | Whether any risk rule is enabled
        """是否启用了任意一项风控 | Whether any risk rule is active.

        返回 | Returns:
            bool: 启用任意项返回 True | True when at least one rule is active.
        """
        return self.stoploss > 0 or self.trailing_stop > 0 or self.max_drawdown > 0  # 任一项大于 0 即启用 | Enabled when any rule is positive


def long_stop_price(  # 计算多头止损价 | Compute the long stop price
    entry: float,  # 多头开仓均价 | Average long entry price
    peak: float,  # 持仓期间最高价 | Highest price while holding
    risk: RiskSettings,  # 风控配置 | Risk settings
) -> float:  # 返回止损价，0 表示未启用 | Return the stop price, 0 when disabled
    """计算多头止损价（固定止损与移动止损取更靠近现价者）| Compute the long stop (tighter of fixed and trailing).

    参数 | Parameters:
        entry (float): 多头均价成本 | Average long cost.
        peak (float): 持仓期间最高价 | Highest price while holding.
        risk (RiskSettings): 风控配置 | Risk settings.

    返回 | Returns:
        float: 止损触发价；两者都未启用时返回 0 | Stop trigger price, 0 when both rules are disabled.
    """
    stops: list[float] = []  # 收集所有启用中的止损价 | Collect every active stop price
    if risk.stoploss > 0 and entry > 0:  # 固定止损启用 | Fixed stoploss is active
        stops.append(entry * (1 - risk.stoploss))  # 开仓价下浮止损比例 | Entry price minus the stoploss ratio
    if risk.trailing_stop > 0 and peak > 0:  # 移动止损启用 | Trailing stop is active
        stops.append(peak * (1 - risk.trailing_stop))  # 最高价下浮移动比例 | Peak price minus the trailing ratio
    return max(stops) if stops else 0.0  # 取最高（最先触发）的止损价 | Take the highest (first-triggered) stop


def short_stop_price(  # 计算空头止损价 | Compute the short stop price
    entry: float,  # 空头开仓均价 | Average short entry price
    trough: float,  # 持仓期间最低价 | Lowest price while holding
    risk: RiskSettings,  # 风控配置 | Risk settings
) -> float:  # 返回止损价，0 表示未启用 | Return the stop price, 0 when disabled
    """计算空头止损价（固定止损与移动止损取更靠近现价者）| Compute the short stop (tighter of fixed and trailing).

    参数 | Parameters:
        entry (float): 空头均价所得 | Average short proceeds.
        trough (float): 持仓期间最低价 | Lowest price while holding.
        risk (RiskSettings): 风控配置 | Risk settings.

    返回 | Returns:
        float: 止损触发价；两者都未启用时返回 0 | Stop trigger price, 0 when both rules are disabled.
    """
    stops: list[float] = []  # 收集所有启用中的止损价 | Collect every active stop price
    if risk.stoploss > 0 and entry > 0:  # 固定止损启用 | Fixed stoploss is active
        stops.append(entry * (1 + risk.stoploss))  # 开仓价上浮止损比例 | Entry price plus the stoploss ratio
    if risk.trailing_stop > 0 and trough > 0:  # 移动止损启用 | Trailing stop is active
        stops.append(trough * (1 + risk.trailing_stop))  # 最低价上浮移动比例 | Trough price plus the trailing ratio
    return min(stops) if stops else 0.0  # 取最低（最先触发）的止损价 | Take the lowest (first-triggered) stop


def gap_fill_price(open_price: float, stop_price: float, is_long: bool) -> float:  # 跳空时按更差价格成交 | Fill at the worse price across a gap
    """跳空穿越止损时按开盘价成交，避免高估风控效果 | Fill at the open when price gaps through the stop.

    目的 | Purpose:
        若多头止损 90 而本根开盘 80，真实成交是 80 而不是 90；按 90 记账会系统性夸大策略收益。
        If a long is stopped at 90 while the bar opens at 80, the real fill is 80, not 90;
        booking 90 would systematically inflate backtest returns.

    参数 | Parameters:
        open_price (float): 本根/本次开盘价 | Bar or tick open price.
        stop_price (float): 止损触发价 | Stop trigger price.
        is_long (bool): 是否多头 | Whether the position is long.

    返回 | Returns:
        float: 实际成交价 | Actual fill price.
    """
    if open_price <= 0:  # 开盘价无效时退回止损价 | Fall back to the stop when the open is invalid
        return stop_price  # 使用止损价 | Use the stop price
    return min(open_price, stop_price) if is_long else max(open_price, stop_price)  # 多头取下限，空头取上限 | Lower bound for longs, upper for shorts
