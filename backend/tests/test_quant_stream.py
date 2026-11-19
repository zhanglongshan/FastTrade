"""WebSocket 行情流的单元测试 | Unit tests for the WebSocket market stream.

覆盖 | Covers:
    1. kline 推送：形成中 K 线与收盘 K 线的区分、去重、收盘序号递增
    2. bookTicker 推送：盘口快照与深度
    3. 收盘事件唤醒等待者（替代 sleep 轮询）
    4. 成交价解析：优先新 bar 开盘价
    5. 缺口检测触发 REST 回补
"""

import asyncio  # 驱动异步用例 | Drive async cases
import json  # 构造 WS 文本帧 | Build WS text frames

from app.modules.quant.execution.stream import MarketStream, _Series  # 被测算子 | Module under test


def _kline_message(open_ms: int, closed: bool, open_price: float = 100.0, close_price: float = 101.0) -> str:  # 构造 kline 帧 | Build a kline frame
    """构造一条 kline 推送文本帧 | Build one kline push frame.

    参数 | Parameters:
        open_ms (int): 开盘毫秒 | Open time in ms.
        closed (bool): 是否收盘 | Whether the bar closed.
        open_price (float): 开盘价 | Open price.
        close_price (float): 收盘价 | Close price.

    返回 | Returns:
        str: JSON 文本 | JSON text.
    """
    return json.dumps({  # 序列化为 JSON | Serialize to JSON
        "e": "kline",  # 事件类型 | Event type
        "s": "BTCUSDT",  # 交易对 | Symbol
        "k": {  # K 线主体 | Kline body
            "t": open_ms,  # 开盘时间 | Open time
            "i": "1m",  # 周期 | Interval
            "o": str(open_price),  # 开盘价 | Open
            "h": str(max(open_price, close_price) + 1),  # 最高价 | High
            "l": str(min(open_price, close_price) - 1),  # 最低价 | Low
            "c": str(close_price),  # 收盘价 | Close
            "v": "12.5",  # 成交量 | Volume
            "x": closed,  # 是否收盘 | Closed flag
        },
    })


def _register(stream: MarketStream, symbol: str = "BTCUSDT", interval: str = "1m") -> None:  # 手工登记序列 | Register a series manually
    """直接登记一条序列，绕过网络订阅 | Register a series directly, bypassing the network subscription.

    参数 | Parameters:
        stream (MarketStream): 行情流 | The stream.
        symbol (str): 交易对 | Symbol.
        interval (str): 周期 | Interval.

    返回 | Returns:
        None。
    """
    stream._series[(symbol, interval)] = _Series(interval=interval)  # 注入序列缓存 | Inject the series cache


def test_closed_bar_is_cached_and_forming_bar_is_separate() -> None:  # 收盘与形成中分离 | Closed vs forming separation
    """收盘 K 线进入缓存，未收盘只更新形成中 K 线 | Closed bars go to the cache; the forming bar stays separate."""
    stream = MarketStream(backfill=lambda *args: asyncio.sleep(0))  # 构造不带网络的流 | Build a stream without network
    _register(stream)  # 登记序列 | Register the series
    stream._handle_raw(_kline_message(0, closed=False, open_price=100, close_price=105))  # 未收盘推送 | Forming push
    assert stream.bars("BTCUSDT", "1m") == []  # 收盘缓存仍为空 | The closed cache stays empty
    assert stream.forming("BTCUSDT", "1m").close == 105  # 形成中 K 线已更新 | The forming bar updated

    stream._handle_raw(_kline_message(0, closed=True, open_price=100, close_price=106))  # 收盘推送 | Close push
    bars = stream.bars("BTCUSDT", "1m")  # 读收盘缓存 | Read the closed cache
    assert len(bars) == 1 and bars[0].close == 106  # 写入一根且为最新值 | One bar with the latest close
    assert stream.bar_seq("BTCUSDT", "1m") == 1  # 收盘序号 +1 | The close counter advanced


def test_duplicate_closed_push_does_not_grow_cache() -> None:  # 重复推送去重 | De-duplicate repeated pushes
    """同一根 K 线重复推送只覆盖不新增 | A repeated push for the same bar overwrites instead of appending."""
    stream = MarketStream(backfill=lambda *args: asyncio.sleep(0))  # 构造流 | Build a stream
    _register(stream)  # 登记 | Register
    stream._handle_raw(_kline_message(60_000, closed=True, close_price=100))  # 第一次 | First push
    stream._handle_raw(_kline_message(60_000, closed=True, close_price=102))  # 重复推送 | Duplicate push
    bars = stream.bars("BTCUSDT", "1m")  # 读缓存 | Read the cache
    assert len(bars) == 1 and bars[0].close == 102  # 只一根且取最新值 | One bar with the newest value
    assert stream.bar_seq("BTCUSDT", "1m") == 1  # 序号只加一次 | The counter advanced once


def test_book_ticker_fills_quote_and_depth() -> None:  # 盘口推送 | bookTicker push
    """bookTicker 更新买一卖一与深度 | bookTicker updates the best bid/ask and depth."""
    stream = MarketStream(backfill=lambda *args: asyncio.sleep(0))  # 构造流 | Build a stream
    stream._handle_raw(json.dumps({"u": 1, "s": "BTCUSDT", "b": "100.5", "B": "2.5", "a": "101.5", "A": "3.5"}))  # 盘口帧 | Quote frame
    quote = stream.quote("BTCUSDT")  # 读盘口 | Read the quote
    assert quote is not None and quote.bid == 100.5 and quote.ask == 101.5  # 价格正确 | Prices parsed
    assert quote.mid == 101.0  # 中间价 | Mid price
    assert stream.depth("BTCUSDT") == (2.5, 3.5)  # 深度正确 | Depth parsed
    assert stream.depth("ETHUSDT") is None  # 未订阅交易对无深度 | No depth for an unknown symbol


def test_wait_bar_is_woken_by_close_event() -> None:  # 收盘事件唤醒 | Close event wakes waiters
    """等待者会被收盘推送立即唤醒，而不是靠 sleep | A waiter is woken by the close push instead of sleeping."""
    stream = MarketStream(backfill=lambda *args: asyncio.sleep(0))  # 构造流 | Build a stream
    _register(stream)  # 登记 | Register

    async def _scenario() -> bool:  # 场景 | Scenario
        """先起等待任务，再推送收盘，检查是否被唤醒 | Start the waiter, push a close, check the wake-up."""
        waiter = asyncio.create_task(stream.wait_bar("BTCUSDT", "1m", timeout=2.0))  # 起等待任务 | Start waiting
        await asyncio.sleep(0)  # 让等待者先挂起 | Let the waiter suspend
        stream._handle_raw(_kline_message(0, closed=True))  # 推送收盘 | Push a close
        return await waiter  # 返回等待结果 | Return the wait result

    assert asyncio.run(_scenario()) is True  # 应被成功唤醒 | Must be woken


def test_wait_bar_times_out_without_push() -> None:  # 无推送时超时 | Timeout without a push
    """没有推送时等待超时返回 False，调用方据此退回 REST | Without a push the wait times out so the caller falls back to REST."""
    stream = MarketStream(backfill=lambda *args: asyncio.sleep(0))  # 构造流 | Build a stream
    _register(stream)  # 登记 | Register
    assert asyncio.run(stream.wait_bar("BTCUSDT", "1m", timeout=0.05)) is False  # 超时返回 False | Times out with False
    assert asyncio.run(stream.wait_bar("ETHUSDT", "1m", timeout=0.05)) is False  # 未订阅也返回 False | Unsubscribed also returns False


def test_execution_price_prefers_new_bar_open() -> None:  # 成交价优先新 bar 开盘 | Execution price prefers the new bar's open
    """已收盘末根之后出现新 bar 时，用新 bar 的开盘价作为成交价 | The new bar's open becomes the fill price."""
    stream = MarketStream(backfill=lambda *args: asyncio.sleep(0))  # 构造流 | Build a stream
    _register(stream)  # 登记 | Register
    stream._handle_raw(_kline_message(0, closed=True, open_price=100, close_price=110))  # 收盘一根 | Close one bar
    stream._handle_raw(_kline_message(60_000, closed=False, open_price=111, close_price=112))  # 新 bar 开始 | New bar starts
    price = asyncio.run(stream.execution_price("BTCUSDT", "1m", forming_wait=0.0))  # 解析成交价 | Resolve the price
    assert price == 111  # 取新 bar 开盘价 | The new bar's open
    assert asyncio.run(stream.execution_price("ETHUSDT", "1m", forming_wait=0.0)) == 0.0  # 未订阅返回 0 | Unsubscribed returns 0


def test_gap_detection_schedules_backfill() -> None:  # 缺口触发回补 | Gap triggers a backfill
    """跳过一根 K 线时调度 REST 回补 | A skipped bar schedules a REST backfill."""
    calls: list[tuple[str, str, int]] = []  # 记录回补调用 | Record backfill calls

    async def _backfill(symbol: str, interval: str, limit: int):  # 假的回补函数 | Fake backfill
        """记录调用并返回空列表 | Record the call and return nothing."""
        calls.append((symbol, interval, limit))  # 记录 | Record
        return []  # 无数据 | No data

    stream = MarketStream(backfill=_backfill)  # 注入假回补 | Inject the fake backfill
    _register(stream)  # 登记 | Register

    async def _scenario() -> None:  # 在事件循环内推送，才能调度后台任务 | Push inside a loop so the task can be scheduled
        """推送两根有缺口的 K 线，再让后台回补跑完 | Push two gapped bars and let the resync run."""
        stream._handle_raw(_kline_message(0, closed=True))  # 第一根 | First bar
        stream._handle_raw(_kline_message(120_000, closed=True))  # 跳过 60s 那根 | Skip the 60s bar
        await asyncio.sleep(0.05)  # 让出事件循环并等待回补 | Yield and await the resync

    asyncio.run(_scenario())  # 驱动事件循环 | Drive the loop
    assert calls and calls[0][0] == "BTCUSDT"  # 确实发起了回补 | A backfill was issued
