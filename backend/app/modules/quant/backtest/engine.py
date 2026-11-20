"""轻量事件驱动回测引擎 | Lightweight event-driven backtesting engine.

设计参考 freqtrade：信号在当前 K 线收盘产生，下一根 K 线开盘成交；
但本实现仅依赖标准库，不要求 pandas，策略可直接调用 api.buy()/sell() 等方法。
Design inspired by freqtrade: signals are generated at the current candle's close and
executed at the next candle's open; this implementation only uses the standard library,
does not require pandas, and strategies can directly call api.buy()/sell() etc.
"""

import csv  # 导入 CSV 读写模块，用于解析 OHLCV 数据文件 | Import CSV module for parsing OHLCV data files
from dataclasses import asdict  # 导入 dataclass 转字典工具，用于序列化成交记录 | Import helper to convert dataclasses to dicts
from datetime import UTC, datetime  # 导入 datetime 与 UTC，用于解析 K 线时间、年化与未来数据判定 | Import datetime and UTC for bar times, annualization and future checks
from pathlib import Path  # 导入 Path，用于文件路径操作 | Import Path for file path operations
from typing import Any  # 导入 Any，用于结果字典的宽松类型标注 | Import Any for loose typing of the result dict

from app.core.exceptions import CustomException  # 导入业务异常类，用于统一抛出可展示给前端的错误 | Import business exception for user-facing errors
from app.core.logger import logger  # 导入日志对象，用于模式冲突告警 | Import logger for mode-conflict warnings

from ..execution.risk import RiskSettings  # 导入风控配置（与实盘共享）| Import risk settings shared with live mode
from ..strategy.api import Candle, Strategy, TradeAPI  # 导入 K 线、策略基类与交易 API（回测撮合核心）| Import Candle, Strategy base and TradeAPI (matching core)
from ..strategy.trading_base import bar_is_closed, parse_bar_time  # 导入收盘判定与时间解析 | Import the bar-closure check and timestamp parser

CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]  # CSV 必需的六列：时间/开/高/低/收/量 | Required CSV columns: date/open/high/low/close/volume


def load_candles(path: Path) -> list[Candle]:
    """从标准 OHLCV CSV 读取 K 线 | Load candles from a standard OHLCV CSV file.

    参数 | Args:
        path (Path): CSV 文件路径 | Path to the CSV file.

    返回 | Returns:
        list[Candle]: 按日期升序排列的 K 线列表 | A list of candles sorted by date ascending.

    异常 | Raises:
        CustomException: 文件不存在、缺列、数值非法或数据为空时抛出 | Raised on missing file, missing columns, invalid numbers or empty data.
    """
    if not path.is_file():  # 校验文件确实存在，否则抛业务异常 | Check the file exists, else raise a business error
        raise CustomException(msg=f"数据文件不存在：{path}")  # 数据文件不存在 | Data file does not exist
    candles: list[Candle] = []  # 存放解析后的 K 线对象 | Accumulator for parsed Candle objects
    with path.open("r", encoding="utf-8-sig", newline="") as file:  # 以 utf-8-sig 读取（兼容带 BOM 的 CSV）| Open with utf-8-sig to tolerate BOM
        reader = csv.DictReader(file)  # 用 DictReader 按表头映射每行 | Use DictReader to map each row by header
        missing = [column for column in CSV_COLUMNS if column not in (reader.fieldnames or [])]  # 找出缺失的必需列 | Find required columns that are missing
        if missing:  # 只要缺一列就拒绝加载 | Reject loading if any required column is missing
            raise CustomException(msg=f"CSV 缺少必需列：{', '.join(missing)}")  # 报告缺失列名 | Report the missing column names
        for line_no, row in enumerate(reader, start=2):  # 逐行解析，行号从 2 开始（第 1 行是表头）| Parse rows; line numbers start at 2 (row 1 is the header)
            try:  # 捕获单行数值转换错误 | Catch per-row numeric conversion errors
                candles.append(  # 构造 Candle 并加入列表 | Build a Candle and append it to the list
                    Candle(  # K 线数据类 | Candle dataclass
                        date=str(row["date"]).strip(),  # 日期字符串，去首尾空白 | Date string, stripped of surrounding whitespace
                        open=float(row["open"]),  # 开盘价转浮点 | Open price as float
                        high=float(row["high"]),  # 最高价转浮点 | High price as float
                        low=float(row["low"]),  # 最低价转浮点 | Low price as float
                        close=float(row["close"]),  # 收盘价转浮点 | Close price as float
                        volume=float(row["volume"]),  # 成交量转浮点 | Volume as float
                    )  # Candle 构造结束 | End of Candle construction
                )  # append 结束 | End of append
            except (TypeError, ValueError) as exc:  # 数值格式非法（空值/非数字）| Invalid numeric format (empty/non-numeric)
                raise CustomException(msg=f"CSV 第 {line_no} 行格式无效：{exc}") from exc  # 带上行号抛异常便于定位 | Raise with the line number for easier diagnosis
    if not candles:  # 没有任何有效数据行 | No valid data rows were parsed
        raise CustomException(msg="数据集为空，无法回测")  # 空数据集无法回测 | Empty dataset cannot be backtested
    # 去重：同一时间戳出现两次通常来自"增量下载后追加"或分区间合并，会让指标在同一根 K 线上
    # 重复计算，且成交记录出现重复行。按时间戳保留最后一条（后写覆盖先写）。
    # Deduplicate: a repeated timestamp usually comes from appending an incremental download or from
    # merging chunked files; it makes indicators count the same bar twice and duplicates fills.
    # The last row wins, matching "later write overwrites earlier".
    unique: dict[str, Candle] = {}  # 时间戳 -> K 线 | Timestamp -> candle
    for candle in candles:  # 逐根归并 | Merge bar by bar
        unique[candle.date] = candle  # 后写覆盖先写 | Later rows overwrite earlier ones
    duplicates = len(candles) - len(unique)  # 被覆盖的重复行数 | Number of overwritten duplicates
    if duplicates:  # 有重复时告警 | Warn when duplicates were found
        logger.warning(f"回测数据存在 {duplicates} 条形重复时间戳，已按时间戳去重（保留最后一条）")  # 记录告警 | Log a warning
    return sorted(unique.values(), key=lambda item: item.date)  # 按日期字符串升序返回 | Return sorted by date


def _last_signal(signals: list[int], index: int) -> int:
    """读取当前下标对应的信号；信号列表不足时取最后一个 | Read the signal at the given index; fall back to the last signal if the list is shorter.

    参数 | Args:
        signals (list[int]): 信号序列，1=做多、-1=做空、0=无 | Signal series: 1=long, -1=short, 0=none.
        index (int): 当前 K 线下标 | Current candle index.

    返回 | Returns:
        int: 对应下标的信号值；空列表返回 0 | Signal value at the index; returns 0 for an empty list.
    """
    if not signals:  # 信号列表为空（策略未产生任何信号）| Signal list is empty (strategy produced no signals)
        return 0  # 返回 0 表示无信号 | Return 0 meaning no signal
    return signals[index] if len(signals) > index else signals[-1]  # 下标有效则取值，否则兼容性地取最后一个信号 | Use the indexed signal if available, else fall back to the last one


def _max_drawdown(curve: list[dict[str, Any]]) -> float:
    """计算最大回撤比例 | Compute the maximum drawdown ratio.

    参数 | Args:
        curve (list[dict[str, Any]]): 净值曲线，每个点含 "equity" 键 | Equity curve; each point has an "equity" key.

    返回 | Returns:
        float: 最大回撤比例（0~1），例如 0.2 表示 20% 回撤 | Max drawdown ratio (0~1); e.g. 0.2 means a 20% drawdown.
    """
    peak = 0.0  # 历史净值峰值 | Running historical equity peak
    worst = 0.0  # 记录到的最大回撤 | The worst drawdown observed
    for point in curve:  # 遍历净值曲线每个点 | Iterate over each point in the equity curve
        equity = float(point["equity"])  # 当前净值转浮点 | Current equity as float
        peak = max(peak, equity)  # 更新历史峰值 | Update the historical peak
        if peak > 0:  # 峰值为正才计算回撤比例，避免除零 | Only compute when peak is positive to avoid division by zero
            worst = max(worst, (peak - equity) / peak)  # 当前回撤与历史最差取最大 | Take the max of current drawdown and the worst so far
    return worst  # 返回最大回撤 | Return the maximum drawdown


def _infer_mode(strategy_cls: type[Strategy]) -> str:  # 按策略实现推断运行模式 | Infer the run mode from the strategy implementation
    """按策略实际重写的方法推断运行模式 | Infer the run mode from the overridden methods.

    目的 | Purpose:
        事件模式策略只重写 ``on_bar``，信号列模式策略只重写 ``populate_entry_trend`` /
        ``populate_exit_trend``。若用错模式，引擎会调用未被重写的空实现，
        结果是 0 笔成交且没有任何报错——本函数用于消除这种静默失败。
        Event-mode strategies only override ``on_bar``; signal-mode ones only override
        ``populate_entry_trend`` / ``populate_exit_trend``. Using the wrong mode drives an empty
        base implementation, which silently yields zero trades; this helper removes that trap.

    参数 | Parameters:
        strategy_cls (type[Strategy]): 待实例化的策略类 | Strategy class to be instantiated.

    返回 | Returns:
        str: "event" 或 "signal" | Either "event" or "signal".

    流程 | Flow:
        检查 on_bar 是否被重写 → 检查 populate_* 是否被重写 → 按命中情况返回；
        两者都重写或都不重写时优先事件模式（与现有示例策略一致）。
        Check whether on_bar is overridden → check populate_* → return accordingly;
        when both or neither are overridden, prefer event mode (consistent with the samples).
    """
    has_event = getattr(strategy_cls, "on_bar", None) is not Strategy.on_bar  # 是否重写了事件回调 | Whether the event callback is overridden
    has_signal = (  # 是否重写了信号列方法 | Whether the signal-column methods are overridden
        getattr(strategy_cls, "populate_entry_trend", None) is not Strategy.populate_entry_trend  # 入场信号列 | Entry signal column
        or getattr(strategy_cls, "populate_exit_trend", None) is not Strategy.populate_exit_trend  # 出场信号列 | Exit signal column
    )
    if has_event and not has_signal:  # 仅事件回调 | Event callback only
        return "event"  # 事件模式 | Event mode
    if has_signal and not has_event:  # 仅信号列 | Signal columns only
        return "signal"  # 信号模式 | Signal mode
    return "event"  # 歧义或都未重写时优先事件模式 | Prefer event mode when ambiguous or unoverridden


def run_backtest(
    candles: list[Candle],
    strategy_cls: type[Strategy],
    *,
    mode: str | None = None,  # event-事件回调，signal-信号列；None 时按策略实现自动推断 | "event"/"signal"; None infers from the strategy
    symbol: str,
    interval: str,
    initial_cash: float = 10000.0,
    fee_rate: float = 0.001,
    slippage: float = 0.0,
    allow_short: bool = True,
    leverage: float = 1.0,  # 杠杆倍数，1 表示现货全额，>1 表示合约保证金交易 | Leverage; 1 is spot, >1 is margin
    min_notional: float = 0.0,  # 交易所最小名义金额，0 表示不约束 | Exchange minimum notional, 0 disables the check
    final_fill_basis: str = "close",  # 末根未成交订单处理：close-按收盘撮合，skip-直接放弃 | Final-bar handling: close or skip
    now: datetime | None = None,  # 收盘判定的参考时刻；None 取当前 UTC，测试可注入固定值 | Reference instant for closure checks; injectable for deterministic tests
) -> dict[str, Any]:
    """执行回测并返回结果摘要 | Run a backtest and return a result summary.

    参数 | Args:
        candles (list[Candle]): K 线序列 | The candle series.
        strategy_cls (type[Strategy]): 策略类（会被实例化）| The strategy class (will be instantiated).
        mode (str): event-事件回调模式，signal-信号列模式 | "event" for callback mode, "signal" for signal-column mode.
        symbol (str): 交易对元数据，传给信号模式策略 | Trading pair metadata passed to signal-mode strategies.
        interval (str): K 线周期元数据 | Candle interval metadata.
        initial_cash (float): 初始资金 | Initial capital.
        fee_rate (float): 单边手续费率 | One-way fee rate.
        slippage (float): 滑点比例 | Slippage ratio.
        allow_short (bool): 是否允许开空；现货回测传 False | Whether shorting is allowed; pass False for spot.
        leverage (float): 杠杆倍数；1 表示全额（现货口径），>1 时按保证金记账并在
            净值跌破维持保证金时强平 | Leverage; 1 is fully funded (spot), >1 books margin and
            liquidates once equity breaches the maintenance margin.
        min_notional (float): 交易所最小名义金额。>0 时低于该值的订单直接取消并记日志，
            使回测能复现实盘"碎单被拒"的行为；0 表示不校验
            | Venue minimum notional. Above 0, dust orders are cancelled and logged so the backtest
            reproduces the live rejection; 0 disables the check.
        final_fill_basis (str): 最后一根 K 线的信号无法等到"下一根开盘"。``close`` 按该根收盘
            价撮合（旧行为，便于与历史结果对比）；``skip`` 直接放弃，与实盘"必须有下一根才能成交"
            的语义一致 | Signals on the last bar have no next open. ``close`` fills at that bar's
            close (legacy, keeps historical results comparable); ``skip`` drops them, matching live
            semantics where a fill always needs the following bar.
        now (datetime | None): 收盘判定的参考时刻，默认当前 UTC。只影响"末尾未收盘/未来数据"
            守卫，不改变撮合口径；测试注入固定值可消除时钟依赖。
            | Reference instant for the closure guard only; it does not affect fill conventions.

    返回 | Returns:
        dict[str, Any]: 收益、回撤、成交明细与净值曲线 | Returns, drawdown, trade details and equity curve.
    """
    if len(candles) < 2:  # 至少需要两根 K 线（一根信号一根成交）| Need at least two candles (one for signal, one for execution)
        raise CustomException(msg="至少需要 2 根 K 线才能回测")  # K 线数量不足 | Not enough candles

    # 收盘守卫：实盘只会用已收盘 K 线算信号，回测若把最后一根"正在形成"的 K 线也当成已收盘，
    # 就等于用尚未走完的收盘价出信号并撮合——典型未来函数。数据文件通常是"下载到此刻"，
    # 末根往往正好是形成中的那根，所以这里必须先丢弃（至少保留两根）。
    # Closure guard: live only ever uses closed bars. Treating a forming last bar as closed means
    # signalling and filling on a price that has not finished forming — textbook look-ahead. Data
    # files are usually downloaded "up to now", so the last bar often is exactly that forming bar.
    dropped_unclosed = 0  # 被丢弃的未收盘根数 | Number of dropped unclosed bars
    now_utc = now or datetime.now(UTC)  # 统一参考时刻，避免逐根比较时时钟漂移 | One reference instant for all checks
    # 晚于当前时钟的 K 线不是"未收盘"，而是时区错乱或伪造数据：当成已收盘使用就是未来函数，
    # 而逐根丢弃又会把整份数据集清空（用户只会看到"数据不足"），所以这里显式报错。
    # Bars dated in the future are not "still forming" but broken timezones or fabricated data.
    # Using them as closed creates look-ahead; dropping them row by row empties the dataset and
    # only surfaces as "not enough data", so this raises an explicit, actionable error instead.
    future_bars = sum(  # 统计未来时间的 K 线数量 | Count future-dated bars
        1
        for candle in candles
        if (parsed_time := parse_bar_time(str(candle.date))) is not None and parsed_time > now_utc  # 仅统计可解析且晚于当前时刻的根 | Only parseable bars after now
    )
    if future_bars:  # 存在未来数据直接拒绝回测 | Refuse to backtest on future data
        raise CustomException(msg=f"数据包含 {future_bars} 根晚于当前时间的 K 线，请检查时区或数据源")  # 抛出可读错误 | Raise an actionable error
    # 连续丢弃尾部所有未收盘根：数据截断在多根未收盘处时（例如分钟线停在盘中），
    # 只丢一根仍会把剩下的形成中 K 线当已收盘，产生未来函数。
    # Drop every trailing unclosed bar: when the data stops mid-bar (intraday minute files),
    # dropping only one still leaves forming bars treated as closed, i.e. look-ahead.
    while len(candles) > 2:  # 保留至少两根，避免把数据集清空 | Keep at least two bars
        last_text = str(candles[-1].date)  # 末根时间文本 | Timestamp of the last bar
        if parse_bar_time(last_text) is None:  # 时间无法解析时不做任何丢弃 | Leave unparseable timestamps alone
            break  # 停止丢弃 | Stop dropping
        if bar_is_closed(last_text, interval, now=now_utc):  # 末根已收盘即正常结束 | Stop at the first closed bar
            break  # 停止丢弃 | Stop dropping
        candles = candles[:-1]  # 丢弃末根 | Drop it
        dropped_unclosed += 1  # 计数 | Count it
    if dropped_unclosed:  # 有丢弃时记录，便于用户核对数据 | Log so users can verify their data
        logger.warning(f"回测数据末 {dropped_unclosed} 根 K 线尚未收盘，已丢弃以避免未来函数")  # 记录告警 | Log a warning
    if len(candles) < 2:  # 丢弃后不足两根 | Not enough bars after dropping
        raise CustomException(msg="有效 K 线不足 2 根（末根未收盘已被丢弃），无法回测")  # 抛出业务异常 | Raise a business error
    if slippage <= 0:  # 未配置滑点会让回测系统性偏乐观 | Zero slippage makes results optimistic
        logger.warning("回测滑点配置为 0：成交价等于理论价，结果通常偏乐观，建议按点差/冲击成本设置")  # 记录告警 | Log a warning
    if final_fill_basis not in {"close", "skip"}:  # 口径只允许两种取值 | Only two bases are supported
        final_fill_basis = "close"  # 非法值回退到兼容行为 | Fall back to the legacy behaviour
    if leverage > 1 and not allow_short:  # 现货数据集不允许开杠杆：保证金口径下"只做多"没有意义，也容易误解
        # 现货数据集的杠杆会被服务层强制为 1，这里再兜一层，避免直接调用引擎时出现口径混淆
        # Leverage is meaningless on a spot dataset and is forced to 1 by the service layer;
        # this guard keeps direct engine callers from mixing the two accounting models.
        leverage = 1.0  # 强制退化为现货口径 | Fall back to the spot basis

    api = TradeAPI(  # 创建交易 API，封装撮合与账本 | Create the TradeAPI that encapsulates matching and accounting
        candles,  # K 线序列 | Candle series
        initial_cash=initial_cash,  # 初始资金 | Initial capital
        fee_rate=fee_rate,  # 手续费率 | Fee rate
        slippage=slippage,  # 滑点比例 | Slippage ratio
        allow_short=allow_short,  # 是否允许做空 | Whether shorting is allowed
        risk=RiskSettings.from_strategy(strategy_cls),  # 从策略类读取风控配置，与实盘同一份 | Read risk config from the strategy class, shared with live
        allow_add=bool(getattr(strategy_cls, "allow_position_add", False)),  # 是否允许重复开仓 | Whether pyramiding is allowed
        leverage=leverage,  # 杠杆倍数 | Leverage
    )  # TradeAPI 创建结束 | End of TradeAPI creation
    api.min_notional = max(0.0, float(min_notional or 0.0))  # 注入交易所最小名义金额 | Inject the venue minimum notional
    strategy = strategy_cls()  # 实例化用户策略 | Instantiate the user strategy
    warmup = max(0, int(getattr(strategy_cls, "startup_candle_count", 0) or 0))  # 预热根数：前 warmup 根不产生信号 | Warmup bars: no signals in the first warmup candles
    metadata = {"pair": symbol, "interval": interval}  # 传给信号模式策略的元数据 | Metadata passed to signal-mode strategies

    # 解析运行模式：未显式传入时按策略实际实现推断（重写 on_bar => event，
    # 只重写 populate_* 系列 => signal）。显式传入但与该实现冲突时给出告警，
    # 因为"事件模式策略按信号列回测"会静默产生 0 笔成交，极难排查。
    # Resolve the run mode: when omitted, infer it from what the strategy actually
    # implements (overriding on_bar => event, only populate_* => signal). Warn on an
    # explicit conflict, because running an event-mode strategy in signal mode
    # silently yields zero trades, which is very hard to diagnose.
    inferred_mode = _infer_mode(strategy_cls)  # 按策略实现推断模式 | Infer the mode from the implementation
    if mode is None:  # 未指定时采用推断值 | Use the inferred value when omitted
        mode = inferred_mode  # 采用推断模式 | Adopt the inferred mode
    elif mode != inferred_mode:  # 显式指定且与实现冲突 | Explicit value conflicts with the implementation
        logger.warning(  # 记录告警，提示可能 0 成交 | Warn about a likely zero-trade run
            f"回测模式({mode})与策略 {strategy_cls.__name__} 的实现({inferred_mode})不一致，"  # 说明冲突 | Describe the conflict
            f"可能产生 0 笔成交；建议传 mode='{inferred_mode}' 或省略该参数"  # 给出建议 | Suggest the fix
        )  # 告警结束 | End of warning

    try:  # 包裹策略执行，统一转换异常 | Wrap strategy execution to normalize exceptions
        for index in range(len(candles)):  # 逐根 K 线推进（事件驱动主循环）| Advance candle by candle (main event-driven loop)
            api.set_index(index)  # 告诉 API 当前处理到第几根（决定成交可见范围）| Tell the API the current index (controls execution visibility)
            api.execute_pending(index)  # 执行上一根挂起的信号单（下一根开盘成交）| Execute pending orders from the previous bar (fill at next open)

            # 风控优先于信号：先用本根区间更新移动止损极值，再判定止损与回撤熔断
            # Risk runs before signals: update trailing extremes with this bar's range, then evaluate stops.
            candle = candles[index]  # 当前 K 线 | Current candle
            api.mark(candle.close, candle.low, candle.high)  # 更新持仓期极值与净值峰值 | Update holding extremes and the equity peak
            for risk_order, risk_price in api.risk_orders(candle.open, candle.low, candle.high):  # 遍历触发的强平单 | Iterate triggered forced exits
                api.execute_now(risk_order, risk_price)  # 当根立即撮合（含跳空处理）| Fill immediately on this bar (gap-adjusted)

            if index >= warmup:  # 过了预热期才运行策略逻辑 | Only run strategy logic after the warmup period
                if mode == "event":  # 事件回调模式 | Event callback mode
                    strategy.on_bar(api, candles, index)  # 直接把 API、K 线与下标交给策略 | Hand API, candles and index directly to the strategy
                else:  # 信号列模式 | Signal-column mode
                    window = candles[: index + 1]  # 只暴露当前及之前的 K 线，防止未来函数 | Expose only current and prior candles to prevent lookahead
                    entries = strategy.populate_entry_trend(window, metadata) or []  # 获取开仓信号列 | Get the entry signal series
                    exits = strategy.populate_exit_trend(window, metadata) or []  # 获取平仓信号列 | Get the exit signal series
                    exit_signal = _last_signal(exits, index)  # 取当前下标对应的平仓信号 | Read the exit signal at the current index
                    entry_signal = _last_signal(entries, index)  # 取当前下标对应的开仓信号 | Read the entry signal at the current index

                    # 平仓优先，避免同根 K 线上开平信号互相覆盖 | Prioritize exits so entry/exit signals on the same bar don't overwrite each other
                    if exit_signal == 1:  # 平多信号 | Exit-long signal
                        api.sell(reason="exit_long")  # 卖出平掉多头 | Sell to close the long position
                    elif exit_signal == -1:  # 平空信号 | Exit-short signal
                        api.buycover(reason="exit_short")  # 买入平掉空头 | Buy to cover the short position

                    if entry_signal == 1:  # 开多信号 | Enter-long signal
                        api.buy(reason="entry_long")  # 买入开多 | Buy to open a long position
                    elif entry_signal == -1:  # 开空信号 | Enter-short signal
                        api.sellshort(reason="entry_short")  # 卖出开空 | Sell to open a short position

            api.record_equity(index)  # 记录本根 K 线结束时的净值快照 | Record an equity snapshot at the end of this candle
    except CustomException:  # 业务异常原样上抛，保留前端可展示信息 | Re-raise business exceptions unchanged to preserve display info
        raise  # 原样抛出 | Re-raise as-is
    except Exception as exc:  # noqa: BLE001 - 用户策略异常需要回传给前端展示 | User strategy errors must be returned to the frontend
        raise CustomException(msg=f"策略执行失败：{type(exc).__name__}: {exc}") from exc  # 包装成业务异常并保留原始异常链 | Wrap into a business exception preserving the cause

    # 最后一根 K 线产生的信号没有"下一根开盘"可成交。默认仍按该根收盘价撮合（旧行为，
    # 保证历史回测结果可复现）；传 skip 则直接放弃，与实盘"必须有下一根才能成交"完全一致。
    # Signals on the last bar have no next open. The default still fills at that bar's close (legacy
    # behaviour, keeps historical results reproducible); passing "skip" drops them, exactly like live.
    api.set_index(len(candles) - 1)  # 定位到最后一根 K 线 | Point to the last candle
    if final_fill_basis == "close":  # 兼容口径：按收盘撮合 | Legacy basis: fill at the close
        api.execute_pending(len(candles) - 1, final=True)  # 强制以最后收盘价撮合所有挂单 | Force-fill all pending orders at the final close
    else:  # 严格口径：末根信号直接放弃 | Strict basis: drop the last bar's signals
        if api.pending_count:  # 有待办订单时记录 | Log when something was dropped
            api.log(f"末根 K 线仍有 {api.pending_count} 笔挂单，按 skip 口径放弃（无下一根可成交）")  # 记录放弃原因 | Log the reason
        api.execute_pending(len(candles) - 1, drop=True)  # 清空队列且不成交 | Clear the queue without filling
    api.equity_curve[-1] = {  # 用最终账户状态覆盖最后一点净值快照 | Overwrite the last equity point with the final account state
        "date": candles[-1].date,  # 最后一根 K 线日期 | Date of the last candle
        "equity": round(api.equity, 6),  # 最终总权益，保留 6 位小数 | Final total equity, rounded to 6 decimals
        "cash": round(api.cash, 6),  # 最终现金，保留 6 位小数 | Final cash, rounded to 6 decimals
        "long_qty": round(api.long_qty, 8),  # 最终多头持仓量，保留 8 位小数 | Final long quantity, rounded to 8 decimals
        "short_qty": round(api.short_qty, 8),  # 最终空头持仓量，保留 8 位小数 | Final short quantity, rounded to 8 decimals
    }  # 净值快照覆盖结束 | End of equity snapshot overwrite

    final_equity = api.equity  # 最终权益用于收益计算 | Final equity used for return calculations
    total_return = final_equity / initial_cash - 1  # 总收益率 = 期末/期初 - 1 | Total return = final/initial - 1
    closed_trades = [trade for trade in api.trades if trade.pnl is not None]  # 已平仓成交（有盈亏值）| Closed trades (those with a pnl value)
    win_trades = [trade for trade in closed_trades if trade.pnl and trade.pnl > 0]  # 盈利的平仓单 | Winning closed trades

    first_date = datetime.fromisoformat(candles[0].date)  # 首根 K 线时间 | First candle time
    last_date = datetime.fromisoformat(candles[-1].date)  # 末根 K 线时间 | Last candle time
    days = max(1, (last_date - first_date).days)  # 回测天数，至少 1 天避免除零 | Backtest duration in days, at least 1 to avoid division by zero
    annualized_return = (final_equity / initial_cash) ** (365 / days) - 1 if final_equity > 0 else -1.0  # 年化收益（复利）；权益为负按 -100% 处理 | Annualized (compounded) return; -100% if equity is non-positive

    return {  # 组装回测结果字典 | Assemble the backtest result dictionary
        "strategy": strategy_cls.__name__,  # 策略类名 | Strategy class name
        "symbol": symbol,  # 交易对 | Trading pair
        "interval": interval,  # K 线周期 | Candle interval
        "initial_cash": initial_cash,  # 初始资金 | Initial capital
        "final_equity": round(final_equity, 6),  # 最终权益 | Final equity
        "total_return": round(total_return, 6),  # 总收益率 | Total return
        "annualized_return": round(annualized_return, 6),  # 年化收益率 | Annualized return
        "max_drawdown": round(_max_drawdown(api.equity_curve), 6),  # 最大回撤 | Maximum drawdown
        "trade_count": len(api.trades),  # 成交总笔数 | Total number of trades
        "closed_trade_count": len(closed_trades),  # 已平仓笔数 | Number of closed trades
        "win_rate": round(len(win_trades) / len(closed_trades), 6) if closed_trades else 0.0,  # 胜率；无平仓单时为 0 | Win rate; 0 when no closed trades
        "trades": [asdict(trade) for trade in api.trades],  # 成交明细（dataclass 转 dict）| Trade details (dataclasses converted to dicts)
        "equity_curve": api.equity_curve,  # 净值曲线 | Equity curve
        "logs": api.logs,  # 交易日志 | Trading logs
        "candle_count": len(candles),  # K 线总数 | Total number of candles
        "warmup_candles": warmup,  # 预热根数 | Number of warmup candles
        "dropped_unclosed": dropped_unclosed,  # 被丢弃的未收盘根数 | Dropped unclosed bars
        "fill_basis": "next_open",  # 常规成交口径：下一根开盘 | Regular fill basis: next open
        "final_fill_basis": final_fill_basis,  # 末根口径：close/skip | Final-bar basis
        "min_notional": api.min_notional,  # 生效的最小名义金额 | Effective minimum notional
        "slippage_assumption": slippage,  # 本次使用的滑点假设，便于审计乐观程度 | Slippage assumption for auditability
        "leverage": api.leverage,  # 实际生效的杠杆倍数（服务层可能把现货强制为 1）| Effective leverage
        "liquidation_price": round(api.liquidation_price, 8),  # 期末理论强平价，0 表示不适用 | Closing liquidation price, 0 when N/A
        "liquidated": api.ledger.liquidated,  # 是否发生过强制平仓 | Whether a liquidation happened
        "risk": {  # 本次回测生效的风控配置，便于核对与实盘是否一致 | Effective risk config for auditability
            "stoploss": api.ledger.risk.stoploss,  # 固定止损 | Fixed stoploss
            "trailing_stop": api.ledger.risk.trailing_stop,  # 移动止损 | Trailing stop
            "max_drawdown": api.ledger.risk.max_drawdown,  # 回撤熔断 | Drawdown breaker
            "allow_position_add": api.allow_add,  # 是否允许加仓 | Whether pyramiding is allowed
        },  # 风控配置结束 | End of risk config
    }  # 结果字典结束 | End of result dictionary
