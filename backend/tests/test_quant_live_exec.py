"""执行器深度约束、手续费回填与多实例行锁的单元测试 | Tests for depth caps, fee backfill and the multi-instance row lock.

覆盖 | Covers:
    1. ``_cap_by_depth``：买吃卖一、卖吃买一、深度为 0 时不可成交
    2. ``PaperExecutor`` 按深度裁剪成交量（模拟盘不再是无限量成交）
    3. ``AccountLedger.execute(fee_override=...)`` 用真实手续费记账
    4. ``_claim_tick``：行锁内判定本根是否已被其他实例处理
"""

import asyncio  # 驱动异步用例 | Drive async cases
from datetime import UTC, datetime, timedelta  # 时间工具 | Datetime helpers

from app.modules.quant.execution.base import AccountLedger, OrderRequest  # 账本与订单 | Ledger and order request
from app.modules.quant.execution.runner import LiveExecutor, PaperExecutor, _cap_by_depth  # 被测算子 | Module under test
from app.modules.quant.runtime.service import _claim_tick  # 行锁占位 | Row-lock claim


def test_cap_by_depth_uses_matching_side() -> None:  # 深度按方向取量 | Depth by side
    """买入用卖一量、卖出用买一量，取较小值 | Buys use the ask size and sells the bid size, taking the min."""
    buy = OrderRequest(side="buy", qty=10, signal_price=100)  # 买入 10 | Buy 10
    sell = OrderRequest(side="sell", qty=10, signal_price=100)  # 卖出 10 | Sell 10
    assert _cap_by_depth(buy, (5.0, 3.0)) == 3.0  # 买入受卖一量限制 | Buys limited by the ask
    assert _cap_by_depth(sell, (5.0, 3.0)) == 5.0  # 卖出受买一量限制 | Sells limited by the bid
    assert _cap_by_depth(buy, None) == 10  # 无深度时不限制 | No depth means no cap
    assert _cap_by_depth(buy, (0.0, 0.0)) is None  # 无量则不成交 | Zero size means no fill


def test_paper_executor_caps_fill_by_depth() -> None:  # 模拟盘按深度成交 | Paper fill capped by depth
    """请求 10 个但卖一只有 4 个，只能成交 4 个 | Requesting 10 with only 4 on the ask fills just 4."""
    ledger = AccountLedger(10_000.0, fee_rate=0.001)  # 构造账本 | Build the ledger
    executor = PaperExecutor(ledger, fee_rate=0.001)  # 构造模拟执行器 | Build the paper executor
    fill = asyncio.run(executor.execute(OrderRequest(side="buy", qty=10, signal_price=100), 100.0, (6.0, 4.0)))  # 带深度撮合 | Fill with depth
    assert fill is not None  # 有成交 | Filled
    assert abs(fill.qty - 4.0) < 1e-12  # 只成交 4 个 | Only four units
    assert abs(ledger.long_qty - 4.0) < 1e-12  # 持仓同步 | Position matches


def test_paper_executor_without_depth_keeps_full_size() -> None:  # 无深度时保持原量 | Full size without depth
    """没有盘口时不裁剪，保持原有行为 | Without a quote nothing is capped, preserving the old behaviour."""
    ledger = AccountLedger(10_000.0, fee_rate=0.001)  # 构造账本 | Build the ledger
    executor = PaperExecutor(ledger, fee_rate=0.001)  # 构造执行器 | Build the executor
    fill = asyncio.run(executor.execute(OrderRequest(side="buy", qty=5, signal_price=100), 100.0, None))  # 无深度 | No depth
    assert fill is not None and abs(fill.qty - 5.0) < 1e-12  # 全额成交 | Fully filled


def test_ledger_fee_override_replaces_rate_estimate() -> None:  # 真实手续费覆盖估算 | Real fee overrides the estimate
    """给定 fee_override 时按真实值扣费，现金与累计手续费都随之变化 | A fee override drives cash and total_fee."""
    ledger = AccountLedger(10_000.0, fee_rate=0.001)  # 费率 0.1% | 0.1% rate
    fill = ledger.execute(OrderRequest(side="buy", qty=1, signal_price=100), 100.0, fee_override=0.05)  # 真实手续费 0.05 | Real fee 0.05
    assert fill is not None  # 有成交 | Filled
    assert abs(fill.fee - 0.05) < 1e-12  # 手续费为真实值 | The real fee
    assert abs(ledger.cash - (10_000.0 - 100.0 - 0.05)) < 1e-12  # 现金按真实手续费扣减 | Cash uses the real fee
    assert abs(ledger.total_fee - 0.05) < 1e-12  # 累计手续费为真实值 | total_fee uses the real fee


class _FakeBind:  # 假的数据库绑定 | Fake DB bind
    """只暴露 dialect.name 的假绑定 | Fake bind exposing only dialect.name."""

    class dialect:  # 假的方言对象 | Fake dialect
        """方言名固定为 sqlite（不触发 FOR UPDATE）| Dialect name fixed to sqlite."""

        name = "sqlite"  # SQLite 不支持 FOR UPDATE | SQLite has no FOR UPDATE


class _FakeResult:  # 假的查询结果 | Fake query result
    """包装单行对象的查询结果 | Query result wrapping one row."""

    def __init__(self, obj: object) -> None:  # 保存单行 | Store the row
        """初始化 | Initialize.

        参数 | Parameters:
            obj (object): 单行对象 | The row object.
        """
        self._obj = obj  # 保存 | Store

    def scalar_one_or_none(self) -> object:  # 返回单行 | Return the row
        """返回包装的单行对象 | Return the wrapped row."""
        return self._obj  # 返回 | Return


class _FakeSession:  # 假的数据库会话 | Fake DB session
    """只实现 _claim_tick 用到的最小接口 | Implements the minimal surface used by _claim_tick."""

    def __init__(self, obj: object) -> None:  # 保存单行 | Store the row
        """初始化 | Initialize.

        参数 | Parameters:
            obj (object): 单行对象 | The row object.
        """
        self.obj = obj  # 单行 | The row
        self.commits = 0  # 提交次数 | Commit count
        self.rollbacks = 0  # 回滚次数 | Rollback count

    async def execute(self, stmt: object) -> _FakeResult:  # 执行查询 | Execute a query
        """忽略语句，直接返回单行 | Ignore the statement and return the row."""
        return _FakeResult(self.obj)  # 返回结果 | Return the result

    async def commit(self) -> None:  # 提交 | Commit
        """记录提交次数 | Record a commit."""
        self.commits += 1  # 计数 | Count

    async def rollback(self) -> None:  # 回滚 | Rollback
        """记录回滚次数 | Record a rollback."""
        self.rollbacks += 1  # 计数 | Count

    def get_bind(self) -> _FakeBind:  # 取绑定 | Get the bind
        """返回假绑定 | Return the fake bind."""
        return _FakeBind()  # 返回 | Return


class _Row:  # 假的实例行 | Fake instance row
    """只带 _claim_tick 需要的字段 | Carries only the fields _claim_tick reads."""

    def __init__(self, last_tick_time: datetime | None, *, deleted: bool = False, interval: str = "1h") -> None:  # 初始化 | Initialize
        """初始化行字段 | Initialize the row fields.

        参数 | Parameters:
            last_tick_time (datetime | None): 上次 tick 时间 | Last tick time.
            deleted (bool): 是否已删除 | Whether soft-deleted.
            interval (str): 周期 | Interval.
        """
        self.id = 1  # 主键 | Primary key
        self.last_tick_time = last_tick_time  # 上次 tick | Last tick time
        self.is_deleted = deleted  # 删除标记 | Deleted flag
        self.interval = interval  # 周期 | Interval


def test_claim_tick_runs_when_last_tick_is_old() -> None:  # 旧 tick 可执行 | Claim when the last tick is old
    """距上次 tick 超过半根 K 线时抢到执行权 | A claim succeeds when half a bar has elapsed."""
    row = _Row(datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2), interval="1h")  # 两小时前 | Two hours ago
    session = _FakeSession(row)  # 假会话 | Fake session
    claimed, ok = asyncio.run(_claim_tick(1, session))  # 抢占 | Claim
    assert claimed is row and ok is True  # 抢到 | Claimed
    assert row.last_tick_time is not None  # 已写入占位时间 | Claim timestamp written


def test_claim_tick_skips_when_another_instance_just_ran() -> None:  # 刚跑过则跳过 | Skip when just run
    """距上次 tick 不足半根 K 线时判定为已被处理 | A claim fails when less than half a bar has elapsed."""
    row = _Row(datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=5), interval="1h")  # 5 秒前 | Five seconds ago
    session = _FakeSession(row)  # 假会话 | Fake session
    claimed, ok = asyncio.run(_claim_tick(1, session))  # 抢占 | Claim
    assert claimed is row and ok is False  # 未抢到 | Not claimed
    assert session.commits >= 1  # 仍需提交以释放行锁 | Must commit to release the lock


def test_claim_tick_force_bypasses_window() -> None:  # 手动运行可绕过 | Forced runs bypass
    """force=True 时忽略时间窗（手动运行一次）| force=True ignores the window for manual runs."""
    row = _Row(datetime.now(UTC).replace(tzinfo=None), interval="1h")  # 刚刚跑过 | Just ran
    session = _FakeSession(row)  # 假会话 | Fake session
    _, ok = asyncio.run(_claim_tick(1, session, force=True))  # 强制抢占 | Force the claim
    assert ok is True  # 抢到 | Claimed


def test_claim_tick_returns_none_when_deleted() -> None:  # 已删除实例不执行 | Deleted instance is not claimed
    """已删除实例返回 (None, False) 并回滚释放锁 | A deleted instance returns (None, False) and rolls back."""
    session = _FakeSession(_Row(None, deleted=True))  # 已删除 | Deleted
    claimed, ok = asyncio.run(_claim_tick(1, session))  # 抢占 | Claim
    assert claimed is None and ok is False  # 未抢到 | Not claimed
    assert session.rollbacks == 1  # 已回滚 | Rolled back


class _FakeClient:  # 假的币安客户端 | Fake Binance client
    """只实现对账用到的接口 | Implements only the surface used by reconciliation."""

    def __init__(self, trades: list[dict], orders: list[dict] | None = None) -> None:  # 初始化 | Initialize
        """保存成交与挂单 | Store trades and open orders.

        参数 | Parameters:
            trades (list[dict]): 成交明细 | Trade fills.
            orders (list[dict] | None): 挂单 | Open orders.
        """
        self.trades = trades  # 成交明细 | Trades
        self.orders = orders or []  # 挂单 | Open orders
        self.cancelled: list[object] = []  # 已撤销的订单号 | Cancelled order ids

    def symbol_info(self, symbol: str) -> dict:  # 交易规则 | Symbol rules
        """返回资产信息 | Return the asset info."""
        return {"base_asset": "BTC", "quote_asset": "USDT"}  # 固定资产 | Fixed assets

    def balances(self) -> dict[str, float]:  # 账户余额 | Balances
        """返回真实余额 | Return the real balances."""
        return {"USDT": 500.0, "BTC": 0.5}  # 现金 500、0.5 个 BTC | 500 USDT and 0.5 BTC

    def my_trades(self, symbol: str, *, order_id: object = None, limit: int = 500) -> list[dict]:  # 成交明细 | Trades
        """返回预置成交明细 | Return the canned trades."""
        return self.trades  # 返回 | Return

    def open_orders(self, symbol: str) -> list[dict]:  # 挂单 | Open orders
        """返回预置挂单 | Return the canned open orders."""
        return self.orders  # 返回 | Return

    def cancel_order(self, symbol: str, *, order_id: object = None, client_order_id: object = None) -> dict:  # 撤单 | Cancel
        """记录并返回成功 | Record and report success."""
        self.cancelled.append(order_id)  # 记录 | Record
        return {"status": "CANCELED"}  # 返回结果 | Return a result


def _trade_row(price: float, qty: float, *, buyer: bool, fee: float = 0.0, ts: int = 0, trade_id: int = 0) -> dict:  # 构造成交 | Build a trade
    """构造一条成交流水 | Build one trade row.

    参数 | Parameters:
        price (float): 成交价 | Price.
        qty (float): 数量 | Quantity.
        buyer (bool): 是否买入 | Whether a buy.
        fee (float): 手续费 | Commission.
        ts (int): 毫秒时间 | Epoch ms.
        trade_id (int): 成交 ID | Trade id.

    返回 | Returns:
        dict: 成交行 | Trade row.
    """
    return {  # 构造 | Build
        "id": trade_id,  # 成交 ID | Trade id
        "price": str(price),  # 价格 | Price
        "qty": str(qty),  # 数量 | Quantity
        "isBuyer": buyer,  # 方向 | Side
        "commission": str(fee),  # 手续费 | Commission
        "commissionAsset": "USDT",  # 计价币 | Quote currency
        "time": ts,  # 时间 | Time
    }


def _live_executor() -> tuple[object, _FakeClient]:  # 构造实盘执行器 | Build a live executor
    """构造一个 dry_run=False 但客户端被替换为假实现的执行器 | Build a live executor with a fake client.

    返回 | Returns:
        tuple[object, _FakeClient]: 执行器与假客户端 | The executor and the fake client.
    """
    executor = LiveExecutor(AccountLedger(10_000.0, fee_rate=0.001), api_key="k", api_secret="s", dry_run=False)  # 真实模式 | Real mode
    client = _FakeClient([])  # 占位客户端 | Placeholder client
    executor._client = client  # 替换为假实现 | Swap in the fake
    return executor, client  # 返回 | Return


def test_sync_uses_replay_when_complete() -> None:  # 完整数据 → 校正手续费 | Complete data corrects fees
    """成交窗口完整时用复算值校正手续费与盈亏 | A complete window corrects fees and PnL."""
    executor, client = _live_executor()  # 构造 | Build
    client.trades = [_trade_row(100, 1, buyer=True, fee=0.1, ts=1, trade_id=1)]  # 买入 1 个 | Buy one unit
    summary = executor.sync_from_exchange("BTCUSDT", price=100.0, trade_limit=1000)  # 对账 | Reconcile
    assert summary["changed"] is True  # 有变化 | Something changed
    assert abs(executor.ledger.total_fee - 0.1) < 1e-12  # 手续费取复算值 | Fees replayed
    assert executor.ledger.long_qty == 0.5  # 持仓以余额为准 | Position follows the balance
    assert executor.ledger.cash == 500.0  # 现金以余额为准 | Cash follows the balance


def test_sync_skips_fee_when_trades_truncated() -> None:  # 明细被截断 → 不动手续费 | Truncated trades leave fees alone
    """成交条数达到上限说明更早的成交缺失，此时不改手续费 | Hitting the cap means older fills are missing, so fees stay untouched."""
    executor, client = _live_executor()  # 构造 | Build
    executor.ledger.total_fee = 9.0  # 假定额本里已有 9.0 手续费 | Pretend the ledger already knows 9.0
    client.trades = [_trade_row(100, 1, buyer=True, fee=0.1, ts=1, trade_id=1), _trade_row(100, 1, buyer=True, fee=0.1, ts=2, trade_id=2)]  # 两条成交 | Two fills
    summary = executor.sync_from_exchange("BTCUSDT", price=100.0, trade_limit=2)  # 上限 2 条 → 视为截断 | Cap of two means truncated
    assert executor.ledger.total_fee == 9.0  # 手续费未被覆盖 | Fees untouched
    assert summary["replay"]["complete"] is True  # 复算本身完整 | The replay itself is complete
    assert executor.ledger.cash == 500.0  # 余额仍然校正 | Balances still corrected


def test_sync_cancels_stale_open_orders() -> None:  # 撤销孤儿挂单 | Cancel orphan orders
    """超过存活阈值的历史挂单会被撤销 | Orders older than the threshold are cancelled."""
    executor, client = _live_executor()  # 构造 | Build
    client.orders = [{"orderId": 77, "time": 1}]  # 极旧的挂单 | A very old order
    summary = executor.sync_from_exchange("BTCUSDT", price=100.0, open_order_ttl=1.0)  # 对账 | Reconcile
    assert summary["cancelled"] == 1  # 撤销一笔 | One cancelled
    assert client.cancelled == [77]  # 撤销的是该单 | The right order
    assert summary["changed"] is True  # 视为有变化 | Counted as a change
