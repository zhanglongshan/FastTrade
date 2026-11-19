"""交易决策共享基元：三种模式唯一的"下单规则"实现 | Shared trading primitives: the single source of truth for order rules.

目的 | Purpose:
    回测的 :class:`~app.modules.quant.strategy.api.TradeAPI` 与模拟盘/实盘的
    :class:`~app.modules.quant.execution.api.StrategyAPI` 原先各自实现了一套
    仓位换算、滑点、开仓门控逻辑，任何一处改动都会造成两种模式结果漂移。
    本模块把这些"纯函数"决策收敛到一处，两个 API 都调用它，从根上消除双份实现。
    The backtest TradeAPI and the paper/live StrategyAPI used to implement their own
    sizing, slippage and entry-gating logic, so any tweak caused result drift between
    modes. This module centralizes those pure decision functions so both APIs share
    one implementation.

包含 | Contents:
    - interval_to_seconds: 周期字符串 → 秒数，用于判断 K 线是否已收盘
    - sizing_qty: 按可用资金/净值比例换算下单数量
    - apply_slippage: 买卖方向的滑点定价
    - can_add_position: 单次开仓门控（是否允许再次开/加仓）
"""

from __future__ import annotations  # 允许前向引用类型标注 | Allow forward-referenced type annotations

from datetime import UTC, datetime, timedelta  # 导入时间工具（K 线收盘判定）| Import datetime helpers for bar-closure checks

# 周期单位 → 秒数换算表 | Interval unit -> seconds conversion table
_UNIT_SECONDS = {  # 单位到秒数的映射常量 | Constant mapping of interval unit to seconds
    "s": 1,  # 秒 | second
    "m": 60,  # 分 | minute
    "h": 3600,  # 时 | hour
    "d": 86400,  # 日 | day
    "w": 604800,  # 周 | week
}  # 映射表结束 | End of mapping

_MONTH_SECONDS = 30 * 86400  # 月线按 30 天近似（币安 1M 周期）| Month approximated as 30 days (Binance 1M)


def interval_to_seconds(interval: str) -> int:  # 周期字符串转秒数 | Convert an interval string to seconds
    """把 ``1m`` / ``4h`` / ``1d`` 之类的周期转成秒数 | Convert an interval like ``1m``/``4h``/``1d`` into seconds.

    目的 | Purpose:
        判断最新一根 K 线是否已经收盘、以及调度器对齐 bar 边界都需要周期秒数。
        Both "is the last candle closed?" and scheduler bar-alignment need the period in seconds.

    参数 | Parameters:
        interval (str): 周期字符串，如 ``1m``、``15m``、``4h``、``1d``、``1w``、``1M``
            | Interval string such as ``1m``, ``15m``, ``4h``, ``1d``, ``1w``, ``1M``.

    返回 | Returns:
        int: 该周期对应的秒数；无法识别时回退 60 秒 | Seconds for the interval, falling back to 60.

    流程 | Flow:
        拆分数字与单位 → 月线单独处理 → 单位查表相乘 → 异常回退 60。
        Split digits and unit -> handle month separately -> multiply via lookup -> fall back to 60.
    """
    text = str(interval or "").strip()  # 归一化为去空白的字符串 | Normalize to a stripped string
    if not text:  # 空字符串直接回退 | Fall back immediately on empty input
        return 60  # 默认 1 分钟 | Default to one minute
    if text[-1] == "M":  # 大写 M 表示月线（币安约定）| Uppercase M means monthly on Binance
        return max(1, int(text[:-1] or 1)) * _MONTH_SECONDS  # 月线按 30 天折算 | Convert months via 30-day approximation
    unit = text[-1].lower()  # 单位统一转小写 | Lowercase the unit character
    if unit not in _UNIT_SECONDS:  # 单位不认识时回退 | Fall back when the unit is unknown
        return 60  # 默认 1 分钟 | Default to one minute
    try:  # 数字部分可能非法，需要容错 | The numeric part may be invalid, so guard it
        count = int(text[:-1] or 1)  # 取数字部分，缺省为 1 | Read the numeric part, defaulting to 1
    except ValueError:  # 数字无法解析 | Numeric part cannot be parsed
        return 60  # 回退默认值 | Fall back to default
    return max(1, count) * _UNIT_SECONDS[unit]  # 数量乘以单位秒数 | Multiply count by unit seconds


def sizing_qty(  # 按资金比例换算下单数量 | Convert a capital ratio into an order quantity
    cash: float,  # 可用资金（买入）或净值（开空）| Available cash (buy) or equity (short)
    ratio: float,  # 使用比例 (0, 1] | Ratio to use, in (0, 1]
    price: float,  # 定价基准 | Pricing baseline
    fee_rate: float,  # 单边手续费率 | One-way fee rate
    slippage: float,  # 滑点比例 | Slippage ratio
) -> float:  # 返回可下单数量 | Return the order quantity
    """按资金比例计算下单数量，并把手续费与滑点预留进预算 | Size an order from a capital ratio, reserving fees and slippage.

    目的 | Purpose:
        两种模式共用同一换算公式，避免"回测买 0.95、实盘买 0.94"这类漂移。
        Both modes share one formula so sizing can never drift between them.

    参数 | Parameters:
        cash (float): 可用资金或净值 | Available cash or equity.
        ratio (float): 使用比例 | Ratio of capital to spend.
        price (float): 定价基准价 | Reference price.
        fee_rate (float): 手续费率 | Fee rate.
        slippage (float): 滑点比例 | Slippage ratio.

    返回 | Returns:
        float: 可下单数量；参数非法时返回 0.0 | Order quantity, or 0.0 for invalid inputs.

    流程 | Flow:
        校验价格/比例 → 预算 = 资金×比例 → 除以含费与滑点的单位成本。
        Validate price/ratio -> budget = capital × ratio -> divide by per-unit cost including fee and slippage.
    """
    if price <= 0 or ratio <= 0 or cash <= 0:  # 任一参数非正都无法下单 | Any non-positive input makes sizing impossible
        return 0.0  # 返回 0 表示不可下单 | Return 0 meaning unorderable
    unit_cost = price * (1 + fee_rate + slippage)  # 单位成本含手续费与滑点 | Per-unit cost including fee and slippage
    if unit_cost <= 0:  # 防御除零 | Guard against division by zero
        return 0.0  # 返回 0 | Return 0
    return cash * ratio / unit_cost  # 预算除以单位成本得数量 | Budget divided by unit cost


def apply_slippage(price: float, is_buy: bool, slippage: float) -> float:  # 按方向应用滑点 | Apply slippage by side
    """按买卖方向给成交价加/减滑点 | Add or subtract slippage to a fill price by side.

    目的 | Purpose:
        统一"买加卖减"的定价规则，避免各执行器各写一遍。
        Centralize the "buys up, sells down" rule so executors don't re-implement it.

    参数 | Parameters:
        price (float): 原始价格 | Raw price.
        is_buy (bool): 是否买入方向 | Whether it is a buy-side order.
        slippage (float): 滑点比例 | Slippage ratio.

    返回 | Returns:
        float: 应用滑点后的价格 | Price after slippage.
    """
    if price <= 0:  # 非正价格直接返回 | Return non-positive prices as-is
        return price  # 原样返回 | Return unchanged
    return price * (1 + slippage) if is_buy else price * (1 - slippage)  # 买加卖减 | Add for buys, subtract for sells


def can_add_position(current_qty: float, pending_qty: float, allow_add: bool) -> bool:  # 是否允许再次开仓 | Whether another entry is allowed
    """单次开仓门控：已有持仓或已有挂单时不再开仓 | Entry gate: block when a position or a pending order already exists.

    目的 | Purpose:
        修复"同一信号反复开仓"（实盘每个 tick 加一次仓、回测每根 K 线加一次仓）的问题，
        让一次信号最多建立一笔仓位。
        Fix repeated entries on the same signal (paper/live added once per tick, backtest
        once per bar) so one signal opens at most one position.

    参数 | Parameters:
        current_qty (float): 当前同方向持仓数量 | Current same-side position quantity.
        pending_qty (float): 尚未成交的同方向挂单数量 | Same-side quantity still pending fill.
        allow_add (bool): 是否允许加仓（金字塔式建仓）| Whether pyramiding is allowed.

    返回 | Returns:
        bool: True 表示允许开仓 | True when a new entry is allowed.
    """
    if allow_add:  # 允许加仓时不做任何限制 | No restriction when pyramiding is enabled
        return True  # 直接放行 | Always allow
    return current_qty <= 1e-12 and pending_qty <= 1e-12  # 无持仓且无挂单才放行 | Allow only with no position and no pending order


def parse_bar_time(date_text: str) -> datetime | None:  # 解析 K 线开盘时间 | Parse a bar open time
    """把 K 线日期字符串解析为带时区的 UTC 时间 | Parse a candle date string into a timezone-aware UTC datetime.

    目的 | Purpose:
        REST 与 WebSocket 两条行情来源的日期字符串格式不同（``+00:00`` / ``Z``），
        统一解析后才能判断收盘、做去重与缺口检测。
        REST and WebSocket produce different date formats (``+00:00`` / ``Z``); parsing them
        uniformly is required for closure checks, de-duplication and gap detection.

    参数 | Parameters:
        date_text (str): K 线日期字符串 | Candle date string.

    返回 | Returns:
        datetime | None: 解析成功返回 UTC 时间，失败返回 None | UTC datetime, or None when unparseable.

    流程 | Flow:
        替换 ``Z`` → 调用 fromisoformat → 缺失时区按 UTC 补齐 → 异常返回 None。
        Replace ``Z`` -> fromisoformat -> attach UTC when naive -> return None on error.
    """
    try:  # 非法字符串需要容错，避免单根坏数据打断整条流水线 | Guard against malformed strings so one bad bar cannot break the pipeline
        parsed = datetime.fromisoformat(str(date_text).replace("Z", "+00:00"))  # 解析 ISO 时间 | Parse the ISO timestamp
    except (TypeError, ValueError):  # 无法解析时返回 None | Return None when unparseable
        return None  # 解析失败 | Parse failed
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed  # 朴素时间按 UTC 补齐 | Assume UTC when naive


def bar_is_closed(date_text: str, interval: str, now: datetime | None = None) -> bool:  # 判断 K 线是否已收盘 | Check whether a bar has closed
    """判断一根 K 线是否已经走完（收盘）| Check whether a candle has finished forming.

    目的 | Purpose:
        币安返回的最后一根 K 线通常是"正在形成"的，收盘价会持续变化；用它计算信号
        会造成盘中穿越、收盘又回去的假信号。本函数把判定收敛到一处，REST 拉取与
        WebSocket 推送共用同一规则。
        Binance's last kline is usually still forming and its close keeps moving; using it
        produces intra-bar signals that vanish by the close. Centralising the check makes the
        REST path and the WebSocket path agree.

    参数 | Parameters:
        date_text (str): K 线开盘时间字符串 | Bar open time string.
        interval (str): K 线周期 | Candle interval.
        now (datetime | None): 参考"现在"，None 时取当前 UTC 时间 | Reference "now", defaults to current UTC.

    返回 | Returns:
        bool: 已收盘返回 True；时间无法解析时保守返回 False | True when closed; False when the timestamp is unparseable.
    """
    opened = parse_bar_time(date_text)  # 解析开盘时间 | Parse the open time
    if opened is None:  # 解析失败时保守视为未收盘 | Conservatively treat unparseable bars as unclosed
        return False  # 未收盘 | Not closed
    closes_at = opened + timedelta(seconds=interval_to_seconds(interval))  # 计算收盘时刻 | Compute the closing moment
    return (now or datetime.now(UTC)) >= closes_at  # 当前时间已过收盘时刻即视为收盘 | Closed once now passes the closing moment
