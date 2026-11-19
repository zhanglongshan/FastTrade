"""币安 WebSocket 行情流：用推送替代轮询 | Binance WebSocket market stream: push instead of polling.

目的 | Purpose:
    原实现每个 tick 都用 REST ``/api/v3/klines`` 轮询一次，带来两个结构性缺陷：
    1) 滞后（①）：调度器是 ``sleep(周期)``，相位随 tick 耗时漂移，收盘后还要等
       几秒到几十秒才能对齐，行情与决策之间存在固定延迟；
    2) 深度缺失（⑤）：REST 只给 OHLCV，拿不到盘口，模拟盘按单一价格无限量成交，
       与真实市场脱节。
    The old design polled REST klines on every tick, causing two structural defects:
    (1) lag — the scheduler slept a fixed period so the phase drifted and each decision
    trailed the close by seconds; (2) no depth — REST returns OHLCV only, so paper fills
    were unlimited at a single price, detached from the real market.

    本模块订阅两条流解决上述问题：
    - ``<symbol>@kline_<interval>``：推送每根 K 线的增量与"收盘"事件，用于**事件驱动**
      地在收盘瞬间触发 tick（替代 ``sleep`` 轮询）；
    - ``<symbol>@bookTicker``：推送最优买卖价与挂单量（L1 盘口），用于给成交设置
      深度上限，并把"下一根开盘价"在无成交时用中间价近似。
    It subscribes to two streams: ``kline`` (incremental + close events, driving ticks
    exactly at the close instead of sleeping) and ``bookTicker`` (best bid/ask plus size,
    used for depth caps and as the fallback "next open" price).

主要流程 | Main flow:
    订阅 → （首次）REST 回补历史 → 收推送并更新缓存 → 收盘时唤醒等待者 →
    校验缺口并按需 REST 重同步 → 断线指数退避重连。
    Subscribe -> (first time) REST backfill -> apply pushes to the cache -> wake waiters on
    close -> detect and repair gaps via REST -> reconnect with exponential backoff.

降级 | Fallback:
    WebSocket 不可用（网络受限/被墙）时 ``wait_bar`` 会超时返回 False，
    调用方据此退回原来的 REST 轮询路径，功能不缺失。
    When WebSocket is unavailable ``wait_bar`` times out with False and the caller falls
    back to the original REST polling path, so functionality never disappears.
"""

from __future__ import annotations  # 前向引用类型标注 | Forward-referenced annotations

import asyncio  # 异步任务与事件 | Async tasks and events
import contextlib  # 上下文工具（忽略关闭异常）| Context helpers for suppressing close errors
import json  # 解析 WS 文本帧 | Parse WS text frames
import time  # 时间戳与单调时钟 | Timestamps and the monotonic clock
from collections import deque  # 定长双端队列存放已收盘 K 线 | Bounded deque for closed bars
from collections.abc import Callable  # 通用类型与回调签名 | Loose typing and callback signatures
from dataclasses import dataclass, field  # 数据类工具 | Dataclass helpers
from datetime import UTC, datetime  # UTC 时间与解析 | UTC time and parsing
from typing import Any

from app.core.logger import logger  # 全局日志器 | Global logger

from ..strategy.api import Candle  # K 线数据结构 | Candle data structure
from ..strategy.trading_base import bar_is_closed, interval_to_seconds, parse_bar_time  # 收盘判定与周期换算 | Closure check and interval helpers
from .exchange import fetch_klines_async  # REST 回补（与数据模块同一接口）| REST backfill using the same endpoint

# 币安现货 WebSocket 入口；使用裸 ``/ws`` 端点以便用 SUBSCRIBE 动态增删订阅
# Binance spot WebSocket endpoint; the raw ``/ws`` path allows dynamic SUBSCRIBE/UNSUBSCRIBE.
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"

# 单条序列最多缓存的已收盘 K 线根数；300 足以覆盖绝大多数指标预热
# Max closed bars cached per series; 300 covers the warm-up of almost every strategy.
DEFAULT_MAX_BARS = 300


@dataclass(frozen=True)  # 冻结数据类：盘口快照不可变 | Frozen dataclass: the quote snapshot is immutable
class Quote:  # 最优买卖价快照（L1 盘口）| Best bid/ask snapshot (L1 book)
    """最优买卖价与挂单量 | Best bid/ask with sizes.

    关键属性 | Key attributes:
        - bid/bid_qty: 买一价与买一量 | Best bid price and size.
        - ask/ask_qty: 卖一价与卖一量 | Best ask price and size.
        - updated_at: 本地接收时刻（``time.monotonic()``）| Local receive time (monotonic).
    """

    bid: float  # 买一价 | Best bid price
    bid_qty: float  # 买一量 | Best bid quantity
    ask: float  # 卖一价 | Best ask price
    ask_qty: float  # 卖一量 | Best ask quantity
    updated_at: float  # 本地接收时刻 | Local receive timestamp

    @property  # 只读属性 | Read-only property
    def mid(self) -> float:  # 中间价 | Mid price
        """买卖中间价，缺失一侧时回退另一侧 | Mid price, falling back to the available side.

        返回 | Returns:
            float: 中间价；两侧都无效时为 0.0 | Mid price, 0.0 when both sides are invalid.
        """
        if self.bid > 0 and self.ask > 0:  # 两侧都有价格时取均值 | Average when both sides exist
            return (self.bid + self.ask) / 2  # 中间价 | Mid price
        return self.ask or self.bid  # 只有一侧时用该侧 | Fall back to the available side

    def age(self) -> float:  # 距接收时刻的秒数 | Seconds since receipt
        """返回盘口快照的年龄（秒）| Return the age of the snapshot in seconds.

        返回 | Returns:
            float: 自接收到现在的秒数 | Seconds since the snapshot was received.
        """
        return max(0.0, time.monotonic() - self.updated_at)  # 单调时钟差值，避免系统时间回拨 | Monotonic delta avoids wall-clock jumps


@dataclass  # 可变数据类：缓存随推送持续更新 | Mutable dataclass: the cache keeps updating
class _Series:  # 单条 (交易对, 周期) 行情序列 | One (symbol, interval) market series
    """单条 (交易对, 周期) 的收盘 K 线与形成中 K 线缓存 | Closed bars plus the forming bar for one series.

    关键属性 | Key attributes:
        - bars: 已收盘 K 线（升序，定长）| Closed bars, ascending and bounded.
        - forming: 正在形成的那根 K 线 | The bar currently forming.
        - forming_open_time: 形成中 K 线的开盘毫秒时间戳 | Open time of the forming bar in ms.
        - seq: 收盘序号，每收到一根收盘 K 线 +1 | Close counter, +1 per closed bar.
    """

    interval: str  # K 线周期 | Candle interval
    max_bars: int = DEFAULT_MAX_BARS  # 缓存上限 | Cache capacity
    bars: deque[Candle] = field(default_factory=deque)  # 已收盘 K 线（__post_init__ 里设定长度）| Closed bars, bounded in __post_init__
    forming: Candle | None = None  # 正在形成的 K 线 | The forming candle
    forming_open_time: int = 0  # 形成中 K 线开盘毫秒 | Forming bar open time in ms
    seq: int = 0  # 收盘序号 | Close counter
    last_resync: float = 0.0  # 上次缺口重同步时刻（单调时钟）| Last gap resync time (monotonic)
    _close_event: asyncio.Event = field(default_factory=asyncio.Event)  # 收盘唤醒事件 | Close wake-up event

    def __post_init__(self) -> None:  # 初始化定长队列 | Build the bounded deque
        """按 ``max_bars`` 重建定长队列 | Rebuild the bounded deque using ``max_bars``.

        目的 | Purpose:
            dataclass 的 ``default_factory`` 拿不到 ``self``，无法把 ``max_bars`` 传给
            ``deque(maxlen=...)``；在 ``__post_init__`` 里重建最简洁。
            A dataclass ``default_factory`` cannot see ``self``, so ``max_bars`` cannot be
            passed to ``deque(maxlen=...)``; rebuilding in ``__post_init__`` is the cleanest fix.

        返回 | Returns:
            None。
        """
        self.bars = deque(self.bars, maxlen=max(2, int(self.max_bars)))  # 用上限重建队列 | Rebuild with the cap

    def clone_bars(self) -> list[Candle]:  # 复制已收盘 K 线列表 | Copy the closed bars
        """返回已收盘 K 线的浅拷贝列表 | Return a shallow copy of the closed bars.

        返回 | Returns:
            list[Candle]: 升序已收盘 K 线 | Ascending closed candles.
        """
        return list(self.bars)  # deque 转 list（Candle 为冻结对象，浅拷贝安全）| Convert to list (Candle is frozen, so shallow copy is safe)

    def push_closed(self, candle: Candle, open_ms: int) -> bool:  # 写入一根收盘 K 线 | Append a closed bar
        """写入一根已收盘 K 线并去重 | Append one closed bar with de-duplication.

        目的 | Purpose:
            推送可能重复（重连补发）或轻微乱序，需要按开盘时间去重并保持升序。
            Pushes may be duplicated (replay after reconnect) or slightly out of order, so
            de-duplicate by open time while keeping ascending order.

        参数 | Parameters:
            candle (Candle): 已收盘 K 线 | The closed candle.
            open_ms (int): 该 K 线开盘毫秒时间戳 | Its open time in milliseconds.

        返回 | Returns:
            bool: 确实新增了一根返回 True，重复/过期返回 False | True when a new bar was appended.
        """
        if self.bars:  # 已有缓存时做去重与排序判断 | De-duplicate/order-check when a cache exists
            last_open = _open_ms(self.bars[-1].date)  # 末根开盘时间 | Open time of the last cached bar
            if open_ms < last_open:  # 比末根更旧：重复或迟到数据 | Older than the last bar: duplicate or late data
                return False  # 忽略 | Ignore it
            if open_ms == last_open:  # 与末根同一根：用新值覆盖（成交量更完整）| Same bar: overwrite with the fuller values
                self.bars[-1] = candle  # 覆盖为最新版本 | Overwrite with the latest version
                return False  # 不算新增 | Not a new bar
        self.bars.append(candle)  # 追加新收盘 K 线 | Append the new closed bar
        return True  # 报告新增成功 | Report success

    def publish(self) -> None:  # 唤醒所有等待收盘的协程 | Wake every coroutine waiting for a close
        """递增收盘序号并唤醒等待者 | Bump the close counter and wake waiters.

        实现要点 | Implementation note:
            每次唤醒后替换为新 Event，避免"clear 与 set 竞争"导致的事件丢失。
            A fresh Event replaces the old one after each wake-up, avoiding the classic
            clear/set race that silently drops wake-ups.
        """
        self.seq += 1  # 递增收盘序号 | Bump the counter
        waiter = self._close_event  # 取出当前等待事件 | Take the current event
        self._close_event = asyncio.Event()  # 立即换上新事件 | Swap in a fresh event
        waiter.set()  # 唤醒旧事件上的等待者 | Wake waiters on the old event

    async def wait(self, seq: int, timeout: float | None) -> bool:  # 等待序号超过给定值 | Wait until the counter passes the given value
        """等待收盘序号超过 ``seq`` | Wait until the close counter exceeds ``seq``.

        参数 | Parameters:
            seq (int): 期望超越的序号（调用方进入等待时的快照）| Counter snapshot taken on entry.
            timeout (float | None): 超时秒数，None 表示无限等待 | Timeout in seconds, None waits forever.

        返回 | Returns:
            bool: 等到新收盘返回 True，超时返回 False | True when a new close arrived, False on timeout.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)  # 计算截止时刻 | Compute the deadline
        while self.seq <= seq:  # 序号未推进则继续等 | Keep waiting while the counter has not advanced
            remaining = None if deadline is None else deadline - time.monotonic()  # 计算剩余等待 | Compute the remaining time
            if remaining is not None and remaining <= 0:  # 已超时 | Timed out
                return False  # 报告超时 | Report a timeout
            try:  # 等待事件可能超时，需要捕获 | The wait may time out, so guard it
                await asyncio.wait_for(self._close_event.wait(), remaining)  # 等待收盘事件 | Wait for the close event
            except TimeoutError:  # 超时未收盘 | Timed out without a close
                return self.seq > seq  # 若期间恰好推进也视为成功 | Treat a racing advance as success
        return True  # 序号已推进 | The counter advanced


def _open_ms(date_text: str) -> int:  # K 线日期 → 开盘毫秒 | Candle date -> open milliseconds
    """把 K 线日期字符串转成开盘毫秒时间戳 | Convert a candle date string into open milliseconds.

    参数 | Parameters:
        date_text (str): K 线日期字符串 | Candle date string.

    返回 | Returns:
        int: 毫秒时间戳；无法解析时返回 0 | Milliseconds, or 0 when unparseable.
    """
    parsed = parse_bar_time(date_text)  # 解析时间 | Parse the timestamp
    return int(parsed.timestamp() * 1000) if parsed else 0  # 转毫秒 | Convert to milliseconds


def _candle_from_kline(kline: dict[str, Any]) -> Candle:  # kline 字段 → Candle | kline payload -> Candle
    """把 WS kline 事件里的 ``k`` 字段转成 Candle | Convert the ``k`` object of a kline event into a Candle.

    参数 | Parameters:
        kline (dict[str, Any]): ``k`` 字段内容 | The ``k`` object.

    返回 | Returns:
        Candle: 该 K 线的 OHLCV | Candle with OHLCV.
    """
    open_time = int(kline.get("t") or 0)  # 开盘毫秒时间戳 | Open time in ms
    date_text = datetime.fromtimestamp(open_time / 1000, tz=UTC).isoformat()  # 与 REST 一致的 ISO 日期 | ISO date identical to the REST path
    return Candle(  # 构造 K 线 | Build the candle
        date_text,  # 日期 | Date
        float(kline.get("o") or 0.0),  # 开盘价 | Open
        float(kline.get("h") or 0.0),  # 最高价 | High
        float(kline.get("l") or 0.0),  # 最低价 | Low
        float(kline.get("c") or 0.0),  # 收盘价 | Close
        float(kline.get("v") or 0.0),  # 成交量 | Volume
    )


def _quote_from_book_ticker(payload: dict[str, Any]) -> Quote:  # bookTicker → Quote | bookTicker payload -> Quote
    """把 bookTicker 推送转成 Quote | Convert a bookTicker push into a Quote.

    参数 | Parameters:
        payload (dict[str, Any]): bookTicker 字段（b/B/a/A）| bookTicker fields (b/B/a/A).

    返回 | Returns:
        Quote: 盘口快照 | The quote snapshot.
    """
    return Quote(  # 构造盘口快照 | Build the quote snapshot
        bid=float(payload.get("b") or 0.0),  # 买一价 | Best bid
        bid_qty=float(payload.get("B") or 0.0),  # 买一量 | Best bid size
        ask=float(payload.get("a") or 0.0),  # 卖一价 | Best ask
        ask_qty=float(payload.get("A") or 0.0),  # 卖一量 | Best ask size
        updated_at=time.monotonic(),  # 记录本地接收时刻 | Record the local receive time
    )


def _half_spread(quote: Quote) -> float:  # 半价差比例 | Half-spread ratio
    """计算相对中间价的半价差 | Compute the half-spread relative to the mid price.

    目的 | Purpose:
        中间价本身不可成交：买入要吃卖一、卖出要打到买一。用 (卖一-买一)/2/中间价
        作为"中点价 → 可成交价"的补偿比例，模拟盘才不会把点差当成免费流动性。
        The mid is not tradeable: buys lift the ask and sells hit the bid. Using
        (ask-bid)/2/mid compensates the mid-to-tradeable gap so paper mode does not treat the
        spread as free liquidity.

    参数 | Parameters:
        quote (Quote): 盘口快照 | The quote snapshot.

    返回 | Returns:
        float: 半价差比例；盘口缺失或异常时返回 0.0 | Half-spread ratio, 0.0 when unusable.
    """
    mid = quote.mid  # 中间价 | Mid price
    if mid <= 0 or quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:  # 盘口不完整或倒挂 | Incomplete or crossed book
        return 0.0  # 不做补偿 | No compensation
    return max(0.0, (quote.ask - quote.bid) / 2.0 / mid)  # 半价差占中间价比例 | Half-spread over the mid


class MarketStream:  # 币安行情推送客户端 | Binance market-data stream client
    """币安 WebSocket 行情客户端（进程内单连接、多订阅复用）| Binance WS client with one connection shared by many subscriptions.

    目的 | Purpose:
        用一个 WebSocket 连接承载所有实例的订阅：``kline`` 驱动收盘事件，``bookTicker``
        提供深度。订阅按 ``(symbol, interval)`` 引用计数，最后一个使用者退订时才真正
        UNSUBSCRIBE，避免实例启停下反复建连。
        A single WebSocket carries every instance's subscriptions: ``kline`` drives close
        events and ``bookTicker`` supplies depth. Subscriptions are reference-counted per
        ``(symbol, interval)`` so the socket is only torn down when the last user leaves.

    关键属性 | Key attributes:
        - _series: (symbol, interval) → _Series 缓存 | Cache per series.
        - _quotes: symbol → Quote 盘口 | Quote per symbol.
        - _refs: 订阅引用计数 | Subscription reference counts.
        - _task: 连接与收包任务 | Connection/pump task.
    """

    def __init__(  # 初始化行情流 | Initialize the stream
        self,  # 实例自身 | The instance
        url: str = BINANCE_WS_URL,  # WS 地址（测试可注入本地地址）| WS URL (tests may inject a local one)
        *,  # 强制关键字参数 | Force keyword-only args
        max_bars: int = DEFAULT_MAX_BARS,  # 每条序列缓存根数 | Bars cached per series
        backfill: Callable[[str, str, int], Any] | None = None,  # REST 回补函数（默认 fetch_klines_async）| REST backfill function
    ) -> None:  # 构造器无返回值 | Constructor returns None
        """初始化行情流（不建立连接）| Initialize the stream (no connection yet).

        参数 | Parameters:
            url (str): WebSocket 地址 | WebSocket URL.
            max_bars (int): 每条序列缓存的已收盘 K 线根数 | Closed bars cached per series.
            backfill (Callable | None): 异步回补函数，签名 ``(symbol, interval, limit)`` | Async backfill, signature ``(symbol, interval, limit)``.
        """
        self.url = url  # 保存 WS 地址 | Store the URL
        self.max_bars = max(2, int(max_bars))  # 缓存根数至少 2 | At least two bars
        self._backfill = backfill or (lambda symbol, interval, limit: fetch_klines_async(symbol, interval, limit=limit))  # 默认走 REST | Default to REST
        self._series: dict[tuple[str, str], _Series] = {}  # 序列缓存 | Series cache
        self._quotes: dict[str, Quote] = {}  # 盘口缓存 | Quote cache
        self._refs: dict[tuple[str, str], int] = {}  # 订阅引用计数 | Subscription refcounts
        self._task: asyncio.Task | None = None  # 收包任务 | Pump task
        self._ws: Any = None  # 当前连接对象 | Current connection object
        self._connected = asyncio.Event()  # 连接就绪事件 | Connection-ready event
        self._lock = asyncio.Lock()  # 保护缓存与订阅的互斥锁 | Lock guarding cache and subscriptions
        self._seq_id = 0  # SUBSCRIBE 请求序号 | SUBSCRIBE request id
        self._resync_tasks: set[asyncio.Task] = set()  # 正在执行的重同步任务 | In-flight resync tasks

    # ------------------------------------------------------------------
    # 生命周期 | Lifecycle
    # ------------------------------------------------------------------
    def is_connected(self) -> bool:  # 是否已连接 | Whether connected
        """当前 WebSocket 是否处于已连接状态 | Whether the WebSocket is currently connected.

        返回 | Returns:
            bool: 已连接返回 True | True when connected.
        """
        return self._connected.is_set()  # 读取连接事件 | Read the connection event

    async def wait_connected(self, timeout: float | None = None) -> bool:  # 等待连接就绪 | Wait until connected
        """等待连接建立，超时返回 False | Wait for the connection, returning False on timeout.

        目的 | Purpose:
            调度器在等待收盘事件前先确认连接是否可用：连不上就立刻走 REST 兜底，
            而不是傻等到"两根 K 线"的超时，避免 1h 实例白等两小时。
            The scheduler checks connectivity before waiting for a close: without it a 1h
            instance would idle for two bars before falling back to REST.

        参数 | Parameters:
            timeout (float | None): 最长等待秒数，None 表示无限 | Max seconds, None means forever.

        返回 | Returns:
            bool: 已连接返回 True | True when connected.
        """
        if self._connected.is_set():  # 已连接则立即返回 | Return immediately when already connected
            return True  # 就绪 | Ready
        try:  # 等待事件可能超时 | The wait may time out
            await asyncio.wait_for(self._connected.wait(), timeout)  # 等待连接事件 | Wait for the event
        except TimeoutError:  # 超时未连上 | Timed out before connecting
            return False  # 报告未就绪 | Report not ready
        return True  # 已就绪 | Ready

    async def start(self) -> None:  # 启动收包任务 | Start the pump task
        """启动后台收包任务（幂等）| Start the background pump task (idempotent).

        返回 | Returns:
            None。
        """
        if self._task is None or self._task.done():  # 任务不存在或已结束才重建 | Recreate only when missing or finished
            self._task = asyncio.create_task(self._run(), name="quant-market-stream")  # 创建命名任务 | Create a named task

    async def stop(self) -> None:  # 停止收包并关闭连接 | Stop the pump and close the socket
        """取消收包任务并关闭连接 | Cancel the pump task and close the connection.

        返回 | Returns:
            None。
        """
        for task in list(self._resync_tasks):  # 取消未完成的重同步 | Cancel in-flight resyncs
            task.cancel()  # 请求取消 | Request cancellation
        self._resync_tasks.clear()  # 清空集合 | Clear the set
        if self._task is not None:  # 有任务才取消 | Cancel when a task exists
            self._task.cancel()  # 请求取消 | Request cancellation
            with contextlib.suppress(asyncio.CancelledError):  # 取消异常属正常 | Cancellation is expected
                await self._task  # 等待任务退出 | Await the task
            self._task = None  # 清空任务引用 | Clear the reference
        await self._close_ws()  # 关闭底层连接 | Close the underlying connection
        self._connected.clear()  # 清空连接标志 | Clear the connected flag

    async def _close_ws(self) -> None:  # 关闭底层连接 | Close the underlying connection
        """关闭当前 WebSocket 连接（忽略关闭异常）| Close the current socket, ignoring close errors.

        返回 | Returns:
            None。
        """
        ws = self._ws  # 取当前连接快照 | Snapshot the connection
        self._ws = None  # 先清空引用，避免重入 | Clear the reference first to avoid re-entry
        if ws is not None:  # 有连接才关闭 | Close only when a connection exists
            with contextlib.suppress(Exception):  # 关闭失败不影响主流程 | A failed close must not break the flow
                await ws.close()  # 关闭连接 | Close it

    # ------------------------------------------------------------------
    # 订阅管理 | Subscription management
    # ------------------------------------------------------------------
    @staticmethod  # 静态方法 | Static method
    def _key(symbol: str, interval: str) -> tuple[str, str]:  # 规范化订阅键 | Normalize a subscription key
        """规范化 (交易对, 周期) 键：交易对去分隔符转大写，周期转小写 | Normalize the (symbol, interval) key.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.
            interval (str): 周期 | Interval.

        返回 | Returns:
            tuple[str, str]: 规范化后的键 | Normalized key.
        """
        return symbol.replace("/", "").replace("_", "").upper(), str(interval)  # 与 REST 路径保持一致 | Match the REST path format

    @staticmethod  # 静态方法 | Static method
    def _stream_name(key: tuple[str, str]) -> str:  # 订阅键 → 流名 | Key -> stream name
        """生成币安流名 | Build the Binance stream name.

        参数 | Parameters:
            key (tuple[str, str]): (交易对, 周期) | (symbol, interval).

        返回 | Returns:
            str: 如 ``btcusdt@kline_1m`` | e.g. ``btcusdt@kline_1m``.
        """
        symbol, interval = key  # 拆解键 | Unpack the key
        return f"{symbol.lower()}@kline_{str(interval).lower()}"  # 币安要求小写 | Binance requires lowercase

    async def subscribe(self, symbol: str, interval: str) -> None:  # 订阅 K 线与盘口 | Subscribe to klines and bookTicker
        """订阅某交易对的 K 线与 bookTicker（引用计数）| Subscribe to klines and bookTicker with refcounting.

        目的 | Purpose:
            首次订阅时先建缓存、启连接、发 SUBSCRIBE，再用 REST 回补历史；回补放在
            订阅之后，避免"回补与订阅之间恰好收盘"造成的数据空洞。
            On first subscribe: create the cache, start the connection, send SUBSCRIBE, then
            backfill via REST. Backfilling after subscribing avoids the hole that appears when
            a bar closes exactly between the backfill and the subscription.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.
            interval (str): 周期 | Interval.

        返回 | Returns:
            None。
        """
        key = self._key(symbol, interval)  # 规范化键 | Normalize the key
        async with self._lock:  # 加锁保护引用计数 | Lock around refcounts
            first = key not in self._refs  # 是否首次订阅 | Whether this is the first subscriber
            self._refs[key] = self._refs.get(key, 0) + 1  # 引用 +1 | Increment the refcount
            if first:  # 首次订阅才建缓存 | Create the cache only on the first subscribe
                self._series[key] = _Series(interval=key[1], max_bars=self.max_bars)  # 初始化序列 | Init the series
        await self.start()  # 确保收包任务在跑 | Ensure the pump is running
        if not first:  # 非首次订阅无需重复发送 | Nothing more to do for later subscribers
            return  # 直接返回 | Return early
        if self.is_connected():  # 已连接则立即订阅 | Subscribe immediately when connected
            await self._send({"method": "SUBSCRIBE", "params": [self._stream_name(key), f"{key[0].lower()}@bookTicker"]})  # 发送订阅 | Send SUBSCRIBE
        await self._seed(key)  # REST 回补历史 | Backfill history via REST

    async def unsubscribe(self, symbol: str, interval: str) -> None:  # 退订 | Unsubscribe
        """退订某交易对（引用计数归零时真正 UNSUBSCRIBE）| Unsubscribe; only the last user sends UNSUBSCRIBE.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.
            interval (str): 周期 | Interval.

        返回 | Returns:
            None。
        """
        key = self._key(symbol, interval)  # 规范化键 | Normalize the key
        async with self._lock:  # 加锁保护引用计数 | Lock around refcounts
            count = self._refs.get(key, 0)  # 当前引用数 | Current refcount
            if count <= 1:  # 最后一个使用者 | Last user
                self._refs.pop(key, None)  # 移除引用 | Drop the refcount
                self._series.pop(key, None)  # 移除缓存 | Drop the cache
                last = True  # 标记需要真正退订 | Mark a real unsubscribe
            else:  # 仍有其他使用者 | Other users remain
                self._refs[key] = count - 1  # 引用 -1 | Decrement
                last = False  # 无需退订 | No real unsubscribe
        if last and self.is_connected():  # 真正退订且已连接 | Real unsubscribe on a live socket
            await self._send({"method": "UNSUBSCRIBE", "params": [self._stream_name(key), f"{key[0].lower()}@bookTicker"]})  # 发送退订 | Send UNSUBSCRIBE

    async def _send(self, message: dict[str, Any]) -> None:  # 发送控制帧 | Send a control frame
        """向 WebSocket 发送一帧 JSON（失败只记日志）| Send one JSON frame (log-only on failure).

        参数 | Parameters:
            message (dict[str, Any]): 待发送的消息 | Message to send.

        返回 | Returns:
            None。
        """
        ws = self._ws  # 取当前连接 | Snapshot the connection
        if ws is None:  # 无连接时放弃 | Give up without a connection
            return  # 直接返回 | Return early
        self._seq_id += 1  # 递增请求序号 | Bump the request id
        message.setdefault("id", self._seq_id)  # 未指定则写入序号 | Fill in the id when absent
        try:  # 发送可能因连接关闭而失败 | Sending may fail on a closing socket
            await ws.send(json.dumps(message))  # 序列化并发送 | Serialize and send
        except Exception as exc:  # noqa: BLE001 - 订阅失败不应中断收包 | Subscribe failures must not stop the pump
            logger.warning(f"行情订阅请求发送失败：{type(exc).__name__}: {exc}")  # 记录告警 | Log a warning

    # ------------------------------------------------------------------
    # 收包循环 | Pump loop
    # ------------------------------------------------------------------
    async def _run(self) -> None:  # 连接并在断线后重连 | Connect and reconnect on failure
        """连接 → 订阅 → 收包；异常退避重连 | Connect -> subscribe -> pump; reconnect with backoff.

        返回 | Returns:
            None。
        """
        from websockets.asyncio.client import connect  # 延迟导入，便于无网环境导入本模块 | Lazy import so the module loads without network deps

        backoff = 1.0  # 初始退避 1 秒 | Initial backoff of one second
        while True:  # 直到任务被取消 | Until the task is cancelled
            try:  # 连接与收包都可能抛异常 | Both connect and pump may raise
                async with connect(self.url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=2048) as ws:  # 建立连接（自动心跳）| Open the connection with heartbeats
                    self._ws = ws  # 记录连接 | Store the connection
                    self._connected.set()  # 标记已连接 | Mark connected
                    backoff = 1.0  # 连接成功重置退避 | Reset backoff after success
                    await self._resubscribe_all()  # 重放所有订阅 | Replay every subscription
                    await self._resync_all()  # 断线期间可能缺数据，重同步 | Resync because data may be missing
                    async for raw in ws:  # 持续收包 | Pump messages
                        self._handle_raw(raw)  # 处理单帧 | Handle one frame
            except asyncio.CancelledError:  # 被取消时退出 | Exit on cancellation
                raise  # 向上传播取消 | Propagate
            except Exception as exc:  # noqa: BLE001 - 网络异常需要自愈 | Network errors must self-heal
                logger.warning(f"行情 WebSocket 连接异常：{type(exc).__name__}: {exc}")  # 记录告警 | Log a warning
            finally:  # 无论何种退出都清理连接状态 | Always clear connection state
                self._connected.clear()  # 清空连接标志 | Clear the connected flag
                await self._close_ws()  # 关闭连接 | Close the socket
            await asyncio.sleep(backoff)  # 退避等待 | Wait before reconnecting
            backoff = min(backoff * 2, 30.0)  # 指数退避上限 30 秒 | Exponential backoff capped at 30s

    async def _resubscribe_all(self) -> None:  # 重放全部订阅 | Replay all subscriptions
        """连接建立后重放当前所有订阅 | Replay every active subscription after connecting.

        返回 | Returns:
            None。
        """
        async with self._lock:  # 读取引用表 | Read the refcount table
            keys = list(self._refs.keys())  # 取键快照 | Snapshot the keys
        for key in keys:  # 逐个重放 | Replay one by one
            await self._send({"method": "SUBSCRIBE", "params": [self._stream_name(key), f"{key[0].lower()}@bookTicker"]})  # 发送订阅 | Send SUBSCRIBE

    async def _resync_all(self) -> None:  # 重同步全部序列 | Resync every series
        """重连后对所有序列做一次 REST 回补 | Backfill every series via REST after a reconnect.

        返回 | Returns:
            None。
        """
        async with self._lock:  # 读取序列键 | Read the series keys
            keys = list(self._series.keys())  # 取键快照 | Snapshot the keys
        for key in keys:  # 逐条回补 | Backfill one by one
            self._schedule_resync(key)  # 用后台任务回补，避免阻塞收包 | Resync in the background to avoid blocking the pump

    def _handle_raw(self, raw: str | bytes) -> None:  # 处理单帧 | Handle one frame
        """解析并分发一帧消息 | Parse and dispatch one frame.

        参数 | Parameters:
            raw (str | bytes): WebSocket 文本帧 | WebSocket text frame.

        返回 | Returns:
            None。
        """
        try:  # JSON 解析失败不应打断收包 | A bad frame must not break the pump
            payload = json.loads(raw)  # 解析 JSON | Parse JSON
        except (TypeError, ValueError):  # 非 JSON 帧 | Not a JSON frame
            return  # 忽略 | Ignore
        if not isinstance(payload, dict):  # 只处理对象帧 | Only handle object frames
            return  # 忽略 | Ignore
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload  # 兼容组合流包装 | Unwrap combined-stream envelopes
        if "result" in data:  # SUBSCRIBE 的应答帧 | SUBSCRIBE acknowledgement
            return  # 无需处理 | Nothing to do
        event = data.get("e")  # 事件类型 | Event type
        if event == "kline":  # K 线推送 | Kline push
            self._apply_kline(data)  # 更新缓存 | Apply to the cache
        elif "b" in data and "a" in data and "s" in data and "k" not in data:  # bookTicker 无 e 字段，按字段识别 | bookTicker has no e field, detect by fields
            self._apply_book(data)  # 更新盘口 | Apply the quote

    def _apply_kline(self, data: dict[str, Any]) -> None:  # 应用 K 线推送 | Apply a kline push
        """把 kline 推送写入序列缓存，收盘时唤醒等待者 | Write a kline push into the cache and wake waiters on close.

        参数 | Parameters:
            data (dict[str, Any]): kline 事件 | Kline event.

        返回 | Returns:
            None。
        """
        kline = data.get("k") if isinstance(data.get("k"), dict) else None  # 取 k 字段 | Read the k field
        if not kline:  # 缺字段的坏帧 | Malformed frame
            return  # 忽略 | Ignore
        symbol = str(data.get("s") or "").upper()  # 交易对 | Symbol
        interval = str(kline.get("i") or "")  # 周期 | Interval
        if not symbol or not interval:  # 关键字段缺失 | Missing key fields
            return  # 忽略 | Ignore
        key = (symbol, interval)  # 序列键 | Series key
        series = self._series.get(key)  # 取缓存（未订阅则忽略）| Fetch the series (ignore unsubscribed)
        if series is None:  # 未订阅的流 | Unsubscribed stream
            return  # 忽略 | Ignore
        candle = _candle_from_kline(kline)  # 转 Candle | Convert to a Candle
        open_time = int(kline.get("t") or 0)  # 开盘毫秒 | Open time in ms
        series.forming = candle  # 更新形成中 K 线（含 open，供"下一根开盘价"使用）| Update the forming bar (its open feeds the next-open price)
        series.forming_open_time = open_time  # 更新开盘时间 | Update the open time
        if not kline.get("x"):  # 未收盘，仅更新形成中 K 线 | Not closed yet: only the forming bar changed
            return  # 直接返回 | Return early
        added = series.push_closed(candle, open_time)  # 写入已收盘缓存 | Append to the closed cache
        if added:  # 只有确实新增才唤醒 | Wake only on a genuinely new bar
            series.publish()  # 唤醒等待收盘的协程 | Wake close waiters
        self._check_gap(key, series, open_time)  # 检查是否缺根 | Check for missing bars

    def _apply_book(self, data: dict[str, Any]) -> None:  # 应用盘口推送 | Apply a bookTicker push
        """更新最优买卖价缓存 | Update the best bid/ask cache.

        参数 | Parameters:
            data (dict[str, Any]): bookTicker 事件 | bookTicker event.

        返回 | Returns:
            None。
        """
        symbol = str(data.get("s") or "").upper()  # 交易对 | Symbol
        if not symbol:  # 缺交易对 | Missing symbol
            return  # 忽略 | Ignore
        self._quotes[symbol] = _quote_from_book_ticker(data)  # 写入盘口快照 | Store the quote

    def _check_gap(self, key: tuple[str, str], series: _Series, open_time: int) -> None:  # 缺口检测 | Gap detection
        """检测收盘 K 线是否缺根，必要时调度 REST 重同步 | Detect a missing bar and schedule a REST resync.

        目的 | Purpose:
            断线、限流或交易所抖动都会造成推送丢根；缺口会让指标与信号失真。
            这里按"上一根应是 ``open_time - 周期``"判断，缺失则后台回补。
            Reconnects, throttling or exchange hiccups drop pushes, which corrupts indicators
            and signals. Comparing against ``open_time - period`` detects the hole and repairs
            it in the background.

        参数 | Parameters:
            key (tuple[str, str]): 序列键 | Series key.
            series (_Series): 序列缓存 | Cached series.
            open_time (int): 本次收盘 K 线开盘毫秒 | Open time of the just-closed bar.

        返回 | Returns:
            None。
        """
        if len(series.bars) < 2:  # 只有一根时无法判断缺口 | A single bar cannot reveal a gap
            return  # 直接返回 | Return early
        previous = _open_ms(series.bars[-2].date)  # 上一根开盘时间 | Previous bar open time
        expected = open_time - interval_to_seconds(key[1]) * 1000  # 期望的上一根开盘时间 | Expected previous open time
        if previous == expected:  # 连续，无缺口 | Contiguous, no gap
            return  # 直接返回 | Return early
        logger.warning(f"行情缺口({key[0]} {key[1]})：期望 {expected} 实际 {previous}，将 REST 重同步")  # 记录缺口 | Log the gap
        self._schedule_resync(key)  # 后台回补 | Repair in the background

    def _schedule_resync(self, key: tuple[str, str]) -> None:  # 调度一次回补 | Schedule one resync
        """在后台执行一次 REST 回补（10 秒内不重复）| Run a REST backfill in the background (throttled to 10s).

        参数 | Parameters:
            key (tuple[str, str]): 序列键 | Series key.

        返回 | Returns:
            None。
        """
        series = self._series.get(key)  # 取序列 | Fetch the series
        if series is None:  # 已退订 | Unsubscribed
            return  # 直接返回 | Return early
        now = time.monotonic()  # 当前单调时间 | Current monotonic time
        if now - series.last_resync < 10.0:  # 限流：10 秒内只回补一次 | Throttle to one resync per 10s
            return  # 直接返回 | Return early
        series.last_resync = now  # 记录本次时刻 | Record the attempt
        task = asyncio.create_task(self._seed(key), name=f"quant-resync-{key[0]}-{key[1]}")  # 创建回补任务 | Create the resync task
        self._resync_tasks.add(task)  # 登记任务 | Register it
        task.add_done_callback(self._resync_tasks.discard)  # 完成后移除 | Drop it when finished

    async def _seed(self, key: tuple[str, str]) -> None:  # REST 回补 | REST backfill
        """用 REST 拉取最近 K 线并合并进缓存 | Fetch recent candles via REST and merge them into the cache.

        参数 | Parameters:
            key (tuple[str, str]): 序列键 | Series key.

        返回 | Returns:
            None。
        """
        symbol, interval = key  # 拆解键 | Unpack the key
        try:  # 回补失败只降级，不影响推送 | A failed backfill only degrades, never blocks pushes
            candles = await self._backfill(symbol, interval, self.max_bars)  # 拉取最近 K 线 | Fetch recent candles
        except Exception as exc:  # noqa: BLE001 - 网络/接口异常都要容错 | Tolerate network/API errors
            logger.warning(f"行情回补失败({symbol} {interval})：{type(exc).__name__}: {exc}")  # 记录告警 | Log a warning
            return  # 放弃本次回补 | Give up on this attempt
        series = self._series.get(key)  # 再次取序列（期间可能退订）| Re-read the series (may have been removed)
        if series is None:  # 已退订 | Unsubscribed
            return  # 直接返回 | Return early
        merged = 0  # 合并计数 | Merge counter
        for candle in candles or []:  # 逐根合并 | Merge bar by bar
            if not bar_is_closed(candle.date, interval):  # 形成中的那根不进收盘缓存 | Skip the forming bar
                if _open_ms(candle.date) >= series.forming_open_time:  # 推送没有更新过它时用 REST 补齐 | Fill the forming bar when pushes have not
                    series.forming = candle  # 记录形成中 K 线 | Store the forming bar
                    series.forming_open_time = _open_ms(candle.date)  # 记录开盘时间 | Store its open time
                continue  # 处理下一根 | Next candle
            merged += 1 if series.push_closed(candle, _open_ms(candle.date)) else 0  # 合并并累计 | Merge and count
        if merged:  # 有新增时唤醒等待者（可能是启动/重连后的首次填充）| Wake waiters when new bars arrived
            series.publish()  # 唤醒 | Wake

    # ------------------------------------------------------------------
    # 对外只读接口 | Public read-only accessors
    # ------------------------------------------------------------------
    def bars(self, symbol: str, interval: str) -> list[Candle]:  # 已收盘 K 线 | Closed bars
        """返回已收盘 K 线（升序、不含形成中那根）| Return the closed bars in ascending order, excluding the forming bar.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.
            interval (str): 周期 | Interval.

        返回 | Returns:
            list[Candle]: 已收盘 K 线；未订阅时为空列表 | Closed bars, empty when unsubscribed.
        """
        series = self._series.get(self._key(symbol, interval))  # 取序列 | Fetch the series
        return series.clone_bars() if series else []  # 返回拷贝或空列表 | Return a copy or an empty list

    def forming(self, symbol: str, interval: str) -> Candle | None:  # 形成中 K 线 | Forming bar
        """返回正在形成的那根 K 线（其 ``open`` 即"下一根开盘价"）| Return the forming bar whose ``open`` is the next-open price.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.
            interval (str): 周期 | Interval.

        返回 | Returns:
            Candle | None: 形成中 K 线，暂无则为 None | The forming candle, or None.
        """
        series = self._series.get(self._key(symbol, interval))  # 取序列 | Fetch the series
        return series.forming if series else None  # 返回形成中 K 线 | Return the forming bar

    def bar_seq(self, symbol: str, interval: str) -> int:  # 收盘序号 | Close counter
        """返回该序列的收盘序号（用于判断是否产生了新 bar）| Return the close counter used to detect a new bar.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.
            interval (str): 周期 | Interval.

        返回 | Returns:
            int: 收盘序号，未订阅为 0 | Close counter, 0 when unsubscribed.
        """
        series = self._series.get(self._key(symbol, interval))  # 取序列 | Fetch the series
        return series.seq if series else 0  # 返回序号 | Return the counter

    async def wait_bar(self, symbol: str, interval: str, timeout: float | None = None, *, after_seq: int | None = None) -> bool:  # 等待下一根收盘 | Wait for the next close
        """等待一根新的已收盘 K 线 | Wait for one new closed bar.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.
            interval (str): 周期 | Interval.
            timeout (float | None): 超时秒数，None 表示无限 | Timeout in seconds, None means forever.
            after_seq (int | None): 调用方已处理到的收盘序号；None 时以当前序号为基线。
                传入调用方自己的序号可以避免"订阅与首次等待之间恰好收盘"被漏掉。
                | The close counter the caller has already processed. Passing it avoids missing a
                close that lands between subscribing and the first wait.

        返回 | Returns:
            bool: 等到返回 True，超时（含未订阅）返回 False | True when a bar closed, False on timeout/unsubscribed.
        """
        series = self._series.get(self._key(symbol, interval))  # 取序列 | Fetch the series
        if series is None:  # 未订阅时立即返回失败，调用方走 REST 兜底 | Fail fast when unsubscribed so the caller uses REST
            return False  # 返回失败 | Return False
        baseline = series.seq if after_seq is None else int(after_seq)  # 以调用方序号或当前序号为基线 | Use the caller's counter or the current one
        return await series.wait(baseline, timeout)  # 等待序号推进 | Wait for the counter to advance

    def quote(self, symbol: str) -> Quote | None:  # 盘口快照 | Quote snapshot
        """返回最优买卖价快照 | Return the best bid/ask snapshot.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.

        返回 | Returns:
            Quote | None: 盘口快照，暂无则为 None | The quote, or None.
        """
        return self._quotes.get(self._key(symbol, "1m")[0])  # 盘口按交易对缓存，与周期无关 | Quotes are cached per symbol regardless of interval

    def depth(self, symbol: str) -> tuple[float, float] | None:  # 盘口深度 | Book depth
        """返回 (买一量, 卖一量) 供成交数量设上限 | Return (bid size, ask size) for depth-capped fills.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.

        返回 | Returns:
            tuple[float, float] | None: 深度；无盘口或已超过 10 秒未更新则 None
                | Depth, or None when unavailable or older than 10 seconds.
        """
        quote = self.quote(symbol)  # 取盘口 | Fetch the quote
        if quote is None or quote.age() > 10.0:  # 无盘口或快照过期 | No quote, or stale
            return None  # 视为无深度约束 | Treat as unconstrained
        if quote.bid_qty <= 0 or quote.ask_qty <= 0:  # 任一侧无量视为不可用 | Unusable when either side is empty
            return None  # 视为无深度约束 | Treat as unconstrained
        return quote.bid_qty, quote.ask_qty  # 返回双边深度 | Return both sides

    async def execution_price(self, symbol: str, interval: str, *, forming_wait: float = 1.0) -> float:  # 解析成交价 | Resolve the execution price
        """解析"下一根开盘价"：优先新 bar 的 open，其次盘口中间价，最后末根收盘 | Resolve the next-open price.

        目的 | Purpose:
            兼容旧调用方：只取价格，不关心口径。需要区分"精确开盘价"与"盘口近似价"时
            请用 :meth:`execution_price_with_basis`。
            Compatibility wrapper returning only the price. Use ``execution_price_with_basis`` when
            the caller must distinguish an exact open from a book-price approximation.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.
            interval (str): 周期 | Interval.
            forming_wait (float): 等待新 bar 出现的最长秒数 | Max seconds to wait for the new bar.

        返回 | Returns:
            float: 成交价；完全无数据时返回 0.0 | Execution price, 0.0 when no data at all.
        """
        price, _basis, _spread = await self.execution_price_with_basis(symbol, interval, forming_wait=forming_wait)  # 复用带口径的实现 | Reuse the basis-aware implementation
        return price  # 只返回价格 | Return the price only

    async def execution_price_with_basis(  # 解析成交价并返回口径 | Resolve the price together with its basis
        self,
        symbol: str,  # 交易对 | Symbol
        interval: str,  # 周期 | Interval
        *,
        forming_wait: float = 1.0,  # 等待新 bar 的最长秒数 | Max seconds to wait for the new bar
    ) -> tuple[float, str, float]:  # (价格, 口径, 半价差比例) | (price, basis, half-spread ratio)
        """返回成交价、口径与半价差，供上层如实标注 | Return price, basis and half-spread for honest labelling.

        目的 | Purpose:
            原先无论价格来自新 bar 开盘价还是盘口中间价，结果一律标成 ``next_open``，
            绩效归因时无法区分"精确开盘价"与"近似价"。本方法把来源显式返回：口径为
            ``mid_fallback`` 时，上层还应按半价差把买入抬到卖一、卖出压到买一。
            Previously every event-driven fill was labelled ``next_open`` regardless of whether the
            price came from the new bar's open or the book mid, so attribution could not tell an exact
            open from an approximation. This method returns the source; on ``mid_fallback`` the caller
            also applies the half-spread so a buy lifts the ask and a sell hits the bid.

        参数 | Parameters:
            symbol (str): 交易对 | Symbol.
            interval (str): 周期 | Interval.
            forming_wait (float): 等待新 bar 出现的最长秒数；给 1 秒是为了让交易所先把新 bar
                推过来，减少退化为盘口价的概率 | Max seconds to wait for the new bar; one second
                gives the venue time to publish it, reducing how often the book price is used.

        返回 | Returns:
            tuple[float, str, float]: 价格、口径（next_open/mid_fallback/forming_open/last_close/none）
            与半价差比例（(ask-bid)/2/mid，缺失时为 0）| Price, basis and half-spread ratio.
        """
        series = self._series.get(self._key(symbol, interval))  # 取序列 | Fetch the series
        if series is None:  # 未订阅 | Unsubscribed
            return 0.0, "none", 0.0  # 无价可用 | No price available
        last_open = _open_ms(series.bars[-1].date) if series.bars else 0  # 末根收盘 K 线的开盘时间 | Open time of the last closed bar
        deadline = time.monotonic() + max(0.0, forming_wait)  # 等待截止时刻 | Wait deadline
        while time.monotonic() <= deadline:  # 短暂轮询，等新 bar 或盘口 | Briefly poll for the new bar or a quote
            if series.forming is not None and series.forming_open_time > last_open:  # 已有更新的一根 | A newer bar exists
                return float(series.forming.open), "next_open", 0.0  # 用它的开盘价，口径精确 | Exact next-open basis
            quote = self.quote(symbol)  # 取盘口 | Fetch the quote
            if quote is not None and quote.mid > 0 and forming_wait <= 0:  # 不再等待时立即用中间价 | Use the mid immediately when not waiting
                return float(quote.mid), "mid_fallback", _half_spread(quote)  # 返回中间价与半价差 | Return the mid and half-spread
            await asyncio.sleep(0.02)  # 20 毫秒后再看 | Check again in 20 ms
        quote = self.quote(symbol)  # 超时后回退盘口 | Fall back to the quote after the deadline
        if quote is not None and quote.mid > 0:  # 盘口可用 | The quote is usable
            return float(quote.mid), "mid_fallback", _half_spread(quote)  # 中间价口径 | Mid-based basis
        if series.forming is not None:  # 有形成中 K 线 | A forming bar exists
            return float(series.forming.open or series.forming.close), "forming_open", 0.0  # 用开盘价或收盘价 | Use its open or close
        if series.bars:  # 最后回退末根收盘 | Last resort: the last close
            return float(series.bars[-1].close), "last_close", 0.0  # 末根收盘 | Last close
        return 0.0, "none", 0.0  # 无任何数据 | No data at all


market_stream = MarketStream()  # 模块级单例：全进程共用一个 WS 连接 | Module-level singleton sharing one WS connection per process
