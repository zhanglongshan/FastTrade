"""实盘对账与订单结果解析的单元测试 | Unit tests for reconciliation and order-result parsing.

覆盖 | Covers:
    1. 成交明细复算：均价成本、已实现盈亏、累计手续费、窗口截断标记
    2. 超期挂单识别
    3. ``AccountLedger.reconcile`` 四类状态一次性校正
    4. ``order_fill_summary`` 的真实成交量/均价/手续费解析（含 BNB 手续费不可信场景）
"""

from app.modules.quant.execution.base import AccountLedger, OrderRequest  # 账本与订单 | Ledger and order request
from app.modules.quant.execution.exchange import order_fill_summary, sum_trade_commission  # 订单结果解析 | Order-result parsing
from app.modules.quant.execution.reconcile import replay_trades, stale_open_orders  # 复算与挂单识别 | Replay and stale orders


def _trade(price: float, qty: float, *, is_buyer: bool, fee: float = 0.0, asset: str = "USDT", time_ms: int = 0, trade_id: int = 0) -> dict:  # 构造成交明细 | Build a trade row
    """构造一条 myTrades 记录 | Build one myTrades row.

    参数 | Parameters:
        price (float): 成交价 | Fill price.
        qty (float): 数量 | Quantity.
        is_buyer (bool): 是否买入 | Whether the buyer side.
        fee (float): 手续费 | Commission.
        asset (str): 手续费币种 | Commission asset.
        time_ms (int): 成交毫秒 | Trade time in ms.
        trade_id (int): 成交 ID | Trade id.

    返回 | Returns:
        dict: 成交记录 | Trade row.
    """
    return {  # 构造记录 | Build the row
        "id": trade_id,  # 成交 ID | Trade id
        "price": str(price),  # 价格（币安返回字符串）| Price (Binance returns strings)
        "qty": str(qty),  # 数量 | Quantity
        "isBuyer": is_buyer,  # 买卖方向 | Side
        "commission": str(fee),  # 手续费 | Commission
        "commissionAsset": asset,  # 手续费币种 | Fee asset
        "time": time_ms,  # 成交时间 | Trade time
    }


def test_replay_trades_computes_cost_pnl_and_fees() -> None:  # 复算成本/盈亏/手续费 | Replay cost, PnL and fees
    """两笔买入后一次性卖出，均价成本与盈亏应符合账本口径 | Two buys then one sell must match the ledger convention."""
    trades = [  # 买入 1@100（费 0.1）、买入 1@200（费 0.2）、卖出 2@300（费 0.6）| Two buys then a sell
        _trade(100, 1, is_buyer=True, fee=0.1, time_ms=1, trade_id=1),  # 第一笔买入 | First buy
        _trade(200, 1, is_buyer=True, fee=0.2, time_ms=2, trade_id=2),  # 第二笔买入 | Second buy
        _trade(300, 2, is_buyer=False, fee=0.6, time_ms=3, trade_id=3),  # 全部卖出 | Full exit
    ]
    result = replay_trades(trades, "USDT")  # 复算 | Replay
    assert result["complete"] is True  # 数据完整 | Complete
    assert result["long_qty"] == 0  # 已平仓 | Flat
    assert abs(result["realized_pnl"] - ((300 - 150) * 2 - 0.6)) < 1e-9  # 盈亏 = 价差×量 - 卖出费 | PnL = spread × qty - sell fee
    assert abs(result["total_fee"] - 0.9) < 1e-9  # 手续费合计（含买入）| Total fees include buys
    assert result["fees_complete"] is True  # 手续费口径完整 | Fees are quote-denominated
    assert result["buys"] == 2 and result["sells"] == 1  # 笔数正确 | Counts are right


def test_replay_trades_flags_truncated_window() -> None:  # 卖出超过买入 → 窗口截断 | Sells exceed buys -> truncated window
    """没有对应买入的卖出说明成交窗口被截断，盈亏不可信 | A sell without a matching buy means the window is truncated."""
    result = replay_trades([_trade(300, 2, is_buyer=False, fee=0.6)], "USDT")  # 只有卖出 | Only a sell
    assert result["complete"] is False  # 标记不完整 | Flagged incomplete
    assert result["long_qty"] == 0  # 持仓不会为负 | The position never goes negative


def test_replay_trades_ignores_foreign_commission_asset() -> None:  # 非计价币手续费不计入 | Foreign-currency fees excluded
    """以 BNB 支付的手续费不能直接累加到 USDT 口径 | A BNB fee cannot be summed into the USDT total."""
    result = replay_trades([_trade(100, 1, is_buyer=True, fee=0.001, asset="BNB")], "USDT")  # BNB 手续费 | BNB fee
    assert result["total_fee"] == 0.0  # USDT 口径为 0 | Zero in the quote currency
    assert result["fees_complete"] is False  # 手续费口径不完整 | Fees are incomplete


def test_stale_open_orders_filters_by_age() -> None:  # 超期挂单识别 | Stale order detection
    """只有存活超过阈值的挂单会被返回，缺时间戳的保守视为超期 | Only orders older than the threshold are returned."""
    orders = [  # 三条挂单 | Three orders
        {"orderId": 1, "time": 1_000_000},  # 很旧 | Very old
        {"orderId": 2, "time": 1_059_000},  # 刚创建 | Just created
        {"orderId": 3},  # 缺时间戳 | Missing timestamp
    ]
    stale = stale_open_orders(orders, max_age_seconds=60, now_ms=1_060_000)  # 60 秒阈值 | 60-second threshold
    assert [item["orderId"] for item in stale] == [1, 3]  # 只返回 1 和 3 | Only 1 and 3


def test_ledger_reconcile_corrects_all_fields() -> None:  # 账本四类状态校正 | Ledger four-field correction
    """现金、多头、空头、手续费、已实现盈亏一次性校正并返回差异 | Cash, long, short, fee and PnL are corrected together."""
    ledger = AccountLedger(10_000.0, fee_rate=0.001)  # 构造账本 | Build the ledger
    ledger.execute(OrderRequest(side="buy", qty=1, signal_price=100), 100)  # 买 1 个 | Buy one unit
    ledger.execute(OrderRequest(side="sellshort", qty=2, signal_price=200), 200)  # 开空 2 个（模拟残留）| Open a residual short
    adjustments = ledger.reconcile(  # 全量校正 | Full correction
        cash=5_000.0,  # 真实现金 | Real cash
        long_qty=3.0,  # 真实多头 | Real long
        short_qty=0.0,  # 现货无空头 | No spot shorts
        total_fee=1.25,  # 真实手续费 | Real fee
        realized_pnl=-4.5,  # 复算盈亏 | Replayed PnL
        price=110.0,  # 最新价 | Latest price
    )
    assert ledger.cash == 5_000.0  # 现金已覆盖 | Cash overwritten
    assert ledger.long_qty == 3.0  # 多头已覆盖 | Long overwritten
    assert ledger.short_qty == 0.0 and ledger.short_proceeds == 0.0  # 空头清零且所得清零 | Short cleared
    assert ledger.total_fee == 1.25  # 手续费已覆盖 | Fee overwritten
    assert ledger.realized_pnl == -4.5  # 盈亏已覆盖 | PnL overwritten
    assert set(adjustments) == {"cash", "long_qty", "short_qty", "total_fee", "realized_pnl"}  # 差异项齐全 | All deltas reported


def test_ledger_reconcile_is_noop_when_consistent() -> None:  # 一致时无修正 | No-op when consistent
    """真实状态与账本一致时不产生任何修正项 | A consistent account produces no adjustments."""
    ledger = AccountLedger(1_000.0)  # 新账本 | Fresh ledger
    assert ledger.reconcile(cash=1_000.0, long_qty=0.0, short_qty=0.0, total_fee=0.0, realized_pnl=0.0) == {}  # 空差异 | No deltas


def test_order_fill_summary_uses_real_fee() -> None:  # 真实手续费解析 | Real fee parsing
    """从 fills 里解析成交量、均价与手续费 | Parse quantity, price and fee from fills."""
    result = {  # 下单响应 | Placement response
        "status": "FILLED",  # 终态 | Terminal
        "executedQty": "2",  # 成交量 | Executed quantity
        "cummulativeQuoteQty": "300",  # 成交额 | Notional
        "fills": [  # 成交明细 | Fill details
            {"qty": "1", "price": "150", "commission": "0.15", "commissionAsset": "USDT"},  # 第一笔 | First fill
            {"qty": "1", "price": "150", "commission": "0.15", "commissionAsset": "USDT"},  # 第二笔 | Second fill
        ],
    }
    summary = order_fill_summary(result, fallback_qty=1.0, fallback_price=1.0, quote_asset="USDT")  # 解析 | Parse
    assert summary["qty"] == 2  # 真实成交量 | True quantity
    assert summary["price"] == 150  # 成交均价 | Average price
    assert abs(summary["fee"] - 0.3) < 1e-12  # 手续费合计 | Total fee
    assert summary["fee_known"] is True  # 手续费可信 | Fee is trustworthy


def test_order_fill_summary_marks_bnb_fee_unknown() -> None:  # BNB 手续费不可信 | BNB fee is not trustworthy
    """手续费以 BNB 计价时标记为不可信，调用方回退到费率估算 | A BNB fee is flagged so the caller falls back to the rate."""
    result = {  # 下单响应 | Placement response
        "executedQty": "1",  # 成交量 | Quantity
        "cummulativeQuoteQty": "100",  # 成交额 | Notional
        "fills": [{"qty": "1", "price": "100", "commission": "0.0001", "commissionAsset": "BNB"}],  # BNB 手续费 | BNB fee
    }
    summary = order_fill_summary(result, quote_asset="USDT")  # 解析 | Parse
    assert summary["fee_known"] is False  # 标记不可信 | Flagged untrustworthy
    assert summary["fee"] == 0.0  # USDT 口径为 0 | Zero in the quote currency
    assert summary["fee_assets"] == ["BNB"]  # 记录币种 | Currency recorded


def test_sum_trade_commission_filters_asset() -> None:  # 成交流水手续费汇总 | Commission summing
    """只累加计价币种的手续费 | Only the quote currency is summed."""
    trades = [  # 两条成交 | Two fills
        {"commission": "0.2", "commissionAsset": "USDT"},  # USDT | USDT
        {"commission": "0.1", "commissionAsset": "BNB"},  # BNB | BNB
    ]
    assert abs(sum_trade_commission(trades, "USDT") - 0.2) < 1e-12  # 只算 USDT | Only USDT counted
    assert abs(sum_trade_commission(trades, "") - 0.3) < 1e-12  # 不筛选时全算 | Sum all when unfiltered
