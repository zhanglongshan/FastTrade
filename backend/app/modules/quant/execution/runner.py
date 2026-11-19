"""统一策略运行时：一次 tick 拉行情、跑策略、执行订单、产出结果 | Unified strategy runtime: one tick fetches data, runs strategy, executes orders, produces results.

目的：为回测之外的模拟盘/实盘提供滚动 K 线窗口驱动的运行时，把"拉行情 → 跑策略 →
撮合订单 → 产出结果"封装成一次 ``run_tick`` 调用，三种模式共享该函数，差异仅在
``executor``。
Purpose: Provide a rolling-candle-window runtime for paper/live modes, wrapping
"fetch data → run strategy → fill orders → produce result" into one ``run_tick`` call;
all modes share it and differ only in ``executor``.

主要流程 | Main flow:
- 模拟盘/实盘没有"完整数据集"，每次 tick 拉取最新 N 根 K 线，把最后一根当作当前
  K 线，调用一次策略回调 | Each tick fetches the latest N candles and treats the last as current.
- 策略通过统一的 :class:`~.api.StrategyAPI` 下单，订单由 :class:`Executor` 即时撮合
  | Strategy orders via StrategyAPI, filled immediately by the Executor.
- 执行器差异：PaperExecutor 本地虚拟撮合；LiveExecutor 调币安下单后再记账（dry_run
  时仍走本地撮合）| PaperExecutor fills locally; LiveExecutor calls Binance then books.
"""

import asyncio  # 导入异步 IO 库 | Import asyncio for thread offloading
from dataclasses import dataclass, field  # 导入 dataclass 工具 | Import dataclass helpers
from datetime import UTC, datetime  # 导入 UTC 时区与 datetime 类 | Import UTC timezone and datetime class
from typing import Any, Protocol  # 导入 Any 与 Protocol 类型 | Import Any and Protocol typing

from app.core.exceptions import CustomException  # 导入自定义异常 | Import CustomException for error signaling

from ..strategy.api import Candle, Strategy  # 导入 K 线与策略类型 | Import Candle and Strategy types
from ..strategy.loader import load_strategy  # 导入策略加载器 | Import strategy loader
from .api import StrategyAPI  # 导入统一策略交易接口 | Import unified StrategyAPI
from .base import AccountLedger, Fill, OrderRequest, RunMode  # 导入账本/成交/订单请求/运行模式 | Import ledger, fill, order request, run mode
from .exchange import fetch_klines_async  # 导入异步行情拉取函数 | Import async kline fetcher


class Executor(Protocol):  # 执行器协议 | Executor protocol
    """执行器协议：把订单请求变成成交 | Executor protocol: turn order requests into fills.

    定义执行器必须实现的方法签名，供 :func:`run_tick` 按协议调用，屏蔽具体撮合差异。
    Defines the methods an executor must implement so ``run_tick`` can call it uniformly.

    关键方法 | Key methods:
    - execute: 异步执行一笔订单 | Asynchronously execute one order.
    - snapshot: 返回可持久化状态 | Return persistable state.
    """

    async def execute(self, order: OrderRequest, price: float) -> Fill | None:  # 异步执行订单 | Execute an order asynchronously
        """执行一笔订单，返回成交记录；被拒绝时返回 None | Execute an order, return Fill or None when rejected.

        参数 | Parameters:
        - order (OrderRequest): 下单请求 | The order request.
        - price (float): 执行参考价格 | Reference execution price.

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
        }


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

    返回 | Returns:
    - TickResult: 本次 tick 价格、净值、持仓、成交与日志 | Price, equity, positions, fills and logs.

    流程 | Flow:
    1) 解析模式/时间/策略 → 2) 准备行情窗口 → 3) 构造统一接口并调用策略 →
    4) 撮合排队订单 → 返回 TickResult。
    Resolve mode/time/strategy → prepare window → build API and run strategy →
    fill queued orders → return TickResult.
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

    last = window[-1]  # 取最后一根作为当前 K 线 | Take last candle as current
    price = last.close  # 用当前 K 线收盘价作为执行参考价 | Use current candle close as reference price

    # 2) 构造统一策略接口
    # 2) Build the unified strategy API
    pending: list[OrderRequest] = []  # 初始化待撮合订单队列 | Init queue of pending orders

    def submit(order: OrderRequest) -> bool:  # 策略下单入口 | Strategy order entry point
        """策略下单入口：排队，tick 末按最新价统一撮合 | Strategy order entry: queue order, fill all at tick end using latest price.

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
        candles=window,  # 传入行情窗口 | Pass candle window
        index=len(window) - 1,  # 设置当前下标为最后一根 | Set index to last candle
        timestamp=now_iso,  # 传入时间标签 | Pass timestamp
        allow_short=allow_short,  # 传入是否允许做空 | Pass shorting flag
    )
    api.symbol = resolved_symbol  # 将规范化交易对写入接口 | Write normalized symbol to API
    api.set_live_price(price)  # 注入实时价供下单与净值使用 | Inject live price for ordering/equity

    # 3) 调用策略（与回测相同的两种模式）
    # 3) Call the strategy (same two modes as backtest)
    index = len(window) - 1  # 当前 K 线下标 | Current candle index
    warmup = max(0, int(getattr(info.cls, "startup_candle_count", 0) or 0))  # 策略预热所需 K 线数 | Warmup candles required by strategy
    try:  # 捕获用户策略异常并统一包装 | Catch user strategy errors and wrap uniformly
        if index >= warmup:  # 仅在过了预热期后运行策略 | Run strategy only after warmup period
            if info.mode == "event":  # 事件驱动模式分支 | Event-driven mode branch
                strategy.on_bar(api, window, index)  # 调用策略 on_bar 回调 | Call strategy on_bar callback
            else:  # 信号驱动模式分支 | Signal-driven mode branch
                metadata = {"pair": symbol, "interval": interval}  # 构造策略元数据 | Build strategy metadata
                entries = strategy.populate_entry_trend(window, metadata) or []  # 计算入场信号序列 | Compute entry signals
                exits = strategy.populate_exit_trend(window, metadata) or []  # 计算出场信号序列 | Compute exit signals
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

    # 4) 撮合排队的订单
    # 4) Fill queued orders
    fills: list[Fill] = []  # 初始化成交记录列表 | Init fill record list
    for order in pending:  # 遍历待撮合队列 | Iterate over pending orders
        try:  # 单笔订单失败不影响其余订单 | One order failure must not block others
            fill = await executor.execute(order, price)  # 调用执行器异步撮合该订单 | Await executor to fill this order
            if fill is not None:  # 成交成功时记录 | Record when fill succeeded
                fills.append(fill)  # 追加成交记录 | Append fill record
        except CustomException as exc:  # 捕获自定义异常并记录日志 | Catch CustomException and log
            api.log(f"订单执行失败({order.side})：{exc}")  # 记录执行失败日志 | Log execution-failure message
        except Exception as exc:  # noqa: BLE001  # 捕获其他异常并记录日志 | Catch other exceptions and log
            api.log(f"订单执行异常({order.side})：{type(exc).__name__}: {exc}")  # 记录执行异常日志 | Log execution-error message

    return TickResult(  # 构造并返回本次 tick 结果 | Build and return the tick result
        mode=mode_value,  # 填充运行模式 | Fill run mode
        symbol=symbol,  # 填充原始交易对 | Fill original symbol
        interval=interval,  # 填充 K 线周期 | Fill candle interval
        strategy=info.name,  # 填充策略名称 | Fill strategy name
        price=round(price, 8),  # 填充保留 8 位小数的最新价 | Fill price rounded to 8 decimals
        equity=round(ledger.equity_at(price), 6),  # 填充保留 6 位小数的净值 | Fill equity rounded to 6 decimals
        cash=round(ledger.cash, 6),  # 填充保留 6 位小数的现金 | Fill cash rounded to 6 decimals
        position={k: round(v, 8) for k, v in ledger.position().items()},  # 将持仓各字段保留 8 位小数 | Round position values to 8 decimals
        fills=[fill.__dict__ for fill in fills],  # 将成交对象转为字典列表 | Convert fill objects to dict list
        logs=api.logs,  # 填充策略日志 | Fill strategy logs
        rejected=api.rejected_orders,  # 填充被拒订单列表 | Fill rejected orders
        tick_time=now_iso,  # 填充 tick 时间标签 | Fill tick timestamp
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

    async def execute(self, order: OrderRequest, price: float) -> Fill | None:  # 本地虚拟撮合 | Fill virtually on local ledger
        """本地虚拟撮合 | Fill virtually on the local ledger.

        参数 | Parameters:
        - order (OrderRequest): 下单请求 | The order request.
        - price (float): 最新价 | Latest price.

        返回 | Returns:
        - Fill | None: 成交记录或未成交 | Fill record or None.
        """
        return self.ledger.execute(order, price, timestamp=datetime.now(UTC).isoformat())  # 委托账本按当前时间撮合 | Delegate to ledger with current timestamp

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
        if not dry_run:  # 仅非 dry_run 时创建真实下单客户端 | Create real client only when not dry-run
            from .exchange import BinanceOrderClient  # 延迟导入下单客户端 | Lazily import order client

            self._client = BinanceOrderClient(api_key, api_secret)  # 用密钥构造下单客户端 | Build order client with credentials

    async def execute(self, order: OrderRequest, price: float) -> Fill | None:  # 执行订单 | Execute an order
        """执行订单：dry_run 走本地撮合，真实模式先下单再记账 | Execute order: dry-run fills locally, real mode orders then books.

        参数 | Parameters:
        - order (OrderRequest): 下单请求 | The order request.
        - price (float): 最新价/回落价 | Latest/fallback price.

        返回 | Returns:
        - Fill | None: 成交记录或未成交 | Fill record or None.
        """
        # 现货不支持做空：即便账本允许，实盘真实下单也必须拒绝，避免误操作
        # Spot has no shorting: even if ledger allows, real live orders must reject to avoid mistakes.
        if not self.dry_run and order.side in {"sellshort", "buycover"}:  # 真实模式下拒绝做空/平空 | Reject short/cover in real mode
            raise CustomException(msg="币安现货不支持做空，请将策略改为仅做多，或使用模拟盘验证做空逻辑")  # 抛出不支持做空异常 | Raise shorting-unsupported error

        if not self.dry_run and self._client is not None:  # 真实模式且客户端就绪时走真实下单 | Go real when not dry-run and client ready
            symbol = order.symbol.replace("/", "").upper()  # 规范化交易对 | Normalize symbol
            result = await asyncio.to_thread(  # 将同步下单放到线程中执行 | Run synchronous order placement in a thread
                self._client.place_market_order,  # 调用下单客户端方法 | Call client's order method
                symbol,  # 交易对参数 | Symbol argument
                order.side,  # 方向参数 | Side argument
                order.qty,  # 数量参数 | Qty argument
                client_order_id=order.client_order_id,  # 自定义订单号 | Custom order id
            )
            # 用实际成交均价（fills 加权）作为记账价，缺失时回落最新价
            # Use weighted average fill price for booking, falling back to latest price.
            executed_price = _extract_avg_price(result) or price  # 提取成交均价，缺失回落参考价 | Extract avg price, fallback to reference
            executed_qty = float(result.get("executedQty") or order.qty) or order.qty  # 读取实际成交量，缺失回落请求量 | Read executed qty, fallback to requested
            real_order = OrderRequest(  # 用实际成交结果构造真实订单请求 | Build real order request from execution result
                side=order.side,  # 填充方向 | Fill side
                qty=executed_qty,  # 填充实际成交量 | Fill executed qty
                signal_price=order.signal_price,  # 填充信号价格 | Fill signal price
                reason=order.reason,  # 填充下单原因 | Fill reason
                symbol=symbol,  # 填充规范化交易对 | Fill normalized symbol
            )
            return self.ledger.execute(real_order, executed_price, timestamp=datetime.now(UTC).isoformat())  # 按实际成交价记账并返回 | Book at executed price and return

        return self.ledger.execute(order, price, timestamp=datetime.now(UTC).isoformat())  # dry_run/无客户端时本地虚拟撮合 | Fill locally in dry-run or no-client case

    def snapshot(self) -> dict[str, Any]:  # 返回账本快照 | Return ledger snapshot
        """返回账本快照 | Return the ledger snapshot.

        返回 | Returns:
        - dict[str, Any]: 账本快照字典 | Ledger snapshot dict.
        """
        return self.ledger.snapshot()  # 委托账本导出快照 | Delegate to ledger snapshot


def _extract_avg_price(result: dict[str, Any]) -> float:  # 从下单返回中提取成交均价 | Extract average fill price from order result
    """从币安下单返回中提取成交均价 | Extract the average fill price from a Binance order result.

    参数 | Parameters:
    - result (dict[str, Any]): 币安下单返回结果 | Binance order result.

    返回 | Returns:
    - float: 成交量加权均价；无有效 fills 时返回 0.0 | Volume-weighted average price, or 0.0 when no valid fills.
    """
    fills = result.get("fills")  # 读取成交明细列表 | Read the fills list
    if not isinstance(fills, list) or not fills:  # 明细缺失或为空时返回 0 | Return 0 when fills missing or empty
        return 0.0  # 返回 0 表示无均价 | Return 0 to signal no average
    total_qty = 0.0  # 初始化累计成交量 | Init accumulated quantity
    total_value = 0.0  # 初始化累计成交金额 | Init accumulated value
    for item in fills:  # 遍历每笔成交明细 | Iterate over each fill item
        try:  # 容错处理字段类型异常 | Guard against malformed fields
            qty = float(item.get("qty") or 0)  # 读取该笔成交量并转 float | Read fill qty as float
            px = float(item.get("price") or 0)  # 读取该笔成交价并转 float | Read fill price as float
        except (TypeError, ValueError):  # 字段无法转换时跳过该笔 | Skip this fill when conversion fails
            continue  # 继续处理下一笔 | Continue to next fill
        total_qty += qty  # 累加成交量 | Accumulate quantity
        total_value += qty * px  # 累加成交金额 | Accumulate value
    return total_value / total_qty if total_qty > 0 else 0.0  # 返回加权均价，总量为 0 时返回 0 | Return VWAP, or 0 when total qty is 0
