"""轻量事件驱动回测引擎 | Lightweight event-driven backtesting engine.

设计参考 freqtrade：信号在当前 K 线收盘产生，下一根 K 线开盘成交；
但本实现仅依赖标准库，不要求 pandas，策略可直接调用 api.buy()/sell() 等方法。
Design inspired by freqtrade: signals are generated at the current candle's close and
executed at the next candle's open; this implementation only uses the standard library,
does not require pandas, and strategies can directly call api.buy()/sell() etc.
"""

import csv  # 导入 CSV 读写模块，用于解析 OHLCV 数据文件 | Import CSV module for parsing OHLCV data files
from dataclasses import asdict  # 导入 dataclass 转字典工具，用于序列化成交记录 | Import helper to convert dataclasses to dicts
from datetime import datetime  # 导入 datetime，用于解析 K 线时间与计算年化 | Import datetime for parsing candle time and annualization
from pathlib import Path  # 导入 Path，用于文件路径操作 | Import Path for file path operations
from typing import Any  # 导入 Any，用于结果字典的宽松类型标注 | Import Any for loose typing of the result dict

from app.core.exceptions import CustomException  # 导入业务异常类，用于统一抛出可展示给前端的错误 | Import business exception for user-facing errors

from ..strategy.api import Candle, Strategy, TradeAPI  # 导入 K 线、策略基类与交易 API（回测撮合核心）| Import Candle, Strategy base and TradeAPI (matching core)

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
    return sorted(candles, key=lambda item: item.date)  # 按日期字符串升序返回（ISO 时间字符串可直接字典序比较）| Return sorted by date (ISO strings sort lexicographically)


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


def run_backtest(
    candles: list[Candle],
    strategy_cls: type[Strategy],
    *,
    mode: str,
    symbol: str,
    interval: str,
    initial_cash: float = 10000.0,
    fee_rate: float = 0.001,
    slippage: float = 0.0,
    allow_short: bool = True,
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

    返回 | Returns:
        dict[str, Any]: 收益、回撤、成交明细与净值曲线 | Returns, drawdown, trade details and equity curve.
    """
    if len(candles) < 2:  # 至少需要两根 K 线（一根信号一根成交）| Need at least two candles (one for signal, one for execution)
        raise CustomException(msg="至少需要 2 根 K 线才能回测")  # K 线数量不足 | Not enough candles

    api = TradeAPI(  # 创建交易 API，封装撮合与账本 | Create the TradeAPI that encapsulates matching and accounting
        candles,  # K 线序列 | Candle series
        initial_cash=initial_cash,  # 初始资金 | Initial capital
        fee_rate=fee_rate,  # 手续费率 | Fee rate
        slippage=slippage,  # 滑点比例 | Slippage ratio
        allow_short=allow_short,  # 是否允许做空 | Whether shorting is allowed
    )  # TradeAPI 创建结束 | End of TradeAPI creation
    strategy = strategy_cls()  # 实例化用户策略 | Instantiate the user strategy
    warmup = max(0, int(getattr(strategy_cls, "startup_candle_count", 0) or 0))  # 预热根数：前 warmup 根不产生信号 | Warmup bars: no signals in the first warmup candles
    metadata = {"pair": symbol, "interval": interval}  # 传给信号模式策略的元数据 | Metadata passed to signal-mode strategies

    try:  # 包裹策略执行，统一转换异常 | Wrap strategy execution to normalize exceptions
        for index in range(len(candles)):  # 逐根 K 线推进（事件驱动主循环）| Advance candle by candle (main event-driven loop)
            api.set_index(index)  # 告诉 API 当前处理到第几根（决定成交可见范围）| Tell the API the current index (controls execution visibility)
            api.execute_pending(index)  # 执行上一根挂起的信号单（下一根开盘成交）| Execute pending orders from the previous bar (fill at next open)

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

    # 最后一根 K 线产生的信号无法在下一根开盘成交，按最后收盘价撮合 | Signals on the last candle can't fill at a next open, so match at the last close
    api.set_index(len(candles) - 1)  # 定位到最后一根 K 线 | Point to the last candle
    api.execute_pending(len(candles) - 1, final=True)  # 强制以最后收盘价撮合所有挂单 | Force-fill all pending orders at the final close
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
    }  # 结果字典结束 | End of result dictionary
