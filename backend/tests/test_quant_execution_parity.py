"""执行一致性修复的回归测试 | Regression tests for the execution-parity fixes.

覆盖本轮的六个修复点：
    1. 风控心跳使用独立占位，不再被信号周期的半根窗节流、也不会吞掉信号 tick
    2. 回测入口丢弃末尾未收盘 K 线，避免未来函数
    3. 按比例开仓在成交价上重算数量，策略显式传 price 不再改变定仓基准
    4. 中间价兜底成交如实标注口径，并按半价差修正买卖价
    5. 盘口深度不足的差额被记录，模拟盘下次 tick 继续补成交
    6. 同根 K 线内允许反手（先平后开）
Covers six fixes:
    1. Risk heartbeats claim independently, so they are neither throttled away by the signal
       period's half-bar window nor able to swallow the signal tick.
    2. The backtest drops a trailing unclosed bar instead of using it as a look-ahead signal.
    3. Ratio-sized entries re-size at the fill price, so an explicit `price` no longer moves the
       sizing basis.
    4. Mid-based fills report their basis and apply the half-spread.
    5. Depth shortfalls are recorded and paper mode retries them on the next tick.
    6. A reversal (exit then entry) can complete within the same bar.
"""

import asyncio  # 导入异步运行时（驱动 run_tick）| Import asyncio to drive run_tick
from datetime import UTC, datetime, timedelta  # 导入时间工具 | Import datetime helpers

import pytest  # 导入测试框架 | Import pytest

from app.core.exceptions import CustomException  # 导入业务异常（未来数据应报错）| Import the domain exception
from app.modules.quant.backtest.engine import run_backtest  # 导入回测入口 | Import the backtest entry point
from app.modules.quant.execution.base import AccountLedger, OrderRequest  # 导入统一账本与订单 | Import the shared ledger and order
from app.modules.quant.execution.risk import RiskSettings  # 导入风控配置 | Import risk settings
from app.modules.quant.execution.runner import PaperExecutor, run_tick  # 导入运行时与模拟执行器 | Import runtime and paper executor
from app.modules.quant.runtime import service as runtime_service  # 导入调度服务（_claim_tick）| Import the scheduler service
from app.modules.quant.strategy.api import Candle, Strategy  # 导入 K 线与策略基类 | Import Candle and Strategy


def _candles(prices: list[float], start: datetime | None = None) -> list[Candle]:  # 构造确定性 K 线 | Build deterministic candles
    """按价格序列构造 K 线（开=收=价格，高低价 ±2%）| Build candles from prices (open=close=price, high/low ±2%)."""
    base = start or datetime(2024, 1, 1)  # 起始时间 | Start time
    return [  # 逐根构造 | Build candle by candle
        Candle(  # 单根 K 线 | A single candle
            (base + timedelta(hours=index)).isoformat(),  # 时间戳 | Timestamp
            price,  # 开盘价 | Open
            price * 1.02,  # 最高价 | High
            price * 0.98,  # 最低价 | Low
            price,  # 收盘价 | Close
            1000,  # 成交量 | Volume
        )  # K 线构造结束 | End of candle
        for index, price in enumerate(prices)  # 遍历价格 | Iterate prices
    ]


class Hold(Strategy):  # 不交易的策略 | Strategy that never trades
    """只用于驱动引擎，不产生任何订单 | Only drives the engine, never orders."""

    name = "空转"  # 策略名 | Strategy name
    startup_candle_count = 0  # 无需预热 | No warmup

    def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
        """什么都不做 | Do nothing."""
        return None  # 显式无操作 | Explicit no-op


class LastBarBuy(Strategy):  # 只在末根开多的策略 | Strategy that buys only on the last bar

    name = "末根开仓"  # 策略名 | Strategy name
    startup_candle_count = 0  # 无需预热 | No warmup

    def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
        """末根开多 | Buy on the last bar."""
        if index == len(candles) - 1:  # 最后一根 | The last bar
            api.buy(ratio=0.5, reason="last_bar")  # 开多 | Open long


class ExplicitPriceBuy(Strategy):  # 显式传信号价的策略 | Strategy passing an explicit signal price

    name = "显式价格"  # 策略名 | Strategy name
    startup_candle_count = 0  # 无需预热 | No warmup

    def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
        """第 1 根用显式价格开多 | Buy at bar 1 with an explicit price."""
        if index == 1:  # 目标 K 线 | Target bar
            api.buy(price=100.0, ratio=0.9, reason="entry")  # 用 100 作信号价 | Use 100 as the signal price


class Reverse(Strategy):  # 同根反手的策略 | Strategy reversing on one bar

    name = "反手"  # 策略名 | Strategy name
    startup_candle_count = 0  # 无需预热 | No warmup

    def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
        """先建多，再同一根内平多并重新开多 | Go long, then exit and re-enter on the same bar."""
        if index == 1:  # 首笔建仓 | Initial entry
            api.buy(ratio=0.9, reason="first")
        elif index == 3:  # 同一根内先平后开 | Exit and re-enter on the same bar
            api.sell(reason="exit")  # 平多入队 | Queue the exit
            api.buy(ratio=0.5, reason="reverse")  # 立刻反手开多 | Immediately re-enter


def test_backtest_drops_unclosed_trailing_bar() -> None:  # 回测丢弃未收盘末根 | The backtest drops an unclosed tail
    """末根未收盘时应被丢弃，并用 dropped_unclosed 记录 | The forming last bar is dropped and reported."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)  # 当前整点 | Current hour start
    closed = _candles([100.0 + index for index in range(5)], start=now - timedelta(hours=6))  # 5 根已收盘 | Five closed bars
    forming = Candle(now.isoformat(), 106.0, 107.0, 105.0, 106.0, 1)  # 正在形成的末根 | Forming last bar
    result = run_backtest(closed + [forming], Hold, mode="event", symbol="X", interval="1h")  # 跑回测 | Run the backtest
    assert result["candle_count"] == 5  # 只用了 5 根 | Only five bars used
    assert result["dropped_unclosed"] == 1  # 记录了 1 根丢弃 | One bar reported as dropped


def test_backtest_skip_basis_drops_last_bar_signal() -> None:  # 末根信号按严格口径放弃 | Strict basis drops the last signal
    """final_fill_basis=skip 时末根信号不成交，close 时按收盘成交 | skip drops it, close fills it."""
    candles = _candles([100.0 + index for index in range(6)])  # 全历史 K 线 | Historical candles
    skipped = run_backtest(candles, LastBarBuy, mode="event", symbol="X", interval="1h", final_fill_basis="skip")  # 严格口径 | Strict basis
    legacy = run_backtest(candles, LastBarBuy, mode="event", symbol="X", interval="1h", final_fill_basis="close")  # 兼容口径 | Legacy basis
    assert skipped["trade_count"] == 0  # 末根信号被放弃 | Last-bar signal dropped
    assert skipped["final_fill_basis"] == "skip"  # 口径被记录 | Basis recorded
    assert legacy["trade_count"] == 1  # 兼容口径仍按收盘成交 | Legacy basis still fills at the close


def test_backtest_min_notional_rejects_dust_orders() -> None:  # 最小名义金额过滤碎单 | Minimum notional rejects dust
    """min_notional 很大时所有订单都不成交，与实盘过滤器一致 | A huge minimum blocks every order."""
    candles = _candles([100.0 + index for index in range(6)])  # 历史 K 线 | Historical candles
    result = run_backtest(candles, LastBarBuy, mode="event", symbol="X", interval="1h", min_notional=1e9)  # 巨额下限 | Huge minimum
    assert result["trade_count"] == 0  # 全部被拒 | Everything rejected
    assert result["min_notional"] == pytest.approx(1e9)  # 生效值被记录 | Effective value recorded


class _FakeResult:  # 假查询结果 | Fake query result
    """只包装一行 | Wraps a single row."""

    def __init__(self, obj: object) -> None:  # 保存单行 | Store the row
        """初始化 | Initialize.

        参数 | Parameters:
            obj (object): 单行对象 | The row object.
        """
        self._obj = obj  # 保存 | Store

    def scalar_one_or_none(self) -> object:  # 返回单行 | Return the row
        """返回包装的行 | Return the wrapped row."""
        return self._obj  # 返回 | Return


class _FakeDialect:  # 假方言 | Fake dialect
    """sqlite 不支持行锁，_claim_tick 会退化为普通读取 | sqlite has no row locks."""

    name = "sqlite"  # 方言名 | Dialect name


class _FakeBind:  # 假绑定 | Fake bind
    """只提供 dialect.name | Provides only dialect.name."""

    dialect = _FakeDialect()  # 方言对象 | The dialect object


class _FakeSession:  # 假数据库会话 | Fake DB session
    """只实现 _claim_tick 用到的最小接口 | Implements the minimal surface used by _claim_tick."""

    def __init__(self, obj: object) -> None:  # 保存单行 | Store the row
        """初始化 | Initialize.

        参数 | Parameters:
            obj (object): 单行对象 | The row object.
        """
        self.obj = obj  # 单行 | The row
        self.commits = 0  # 提交次数 | Commit count

    async def execute(self, stmt: object) -> _FakeResult:  # 执行查询 | Execute a query
        """忽略语句直接返回单行 | Ignore the statement and return the row."""
        return _FakeResult(self.obj)  # 返回结果 | Return the result

    async def commit(self) -> None:  # 提交 | Commit
        """记录提交次数 | Record a commit."""
        self.commits += 1  # 计数 | Count

    async def rollback(self) -> None:  # 回滚 | Rollback
        """空实现 | No-op."""
        return None  # 无操作 | Nothing

    def get_bind(self) -> _FakeBind:  # 取绑定 | Get the bind
        """返回假绑定，使 _claim_tick 走普通读取 | Return a fake bind so the plain read is used."""
        return _FakeBind()  # 返回 | Return


class _Row:  # 假的实例行 | Fake instance row
    """只带 _claim_tick 需要的字段 | Carries only the fields _claim_tick reads."""

    def __init__(  # 初始化 | Initialize
        self,  # 实例自身 | The instance
        last_tick_time: datetime | None,  # 上次信号 tick | Last signal tick
        *,  # 强制关键字参数 | Force keyword-only params
        interval: str = "1h",  # 周期 | Interval
        last_risk_time: datetime | None = None,  # 上次风控心跳（数据库列）| Last risk heartbeat (DB column)
    ) -> None:  # 无返回值 | No return value
        """初始化行字段 | Initialize the row fields.

        参数 | Parameters:
            last_tick_time (datetime | None): 上次 tick 时间 | Last tick time.
            interval (str): 周期 | Interval.
            last_risk_time (datetime | None): 上次风控心跳时间 | Last risk heartbeat time.
        """
        self.id = 1  # 主键 | Primary key
        self.last_tick_time = last_tick_time  # 上次 tick | Last tick time
        self.last_risk_time = last_risk_time  # 上次心跳 | Last heartbeat
        self.is_deleted = False  # 未删除 | Not deleted
        self.interval = interval  # 周期 | Interval


def test_risk_heartbeat_claim_does_not_block_signal_tick() -> None:  # 风控心跳不吞掉信号 tick | Heartbeats never swallow the signal tick
    """1m 风控心跳不写 last_tick_time，因此半根窗内仍能跑到信号 tick | Heartbeats leave the claim column alone."""
    row = _Row(datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=45), interval="1h")  # 45 分钟前跑过信号 | Signal ran 45m ago
    before = row.last_tick_time  # 记录原值 | Remember the original
    session = _FakeSession(row)  # 假会话 | Fake session
    _, ok_beat = asyncio.run(runtime_service._claim_tick(1, session, touch=False))  # 风控心跳 | Risk heartbeat
    assert ok_beat is True  # 心跳不被半根窗挡住 | Not blocked by the half-bar window
    assert row.last_tick_time == before  # 未改写占位时间 | Claim column untouched
    _, ok_signal = asyncio.run(runtime_service._claim_tick(1, session))  # 随后的信号 tick | The following signal tick
    assert ok_signal is True  # 信号 tick 仍能抢到 | The signal tick still claims
    _, ok_again = asyncio.run(runtime_service._claim_tick(1, session, touch=False))  # 30 秒内重复心跳 | Repeat heartbeat within 30s
    assert ok_again is False  # 被短窗节流 | Throttled by the short window


def _patch_strategy(monkeypatch: pytest.MonkeyPatch, cls: type[Strategy]) -> str:  # 替换策略加载器 | Patch the strategy loader
    """让 run_tick 加载到测试内的策略类（测试策略未落盘）| Make run_tick resolve the in-test strategy.

    参数 | Parameters:
        monkeypatch (pytest.MonkeyPatch): pytest 补丁工具 | pytest patching helper.
        cls (type[Strategy]): 测试策略类 | The in-test strategy class.

    返回 | Returns:
        str: 可直接传给 run_tick 的策略名 | A name usable with run_tick.
    """
    from types import SimpleNamespace  # 局部导入轻量命名空间 | Local import of a lightweight namespace

    monkeypatch.setattr(  # 替换执行器内的加载函数 | Patch the loader inside the runner module
        "app.modules.quant.execution.runner.load_strategy",  # 目标函数路径 | Target function path
        lambda _name, *args, **kwargs: SimpleNamespace(name=cls.name, mode="event", cls=cls),  # 返回测试策略 | Return the test strategy
    )
    return cls.name  # 返回策略名 | Return the strategy name


def test_paper_sizes_at_fill_price_not_signal_price(monkeypatch: pytest.MonkeyPatch) -> None:  # 定仓基准是成交价 | Sizing uses the fill price
    """策略显式传 price=100 时，数量仍按成交价 200 计算 | An explicit price no longer moves the sizing basis."""
    name = _patch_strategy(monkeypatch, ExplicitPriceBuy)  # 注入测试策略 | Inject the test strategy
    candles = _candles([100.0, 100.0], start=datetime.now(UTC) - timedelta(hours=5))  # 两根已收盘 | Two closed bars
    ledger = AccountLedger(10000.0, fee_rate=0.001, slippage=0.0)  # 构造账本 | Build the ledger
    tick = asyncio.run(  # 驱动一次 tick | Drive one tick
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.001),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=candles,  # 已收盘窗口 | Closed window
            execution_price=200.0,  # 成交价 200 | Fill price 200
        )
    )
    assert tick.fills, "应产生一笔买入 | A buy fill is expected"
    expected = 10000.0 * 0.9 / (200.0 * 1.001)  # 按成交价 200 定仓 | Size at the fill price
    assert tick.fills[0]["price"] == pytest.approx(200.0)  # 成交价正确 | Fill price is right
    assert tick.fills[0]["qty"] == pytest.approx(expected)  # 数量按 200 计算 | Quantity sized at 200
    assert tick.fills[0]["qty"] < 60  # 明显不是按 100 算出的 89.9 | Clearly not sized at 100


def test_mid_fallback_is_labelled_and_pays_half_spread(monkeypatch: pytest.MonkeyPatch) -> None:  # 中间价口径有标注且付点差 | Mid basis is labelled and pays the spread
    """fill_basis_hint=mid_fallback 时买入价上浮半价差，结果口径如实记录 | Buys pay the half-spread and the basis is recorded."""
    name = _patch_strategy(monkeypatch, ExplicitPriceBuy)  # 注入测试策略 | Inject the test strategy
    candles = _candles([100.0, 100.0], start=datetime.now(UTC) - timedelta(hours=5))  # 两根已收盘 | Two closed bars
    ledger = AccountLedger(10000.0, fee_rate=0.0, slippage=0.0)  # 构造账本 | Build the ledger
    tick = asyncio.run(  # 驱动一次 tick | Drive one tick
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=candles,  # 已收盘窗口 | Closed window
            execution_price=200.0,  # 中间价 | Mid price
            fill_basis_hint="mid_fallback",  # 口径提示 | Basis hint
            spread_ratio=0.001,  # 半价差 10bp | 10bp half-spread
        )
    )
    assert tick.fill_basis == "mid_fallback"  # 口径如实记录 | Basis recorded honestly
    assert tick.fills[0]["price"] == pytest.approx(200.0 * 1.001)  # 买入价上浮半价差 | Buy pays the half-spread


def test_depth_shortfall_is_recorded_and_retried(monkeypatch: pytest.MonkeyPatch) -> None:  # 深度差额被记录并补成交 | Depth shortfalls are recorded and retried
    """卖一量不足时只成交一部分，差额记录到 unfilled 并在下一 tick 补上 | Shortfalls are recorded and filled next tick."""
    name = _patch_strategy(monkeypatch, ExplicitPriceBuy)  # 注入测试策略 | Inject the test strategy
    candles = _candles([100.0, 100.0], start=datetime.now(UTC) - timedelta(hours=5))  # 两根已收盘 | Two closed bars
    ledger = AccountLedger(10000.0, fee_rate=0.0, slippage=0.0)  # 构造账本 | Build the ledger
    first = asyncio.run(  # 第一次 tick：深度只有 4 | First tick with depth 4
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=candles,  # 已收盘窗口 | Closed window
            execution_price=100.0,  # 成交价 100 | Fill price 100
            depth=(1000.0, 4.0),  # 卖一只有 4 | Only four on the ask
        )
    )
    assert first.fills[0]["qty"] == pytest.approx(4.0)  # 只成交 4 | Only four filled
    assert first.unfilled and first.unfilled[0]["reason"] == "depth"  # 差额被记录 | Shortfall recorded
    assert ledger.remainders, "剩余量应进入补成交队列 | The remainder must be queued"
    second = asyncio.run(  # 第二次 tick：深度充足 | Second tick with plenty of depth
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=candles,  # 已收盘窗口 | Closed window
            execution_price=100.0,  # 成交价 100 | Fill price 100
            depth=(1000.0, 1000.0),  # 深度充足 | Plenty of depth
        )
    )
    assert second.fills, "应有补成交 | The remainder must fill"
    assert not ledger.remainders, "补成交后队列应清空 | The queue must be drained"
    expected_total = 10000.0 * 0.9 / 100.0  # 名义 90 个 | Ninety units notional
    assert ledger.long_qty == pytest.approx(expected_total)  # 最终全部成交 | Fully filled in the end


def test_reverse_position_in_same_bar() -> None:  # 同根反手可行 | Reversal within one bar
    """先平多再开多的订单在同一根都能入队并成交 | Both the exit and the re-entry fill."""
    candles = _candles([100.0, 100.0, 100.0, 100.0, 100.0, 100.0])  # 六根平价 K 线 | Six flat candles
    result = run_backtest(candles, Reverse, mode="event", symbol="X", interval="1h", fee_rate=0.0, slippage=0.0)  # 跑回测 | Run the backtest
    reasons = [trade["reason"] for trade in result["trades"]]  # 取成交原因 | Collect fill reasons
    assert "first" in reasons  # 首笔建仓 | Initial entry
    assert "exit" in reasons  # 平仓成交 | Exit filled
    assert "reverse" in reasons  # 反手成交（旧门控会拒绝）| Re-entry filled (the old gate rejected it)


class StopLoss5(Strategy):  # 只带 5% 固定止损、从不主动下单 | Carries a 5% stop and never orders
    """用于验证风控平仓路径 | Drives the forced-exit path under test."""

    name = "止损五"  # 策略名 | Strategy name
    startup_candle_count = 0  # 无需预热 | No warmup
    stoploss = 0.05  # 相对开仓价 -5% | Five percent below the entry price

    def on_bar(self, api, candles, index):  # 逐根回调 | Per-bar callback
        """不产生任何订单，只让风控接管 | Never orders, leaving exits to the risk layer."""
        return None  # 显式无操作 | Explicit no-op


def _long_ledger(qty: float = 10.0, price: float = 100.0) -> AccountLedger:  # 构造带多头持仓的账本 | Build a ledger holding a long
    """以 5% 止损建仓 qty 个多头 | Open a long of qty with a 5% stop."""
    ledger = AccountLedger(10000.0, fee_rate=0.0, slippage=0.0, risk=RiskSettings(stoploss=0.05))  # 关闭费用以便断言价格 | Zero fees so prices stay exact
    ledger.execute(OrderRequest(side="buy", qty=qty, signal_price=price), price)  # 直接记账建仓 | Book the entry directly
    return ledger  # 返回账本 | Return the ledger


def test_risk_heartbeat_persist_does_not_reset_claim_clock() -> None:  # 心跳持久化不改占位时间 | Persisting a heartbeat must not move the claim clock
    """风控心跳写库时也不能改写 last_tick_time，否则下一根信号 tick 会被半根窗吞掉 | A persisted heartbeat must not swallow the next signal tick.

    背景：``_claim_tick(touch=False)`` 本身不写占位，但持久化阶段若仍无条件写
    ``obj.last_tick_time``，1h 实例会被每分钟一次的心跳不断刷新，导致
    ``SELECT FOR UPDATE`` 内的半根窗判定（30 分钟）永远认为"刚跑过"，策略彻底停摆。
    Background: ``_claim_tick(touch=False)`` leaves the column alone, but if the persistence
    step still writes ``obj.last_tick_time`` unconditionally, a 1h instance gets refreshed every
    minute, so the half-bar window inside the row lock always looks "just ran" and the strategy
    stops executing entirely.
    """
    row = _Row(datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=50), interval="1h")  # 50 分钟前跑过信号 | Signal ran 50m ago
    before = row.last_tick_time  # 记录原值 | Remember the original
    runtime_service._touch_tick_time(row, risk_only=True)  # 心跳持久化 | Persist a heartbeat
    assert row.last_tick_time == before, "风控心跳不得改写占位时间 | A heartbeat must not touch the claim column"
    _, ok_signal = asyncio.run(runtime_service._claim_tick(1, _FakeSession(row)))  # 随后的信号 tick | The following signal tick
    assert ok_signal is True, "心跳之后信号 tick 必须仍能抢到 | The signal tick must still claim after heartbeats"


def test_risk_heartbeat_persist_updates_clock_for_signal_tick() -> None:  # 信号 tick 仍要写占位 | Signal ticks still write the claim
    """信号周期 tick 持久化后必须刷新占位时间，保证幂等窗生效 | Signal ticks must refresh the claim so the idempotency window works."""
    row = _Row(datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3), interval="1h")  # 三小时前 | Three hours ago
    before = row.last_tick_time  # 记录原值 | Remember the original
    runtime_service._touch_tick_time(row, risk_only=False)  # 信号 tick 持久化 | Persist a signal tick
    assert row.last_tick_time != before, "信号 tick 必须刷新占位时间 | A signal tick must refresh the claim"
    _, ok_again = asyncio.run(runtime_service._claim_tick(1, _FakeSession(row)))  # 紧接着再来一次 | An immediate second run
    assert ok_again is False, "刚跑过的信号 tick 应被半根窗挡住 | The half-bar window must reject the duplicate"


def test_stop_loss_depth_shortfall_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:  # 止损被深度截断要留痕 | A truncated stop must be surfaced
    """止损单被盘口深度截断时，剩余仓位必须记录到 unfilled，而不是静默留在账上 | Depth-capped stops must be recorded, not silently left open."""
    name = _patch_strategy(monkeypatch, StopLoss5)  # 绑定测试策略 | Bind the test strategy
    ledger = _long_ledger(qty=10.0)  # 多头 10 个 | Ten units long
    candles = _candles([100.0, 100.0, 90.0], start=datetime.now(UTC) - timedelta(hours=5))  # 末根收盘 90 跌破止损 95 | The last close breaches the 95 stop
    tick = asyncio.run(  # 驱动一次 tick | Drive one tick
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=candles,  # 已收盘窗口 | Closed window
            execution_price=90.0,  # 破位后市价 90 | Market price after the breach
            depth=(1.0, 1000.0),  # 买一只有 1 个 | Only one unit on the bid
        )
    )
    stops = [fill for fill in tick.fills if fill.get("reason") == "stop_loss"]  # 取止损成交 | Collect stop fills
    assert stops, "止损必须触发 | The stop must trigger"
    assert stops[0]["qty"] == pytest.approx(1.0), "只能成交买一挂着的 1 个 | Only the bid's single unit fills"
    assert any(item.get("reason") == "stop_loss" for item in tick.unfilled), "剩余 9 个必须出现在 unfilled 里 | The remaining nine must be reported"
    assert ledger.long_qty == pytest.approx(9.0), "未平部分仍在账上，用户必须能看见 | The unfilled remainder stays open and visible"


def test_stop_loss_mid_fallback_hits_the_bid(monkeypatch: pytest.MonkeyPatch) -> None:  # 止损也要吃买一 | Stops pay the half-spread too
    """中间价口径下卖出止损应压到买一，与策略单同一套点差规则 | Mid-based stops must hit the bid like strategy orders do."""
    name = _patch_strategy(monkeypatch, StopLoss5)  # 绑定测试策略 | Bind the test strategy
    ledger = _long_ledger(qty=10.0)  # 多头 10 个 | Ten units long
    candles = _candles([100.0, 100.0, 94.0], start=datetime.now(UTC) - timedelta(hours=5))  # 末根跌破止损 | The last bar breaches the stop
    tick = asyncio.run(  # 驱动一次 tick | Drive one tick
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=candles,  # 已收盘窗口 | Closed window
            execution_price=100.0,  # 中间价 100 | Mid price 100
            fill_basis_hint="mid_fallback",  # 中间价口径 | Mid-based basis
            spread_ratio=0.001,  # 半价差 10bp | Ten basis points
            depth=(1000.0, 1000.0),  # 深度充足 | Plenty of depth
        )
    )
    stops = [fill for fill in tick.fills if fill.get("reason") == "stop_loss"]  # 取止损成交 | Collect stop fills
    assert stops, "止损必须触发 | The stop must trigger"
    assert stops[0]["price"] == pytest.approx(100.0 * 0.999), "卖出应打到买一（中间价减半价差）| Sells hit the bid, i.e. the mid minus the half-spread"


def test_backtest_drops_multiple_unclosed_trailing_bars() -> None:  # 连续多根未收盘都要丢 | Every trailing unclosed bar is dropped
    """数据截断在多根未收盘处时，全部丢弃而不是只丢最后一根 | Truncated data must drop every unclosed tail bar."""
    # 注入固定参考时刻，避免用例随真实时钟漂移 | Inject a fixed clock so this test never drifts
    fixed = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)  # 固定整点 | Fixed hour boundary
    closed = _candles([100.0 + index for index in range(4)], start=fixed - timedelta(hours=5))  # 4 根 1h 已收盘 | Four closed hourly bars
    # 场景：数据文件是 1m 粒度，但回测按 1h 周期判定。这 5 根 minute bar 的开盘时间在
    # fixed-5min..fixed-1min，按 1h 计算的收盘时刻都晚于 fixed，因此全部尚未收盘。
    # Scenario: minute-grained rows evaluated with an hourly interval; each closes after `fixed`.
    forming = [  # 连续五根按 1h 判定未收盘的行 | Five rows still forming under the 1h interval
        Candle((fixed - timedelta(minutes=5 - index)).isoformat(), 104.0 + index, 105.0 + index, 103.0 + index, 104.0 + index, 1)
        for index in range(5)
    ]
    result = run_backtest(closed + forming, Hold, mode="event", symbol="X", interval="1h", now=fixed)  # 跑回测 | Run the backtest
    assert result["candle_count"] == 4  # 五根都被丢弃 | All five dropped
    assert result["dropped_unclosed"] == 5  # 计数如实 | Count reported honestly


def test_backtest_rejects_future_dated_data() -> None:  # 未来数据直接报错 | Future-dated data raises
    """整段数据晚于当前时钟（时区错乱）时应报业务异常，而不是当已收盘 K 线用 | Future-dated data must raise, not be used as closed bars."""
    fixed = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)  # 固定参考时刻 | Fixed reference instant
    candles = _candles([100.0 + index for index in range(4)], start=fixed + timedelta(hours=2))  # 全部在未来 | Everything in the future
    with pytest.raises(CustomException):  # 必须显式失败 | Must fail loudly
        run_backtest(candles, Hold, mode="event", symbol="X", interval="1h", now=fixed)  # 跑回测 | Run the backtest


def test_paper_rejects_orders_below_min_notional(monkeypatch: pytest.MonkeyPatch) -> None:  # 模拟盘也校验最小名义额 | Paper mode honours the minimum notional
    """min_notional 高于订单名义额时模拟盘不成交，并记 unfilled | Paper mode must reject dust like the venue does."""
    name = _patch_strategy(monkeypatch, ExplicitPriceBuy)  # 绑定测试策略 | Bind the test strategy
    ledger = AccountLedger(10000.0, fee_rate=0.0, slippage=0.0)  # 构造账本 | Build the ledger
    candles = _candles([100.0, 100.0], start=datetime.now(UTC) - timedelta(hours=5))  # 两根已收盘 | Two closed bars
    tick = asyncio.run(  # 驱动一次 tick | Drive one tick
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=candles,  # 已收盘窗口 | Closed window
            execution_price=100.0,  # 成交价 100 | Fill price 100
            depth=(1000.0, 1000.0),  # 深度充足 | Plenty of depth
            min_notional=1e9,  # 交易所下限极高 | A huge venue minimum
        )
    )
    assert not tick.fills, "低于最小名义额不得成交 | Dust must not fill"
    assert any(item.get("reason") == "min_notional" for item in tick.unfilled), "原因必须可见 | The reason must be visible"
    assert ledger.long_qty == pytest.approx(0.0), "账本不得建仓 | No position may be opened"


def test_risk_heartbeat_does_not_raise_equity_peak(monkeypatch: pytest.MonkeyPatch) -> None:  # 心跳不抬净值峰值 | Heartbeats must not lift the equity peak
    """盘中风控心跳只更新止损极值，不抬 peak_equity，回撤熔断口径与回测一致 | Heartbeats leave the equity peak alone."""
    name = _patch_strategy(monkeypatch, Hold)  # 用不交易的策略 | Use a strategy that never trades
    ledger = _long_ledger(qty=10.0, price=100.0)  # 多头 10 个 | Ten units long
    ledger.peak_equity = 10000.0  # 固定峰值，便于观察 | Pin the peak so the change is visible
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)  # 当前整点 | Current hour start
    risk_candles = _candles([100.0, 200.0], start=now - timedelta(hours=3))  # 末根收盘 200 | The last bar closes at 200
    asyncio.run(  # 盘中风控心跳 | Intra-bar risk heartbeat
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=risk_candles,  # 窗口 | Window
            execution_price=200.0,  # 价格 200 | Price 200
            risk_only=True,  # 只跑风控 | Risk only
        )
    )
    assert ledger.peak_equity == pytest.approx(10000.0), "心跳不得抬高净值峰值 | A heartbeat must not lift the peak"
    assert ledger.long_peak == pytest.approx(200.0 * 1.02), "移动止损极值仍应更新 | Trailing extremes still update"
    asyncio.run(  # 信号周期 tick | A signal-period tick
        run_tick(  # 统一运行时 | Unified runtime
            mode="paper",  # 模拟盘 | Paper mode
            executor=PaperExecutor(ledger, fee_rate=0.0),  # 本地撮合 | Local fills
            ledger=ledger,  # 账本 | Ledger
            strategy_name=name,  # 策略名 | Strategy name
            symbol="BTC/USDT",  # 交易对 | Symbol
            interval="1h",  # 周期 | Interval
            candles=risk_candles,  # 窗口 | Window
            execution_price=200.0,  # 价格 200 | Price 200
        )
    )
    assert ledger.peak_equity > 10000.0, "信号 tick 必须抬高峰值 | Signal ticks must lift the peak"


def test_risk_heartbeat_throttle_survives_new_process() -> None:  # 心跳节流跨副本生效 | Heartbeat throttling works across replicas
    """另一个副本（进程内字典为空）也必须看到上一次心跳，避免重复发平仓单 | A second replica must see the previous heartbeat."""
    recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=5)  # 5 秒前刚跑过心跳 | A heartbeat 5s ago
    row = _Row(datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=50), interval="1h", last_risk_time=recent)  # 数据库里已有心跳 | The DB already has it
    _, ok = asyncio.run(runtime_service._claim_tick(1, _FakeSession(row), touch=False))  # 新副本尝试心跳 | The new replica tries
    assert ok is False, "数据库中的心跳时间必须挡住重复心跳 | The persisted heartbeat must throttle the duplicate"
    stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=120)  # 两分钟前 | Two minutes ago
    row.last_risk_time = stale  # 超出 30 秒窗 | Outside the 30s window
    _, ok_again = asyncio.run(runtime_service._claim_tick(1, _FakeSession(row), touch=False))  # 再次尝试 | Try again
    assert ok_again is True, "超过窗口后允许再来一次 | Allowed once the window elapsed"
    assert row.last_risk_time is not None and row.last_risk_time > stale, "必须写回新的心跳时间 | The new heartbeat must be persisted"


def test_symbol_rule_parser_reads_notional_and_lot_filters() -> None:  # 解析交易所过滤器 | Parse venue filters
    """exchangeInfo 的 MIN_NOTIONAL/LOT_SIZE/MARKET_LOT_SIZE 必须被正确解析 | The parser must read the order-gating filters."""
    from app.modules.quant.execution.exchange import _parse_symbol_item  # 局部导入，避免污染测试头部 | Local import

    item = {  # 构造一个 exchangeInfo 条目 | Build one exchangeInfo entry
        "baseAsset": "BTC",  # 基础资产 | Base asset
        "quoteAsset": "USDT",  # 计价资产 | Quote asset
        "filters": [  # 过滤器列表 | Filter list
            {"filterType": "MIN_NOTIONAL", "minNotional": "10"},  # 最小名义额 10 USDT | 10 USDT minimum
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.0001", "maxQty": "100"},  # 数量规则 | Lot rules
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00001", "minQty": "0.0002", "maxQty": "50"},  # 市价规则 | Market rules
        ],
    }
    rules = _parse_symbol_item(item)  # 解析 | Parse
    assert rules["min_notional"] == pytest.approx(10.0)  # 最小名义额 | Minimum notional
    assert rules["market_min_qty"] == pytest.approx(0.0002)  # 市价最小量优先用于市价单 | Market minimum
    assert rules["quote_asset"] == "USDT"  # 计价资产 | Quote asset


def test_symbol_rules_degrades_to_unconstrained_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:  # 查询失败不阻断交易 | A failed lookup must not block trading
    """规则接口不可用时返回"无约束"并短时缓存，而不是抛异常 | Degrade to unconstrained instead of raising."""
    import httpx  # 导入 httpx 构造异常 | Import httpx to raise the error

    from app.modules.quant.execution import exchange as exchange_module  # 导入模块以替换客户端 | Import the module to patch

    exchange_module._RULES_CACHE.clear()  # 清掉缓存，保证走查询分支 | Clear the cache so the lookup runs

    class _BoomClient:  # 必定失败的客户端 | A client that always fails
        """模拟网络不可用 | Simulates an unreachable network."""

        def __init__(self, *args: object, **kwargs: object) -> None:  # 构造即失败 | Fail on construction
            """抛出连接错误 | Raise a connection error."""
            raise httpx.ConnectError("boom")  # 连接失败 | Connection failure

    monkeypatch.setattr(exchange_module.httpx, "AsyncClient", _BoomClient)  # 替换异步客户端 | Patch the async client
    rules = asyncio.run(exchange_module.symbol_rules("BTC/USDT"))  # 查询 | Query
    assert rules["min_notional"] == 0.0, "失败时退化为无约束 | Degrades to unconstrained"
    assert exchange_module._RULES_CACHE, "失败结果应短时缓存，避免每 tick 重试 | Failures are cached briefly"
    exchange_module._RULES_CACHE.clear()  # 清理，避免影响其他用例 | Clean up for other tests
