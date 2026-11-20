"""统一策略运行时：一次 tick 拉行情、跑策略、执行订单、产出结果 | Unified strategy runtime: one tick fetches data, runs strategy, executes orders, produces results.

目的：为回测之外的模拟盘/实盘提供滚动 K 线窗口驱动的运行时，把"拉行情 → 跑策略 →
撮合订单 → 产出结果"封装成一次 ``run_tick`` 调用，三种模式共享该函数，差异仅在
``executor``。
Purpose: Provide a rolling-candle-window runtime for paper/live modes, wrapping
"fetch data → run strategy → fill orders → produce result" into one ``run_tick`` call;
all modes share it and differ only in ``executor``.

主要流程 | Main flow:
- 模拟盘/实盘没有"完整数据集"，行情由 WebSocket 推送维护成滚动窗口；每根 K 线收盘时
  以"已收盘窗口 + 新一根开盘价"调用一次策略，与回测时序一致 | Candles come from the
  WebSocket stream; each close runs the strategy on the closed window plus the new open.
- 策略通过统一的 :class:`~.api.StrategyAPI` 下单，订单由 :class:`Executor` 即时撮合
  | Strategy orders via StrategyAPI, filled immediately by the Executor.
- 执行器差异：PaperExecutor 本地虚拟撮合（受盘口深度约束）；LiveExecutor 调币安下单、
  回查确认后记账（dry_run 时仍走本地撮合）| PaperExecutor fills locally with a depth cap;
  LiveExecutor places, confirms and books (dry_run still fills locally).
"""

import asyncio  # 导入异步 IO 库 | Import asyncio for thread offloading
import time  # 导入时间库（对账节流）| Import time for reconciliation throttling
from dataclasses import dataclass, field  # 导入 dataclass 工具 | Import dataclass helpers
from datetime import UTC, datetime  # 导入 UTC 与时间类 | Import UTC and datetime
from typing import Any, Protocol  # 导入 Any 与 Protocol 类型 | Import Any and Protocol typing

from app.core.exceptions import CustomException  # 导入自定义异常 | Import CustomException for error signaling
from app.core.logger import logger  # 导入日志器（风控异常记录）| Import the logger for risk-error logging

from ..strategy.api import Candle, Strategy  # 导入 K 线与策略类型 | Import Candle and Strategy types
from ..strategy.loader import load_strategy  # 导入策略加载器 | Import strategy loader
from ..strategy.trading_base import bar_is_closed, sizing_qty  # 导入周期换算/收盘判定/数量换算 | Import interval, closure and sizing helpers
from .api import StrategyAPI  # 导入统一策略交易接口 | Import unified StrategyAPI
from .base import AccountLedger, Fill, OrderRequest, RunMode  # 导入账本/成交/订单请求/运行模式 | Import ledger, fill, order request, run mode
from .exchange import fetch_klines_async, order_fill_summary, sum_trade_commission  # 导入行情与订单结果解析 | Import market data and order-result parsing
from .reconcile import reconcile_summary, replay_trades, stale_open_orders  # 导入对账纯函数 | Import reconciliation helpers


class Executor(Protocol):  # 执行器协议 | Executor protocol
    """执行器协议：把订单请求变成成交 | Executor protocol: turn order requests into fills.

    定义执行器必须实现的方法签名，供 :func:`run_tick` 按协议调用，屏蔽具体撮合差异。
    Defines the methods an executor must implement so ``run_tick`` can call it uniformly.

    关键方法 | Key methods:
    - execute: 异步执行一笔订单 | Asynchronously execute one order.
    - snapshot: 返回可持久化状态 | Return persistable state.
    """

    async def execute(self, order: OrderRequest, price: float, depth: tuple[float, float] | None = None) -> Fill | None:  # 异步执行订单 | Execute an order asynchronously
        """执行一笔订单，返回成交记录；被拒绝时返回 None | Execute an order, return Fill or None when rejected.

        参数 | Parameters:
        - order (OrderRequest): 下单请求 | The order request.
        - price (float): 执行参考价格 | Reference execution price.
        - depth (tuple[float, float] | None): (买一量, 卖一量) 盘口深度，None 表示不限制
          | (bid size, ask size) top-of-book depth, None means unconstrained.

        返回 | Returns:
        - Fill | None: 成交记录或被拒时的 None | Fill record, or None when rejected.
        """
        ...  # 协议方法无具体实现，由子类提供 | No implementation in protocol; provided by implementers

    def snapshot(self) -> dict[str, Any]:  # 返回执行器可持久化状态 | Return executor persistable state
        """返回执行器可持久化的状态（账本快照等） | Return executor state (ledger snapshot, etc.) for persistence.

        返回 | Returns:
        - dict[str, Any]: 可持久化状态字典 | Persistable state dict.
        """
        ...  # 协议方法无具体实现，由子类提供 | No implementation in protocol; provided by implementers


@dataclass  # 声明数据类装饰器 | Declare dataclass decorator
class TickResult:  # 一次策略运行的结果 | Result of one strategy tick
    """一次策略运行的结果 | Result of one strategy tick.

    汇总一次 tick 的模式、行情、账户与成交/日志/错误等信息，便于落库与展示。
    Summarizes mode, market, account, fills/logs/errors of one tick for storage and display.

    关键属性 | Key attributes:
    - mode/symbol/interval/strategy: 运行上下文标识 | Run-context identifiers.
    - price/equity/cash/position: 行情与账户快照 | Market and account snapshot.
    - fills/logs/rejected/error: 成交、日志、被拒订单与错误 | Fills, logs, rejections, error.
    - tick_time: tick 时间标签 | Tick timestamp label.
    """

    mode: str  # 运行模式字符串 | Run mode string
    symbol: str  # 交易对 | Trading symbol
    interval: str  # K 线周期 | Candle interval
    strategy: str  # 策略名称 | Strategy name
    price: float  # 当前价格 | Current price
    equity: float  # 账户净值 | Account equity
    cash: float  # 可用现金 | Available cash
    position: dict[str, float]  # 持仓快照 | Position snapshot
    fills: list[dict[str, Any]] = field(default_factory=list)  # 成交记录列表，工厂函数避免共享默认值 | Fill list via factory
    logs: list[str] = field(default_factory=list)  # 日志列表，工厂函数避免共享默认值 | Log list via factory
    rejected: list[str] = field(default_factory=list)  # 被拒订单列表，工厂函数避免共享默认值 | Rejected-order list via factory
    error: str | None = None  # 错误信息，默认 None | Error message, default None
    tick_time: str = ""  # tick 时间标签，默认空串 | Tick timestamp label, default empty
    signal_bar: str = ""  # 产生信号的那根已收盘 K 线时间 | Closed signal bar timestamp
    fill_basis: str = ""  # 成交价口径：next_open / mid_fallback / bar_close / risk_only | Fill price basis
    leverage: float = 1.0  # 本次运行生效的杠杆倍数 | Effective leverage for this tick
    liquidation_price: float = 0.0  # 当前持仓的理论强平价，0 表示不适用 | Liquidation price, 0 when N/A
    # 因盘口深度不足而未成交的数量：回测按全量成交记账，实盘/模拟盘必须把差额显式记下来，
    # 否则"少成交"完全静默，绩效偏差无法归因。
    # Unfilled quantity left by the book-depth cap: the backtest books the full size, so paper/live
    # must record the shortfall explicitly instead of silently under-filling.
    unfilled: list[dict[str, Any]] = field(default_factory=list)  # 未成交明细 | Unfilled details

    def to_dict(self) -> dict[str, Any]:  # 转为可序列化字典 | Convert to serializable dict
        """转为可落库/可序列化的字典 | Convert to a dict suitable for persistence/serialization.

        返回 | Returns:
        - dict[str, Any]: 含所有结果字段的字典 | Dict with all result fields.
        """
        return {  # 构造结果字典 | Build the result dict
            "mode": self.mode,  # 运行模式 | Run mode
            "symbol": self.symbol,  # 交易对 | Trading symbol
            "interval": self.interval,  # K 线周期 | Candle interval
            "strategy": self.strategy,  # 策略名称 | Strategy name
            "price": self.price,  # 当前价格 | Current price
            "equity": self.equity,  # 账户净值 | Account equity
            "cash": self.cash,  # 可用现金 | Available cash
            "position": self.position,  # 持仓快照 | Position snapshot
            "fills": self.fills,  # 成交记录列表 | Fill list
            "logs": self.logs,  # 日志列表 | Log list
            "rejected": self.rejected,  # 被拒订单列表 | Rejected-order list
            "error": self.error,  # 错误信息 | Error message
            "tick_time": self.tick_time,  # tick 时间标签 | Tick timestamp
            "signal_bar": self.signal_bar,  # 信号 bar 时间 | Signal bar timestamp
            "fill_basis": self.fill_basis,  # 成交价口径 | Fill price basis
            "leverage": self.leverage,  # 杠杆倍数 | Leverage
            "liquidation_price": self.liquidation_price,  # 理论强平价 | Liquidation price
            "unfilled": self.unfilled,  # 深度不足未成交明细 | Unfilled details
        }


def _bar_is_closed(candle: Candle, interval: str) -> bool:  # 判断 K 线是否已收盘 | Check whether a candle is closed
    """判断一根 K 线是否已经走完（收盘） | Check whether a candle has finished forming.

    目的 | Purpose:
        币安返回的最后一根 K 线通常是"正在形成"的，其收盘价会持续变化；
        用它计算信号会造成盘中穿越、到收盘又回去的假信号。此函数用于识别并丢弃它。
        判定规则已收敛到 :func:`~..strategy.trading_base.bar_is_closed`，REST 与
        WebSocket 两条行情来源共用同一份实现，避免口径漂移。
        Binance's last kline is usually still forming and its close keeps moving; using it
        creates intra-bar signals that vanish by the close. The rule now lives in
        ``trading_base.bar_is_closed`` so the REST and WebSocket paths cannot drift apart.

    参数 | Parameters:
        candle (Candle): 待判断的 K 线 | Candle to check.
        interval (str): K 线周期 | Candle interval.

    返回 | Returns:
        bool: 已收盘返回 True；时间无法解析时保守返回 False | True when closed; False when the timestamp is unparseable.
    """
    return bar_is_closed(str(candle.date), interval)  # 复用共享判定 | Reuse the shared check


def _resolve_signal_and_price(  # 解析信号窗口与成交价 | Resolve the signal window and fill price
    window: list[Candle],  # 完整行情窗口 | Full market window
    interval: str,  # K 线周期 | Candle interval
    drop_unclosed: bool,  # 是否丢弃未收盘 K 线 | Whether to drop the unclosed candle
) -> tuple[list[Candle], float, Candle, str]:  # (信号窗口, 成交价, 成交参考 bar, 口径) | (signal window, fill price, reference bar, basis)
    """把"最新窗口"转换成"与回测一致的信号窗口 + 成交价" | Convert the latest window into a backtest-consistent signal window and fill price.

    目的 | Purpose:
        回测的语义是"第 N 根收盘出信号，第 N+1 根开盘成交"。实盘要复刻它，必须：
        1) 丢掉正在形成的最后一根，避免用未收盘价算信号；
        2) 用它的**开盘价**作为成交价，这正是"下一根开盘"。
        Backtest semantics are "signal at bar N close, fill at bar N+1 open". To replicate it
        live we must drop the forming bar (so signals use closed data) and fill at that bar's
        open, which literally is "the next bar's open".

    参数 | Parameters:
        window (list[Candle]): 按时间升序的行情窗口 | Time-ascending market window.
        interval (str): K 线周期 | Candle interval.
        drop_unclosed (bool): False 时退回旧的"按最后一根收盘价成交"行为 | When False, fall back to legacy close-price fills.

    返回 | Returns:
        tuple: (信号窗口, 成交价, 成交参考 bar, 口径字符串) | (signal window, fill price, reference bar, basis label).

    流程 | Flow:
        未启用兼容模式且末根未收盘 → 信号窗口去掉末根、成交价取末根开盘；
        否则 → 信号窗口原样、成交价取末根收盘。
        When the last bar is unclosed and compat mode is off, drop it and fill at its open;
        otherwise keep the window and fill at the last close.
    """
    last = window[-1]  # 取最后一根 K 线 | Take the last candle
    if drop_unclosed and len(window) >= 2 and not _bar_is_closed(last, interval):  # 末根仍在形成 | The last bar is still forming
        return window[:-1], float(last.open), last, "next_open"  # 信号用已收盘根，按下一根开盘成交 | Signal on closed bars, fill at next open
    return window, float(last.close), last, "bar_close"  # 兼容模式：按最后一根收盘价成交 | Compat mode: fill at the last close


def _last_signal(signals: list[int], index: int) -> int:  # 读取当前下标信号 | Read signal at current index
    """读取当前下标信号；越界时取最后一个 | Read the signal at index; use the last one when out of range.

    参数 | Parameters:
    - signals (list[int]): 信号序列 | Signal sequence.
    - index (int): 期望读取的下标 | Desired index.

    返回 | Returns:
    - int: 对应信号；空序列返回 0 | Corresponding signal, 0 for empty sequence.
    """
    if not signals:  # 信号序列为空时返回 0 | Return 0 when signal sequence is empty
        return 0  # 返回默认无信号值 | Return default no-signal value
    return signals[index] if len(signals) > index else signals[-1]  # 下标有效取对应值，否则取最后一个 | Return at index if valid, else last


async def run_tick(  # 执行一次策略 tick  | Execute one strategy tick
    *,  # 强制后续参数为关键字参数 | Force all params to be keyword-only
    mode: RunMode | str,  # 运行模式 | Run mode
    executor: Executor,  # 订单执行器 | Order executor
    ledger: AccountLedger,  # 账户账本 | Account ledger
    strategy_name: str,  # 策略类名 | Strategy class name
    symbol: str,  # 交易对 | Trading symbol
    interval: str,  # K 线周期 | Candle interval
    candles: list[Candle] | None = None,  # 可选历史 K 线 | Optional historical candles
    kline_limit: int = 300,  # 拉取 K 线根数 | Number of candles to fetch
    allow_short: bool = True,  # 是否允许做空 | Whether shorting allowed
    drop_unclosed: bool = True,  # 是否丢弃未收盘 K 线（与回测对齐）| Whether to drop the forming candle
    last_signal_bar: str = "",  # 上次已处理的信号 bar（幂等，避免重复处理同一根）| Last processed signal bar for idempotency
    execution_price: float | None = None,  # 事件驱动路径的成交价（新 bar 开盘价）| Fill price for the event-driven path (new bar open)
    depth: tuple[float, float] | None = None,  # (买一量, 卖一量) 盘口深度 | (bid size, ask size) top-of-book depth
    risk_only: bool = False,  # 只跑风控、不跑策略（盘中 1 分钟风控检查）| Run risk only, no strategy (intra-bar 1m risk check)
    fill_basis_hint: str = "",  # 行情流给出的成交价口径（next_open/mid_fallback）| Basis reported by the stream
    spread_ratio: float = 0.0,  # 半价差比例；mid_fallback 时买加卖减以吃掉点差 | Half-spread applied to mid-based fills
    min_notional: float = 0.0,  # 交易所最小名义额（计价币），0 表示不约束 | Venue minimum notional, 0 disables
) -> TickResult:  # 返回本次 tick 的结果 | Return this tick's result
    """执行一次策略 tick | Execute one strategy tick.

    参数 | Parameters:
    - mode (RunMode | str): 运行模式（paper/live）| Run mode.
    - executor (Executor): 订单执行器，决定订单如何成交 | Executor deciding how orders fill.
    - ledger (AccountLedger): 账户账本（由上层从持久化状态恢复）| Ledger restored upstream.
    - strategy_name (str): 策略类名 | Strategy class name.
    - symbol (str): 交易对 | Trading symbol.
    - interval (str): K 线周期 | Candle interval.
    - candles (list[Candle] | None): 可选历史 K 线；不传则从币安拉取最新窗口 | Optional candles; fetch if omitted.
    - kline_limit (int): 拉取 K 线根数 | Number of candles to fetch.
    - allow_short (bool): 是否允许做空（实盘现货 False）| Whether shorting allowed.
    - execution_price (float | None): 事件驱动路径下由行情流给出的"下一根开盘价"。给出时
      跳过 ``_resolve_signal_and_price``（窗口已是纯收盘数据），保证成交口径仍是 next_open。
      | The next-open price supplied by the market stream. When given, the REST-oriented
      resolution is skipped because the window already holds closed bars only.
    - depth (tuple[float, float] | None): 盘口深度，用于给成交量设上限（模拟盘无深度是
      与实际最大的差距之一）| Top-of-book depth used to cap fill size.
    - risk_only (bool): 只跑风控不跑策略。周期大于 1 分钟时，调度器会在每根 1 分钟
      K 线收盘时调用一次，把止损发现延迟从"一根大周期"缩短到 1 分钟。
      | Risk-only mode: for intervals above one minute the scheduler calls this on every 1m
      close, shortening stop detection lag from one big bar to one minute.
    - fill_basis_hint (str): 行情流解析出的成交价口径。为 ``mid_fallback`` 时说明新 bar
      尚未推送、价格来自盘口中间价，结果里必须如实标注，便于区分"精确开盘价"与"近似价"。
      | The basis reported by the stream. ``mid_fallback`` means the new bar was not published yet
      and the price is the book mid, so the result must label it instead of claiming next_open.
    - spread_ratio (float): 半价差比例，仅对 ``mid_fallback`` 生效：买入加、卖出减，
      把"中间价不等于可成交价"这层成本补回来。
      | Half-spread, applied only to mid-based fills: added for buys, subtracted for sells, so a
      mid price is not mistaken for a tradeable one.
    - min_notional (float): 交易所最小名义额（计价币）。>0 时低于该值的**开仓**订单被取消并
      记入 ``unfilled[reason=min_notional]``，与回测的 ``api.min_notional`` 同口径；平仓单不
      校验，避免小仓位被规则卡住永远平不掉。
      | Venue minimum notional. Above zero, **entry** orders below it are cancelled and reported as
      ``min_notional``, matching the backtest's ``api.min_notional``; exits are exempt so a small
      position can always be closed.

    返回 | Returns:
    - TickResult: 本次 tick 价格、净值、持仓、成交与日志 | Price, equity, positions, fills and logs.

    流程 | Flow:
    1) 解析模式/时间/策略 → 2) 准备行情窗口 → 3) 风控强平 → （risk_only 则返回）→
    4) 构造统一接口并调用策略 → 5) 撮合排队订单 → 返回 TickResult。
    Resolve mode/time/strategy → prepare window → risk exits → (return when risk_only) →
    build API and run strategy → fill queued orders → return TickResult.
    """
    mode_value = mode.value if isinstance(mode, RunMode) else str(mode)  # 将运行模式统一为字符串 | Normalize mode to string
    now_iso = datetime.now(UTC).isoformat()  # 生成 UTC ISO 格式当前时间标签 | Generate UTC ISO timestamp
    info = load_strategy(strategy_name)  # 按名称加载策略信息 | Load strategy info by name
    strategy: Strategy = info.cls()  # 实例化策略对象 | Instantiate the strategy object
    resolved_symbol = symbol.replace("/", "").replace("_", "").upper()  # 规范化交易对：去斜杠/下划线并转大写 | Normalize symbol

    # 1) 行情窗口
    # 1) Market-data window
    if candles:  # 提供了历史 K 线时直接使用 | Use provided historical candles directly
        window = sorted(candles, key=lambda item: item.date)  # 按日期升序排序 | Sort by date ascending
    else:  # 未提供时从币安拉取最新窗口 | Otherwise fetch latest window from Binance
        warmup_hint = max(0, int(getattr(info.cls, "startup_candle_count", 0) or 0))  # 读取策略预热 K 线数提示 | Read strategy warmup candle hint
        window = await fetch_klines_async(resolved_symbol, interval, limit=max(warmup_hint + 5, kline_limit))  # 异步拉取足够根数的 K 线 | Fetch enough candles asynchronously
    if len(window) < 2:  # K 线不足两根无法运行 | Need at least two candles to run
        raise CustomException(msg="K 线不足，至少需要 2 根才能运行策略")  # 抛出数据不足异常 | Raise insufficient-data error

    # 2) 解析信号窗口与成交价：与回测"第 N 根收盘出信号、第 N+1 根开盘成交"严格对齐
    # 2) Resolve the signal window and fill price to match "signal at N close, fill at N+1 open".
    if execution_price is not None and execution_price > 0 and candles:  # 事件驱动：行情流已给出收盘窗口与开盘价 | Event-driven: the stream supplied closed bars and the open
        signal_window = window  # 窗口本身只含已收盘 K 线 | The window already holds closed bars only
        price = float(execution_price)  # 直接使用新 bar 的开盘价 | Use the new bar's open directly
        # 口径以行情流的实际来源为准：拿不到新 bar 开盘价时它是 mid_fallback，
        # 不能在这里一律标成 next_open，否则"近似成交"会被误认为"精确开盘"。
        # The basis follows what the stream actually produced: when the new bar's open was not
        # available it is mid_fallback, so it must not be relabelled as an exact next_open.
        fill_basis = fill_basis_hint or "next_open"  # 口径标记 | Basis label
        stale_bar = False  # 事件驱动不会出现陈旧窗口 | No stale window on the event path
    else:  # REST 路径：按"末根是否收盘"推断成交口径 | REST path: infer the basis from bar closure
        signal_window, price, _ref_bar, fill_basis = _resolve_signal_and_price(window, interval, drop_unclosed)  # 解析执行口径（_ref_bar 为正在形成那根，风控不再使用）| Resolve the execution basis
        # 陈旧行情守卫：实时拉取的窗口若末根已收盘，说明交易所尚未发布新 bar。
        # 此时按该根收盘价成交会退回旧的"当根收盘成交"口径（回测是"下一根开盘"），
        # 属于偶发但有方向的偏差。因此这类 tick 只跑风控、不产生新信号。
        # Stale-data guard: if a live-fetched window ends on a closed bar, the exchange has not
        # published the next one yet. Filling at that close would fall back to the legacy
        # "fill on the signal close" basis, a small but directional bias.
        stale_bar = bool(candles is None and drop_unclosed and fill_basis == "bar_close")  # 是否落在陈旧行情上 | Whether the window is stale
    if len(signal_window) < 2:  # 信号窗口至少两根才能算指标 | At least two bars are needed for indicators
        raise CustomException(msg="K 线不足，至少需要 2 根才能运行策略")  # 抛出数据不足异常 | Raise insufficient-data error

    # 3) 风控优先：用**最近一根已收盘** K 线的完整区间更新极值与判定止损。
    #    不能沿用正在形成那根的区间——它刚开盘时 low≈high≈open，等于只检查了跳空，
    #    盘中触及止损的情况会被静默漏掉（回测用整根 low/high，会正常止损）。
    #    代价是止损最多晚一根 K 线发现，但不会漏判。
    # 3) Risk first: use the LATEST CLOSED bar's full range for extremes and stops.
    #    Using the forming bar's range is wrong — right after its open low≈high≈open, so only
    #    gaps are detected and intra-bar stop breaches are silently missed (the backtest uses
    #    the full bar range and does trigger). The cost is up to one bar of detection lag,
    #    but breaches are never skipped.
    risk_bar = signal_window[-1]  # 最近一根已收盘 K 线（区间完整）| The latest closed bar with a complete range
    ledger.mark(risk_bar.close, low=risk_bar.low, high=risk_bar.high, update_equity_peak=not risk_only)  # 更新风控状态（心跳不抬净值峰值）| Update risk state; heartbeats leave the equity peak alone
    fills: list[Fill] = []  # 初始化成交记录列表 | Init fill record list
    unfilled: list[dict[str, Any]] = []  # 深度不足未成交明细 | Unfilled-by-depth details
    # 本次风控新产生的补单先放本地：同一份静态盘口已经用于止损主单，若立刻再登记到全局队列，
    # 本次 tick 的补成交会把"同一档流动性"重复用一次，凭空虚增成交量。
    # Risk remainders stay local for now: the same static book snapshot already capped the main stop,
    # so registering them globally would let this tick's retry reuse that one liquidity level twice.
    risk_carries: list[OrderRequest] = []  # 本次风控待下次 tick 补平的剩余量 | Exit remainders awaiting the next tick
    # 止损按"发现破位时的市价"成交：模拟盘/实盘只能在收盘后发市价单，真实成交价就是当前价。
    # 若仍按 min(开盘价, 止损价) 记账，价格回升时账本会长期低于交易所余额，只能靠对账抹平。
    # Stops fill at the market price seen on detection: paper/live can only send a market order after
    # the close, so booking min(open, stop) would keep the ledger below the exchange balance and
    # force reconciliation to paper over the gap.
    for risk_order, risk_price in ledger.risk_orders(price, low=risk_bar.low, high=risk_bar.high, stop_fill_at_market=True):  # 遍历强平订单 | Iterate forced exits
        try:  # 单笔风控失败不应中断整个 tick | One failed risk exit must not abort the tick
            # 风控单必须与策略单共用同一套成交价口径：中间价兜底时买入抬到卖一、卖出压到买一。
            # 否则止损会被记成"按中间价卖出"，账本系统性偏乐观，且与同一 tick 的普通订单互相矛盾。
            # Forced exits share the strategy orders' pricing rules: on a mid-based basis buys lift the
            # ask and sells hit the bid. Otherwise stops book at the mid, the ledger drifts optimistic
            # and two orders in the same tick follow contradictory conventions.
            risk_buys = risk_order.side in {"buy", "buycover"}  # 风控单是否为买入方向 | Whether the exit buys
            risk_fill_price = risk_price  # 默认按基准价成交 | Default to the resolved price
            if fill_basis == "mid_fallback" and spread_ratio > 0:  # 仅中间价口径补点差 | Only mid-based fills pay the spread
                risk_fill_price = risk_price * (1 + spread_ratio) if risk_buys else risk_price * (1 - spread_ratio)  # 买加卖减 | Add for buys, subtract for sells
            # 止损单同样受盘口深度约束，会被截断；剩余仓位若静默留在账上就是"止损没止住"，
            # 必须写进 unfilled 让页面可见，模拟盘还要留到下一 tick 继续平。
            # A stop is depth-capped too. Leaving the unfilled remainder silently open means the stop
            # did not actually stop anything, so it is reported and paper mode retries it next tick.
            risk_short = 0.0  # 深度不足导致的未平数量 | Quantity left unfilled by book depth
            if depth is not None and float(risk_order.qty) > 0:  # 有盘口才可能被裁剪 | Only depth-capped when a book exists
                risk_available = float(depth[1] if risk_buys else depth[0])  # 买吃卖一、卖吃买一 | Buys hit the ask, sells the bid
                risk_short = max(0.0, float(risk_order.qty) - max(0.0, risk_available))  # 超出流动性的部分 | Excess over available liquidity
            risk_fill = await executor.execute(risk_order, risk_fill_price, depth)  # 立即撮合强平单（带深度约束）| Fill the forced exit immediately with depth
            if risk_fill is not None:  # 成交则记录 | Record on success
                fills.append(risk_fill)  # 追加成交记录 | Append the fill
            if risk_short > 1e-12:  # 只平掉一部分时必须留痕 | A partial exit must always be visible
                unfilled.append({"side": risk_order.side, "qty": round(risk_short, 8), "reason": "stop_loss"})  # 记录未平数量 | Record the unfilled exit
                logger.warning(f"止损单被盘口深度截断：{risk_order.side} 剩余 {risk_short:.8g} 未平，请关注风险敞口")
                if mode_value == RunMode.PAPER.value:  # 模拟盘留到下一 tick 继续平 | Paper retries on the next tick
                    risk_carries.append(  # 暂存，等本 tick 结束后再入队 | Stash it until this tick finishes
                        OrderRequest(  # 以差额数量构造剩余止损单 | Build the remainder exit
                            side=risk_order.side,  # 方向 | Side
                            qty=risk_short,  # 差额数量 | Shortfall quantity
                            signal_price=risk_order.signal_price,  # 信号价 | Signal price
                            reason=risk_order.reason,  # 原因（stop_loss）| Reason (stop_loss)
                            symbol=risk_order.symbol,  # 交易对 | Symbol
                            client_order_id="",  # 补单不沿用幂等号 | No reused id for retries
                            qty_ratio=0.0,  # 固定数量补单 | Fixed quantity, no re-sizing
                        )
                    )  # 暂存结束 | End of stashing
        except CustomException as exc:  # 捕获业务异常并记录 | Catch business errors and log
            logger.warning(f"风控平仓失败({risk_order.side})：{exc}")  # 记录风控失败 | Log the risk-exit failure
        except Exception as exc:  # noqa: BLE001 - 风控异常不应阻塞策略 | Risk errors must not block the strategy
            logger.warning(f"风控平仓异常({risk_order.side})：{type(exc).__name__}: {exc}")  # 记录风控异常 | Log the risk-exit error

    # 3.5) 盘中风控检查：只跑风控、不跑策略，直接返回
    # 3.5) Intra-bar risk check: risk only, no strategy, return immediately.
    if risk_only:  # 调度器在 1 分钟收盘时调用 | Called by the scheduler on every 1m close
        for pending_carry in risk_carries:  # 心跳只登记剩余量，不开新仓 | Heartbeats only queue remainders, never open positions
            ledger.add_remainder(pending_carry)  # 交回队列，下次完整 tick 继续平 | Hand back for the next full tick
        return TickResult(  # 构造风控结果 | Build the risk-only result
            mode=mode_value,  # 运行模式 | Run mode
            symbol=symbol,  # 原始交易对 | Original symbol
            interval=interval,  # K 线周期 | Candle interval
            strategy=info.name,  # 策略名称 | Strategy name
            price=round(price, 8),  # 成交参考价 | Reference price
            equity=round(ledger.equity_at(price), 6),  # 账户净值 | Account equity
            cash=round(ledger.cash, 6),  # 可用现金 | Available cash
            position={k: round(v, 8) for k, v in ledger.position().items()},  # 持仓快照 | Position snapshot
            fills=[fill.__dict__ for fill in fills],  # 仅可能包含风控成交 | Only risk fills are possible
            logs=[f"[{risk_bar.date}] 盘中风控检查（1m）：{'触发平仓' if fills else '无触发'}"],  # 记录检查结果 | Record the check outcome
            rejected=[],  # 风控路径不产生被拒订单 | No rejections on this path
            unfilled=unfilled,  # 心跳触发的止损若被深度截断也要可见 | Truncated heartbeat stops stay visible
            tick_time=now_iso,  # tick 时间 | Tick time
            signal_bar=risk_bar.date,  # 风控所用 K 线 | Bar used by the risk check
            fill_basis="risk_only",  # 口径标记 | Basis label
            leverage=ledger.leverage,  # 杠杆倍数 | Leverage
            liquidation_price=round(ledger.liquidation_price(), 8),  # 理论强平价 | Liquidation price
        )  # 结果构造结束 | End of result construction

    # 3.8) 补成交：上一次因盘口深度不足被截断的剩余量，在本 tick 的价格上再试一次
    # 3.8) Retry remainders: quantity truncated by the depth cap on a previous tick is retried here
    #      at this tick's price, so paper mode does not permanently under-fill versus the backtest.
    if mode_value == RunMode.PAPER.value and ledger.remainders:  # 仅模拟盘补成交（实盘不能凭空重发市价单）| Paper only: never re-send a live market order blindly
        for carry in ledger.take_remainders():  # 取出并清空待补队列 | Drain the retry queue
            try:  # 单笔失败不影响其余补单 | One failure must not block the rest
                carry_fill = await executor.execute(carry, price, depth)  # 按本次价格重试 | Retry at this tick's price
            except Exception as exc:  # noqa: BLE001 - 补成交异常只记录 | Log retry errors only
                logger.warning(f"补成交失败({carry.side})：{type(exc).__name__}: {exc}")  # 记录补成交异常 | Log the retry error
                ledger.add_remainder(carry)  # 异常时保留，不能把剩余量丢掉 | Keep it: an exception must not drop the remainder
                continue  # 跳过该笔 | Skip this one
            if carry_fill is not None:  # 成交则记入本 tick | Book it when filled
                fills.append(carry_fill)  # 追加成交 | Append the fill
                carry_short = float(carry.qty) - float(carry_fill.qty)  # 仍被深度截断的部分 | What depth still truncated
                if carry_short > 1e-12:  # 部分成交时保留剩余 | Keep the rest on partial fills
                    ledger.add_remainder(_replace_qty(carry, carry_short))  # 重新入队，下个 tick 再来 | Requeue for the next tick
            else:  # 仍然无法成交：放弃，避免无限重试 | Still unfillable: drop it instead of retrying forever
                unfilled.append({"side": carry.side, "qty": round(float(carry.qty), 8), "reason": "depth_retry_failed"})  # 记录放弃原因 | Record why it was dropped
    for pending_carry in risk_carries:  # 本次风控的剩余量在补成交之后再入队 | Queue this tick's exit remainders after the retry
        ledger.add_remainder(pending_carry)  # 下一个 tick 继续平 | Continue exiting next tick

    # 4) 构造统一策略接口（绑定"已收盘"窗口与当前成交价）
    # 4) Build the unified strategy API bound to the closed window and the fill price.
    pending: list[OrderRequest] = []  # 初始化待撮合订单队列 | Init queue of pending orders

    def submit(order: OrderRequest) -> bool:  # 策略下单入口 | Strategy order entry point
        """策略下单入口：排队，tick 末按统一成交价撮合 | Strategy order entry: queue order, fill at tick end at the resolved price.

        参数 | Parameters:
        - order (OrderRequest): 下单请求 | The order request.

        返回 | Returns:
        - bool: 始终返回 True 表示受理入队 | Always True to signal accepted/enqueued.
        """
        pending.append(order)  # 将订单加入待撮合队列 | Append order to pending queue
        return True  # 返回受理成功 | Return accepted

    api = StrategyAPI(  # 构造统一策略接口 | Build the unified strategy API
        ledger,  # 传入账户账本 | Pass account ledger
        submit=submit,  # 注入下单回调 | Inject submission callback
        candles=signal_window,  # 传入"已收盘"的信号窗口 | Pass the closed signal window
        index=len(signal_window) - 1,  # 当前下标为最后一根已收盘 K 线 | Index the last closed bar
        timestamp=now_iso,  # 传入时间标签 | Pass timestamp
        allow_short=allow_short,  # 传入是否允许做空 | Pass shorting flag
        allow_add=bool(getattr(info.cls, "allow_position_add", False)),  # 策略是否允许重复加仓 | Whether the strategy allows pyramiding
        bar_ts=signal_window[-1].date,  # 信号 bar 时间戳，用于幂等订单号 | Signal bar timestamp for idempotent ids
    )
    api.symbol = resolved_symbol  # 将规范化交易对写入接口 | Write normalized symbol to API
    api.set_live_price(price)  # 注入成交价供下单与净值使用 | Inject the fill price for ordering and equity

    # 5) 调用策略（与回测相同的两种模式）
    # 5) Call the strategy (same two modes as backtest)
    index = len(signal_window) - 1  # 当前信号 K 线下标 | Current signal bar index
    warmup = max(0, int(getattr(info.cls, "startup_candle_count", 0) or 0))  # 策略预热所需 K 线数 | Warmup candles required by strategy
    repeated_bar = bool(last_signal_bar) and signal_window[-1].date == last_signal_bar  # 判断是否重复处理同一根 K 线 | Detect a repeated bar
    try:  # 捕获用户策略异常并统一包装 | Catch user strategy errors and wrap uniformly
        if stale_bar:  # 行情未刷新到下一根，本轮不交易 | The window has not rolled to the next bar, skip trading
            api.log(f"行情尚未刷新到下一根（末根 {signal_window[-1].date} 已收盘），本轮跳过信号计算")  # 记录跳过原因 | Log the skip reason
        elif repeated_bar:  # 同一根 K 线已处理过，跳过信号，避免陈旧信号二次开仓 | Skip a bar already processed
            api.log(f"K 线 {signal_window[-1].date} 已处理过，跳过信号计算")  # 记录跳过原因 | Log the skip reason
        elif index >= warmup:  # 仅在过了预热期后运行策略 | Run strategy only after warmup period
            if info.mode == "event":  # 事件驱动模式分支 | Event-driven mode branch
                strategy.on_bar(api, signal_window, index)  # 调用策略 on_bar 回调 | Call strategy on_bar callback
            else:  # 信号驱动模式分支 | Signal-driven mode branch
                metadata = {"pair": symbol, "interval": interval}  # 构造策略元数据 | Build strategy metadata
                entries = strategy.populate_entry_trend(signal_window, metadata) or []  # 计算入场信号序列 | Compute entry signals
                exits = strategy.populate_exit_trend(signal_window, metadata) or []  # 计算出场信号序列 | Compute exit signals
                exit_signal = _last_signal(exits, index)  # 读取当前出场信号 | Read current exit signal
                entry_signal = _last_signal(entries, index)  # 读取当前入场信号 | Read current entry signal
                if exit_signal == 1:  # 出场信号为 1：平多 | Exit signal 1: close long
                    api.sell(reason="exit_long")  # 调用卖出平多 | Call sell to close long
                elif exit_signal == -1:  # 出场信号为 -1：平空 | Exit signal -1: cover short
                    api.buycover(reason="exit_short")  # 调用买入平空 | Call buycover to cover short
                if entry_signal == 1:  # 入场信号为 1：开多 | Entry signal 1: open long
                    api.buy(reason="entry_long")  # 调用买入开多 | Call buy to open long
                elif entry_signal == -1:  # 入场信号为 -1：开空 | Entry signal -1: open short
                    api.sellshort(reason="entry_short")  # 调用卖出开空 | Call sellshort to open short
    except CustomException:  # 自定义异常原样上抛，保留上层语义 | Re-raise CustomException as-is
        raise  # 直接上抛当前异常 | Re-raise current exception
    except Exception as exc:  # noqa: BLE001 - 用户策略异常需要回传展示  # 捕获其他异常 | Catch other exceptions
        raise CustomException(msg=f"策略执行失败：{type(exc).__name__}: {exc}") from exc  # 包装为自定义异常上抛 | Wrap and re-raise as CustomException

    # 6) 撮合排队的订单（统一按解析出的成交价，与回测同一口径）
    # 6) Fill queued orders at the resolved price (same basis as the backtest).
    for order in pending:  # 遍历待撮合队列 | Iterate over pending orders
        try:  # 单笔订单失败不影响其余订单 | One order failure must not block others
            is_buy_side = order.side in {"buy", "buycover"}  # 是否为买入方向 | Whether the side buys
            # 中间价不是可成交价：买入要吃到卖一、卖出要打到买一，用半价差把点差补回来。
            # 只有 mid_fallback 才需要，来自真实开盘价时再叠加就是重复计费。
            # A mid price is not tradeable: buys lift the ask and sells hit the bid, so the half-spread
            # is applied — only for mid_fallback, since an actual open price already is a traded price.
            order_price = price  # 默认按基准价成交 | Default to the resolved price
            if fill_basis == "mid_fallback" and spread_ratio > 0:  # 仅在中间价口径下补点差 | Only for mid-based fills
                order_price = price * (1 + spread_ratio) if is_buy_side else price * (1 - spread_ratio)  # 买加卖减半价差 | Add for buys, subtract for sells
            fill_qty = float(order.qty)  # 默认沿用预估数量 | Default to the estimated quantity
            # 按比例开仓必须在成交价上重算：与回测 execute_pending 完全同一条公式，
            # 否则信号价与成交价的差额会让实际占用资金偏离设定比例。
            # Ratio-sized entries re-size at the fill price with the very same formula the backtest
            # uses in execute_pending; otherwise the signal-to-fill price gap shifts the real exposure.
            if order.qty_ratio > 0 and order_price > 0:  # 仅比例单需要重算 | Only ratio-sized orders need re-sizing
                fill_qty = sizing_qty(  # 成交价口径的数量 | Quantity at the fill price
                    ledger.entry_budget(order_price, side=order.side),  # 该方向的购买力 | Buying power for the side
                    order.qty_ratio,  # 开仓比例 | Entry ratio
                    order_price,  # 成交价 | Fill price
                    ledger.fee_rate,  # 手续费率 | Fee rate
                    ledger.slippage,  # 滑点比例 | Slippage ratio
                )  # 数量换算结束 | End of sizing
            sized = _replace_qty(order, fill_qty)  # 用成交价口径数量构造订单 | Build the order at the fill quantity
            # 交易所最小名义额：低于下限的订单在实盘会被直接拒绝，模拟盘若不校验就会成交
            # "实盘永远做不到"的碎单，让两种模式的结果失去可比性。平仓单豁免，否则小仓位平不掉。
            # Venue minimum notional: the venue rejects such orders outright, so paper mode must too,
            # otherwise it fills dust the live account never could. Exits are exempt so a small
            # position can still be closed.
            if min_notional > 0 and order.side in {"buy", "sellshort"} and fill_qty > 0 and fill_qty * order_price < min_notional:  # 仅校验开仓单 | Only entries are gated
                unfilled.append({"side": order.side, "qty": round(fill_qty, 8), "reason": "min_notional"})  # 记录未成交原因 | Record the rejection reason
                api.log(f"订单名义额 {fill_qty * order_price:.8g} 低于交易所最小额 {min_notional}，已取消：{order.side}")  # 写入策略日志 | Log the cancellation
                continue  # 跳过该单 | Skip this order
            # 深度差额先算出来：执行器会把这部分裁掉且不回报，只有在这里记录才能看见"少成交了多少"
            # Compute the depth shortfall first: the executor silently truncates it, so this is the only
            # place where "how much did not fill" can be observed.
            depth_short = 0.0  # 深度不足的差额 | Shortfall caused by depth
            if depth is not None and fill_qty > 0:  # 有盘口深度才可能被裁剪 | Only depth-capped when a book exists
                available = float(depth[1] if is_buy_side else depth[0])  # 买吃卖一、卖吃买一 | Buys hit the ask, sells the bid
                depth_short = max(0.0, fill_qty - max(0.0, available))  # 超出可得流动性的部分 | Excess over available liquidity
            fill = await executor.execute(sized, order_price, depth)  # 调用执行器异步撮合该订单（带深度约束）| Await executor to fill this order with depth
            if fill is not None:  # 成交成功时记录 | Record when fill succeeded
                fills.append(fill)  # 追加成交记录 | Append fill record
            if depth_short > 1e-12:  # 存在深度差额时记账 | Book the shortfall when present
                unfilled.append({  # 记录未成交明细 | Record the unfilled detail
                    "side": order.side,  # 方向 | Side
                    "qty": round(depth_short, 8),  # 未成交数量 | Unfilled quantity
                    "reason": "depth",  # 原因：盘口深度不足 | Reason: insufficient book depth
                })  # 明细结束 | End of the detail
                # 模拟盘把剩余量留到下一次 tick 重试；实盘不能凭空补发市价单，只记录不重试
                # Paper keeps the remainder for the next tick; live never re-sends a market order, so
                # it is only recorded.
                if mode_value == RunMode.PAPER.value:  # 仅模拟盘补成交 | Paper only
                    ledger.add_remainder(  # 登记待补订单 | Register the remainder
                        OrderRequest(  # 以差额数量构造剩余订单 | Build the remainder order
                            side=order.side,  # 方向 | Side
                            qty=depth_short,  # 差额数量 | Shortfall quantity
                            signal_price=order.signal_price,  # 信号价 | Signal price
                            reason=order.reason,  # 原因 | Reason
                            symbol=order.symbol,  # 交易对 | Symbol
                            client_order_id="",  # 补单不沿用幂等号，避免交易所判定重复 | No reused id for retries
                            qty_ratio=0.0,  # 差额按固定数量补，不再按比例重算 | Fixed quantity, no re-sizing
                        )
                    )  # 登记结束 | End of registration
        except CustomException as exc:  # 捕获自定义异常并记录日志 | Catch CustomException and log
            api.log(f"订单执行失败({order.side})：{exc}")  # 记录执行失败日志 | Log execution-failure message
        except Exception as exc:  # noqa: BLE001  # 捕获其他异常并记录日志 | Catch other exceptions and log
            api.log(f"订单执行异常({order.side})：{type(exc).__name__}: {exc}")  # 记录执行异常日志 | Log execution-error message

    return TickResult(  # 构造并返回本次 tick 结果 | Build and return the tick result
        mode=mode_value,  # 填充运行模式 | Fill run mode
        symbol=symbol,  # 填充原始交易对 | Fill original symbol
        interval=interval,  # 填充 K 线周期 | Fill candle interval
        strategy=info.name,  # 填充策略名称 | Fill strategy name
        price=round(price, 8),  # 填充保留 8 位小数的成交价 | Fill price rounded to 8 decimals
        equity=round(ledger.equity_at(price), 6),  # 填充保留 6 位小数的净值 | Fill equity rounded to 6 decimals
        cash=round(ledger.cash, 6),  # 填充保留 6 位小数的现金 | Fill cash rounded to 6 decimals
        position={k: round(v, 8) for k, v in ledger.position().items()},  # 将持仓各字段保留 8 位小数 | Round position values to 8 decimals
        fills=[fill.__dict__ for fill in fills],  # 将成交对象转为字典列表 | Convert fill objects to dict list
        logs=api.logs,  # 填充策略日志 | Fill strategy logs
        rejected=api.rejected_orders,  # 填充被拒订单列表 | Fill rejected orders
        tick_time=now_iso,  # 填充 tick 时间标签 | Fill tick timestamp
        signal_bar=signal_window[-1].date,  # 填充信号 bar 时间 | Fill the signal bar timestamp
        fill_basis="stale" if stale_bar else fill_basis,  # 填充成交价口径；陈旧行情标记为 stale | Fill-price basis, marked stale when skipped
        leverage=ledger.leverage,  # 本次运行生效的杠杆 | Effective leverage
        liquidation_price=round(ledger.liquidation_price(), 8),  # 当前持仓的理论强平价 | Liquidation price of the current position
        unfilled=unfilled,  # 深度不足未成交明细 | Unfilled-by-depth details
    )


class PaperExecutor:  # 模拟盘执行器 | Paper trading executor
    """模拟盘执行器：按最新价在本地账本即时虚拟撮合 | Paper executor: fill virtually on local ledger at latest price.

    目的：在不触碰真实资金的前提下用本地账本验证策略，撮合逻辑复用 AccountLedger。
    Purpose: Validate strategies with a local ledger without real funds, reusing AccountLedger.

    关键属性 | Key attributes:
    - ledger: 用于虚拟撮合的账户账本 | Ledger used for virtual fills.
    - fee_rate: 手续费率（传给账本）| Fee rate passed to ledger.
    """

    def __init__(self, ledger: AccountLedger, fee_rate: float = 0.001) -> None:  # 初始化模拟执行器 | Initialize paper executor
        """初始化模拟执行器 | Initialize the paper executor.

        参数 | Parameters:
        - ledger (AccountLedger): 账户账本 | Account ledger.
        - fee_rate (float): 手续费率 | Fee rate.

        返回 | Returns:
        - None: 构造器不返回显式值 | Constructor returns no explicit value.
        """
        self.ledger = ledger  # 保存账本引用 | Store ledger reference
        self.fee_rate = fee_rate  # 保存手续费率 | Store fee rate

    async def execute(self, order: OrderRequest, price: float, depth: tuple[float, float] | None = None) -> Fill | None:  # 本地虚拟撮合 | Fill virtually on local ledger
        """本地虚拟撮合 | Fill virtually on the local ledger.

        参数 | Parameters:
        - order (OrderRequest): 下单请求 | The order request.
        - price (float): 最新价 | Latest price.
        - depth (tuple[float, float] | None): (买一量, 卖一量)，用于给成交量设上限
          | (bid size, ask size) used to cap the fill size.

        返回 | Returns:
        - Fill | None: 成交记录或未成交 | Fill record or None.
        """
        qty = _cap_by_depth(order, depth)  # 按盘口深度裁剪数量 | Cap the quantity by book depth
        if qty is None:  # 深度不足，视为无法成交 | Not enough depth: treat as unfilled
            return None  # 返回未成交 | Return no fill
        capped = _replace_qty(order, qty)  # 构造裁剪后的订单请求 | Build the capped order request
        return self.ledger.execute(capped, price, timestamp=datetime.now(UTC).isoformat())  # 委托账本按当前时间撮合 | Delegate to ledger with current timestamp

    def snapshot(self) -> dict[str, Any]:  # 返回账本快照 | Return ledger snapshot
        """返回账本快照 | Return the ledger snapshot.

        返回 | Returns:
        - dict[str, Any]: 账本快照字典 | Ledger snapshot dict.
        """
        return self.ledger.snapshot()  # 委托账本导出快照 | Delegate to ledger snapshot


class LiveExecutor:  # 实盘执行器 | Live trading executor
    """实盘执行器：按最新价在币安下单，成交后记账 | Live executor: place Binance orders at latest price, then book fills.

    ``dry_run=True``（默认）时不下真实订单，仅用本地账本模拟撮合并标注，
    便于先用真实行情验证策略而不动用资金。
    With ``dry_run=True`` (default) no real order is placed; only local virtual fills
    are used so strategies can be validated on real data without risking funds.

    关键属性 | Key attributes:
    - ledger: 账户账本 | Account ledger.
    - dry_run: 是否模拟运行 | Whether running in dry-run mode.
    - _client: 非 dry_run 时的币安下单客户端 | Binance order client when not dry-run.
    """

    def __init__(  # 初始化实盘执行器 | Initialize live executor
        self,  # 实例自身引用 | Instance reference
        ledger: AccountLedger,  # 账户账本 | Account ledger
        *,  # 强制后续参数为关键字参数 | Force following params to be keyword-only
        api_key: str = "",  # 解密后的币安 API Key | Decrypted Binance API key
        api_secret: str = "",  # 解密后的币安 API Secret | Decrypted Binance API secret
        dry_run: bool = True,  # 是否模拟运行，默认 True | Whether to dry-run, default True
    ) -> None:  # 构造器无返回值 | Constructor returns None
        """初始化实盘执行器 | Initialize the live executor.

        参数 | Parameters:
        - ledger (AccountLedger): 账户账本 | Account ledger.
        - api_key (str): 解密后的币安密钥 | Decrypted Binance key.
        - api_secret (str): 解密后的币安密钥 | Decrypted Binance secret.
        - dry_run (bool): 是否模拟运行（True 不下真实单）| Whether dry-run (no real orders).

        返回 | Returns:
        - None: 构造器不返回显式值 | Constructor returns no explicit value.
        """
        self.ledger = ledger  # 保存账本引用 | Store ledger reference
        self.dry_run = dry_run  # 保存 dry_run 开关 | Store dry-run flag
        self._client = None  # 初始化下单客户端为 None | Init order client to None
        self._last_sync = 0.0  # 上次与交易所对账的单调时刻（节流用）| Last reconciliation time for throttling
        if not dry_run:  # 仅非 dry_run 时创建真实下单客户端 | Create real client only when not dry-run
            from .exchange import BinanceOrderClient  # 延迟导入下单客户端 | Lazily import order client

            self._client = BinanceOrderClient(api_key, api_secret)  # 用密钥构造下单客户端 | Build order client with credentials

    async def execute(self, order: OrderRequest, price: float, depth: tuple[float, float] | None = None) -> Fill | None:  # 执行订单 | Execute an order
        """执行订单：dry_run 走本地撮合，真实模式先下单再记账 | Execute order: dry-run fills locally, real mode orders then books.

        参数 | Parameters:
        - order (OrderRequest): 下单请求 | The order request.
        - price (float): 最新价/回落价 | Latest/fallback price.
        - depth (tuple[float, float] | None): (买一量, 卖一量) 盘口深度 | (bid size, ask size) book depth.

        返回 | Returns:
        - Fill | None: 成交记录或未成交 | Fill record or None.

        流程 | Flow:
        现货禁空校验 → 深度裁剪 → 交易所过滤器规整/校验 → 下单并回查确认 →
        用真实成交量/均价/手续费记账；dry_run 直接本地撮合。
        Spot-short guard → depth cap → exchange filter normalization/validation →
        place and confirm → book the true quantity/price/fee; dry-run fills locally.
        """
        # 现货不支持做空：即便账本允许，实盘真实下单也必须拒绝，避免误操作
        # Spot has no shorting: even if ledger allows, real live orders must reject to avoid mistakes.
        if not self.dry_run and order.side in {"sellshort", "buycover"}:  # 真实模式下拒绝做空/平空 | Reject short/cover in real mode
            raise CustomException(msg="币安现货不支持做空，请将策略改为仅做多，或使用模拟盘验证做空逻辑")  # 抛出不支持做空异常 | Raise shorting-unsupported error

        if not self.dry_run and self._client is not None:  # 真实模式且客户端就绪时走真实下单 | Go real when not dry-run and client ready
            symbol = order.symbol.replace("/", "").upper()  # 规范化交易对 | Normalize symbol
            capped_qty = _cap_by_depth(order, depth)  # 先按盘口深度裁剪，避免市价单扫穿挂单 | Cap by depth so a market order cannot sweep the book
            if capped_qty is None:  # 深度为 0 表示当前不可成交 | Zero depth means nothing is fillable now
                raise CustomException(msg="盘口深度不足，已跳过下单")  # 抛出深度不足异常 | Raise a depth error
            qty = self._client.normalize_qty(symbol, capped_qty, market=True)  # 按 MARKET_LOT_SIZE 规整数量 | Normalize against MARKET_LOT_SIZE
            accepted, reason = self._client.check_order(symbol, qty, price)  # 本地预校验过滤器 | Pre-validate against exchange filters
            if not accepted:  # 不满足交易所规则 | Exchange rules not satisfied
                raise CustomException(msg=reason or "订单未通过交易所过滤器校验")  # 抛出校验失败异常 | Raise a validation error
            result = await asyncio.to_thread(  # 将同步下单放到线程中执行 | Run synchronous order placement in a thread
                self._client.place_market_order_verified,  # 下单并回查确认成交 | Place and confirm the fill
                symbol,  # 交易对参数 | Symbol argument
                order.side,  # 方向参数 | Side argument
                qty,  # 规整后的数量 | Normalized quantity
                client_order_id=order.client_order_id,  # 自定义订单号（幂等）| Custom order id for idempotency
            )
            quote_asset = str(self._client.symbol_info(symbol).get("quote_asset") or "")  # 计价资产（识别手续费币种）| Quote asset for fee currency detection
            summary = _summarize_result(result, qty, price, quote_asset)  # 解析真实成交量/均价/手续费 | Parse true qty/price/fee
            fee_override: float | None = summary["fee"] if summary["fee_known"] else None  # 手续费可信时回填真实值 | Backfill the real fee when trustworthy
            if fee_override is None and result.get("orderId") is not None:  # 下单响应缺手续费时用成交流水补齐 | Fill in from the trade list when the response lacks fees
                fee_override = await self._fee_from_trades(symbol, result.get("orderId"), quote_asset)  # 查询成交明细手续费 | Query commission from the trade list
            executed_price = summary["price"] or price  # 真实成交均价，缺失回落参考价 | True average price, falling back to the reference
            executed_qty = summary["qty"] or qty  # 真实成交量，缺失回落请求量 | True quantity, falling back to the request
            real_order = OrderRequest(  # 用实际成交结果构造真实订单请求 | Build real order request from the execution result
                side=order.side,  # 填充方向 | Fill side
                qty=executed_qty,  # 填充实际成交量 | Fill executed qty
                signal_price=order.signal_price,  # 填充信号价格 | Fill signal price
                reason=order.reason,  # 填充下单原因 | Fill reason
                symbol=symbol,  # 填充规范化交易对 | Fill normalized symbol
                client_order_id=order.client_order_id,  # 保留幂等订单号 | Keep the idempotent order id
            )
            # 真实成交价已包含市场滑点，记账时不再叠加配置滑点，避免双重计算
            # Real fills already include market slippage, so do not add the configured slippage again.
            return self.ledger.execute(  # 按实际成交价记账并返回 | Book at the executed price and return
                real_order,  # 真实订单 | Real order
                executed_price,  # 真实成交均价 | Real average fill price
                timestamp=datetime.now(UTC).isoformat(),  # 成交时间 | Fill timestamp
                apply_slippage=False,  # 不再叠加滑点 | Do not double-count slippage
                fee_override=fee_override,  # 回填交易所真实手续费 | Backfill the real exchange fee
            )

        capped = _cap_by_depth(order, depth)  # dry_run 同样受深度约束 | Dry-run is depth-capped too
        if capped is None:  # 深度不足视为未成交 | Insufficient depth means no fill
            return None  # 返回未成交 | Return no fill
        return self.ledger.execute(_replace_qty(order, capped), price, timestamp=datetime.now(UTC).isoformat())  # dry_run/无客户端时本地虚拟撮合 | Fill locally in dry-run or no-client case

    async def _fee_from_trades(self, symbol: str, order_id: Any, quote_asset: str) -> float | None:  # 从成交流水取手续费 | Read the fee from the trade list
        """按订单号查询成交明细并汇总手续费 | Query the trade list by order id and sum the commission.

        目的 | Purpose:
            下单响应的 ``fills`` 并非总会返回；缺失时若不回填，账本的 ``total_fee``
            会长期低于交易所实际扣费。
            The placement response does not always include ``fills``; without a backfill the
            ledger's ``total_fee`` would stay below the fee actually charged.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.
            - order_id (Any): 币安订单号 | Binance order id.
            - quote_asset (str): 计价资产 | Quote asset.

        返回 | Returns:
            - float | None: 汇总手续费；查询失败返回 None | Total commission, or None on failure.
        """
        if self._client is None or order_id in (None, ""):  # 无客户端或订单号时无法查询 | Cannot query without a client or an id
            return None  # 返回 None | Return None
        try:  # 查询失败不应影响记账 | A failed query must not block booking
            trades = await asyncio.to_thread(self._client.my_trades, symbol, order_id=order_id, limit=100)  # 查询该订单成交 | Query the order's fills
        except CustomException as exc:  # 网络/权限异常 | Network or permission error
            logger.warning(f"手续费回填失败({symbol} {order_id})：{exc}")  # 记录告警 | Log a warning
            return None  # 返回 None 表示未知 | Return None meaning unknown
        fee = sum_trade_commission(trades, quote_asset)  # 汇总计价币手续费 | Sum quote-denominated fees
        return fee if fee > 0 else None  # 无手续费时返回 None 让调用方按费率估算 | Fall back to the rate when nothing is found

    def sync_from_exchange(  # 与交易所真实状态对账 | Reconcile with the real exchange state
        self,
        symbol: str,  # 交易对，如 BTCUSDT | Symbol, e.g. BTCUSDT
        price: float = 0.0,  # 最新价，用于缺少成本时的兜底 | Latest price used when cost is unknown
        *,
        open_order_ttl: float = 60.0,  # 挂单最大存活秒数，超过则撤销 | Max open-order age before cancelling
        trade_limit: int = 1000,  # 复算用的成交明细条数 | Fills pulled for the replay
        min_interval: float = 0.0,  # 两次对账的最小间隔秒数（节流）| Minimum seconds between reconciliations
        force: bool = False,  # 忽略节流强制执行 | Ignore the throttle
    ) -> dict[str, Any]:  # 返回结构化对账摘要 | Return a structured reconciliation summary
        """用交易所真实余额、成交明细与挂单校正本地账本 | Correct the ledger from real balances, fills and open orders.

        目的 | Purpose:
            原实现只覆盖"现金 + 多头"，且完全不处理手续费、已实现盈亏与挂单：
            - 空头残留（现货本不该有）会让净值凭空变化；
            - 手续费按配置费率估算，与 VIP 等级/BNB 抵扣长期偏离；
            - 崩溃或回查超时留下的孤儿挂单占用资金，之后还可能成交；
            - 已实现盈亏不校正则绩效统计永久失真。
            本方法把这四类问题一次性对齐，并把"改了什么、依据是什么"返回给调用方。
            The previous version handled cash and the long position only, ignoring fees,
            realized PnL and open orders: leftover shorts distorted equity, estimated fees
            drifted from the real VIP/BNB-charged amounts, orphan orders locked capital and
            could still fill, and an uncorrected realized PnL skewed performance forever.
            This method aligns all four and reports what changed and why.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.
            - price (float): 最新价，用于成本与净值兜底 | Latest price for cost/equity fallbacks.
            - open_order_ttl (float): 挂单存活上限（秒），超过即撤单 | Open-order age limit in seconds.
            - trade_limit (int): 拉取成交明细条数 | Number of fills to pull.
            - min_interval (float): 对账节流间隔（秒）| Throttle interval in seconds.
            - force (bool): 忽略节流 | Bypass the throttle.

        返回 | Returns:
            - dict[str, Any]: 含 changed/adjustments/replay/cancelled | Summary with those sections.

        流程 | Flow:
            节流判断 → 读资产与余额 → 拉成交明细复算手续费/盈亏 → 覆盖账本 →
            撤销超期挂单 → 返回摘要；任何网络/权限失败都只告警不中断交易。
            Throttle → read assets/balances → replay fills for fees/PnL → overwrite the ledger →
            cancel stale orders → return the summary; network/permission failures only warn.
        """
        if self.dry_run or self._client is None:  # 模拟盘无需对账 | Paper mode needs no reconciliation
            return {}  # 直接返回空摘要 | Return an empty summary
        resolved = str(symbol or "").replace("/", "").replace("_", "").upper()  # 规范化交易对 | Normalize the symbol
        if not resolved:  # 没有交易对信息时跳过 | Skip when no symbol is known
            return {}  # 返回空摘要 | Return an empty summary
        now = time.monotonic()  # 当前单调时间 | Current monotonic time
        if not force and min_interval > 0 and now - self._last_sync < min_interval:  # 节流：间隔未到则跳过 | Throttle: skip when the interval has not elapsed
            return {}  # 返回空摘要 | Return an empty summary
        self._last_sync = now  # 记录本次对账时刻 | Record the attempt
        try:  # 对账失败不应中断交易，只记录告警 | Reconciliation failure must not stop trading
            info = self._client.symbol_info(resolved)  # 查询基础/计价资产 | Fetch base/quote assets
            balances = self._client.balances()  # 查询真实余额 | Fetch real balances
        except CustomException as exc:  # 网络或鉴权失败 | Network or auth failure
            logger.warning(f"实盘对账失败({resolved})：{exc}")  # 记录告警 | Log a warning
            return {}  # 返回空摘要 | Return an empty summary
        base_asset = str(info.get("base_asset") or "")  # 基础资产名 | Base asset name
        quote_asset = str(info.get("quote_asset") or "")  # 计价资产名 | Quote asset name

        # 1) 成交明细复算：手续费与已实现盈亏以交易所为准
        # 1) Replay the trade list: fees and realized PnL follow the exchange.
        replay: dict[str, Any] = {}  # 复算结果 | Replay outcome
        truncated = False  # 成交明细是否被条数上限截断 | Whether the trade list hit the row cap
        try:  # 权限不足或接口异常时退化为只校正余额 | Degrade to balance-only when permissions fail
            trades = self._client.my_trades(resolved, limit=max(1, int(trade_limit)))  # 拉取成交明细 | Pull the fills
            truncated = len(trades) >= max(1, int(trade_limit))  # 达到上限说明更早的成交未被返回 | Hitting the cap means older fills are missing
            replay = replay_trades(trades, quote_asset)  # 复算持仓/手续费/盈亏 | Replay position/fees/PnL
        except CustomException as exc:  # 网络或权限异常 | Network or permission error
            logger.warning(f"成交明细复算失败({resolved})：{exc}")  # 记录告警 | Log a warning
        # 只有"窗口完整 + 手续费全部以计价币计 + 未被条数截断"时才用复算值校正，
        # 否则宁可不动，也不要把一个偏小的手续费写进账本。
        # Only correct from the replay when the window is complete, every fee is quote-denominated
        # and the list was not truncated; otherwise leave the ledger alone rather than write a
        # too-small fee total into it.
        trustworthy = bool(replay) and bool(replay.get("complete")) and bool(replay.get("fees_complete")) and not truncated  # 复算是否可信 | Whether the replay is trustworthy
        real_fee = float(replay.get("total_fee") or 0.0) if trustworthy else None  # 可信时才校正手续费 | Correct fees only when trustworthy
        real_pnl = float(replay.get("realized_pnl") or 0.0) if trustworthy else None  # 可信时才校正盈亏 | Correct PnL only when trustworthy
        if replay and not trustworthy:  # 不可信时记录原因，便于排查 | Log why the replay was rejected
            logger.warning(f"成交明细不足以校正盈亏({resolved})：complete={replay.get('complete')} fees_complete={replay.get('fees_complete')} truncated={truncated}")  # 记录原因 | Log the reason

        # 2) 余额校正：现金、多头；现货没有空头，账本里若有残留必须清零
        # 2) Balance correction: cash and long; spot has no shorts, so any leftover must be cleared.
        cash = float(balances.get(quote_asset) or 0.0) if quote_asset in balances else None  # 计价资产余额 | Quote balance
        long_qty = float(balances.get(base_asset) or 0.0) if base_asset in balances else None  # 基础资产余额 | Base balance
        adjustments = self.ledger.reconcile(  # 一次性写入账本 | Apply to the ledger in one pass
            cash=cash,  # 真实现金 | Real cash
            long_qty=long_qty,  # 真实多头 | Real long position
            short_qty=0.0,  # 现货空头恒为 0 | Spot shorts are always 0
            total_fee=real_fee,  # 复算手续费 | Replayed fee
            realized_pnl=real_pnl,  # 复算盈亏 | Replayed PnL
            price=price,  # 最新价兜底成本 | Latest price for cost fallback
        )

        # 3) 挂单清理：市价单不应留下挂单，超期的都是孤儿单，撤销避免之后成交
        # 3) Open-order cleanup: market orders should not leave any; stale ones are orphans that may still fill.
        cancelled = 0  # 撤销计数 | Cancelled counter
        try:  # 挂单查询/撤销失败只告警 | Query/cancel failures only warn
            open_orders = self._client.open_orders(resolved)  # 查询当前挂单 | List open orders
            for order in stale_open_orders(open_orders, open_order_ttl):  # 只处理超期挂单 | Only handle stale ones
                try:  # 单笔撤单失败不影响其余 | One cancel failure must not block the rest
                    self._client.cancel_order(resolved, order_id=order.get("orderId"))  # 撤单 | Cancel it
                    cancelled += 1  # 计数 | Count
                    logger.warning(f"撤销超期挂单({resolved})：orderId={order.get('orderId')}")  # 记录撤销 | Log the cancellation
                except CustomException as exc:  # 可能已成交或已撤销 | May already be filled or cancelled
                    logger.warning(f"撤单失败({resolved} {order.get('orderId')})：{exc}")  # 记录告警 | Log a warning
        except CustomException as exc:  # 挂单接口不可用 | The open-orders endpoint is unavailable
            logger.warning(f"挂单查询失败({resolved})：{exc}")  # 记录告警 | Log a warning

        summary = reconcile_summary(adjustments, replay, cancelled)  # 合并摘要 | Merge the summary
        if summary["changed"]:  # 有变化时输出一条可审计日志 | Log one auditable line when something changed
            logger.info(f"实盘对账({resolved})：{summary}")  # 记录摘要 | Log the summary
        return summary  # 返回摘要 | Return the summary

    def snapshot(self) -> dict[str, Any]:  # 返回账本快照 | Return ledger snapshot
        """返回账本快照 | Return the ledger snapshot.

        返回 | Returns:
        - dict[str, Any]: 账本快照字典 | Ledger snapshot dict.
        """
        return self.ledger.snapshot()  # 委托账本导出快照 | Delegate to ledger snapshot


def _cap_by_depth(order: OrderRequest, depth: tuple[float, float] | None) -> float | None:  # 按盘口深度裁剪成交量 | Cap the fill quantity by book depth
    """按 L1 盘口深度给成交量设上限 | Cap the fill quantity by the L1 book depth.

    目的 | Purpose:
        模拟盘此前按单一价格"无限量"成交，规模一样本下等于假设自己永远没有市场冲击；
        实盘下不设上限则一张大市价单会直接扫穿多档挂单。用 bookTicker 的买一/卖一量
        做上限后，超出部分视为"本次未成交"，与真实撮合的可得流动性一致。
        Paper trading used to fill unlimited size at one price, effectively assuming zero
        market impact; live orders without a cap sweep several levels. Capping by the best
        bid/ask size treats the excess as unfilled, matching the liquidity actually available.

    参数 | Parameters:
        order (OrderRequest): 下单请求 | The order request.
        depth (tuple[float, float] | None): (买一量, 卖一量)，None 表示无约束 | (bid size, ask size), None means unconstrained.

    返回 | Returns:
        float | None: 可成交数量；深度为 0 时返回 None（本次不可成交）| Fillable quantity, None when depth is zero.

    流程 | Flow:
        无深度 → 原量；否则买用卖一量、卖用买一量取较小值。
        No depth → the requested quantity; otherwise take the min with the matching side's size.
    """
    qty = float(order.qty)  # 读取请求数量 | Read the requested quantity
    if depth is None:  # 无深度约束 | No depth constraint
        return qty  # 原样返回 | Return as requested
    bid_qty, ask_qty = float(depth[0]), float(depth[1])  # 拆解双边深度 | Unpack both sides
    available = ask_qty if order.side in {"buy", "buycover"} else bid_qty  # 买吃卖一、卖吃买一 | Buys hit the ask, sells hit the bid
    if available <= 0:  # 对应一侧无量 | The matching side is empty
        return None  # 本次不可成交 | Nothing fillable now
    return min(qty, available)  # 取请求量与可得量的较小值 | Take the smaller of the request and the available size


def _replace_qty(order: OrderRequest, qty: float) -> OrderRequest:  # 用新数量构造订单副本 | Build an order copy with a new quantity
    """返回数量被替换后的订单请求（数量未变则原样返回）| Return the order with a replaced quantity (same object when unchanged).

    目的 | Purpose:
        深度裁剪后需要把新数量交给账本，但 ``OrderRequest`` 是 dataclass，
        直接改字段会污染调用方持有的对象；未变化时返回原对象以避免多余分配。
        Depth capping must hand the new quantity to the ledger, but mutating the dataclass
        would corrupt the caller's object; the original is returned unchanged when equal.

    参数 | Parameters:
        order (OrderRequest): 原订单 | The original order.
        qty (float): 新数量 | The new quantity.

    返回 | Returns:
        OrderRequest: 订单（原对象或副本）| The order (original or a copy).
    """
    if abs(float(qty) - float(order.qty)) <= 1e-12:  # 数量未变 | Quantity unchanged
        return order  # 返回原对象 | Return the original
    return OrderRequest(  # 构造副本 | Build a copy
        side=order.side,  # 方向 | Side
        qty=float(qty),  # 新数量 | New quantity
        signal_price=order.signal_price,  # 信号价 | Signal price
        reason=order.reason,  # 原因 | Reason
        symbol=order.symbol,  # 交易对 | Symbol
        client_order_id=order.client_order_id,  # 订单号 | Order id
        qty_ratio=order.qty_ratio,  # 保留开仓比例，避免下游再算时丢失口径 | Keep the entry ratio
    )


def _summarize_result(  # 解析订单结果 | Parse the order result
    result: dict[str, Any],  # 订单结果 | Order result
    fallback_qty: float,  # 成交量回退值 | Quantity fallback
    fallback_price: float,  # 成交价回退值 | Price fallback
    quote_asset: str,  # 计价资产 | Quote asset
) -> dict[str, Any]:  # 返回 qty/price/fee/fee_known | Return the summary fields
    """调用共享解析函数，便于在测试中替换 | Delegate to the shared parser so tests can stub it.

    参数 | Parameters:
        result (dict[str, Any]): 订单结果 | Order result.
        fallback_qty (float): 成交量回退值 | Quantity fallback.
        fallback_price (float): 成交价回退值 | Price fallback.
        quote_asset (str): 计价资产 | Quote asset.

    返回 | Returns:
        dict[str, Any]: 成交量/均价/手续费/是否可信 | Quantity, price, fee and trust flag.
    """
    return order_fill_summary(result, fallback_qty=fallback_qty, fallback_price=fallback_price, quote_asset=quote_asset)  # 委托共享实现 | Delegate to the shared implementation
