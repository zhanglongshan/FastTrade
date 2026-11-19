"""实盘对账的纯函数：成交复算、挂单识别 | Pure helpers for live reconciliation: trade replay and stale orders.

目的 | Purpose:
    对账需要做两件"从交易所数据反推账本"的事：用成交明细复算累计手续费与已实现
    盈亏、识别长期未成交的挂单。这两件事都是纯计算，抽成无 IO 的函数后可以脱离
    网络单测，也便于审计人员复现结果。
    Reconciliation must derive ledger state from exchange data: replay the trade list to
    recompute cumulative fees and realized PnL, and detect stale open orders. Both are pure
    computations, extracted here so they can be unit-tested without a network and audited
    reproducibly.

主要流程 | Main flow:
    myTrades → 按时间排序 → 逐笔维护"持仓量/均价成本" → 汇总手续费与已实现盈亏。
    myTrades -> sort by time -> maintain quantity/average cost per fill -> aggregate fees and PnL.

口径 | Accounting convention:
    与 :class:`~app.modules.quant.execution.base.AccountLedger` 完全一致：
    买入只增加成本（手续费只减现金，不进盈亏），卖出把本笔手续费计入已实现盈亏。
    Identical to AccountLedger: buys only extend the cost basis (their fee reduces cash but
    not PnL), sells charge their own fee against realized PnL.
"""

from __future__ import annotations  # 前向引用类型标注 | Forward-referenced annotations

import time  # 时间戳（挂单年龄）| Timestamps for order age
from typing import Any  # 通用类型 | Loose typing


def _to_float(value: Any, default: float = 0.0) -> float:  # 安全转 float | Safely convert to float
    """把任意字段转成 float，失败返回默认值 | Convert any field to float, returning a default on failure.

    参数 | Parameters:
        value (Any): 原始字段 | Raw field.
        default (float): 失败时的默认值 | Default on failure.

    返回 | Returns:
        float: 转换结果 | Converted value.
    """
    try:  # 币安字段常为字符串，且可能为空 | Binance fields are often strings and may be empty
        return float(value)  # 直接转换 | Convert directly
    except (TypeError, ValueError):  # 转换失败时回退 | Fall back on failure
        return default  # 返回默认值 | Return the default


def replay_trades(  # 成交明细复算持仓与盈亏 | Replay trades into position and PnL
    trades: list[dict[str, Any]] | None,  # myTrades 返回的成交列表 | Trade list from myTrades
    quote_asset: str = "",  # 计价资产（识别手续费币种）| Quote asset (fee currency)
) -> dict[str, Any]:  # 返回复算结果 | Return the replay result
    """用成交明细复算多头持仓、累计手续费与已实现盈亏 | Recompute the long position, fees and realized PnL from fills.

    目的 | Purpose:
        本地账本可能因崩溃、丢单或并发写入而与交易所错位；成交明细是交易所侧的
        权威记录，用它复算可以把 ``total_fee`` 与 ``realized_pnl`` 拉回真实值。
        The local ledger can drift after a crash, a lost order or concurrent writes; the trade
        list is the exchange's authoritative record and replaying it restores ``total_fee``
        and ``realized_pnl``.

    参数 | Parameters:
        trades (list[dict] | None): 成交明细（字段 qty/price/isBuyer/commission/...）| Trade fills.
        quote_asset (str): 计价资产，用于只累加该币种的手续费 | Quote asset used to filter fees.

    返回 | Returns:
        dict[str, Any]: 含 long_qty/realized_pnl/total_fee/complete/fees_complete/buys/sells | Replay outcome.
            - ``complete`` 为 False 表示卖出量超过买入量（成交窗口被截断），盈亏与手续费都不可信。
            - ``fees_complete`` 为 False 表示存在以 BNB 等非计价币支付的手续费，合计会偏小。
            - ``complete=False`` means sells exceeded buys (truncated window), so PnL and fees are
              unreliable. ``fees_complete=False`` means some fee was paid in a foreign currency
              (e.g. BNB) and the total would be understated.

    流程 | Flow:
        按 (time, id) 排序 → 买入更新均价成本 → 卖出结算盈亏 → 累计手续费 → 标注完整性。
        Sort by (time, id) → buys extend average cost → sells settle PnL → accumulate fees →
        flag completeness.
    """
    items = [item for item in (trades or []) if isinstance(item, dict)]  # 过滤异常项 | Drop malformed entries
    items.sort(key=lambda item: (_to_float(item.get("time")), _to_float(item.get("id"))))  # 按时间与序号排序 | Sort by time then id
    qty = 0.0  # 当前持仓量 | Current quantity
    cost = 0.0  # 当前均价成本 | Current average cost
    realized = 0.0  # 已实现盈亏 | Realized PnL
    fee_total = 0.0  # 累计手续费（计价币）| Cumulative fee in the quote currency
    buys = 0  # 买入笔数 | Buy count
    sells = 0  # 卖出笔数 | Sell count
    oversold = False  # 是否出现卖出超过持仓 | Whether sells exceeded the position
    foreign_fee = False  # 是否存在非计价币手续费（如 BNB）| Whether a non-quote fee (e.g. BNB) appeared
    for item in items:  # 逐笔复算 | Replay fill by fill
        price = _to_float(item.get("price"))  # 成交价 | Fill price
        fill_qty = _to_float(item.get("qty"))  # 成交量 | Fill quantity
        if price <= 0 or fill_qty <= 0:  # 无效成交跳过 | Skip invalid fills
            continue  # 继续下一笔 | Continue
        commission = _to_float(item.get("commission"))  # 本笔手续费 | Fee of this fill
        asset = str(item.get("commissionAsset") or "")  # 手续费币种 | Fee currency
        quote_fee = not quote_asset or asset.upper() == quote_asset.upper()  # 是否为计价币手续费 | Whether the fee is in the quote currency
        if quote_asset and not quote_fee and commission > 0:  # 以外币计费时无法直接相加 | Foreign fees cannot be summed directly
            foreign_fee = True  # 标记手续费不完整 | Mark fees as incomplete
        fee = commission if quote_fee else 0.0  # 只统计计价币手续费 | Count quote-denominated fees only
        fee_total += fee  # 累计手续费 | Accumulate the fee
        if bool(item.get("isBuyer")):  # 买入：扩展成本基础 | Buy: extend the cost basis
            cost = (cost * qty + price * fill_qty) / (qty + fill_qty)  # 成交金额加权均价 | Weighted average cost
            qty += fill_qty  # 增加持仓 | Increase the position
            buys += 1  # 计数 | Count
        else:  # 卖出：结算盈亏 | Sell: settle PnL
            sold = min(fill_qty, qty) if qty > 0 else 0.0  # 可结算数量不超过持仓 | Settle no more than held
            if fill_qty > qty + 1e-12:  # 卖出量超过持仓说明窗口被截断 | Selling more than held means the window is truncated
                oversold = True  # 标记不完整 | Mark incomplete
            realized += (price - cost) * sold - fee  # 与账本一致的平仓盈亏 | Closing PnL consistent with the ledger
            qty = max(0.0, qty - fill_qty)  # 扣减持仓 | Reduce the position
            if qty <= 1e-12:  # 持仓归零 | Flat again
                qty = 0.0  # 归零 | Zero out
                cost = 0.0  # 清零成本 | Reset the cost
            sells += 1  # 计数 | Count
    return {  # 返回复算结果 | Return the outcome
        "long_qty": qty,  # 复算持仓 | Replayed position
        "realized_pnl": realized,  # 复算已实现盈亏 | Replayed realized PnL
        "total_fee": fee_total,  # 复算累计手续费 | Replayed cumulative fee
        "complete": not oversold,  # 持仓窗口是否完整 | Whether the position window is complete
        "fees_complete": not foreign_fee,  # 手续费是否全部以计价币计 | Whether every fee is quote-denominated
        "buys": buys,  # 买入笔数 | Buy count
        "sells": sells,  # 卖出笔数 | Sell count
    }


def stale_open_orders(  # 识别长期未成交的挂单 | Detect stale open orders
    orders: list[dict[str, Any]] | None,  # openOrders 返回的挂单 | Open orders
    max_age_seconds: float = 60.0,  # 允许的最大存活秒数 | Maximum allowed age in seconds
    now_ms: int | None = None,  # 当前毫秒时间（测试可注入）| Current time in ms (injectable for tests)
) -> list[dict[str, Any]]:  # 返回超期挂单 | Return the stale orders
    """筛选存活时间超过阈值的挂单 | Filter open orders older than the threshold.

    目的 | Purpose:
        市价单理论上立即成交，出现的挂单通常是"回查超时/进程崩溃"留下的孤儿单，
        它们占用资金并可能在之后成交，对账时必须撤销并告警。
        Market orders fill immediately, so any open order is usually an orphan left by a
        timeout or a crash; it locks capital and may still fill, so reconciliation must cancel
        it and raise a warning.

    参数 | Parameters:
        orders (list[dict] | None): 挂单列表 | Open-order list.
        max_age_seconds (float): 允许的存活秒数 | Allowed age in seconds.
        now_ms (int | None): 当前毫秒时间，None 取系统时间 | Current epoch ms, defaults to now.

    返回 | Returns:
        list[dict[str, Any]]: 超期挂单（原样返回，便于调用方撤单）| Stale orders, returned as-is for cancelling.

    流程 | Flow:
        取当前时间 → 逐单计算 (now - timeCreated) → 超过阈值则收集。
        Read now → compute (now - timeCreated) per order → collect those past the threshold.
    """
    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)  # 当前毫秒 | Current milliseconds
    threshold_ms = max(0.0, float(max_age_seconds)) * 1000  # 阈值换算成毫秒 | Convert the threshold to ms
    stale: list[dict[str, Any]] = []  # 收集超期挂单 | Collect stale orders
    for item in orders or []:  # 遍历挂单 | Iterate over open orders
        if not isinstance(item, dict):  # 跳过异常项 | Skip malformed entries
            continue  # 继续下一单 | Continue
        created = _to_float(item.get("time") or item.get("timeCreated") or item.get("transactTime"))  # 下单时间 | Creation time
        if created <= 0:  # 缺少时间信息时保守视为超期 | Treat missing timestamps as stale
            stale.append(item)  # 收集该单 | Collect it
            continue  # 继续下一单 | Continue
        if current_ms - created >= threshold_ms:  # 超过阈值 | Past the threshold
            stale.append(item)  # 收集该单 | Collect it
    return stale  # 返回超期挂单 | Return the stale orders


def reconcile_summary(  # 汇总对账结果 | Summarize a reconciliation run
    adjustments: dict[str, float] | None,  # 账本修正项 | Ledger adjustments
    replayed: dict[str, Any] | None = None,  # 成交复算结果 | Trade replay outcome
    cancelled: int = 0,  # 撤销的挂单数 | Number of cancelled orders
) -> dict[str, Any]:  # 返回结构化摘要 | Return a structured summary
    """把账本修正、成交复算与撤单数量合并成一条可落日志的摘要 | Merge adjustments, replay and cancels into one loggable summary.

    目的 | Purpose:
        对账日志需要一眼看出"改了什么、依据是什么"，而不是散落在多条日志里。
        A reconciliation log must show what changed and on what evidence in one place.

    参数 | Parameters:
        adjustments (dict | None): ``AccountLedger.reconcile`` 的返回值 | Return of ``AccountLedger.reconcile``.
        replayed (dict | None): ``replay_trades`` 的返回值 | Return of ``replay_trades``.
        cancelled (int): 本次撤销的挂单数 | Orders cancelled in this run.

    返回 | Returns:
        dict[str, Any]: 含 changed/adjustments/replay/cancelled | Summary with the key sections.
    """
    changes = dict(adjustments or {})  # 复制修正项 | Copy the adjustments
    return {  # 构造摘要 | Build the summary
        "changed": bool(changes) or bool(cancelled),  # 是否有任何变化 | Whether anything changed
        "adjustments": changes,  # 账本修正 | Ledger adjustments
        "replay": dict(replayed or {}),  # 成交复算 | Trade replay
        "cancelled": int(cancelled),  # 撤销挂单数 | Cancelled count
    }
