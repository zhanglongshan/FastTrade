"""轻量事件驱动回测引擎。

设计参考 freqtrade：信号在当前 K 线收盘产生，下一根 K 线开盘成交；
但本实现仅依赖标准库，不要求 pandas，策略可直接调用 api.buy()/sell() 等方法。
"""

import csv
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.exceptions import CustomException

from ..strategy.api import Candle, Strategy, TradeAPI

CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def load_candles(path: Path) -> list[Candle]:
    """从标准 OHLCV CSV 读取 K 线。"""
    if not path.is_file():
        raise CustomException(msg=f"数据文件不存在：{path}")
    candles: list[Candle] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing = [column for column in CSV_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise CustomException(msg=f"CSV 缺少必需列：{', '.join(missing)}")
        for line_no, row in enumerate(reader, start=2):
            try:
                candles.append(
                    Candle(
                        date=str(row["date"]).strip(),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise CustomException(msg=f"CSV 第 {line_no} 行格式无效：{exc}") from exc
    if not candles:
        raise CustomException(msg="数据集为空，无法回测")
    return sorted(candles, key=lambda item: item.date)


def _last_signal(signals: list[int], index: int) -> int:
    """读取当前下标对应的信号；信号列表不足时取最后一个。"""
    if not signals:
        return 0
    return signals[index] if len(signals) > index else signals[-1]


def _max_drawdown(curve: list[dict[str, Any]]) -> float:
    """计算最大回撤比例。"""
    peak = 0.0
    worst = 0.0
    for point in curve:
        equity = float(point["equity"])
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


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
) -> dict[str, Any]:
    """执行回测并返回结果摘要。

    参数:
    - candles: K 线序列。
    - strategy_cls: 策略类。
    - mode: event-事件回调，signal-信号列。
    - symbol/interval: 元数据，传给信号模式策略。
    - initial_cash/fee_rate/slippage: 账户与成交参数。

    返回:
    - dict[str, Any]: 收益、回撤、成交明细与净值曲线。
    """
    if len(candles) < 2:
        raise CustomException(msg="至少需要 2 根 K 线才能回测")

    api = TradeAPI(candles, initial_cash=initial_cash, fee_rate=fee_rate, slippage=slippage)
    strategy = strategy_cls()
    warmup = max(0, int(getattr(strategy_cls, "startup_candle_count", 0) or 0))
    metadata = {"pair": symbol, "interval": interval}

    try:
        for index in range(len(candles)):
            api.set_index(index)
            api.execute_pending(index)

            if index >= warmup:
                if mode == "event":
                    strategy.on_bar(api, candles, index)
                else:
                    window = candles[: index + 1]
                    entries = strategy.populate_entry_trend(window, metadata) or []
                    exits = strategy.populate_exit_trend(window, metadata) or []
                    exit_signal = _last_signal(exits, index)
                    entry_signal = _last_signal(entries, index)

                    # 平仓优先，避免同根 K 线上开平信号互相覆盖
                    if exit_signal == 1:
                        api.sell(reason="exit_long")
                    elif exit_signal == -1:
                        api.buycover(reason="exit_short")

                    if entry_signal == 1:
                        api.buy(reason="entry_long")
                    elif entry_signal == -1:
                        api.sellshort(reason="entry_short")

            api.record_equity(index)
    except CustomException:
        raise
    except Exception as exc:  # noqa: BLE001 - 用户策略异常需要回传给前端展示
        raise CustomException(msg=f"策略执行失败：{type(exc).__name__}: {exc}") from exc

    # 最后一根 K 线产生的信号无法在下一根开盘成交，按最后收盘价撮合
    api.set_index(len(candles) - 1)
    api.execute_pending(len(candles) - 1, final=True)
    api.equity_curve[-1] = {
        "date": candles[-1].date,
        "equity": round(api.equity, 6),
        "cash": round(api.cash, 6),
        "long_qty": round(api.long_qty, 8),
        "short_qty": round(api.short_qty, 8),
    }

    final_equity = api.equity
    total_return = final_equity / initial_cash - 1
    closed_trades = [trade for trade in api.trades if trade.pnl is not None]
    win_trades = [trade for trade in closed_trades if trade.pnl and trade.pnl > 0]

    first_date = datetime.fromisoformat(candles[0].date)
    last_date = datetime.fromisoformat(candles[-1].date)
    days = max(1, (last_date - first_date).days)
    annualized_return = (final_equity / initial_cash) ** (365 / days) - 1 if final_equity > 0 else -1.0

    return {
        "strategy": strategy_cls.__name__,
        "symbol": symbol,
        "interval": interval,
        "initial_cash": initial_cash,
        "final_equity": round(final_equity, 6),
        "total_return": round(total_return, 6),
        "annualized_return": round(annualized_return, 6),
        "max_drawdown": round(_max_drawdown(api.equity_curve), 6),
        "trade_count": len(api.trades),
        "closed_trade_count": len(closed_trades),
        "win_rate": round(len(win_trades) / len(closed_trades), 6) if closed_trades else 0.0,
        "trades": [asdict(trade) for trade in api.trades],
        "equity_curve": api.equity_curve,
        "logs": api.logs,
        "candle_count": len(candles),
        "warmup_candles": warmup,
    }
