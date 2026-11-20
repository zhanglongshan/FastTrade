"""币安行情与下单网关 | Binance market-data and order gateway.

目的：封装与币安交互的两类 REST 调用——公开行情与签名下单，使模拟盘与实盘
共用同一行情来源，并让实盘下单受 ``dry_run`` 开关与密钥安全策略约束。
Purpose: Wrap two kinds of Binance REST calls—public market data and signed order
placement—so paper/live share one market-data source, while live orders are gated by
the ``dry_run`` switch and key-security policy.

主要流程 | Main flow:
- 行情：公开 REST ``/api/v3/klines``，与数据下载模块共用同一接口，模拟盘与实盘
  都从这里拉取最新 K 线喂给策略 | Public klines endpoint shared with the data module.
- 下单：签名 REST ``/api/v3/order``（HMAC-SHA256），仅实盘模式使用，且受 ``dry_run``
  约束——默认只撮合不下单 | Signed order endpoint, live only and dry_run-gated.
- 安全：密钥经 CryptoUtil 加密落库，运行时才解密到内存，不写入任何日志。
  Secrets are encrypted at rest and only decrypted in memory, never logged.
"""

import hashlib  # 导入哈希库 | Import hashing library for HMAC-SHA256 signing
import hmac  # 导入 HMAC 模块 | Import HMAC module for signed requests
import math  # 导入数学库（数量按步长向下取整）| Import math for step-rounded quantities
import time  # 导入时间模块 | Import time module for request timestamps
from typing import Any  # 导入 Any 类型 | Import Any for generic dict/value typing
from urllib.parse import urlencode  # 导入 URL 编码函数 | Import urlencode for building query strings

import httpx  # 导入 httpx 客户端库 | Import httpx for synchronous HTTP requests

from app.core.exceptions import CustomException  # 导入自定义异常 | Import CustomException for error signaling
from app.core.logger import logger  # 导入日志对象 | Import logger for error logging

from ..data.binance_download import BINANCE_API_KLINES_URL, row_from_raw  # 导入行情 URL 与行解析函数 | Import klines URL and row parser
from ..strategy.api import Candle  # 导入 Candle K 线类型 | Import Candle candle data type

# 实盘下单接口；与行情接口同域，便于统一出口白名单审计
# Live order endpoint; same domain as market data for unified egress allow-list auditing.
BINANCE_ORDER_URL = "https://api.binance.com/api/v3/order"  # 币安实盘下单地址常量 | Binance live order URL constant
BINANCE_ACCOUNT_URL = "https://api.binance.com/api/v3/account"  # 账户信息接口（对账用）| Account endpoint used for reconciliation
BINANCE_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"  # 交易规则接口（步长/最小名义额）| Exchange rules endpoint (step size / min notional)
BINANCE_OPEN_ORDERS_URL = "https://api.binance.com/api/v3/openOrders"  # 当前挂单（对账用）| Current open orders (reconciliation)
BINANCE_MY_TRADES_URL = "https://api.binance.com/api/v3/myTrades"  # 成交明细（手续费回填/盈亏复核）| Trade list (fee backfill / PnL audit)
DEFAULT_KLINE_LIMIT = 300  # 默认拉取 K 线根数 | Default number of candles to fetch
ORDER_FINAL_STATUS = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}  # 订单终态集合 | Terminal order statuses


def _empty_symbol_rules() -> dict[str, float | str]:  # 空规则：全部按"无约束"处理 | Empty rules: everything unconstrained
    """构造"无约束"的交易对规则 | Build an unconstrained rule set.

    返回 | Returns:
        dict[str, float | str]: 所有过滤器为 0 / 空串 | All filters zeroed or blank.
    """
    return {  # 默认规则 | Default rules
        "base_asset": "",  # 基础资产 | Base asset
        "quote_asset": "",  # 计价资产 | Quote asset
        "tick_size": 0.0,  # 价格最小变动 | Price tick
        "step_size": 0.0,  # 限价/默认数量步长 | Limit/default quantity step
        "min_qty": 0.0,  # 最小数量 | Minimum quantity
        "max_qty": 0.0,  # 最大数量 | Maximum quantity
        "market_step_size": 0.0,  # 市价单数量步长 | Market-order quantity step
        "market_min_qty": 0.0,  # 市价单最小数量 | Market-order minimum quantity
        "market_max_qty": 0.0,  # 市价单最大数量 | Market-order maximum quantity
        "min_notional": 0.0,  # 最小名义额 | Minimum notional
        "max_notional": 0.0,  # 最大名义额（0 表示无限制）| Maximum notional (0 means unlimited)
    }


def _parse_symbol_item(item: dict[str, Any]) -> dict[str, float | str]:  # 解析单个交易对的过滤器 | Parse one symbol's filters
    """把 exchangeInfo 的单个交易对条目解析成规则字典 | Turn one exchangeInfo symbol entry into a rule dict.

    参数 | Parameters:
        item (dict[str, Any]): exchangeInfo.symbols 中的一项 | One entry of exchangeInfo.symbols.

    返回 | Returns:
        dict[str, float | str]: 与 :func:`_empty_symbol_rules` 同结构的规则 | Rules shaped like the empty rule set.
    """
    info = _empty_symbol_rules()  # 先取无约束默认值 | Start from the unconstrained defaults
    info["base_asset"] = str(item.get("baseAsset") or "")  # 基础资产 | Base asset
    info["quote_asset"] = str(item.get("quoteAsset") or "")  # 计价资产 | Quote asset
    for rule in item.get("filters", []) or []:  # 遍历过滤器 | Iterate over filters
        kind = str(rule.get("filterType") or "")  # 过滤器类型 | Filter type
        if kind == "PRICE_FILTER":  # 价格精度 | Price precision
            info["tick_size"] = float(rule.get("tickSize") or 0.0)  # 记录最小价格变动 | Record the tick size
        elif kind == "LOT_SIZE":  # 默认数量规则 | Default quantity rules
            info["step_size"] = float(rule.get("stepSize") or 0.0)  # 记录步长 | Record the step
            info["min_qty"] = float(rule.get("minQty") or 0.0)  # 记录最小数量 | Record the minimum
            info["max_qty"] = float(rule.get("maxQty") or 0.0)  # 记录最大数量 | Record the maximum
        elif kind == "MARKET_LOT_SIZE":  # 市价单专用数量规则 | Market-order quantity rules
            info["market_step_size"] = float(rule.get("stepSize") or 0.0)  # 市价步长 | Market step
            info["market_min_qty"] = float(rule.get("minQty") or 0.0)  # 市价最小量 | Market minimum
            info["market_max_qty"] = float(rule.get("maxQty") or 0.0)  # 市价最大量 | Market maximum
        elif kind in {"MIN_NOTIONAL", "NOTIONAL"}:  # 名义额限制（新旧两种过滤器名）| Notional filters (old and new names)
            info["min_notional"] = float(rule.get("minNotional") or rule.get("notional") or 0.0)  # 最小名义额 | Minimum notional
            info["max_notional"] = float(rule.get("maxNotional") or 0.0)  # 最大名义额 | Maximum notional
    return info  # 返回解析结果 | Return the parsed rules


_RULES_CACHE: dict[str, tuple[float, dict[str, float | str], bool]] = {}  # 交易对 -> (取到时刻, 规则, 是否成功) | Symbol -> (fetched_at, rules, ok)
_RULES_TTL_OK = 3600.0  # 成功结果缓存 1 小时（过滤器极少变动）| Cache successes for an hour
_RULES_TTL_FAIL = 60.0  # 失败结果只缓存 1 分钟，避免每秒重试 | Cache failures briefly to avoid hammering


async def symbol_rules(symbol: str) -> dict[str, float | str]:  # 异步获取交易对规则（带缓存）| Fetch symbol rules asynchronously with a cache
    """异步查询交易对过滤器规则，供模拟盘与实盘共同使用 | Fetch venue filters for both paper and live modes.

    目的 | Purpose:
        模拟盘此前完全不校验交易所规则，能成交实盘必被拒的碎单，两种模式结果不可比。
        这里提供一份带进程缓存的异步查询：命中缓存是纯内存读，不阻塞事件循环；
        失败时退化为"无约束"并短时缓存，避免把网络故障放大成交易中断。
        Paper mode previously ignored venue filters and filled dust the live account never could.
        This gives both modes one cached, asynchronous lookup: cache hits are pure memory reads,
        and failures degrade to "unconstrained" with a short cache so a network hiccup does not
        turn into a trading outage.

    参数 | Parameters:
        symbol (str): 交易对，如 BTC/USDT 或 BTCUSDT | Symbol such as BTC/USDT or BTCUSDT.

    返回 | Returns:
        dict[str, float | str]: 规则字典；失败时所有过滤器为 0 | Rule dict; all filters zero on failure.
    """
    key = str(symbol or "").replace("/", "").upper()  # 统一成交易所写法 | Normalize to the venue's form
    if not key:  # 空交易对直接返回空规则 | Empty symbol yields empty rules
        return _empty_symbol_rules()  # 无约束 | Unconstrained
    cached = _RULES_CACHE.get(key)  # 读缓存 | Read the cache
    now = time.monotonic()  # 单调时钟，避免系统时间跳变 | Monotonic clock, immune to wall-clock jumps
    if cached is not None:  # 命中缓存时按成功/失败区分 TTL | Distinguish TTL by outcome
        fetched_at, rules, ok = cached  # 解包 | Unpack
        if now - fetched_at < (_RULES_TTL_OK if ok else _RULES_TTL_FAIL):  # 未过期 | Still fresh
            return rules  # 直接返回 | Return the cached rules
    try:  # 查询失败不应中断交易 | A failed lookup must not stop trading
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "FastTradeQuant/1.0"}) as client:  # 异步客户端 | Async client
            response = await client.get(BINANCE_EXCHANGE_INFO_URL, params={"symbol": key})  # 请求规则 | Request the rules
            response.raise_for_status()  # 校验状态 | Check the status
            payload = response.json()  # 解析 JSON | Parse the JSON
        item = (payload.get("symbols") or [{}])[0]  # 取第一个交易对 | Take the first symbol entry
        rules = _parse_symbol_item(item)  # 解析过滤器 | Parse the filters
        _RULES_CACHE[key] = (now, rules, True)  # 缓存成功结果 | Cache the success
        return rules  # 返回规则 | Return the rules
    except (httpx.HTTPError, ValueError, IndexError) as exc:  # 网络/格式异常 | Network or shape error
        logger.warning(f"交易规则异步查询失败({symbol})：{type(exc).__name__}: {exc}")  # 记录告警 | Log a warning
        rules = _empty_symbol_rules()  # 退化为无约束 | Degrade to unconstrained
        _RULES_CACHE[key] = (now, rules, False)  # 短时缓存失败结果 | Cache the failure briefly
        return rules  # 返回空规则 | Return empty rules


def fetch_klines(  # 同步拉取最新 K 线 | Fetch latest candles synchronously
    symbol: str,  # 交易对，如 BTCUSDT | Trading symbol, e.g. BTCUSDT
    interval: str,  # K 线周期 | Candle interval
    *,  # 强制后续参数为关键字参数 | Force following params to be keyword-only
    limit: int = DEFAULT_KLINE_LIMIT,  # 拉取根数，至少 2 | Number of candles, at least 2
    client: httpx.AsyncClient | None = None,  # 可复用客户端，None 时临时创建 | Reusable client, create temp when None
) -> list[Candle]:  # 返回按时间升序的 K 线列表 | Return time-ascending candle list
    """拉取最新 K 线（同步执行，调用方用 asyncio.to_thread 包裹） | Fetch latest candles (synchronous; wrap with asyncio.to_thread).

    参数 | Parameters:
    - symbol (str): 交易对，如 BTCUSDT（无斜杠）| Trading symbol without slash.
    - interval (str): K 线周期 | Candle interval.
    - limit (int): 拉取根数，至少 2 | Number of candles, at least 2.
    - client (httpx.AsyncClient | None): 可复用的 httpx 客户端；为 None 时临时创建 | Reusable httpx client, temp when None.

    返回 | Returns:
    - list[Candle]: 按时间升序的 K 线 | Time-ascending candles.

    流程 | Flow:
    组装参数 → 选择/创建客户端 → GET 请求并校验状态 → 解析 JSON →
    关闭自建客户端 → 校验数据 → 转 Candle 并按日期排序返回。
    Build params → select/create client → GET and check status → parse JSON →
    close owned client → validate data → convert to Candle and sort by date.
    """
    params = {"symbol": symbol, "interval": interval, "limit": max(2, int(limit))}  # 组装请求参数，limit 至少为 2 | Build params, limit at least 2
    own_client = client is None  # 标记是否为请求临时创建客户端 | Flag whether a temporary client was created
    target = client or httpx.Client(timeout=20.0, headers={"User-Agent": "FastTradeQuant/1.0"})  # 复用传入客户端或新建带超时/UA 的客户端 | Reuse given client or create one
    try:  # 捕获网络/解析异常并统一抛自定义异常 | Catch network/parse errors and wrap in CustomException
        response = target.get(BINANCE_API_KLINES_URL, params=params)  # 发起 GET 行情请求 | Issue GET market-data request
        response.raise_for_status()  # HTTP 错误时抛异常 | Raise on HTTP error status
        payload = response.json()  # 解析响应 JSON | Parse response JSON
    except (httpx.HTTPError, ValueError) as exc:  # 捕获 HTTP 错误或 JSON 解析错误 | Catch HTTP or JSON decode errors
        raise CustomException(msg=f"币安行情获取失败：{exc}") from exc  # 包装为自定义异常并上抛 | Wrap and re-raise as CustomException
    finally:  # 无论成功失败都要清理自建客户端 | Always clean up owned client
        if own_client:  # 仅关闭本函数创建的客户端 | Only close the client created here
            target.close()  # type: ignore[union-attr]  # 关闭临时客户端释放连接 | Close temporary client to release connections

    if not isinstance(payload, list) or not payload:  # 校验返回为非空列表 | Validate payload is a non-empty list
        raise CustomException(msg="币安未返回 K 线数据")  # 数据为空时抛异常 | Raise when no candle data returned
    candles = [Candle(**row_from_raw(row)) for row in payload]  # 将每行原始数据解析为 Candle 对象 | Convert each raw row into a Candle
    return sorted(candles, key=lambda item: item.date)  # 按日期升序排序后返回 | Return candles sorted ascending by date


async def fetch_klines_async(symbol: str, interval: str, limit: int = DEFAULT_KLINE_LIMIT) -> list[Candle]:  # 异步拉取最新 K 线 | Fetch latest candles asynchronously
    """异步拉取最新 K 线 | Fetch latest candles asynchronously.

    参数 | Parameters:
    - symbol (str): 交易对 | Trading symbol.
    - interval (str): K 线周期 | Candle interval.
    - limit (int): 拉取根数 | Number of candles.

    返回 | Returns:
    - list[Candle]: 按时间升序的 K 线 | Time-ascending candles.
    """
    import asyncio  # 函数内导入 asyncio，避免与同步逻辑耦合 | Import asyncio locally to avoid coupling with sync logic

    return await asyncio.to_thread(fetch_klines, symbol, interval, limit=limit)  # 将同步拉取放到线程中执行以免阻塞事件循环 | Run sync fetch in a thread to avoid blocking loop


class BinanceOrderClient:  # 币安现货下单客户端 | Binance spot order client
    """币安现货下单客户端（仅实盘使用） | Binance spot order client (live mode only).

    目的：用 API Key/Secret 对下单请求做 HMAC-SHA256 签名并提交市价单。
    Purpose: Sign order requests with HMAC-SHA256 using API Key/Secret and submit market orders.

    关键属性 | Key attributes:
    - api_key: 币安 API Key（明文，由调用方解密后传入）| Binance API key (plaintext, decrypted by caller).
    - api_secret: 币安 API Secret（明文）| Binance API secret (plaintext).
    """

    def __init__(self, api_key: str, api_secret: str) -> None:  # 初始化签名客户端 | Initialize signed client
        """初始化签名客户端 | Initialize the signed client.

        参数 | Parameters:
        - api_key (str): 币安 API Key（明文，由调用方从加密存储解密）| Binance API key (plaintext).
        - api_secret (str): 币安 API Secret（明文）| Binance API secret (plaintext).

        返回 | Returns:
        - None: 构造器不返回显式值 | Constructor returns no explicit value.
        """
        if not api_key or not api_secret:  # 密钥缺失时拒绝初始化 | Refuse init when credentials are missing
            raise CustomException(msg="实盘模式必须配置 API Key 与 Secret")  # 抛出配置缺失异常 | Raise missing-config error
        self.api_key = api_key  # 保存 API Key | Store API key
        self.api_secret = api_secret  # 保存 API Secret | Store API secret

    def _sign(self, params: dict[str, Any]) -> str:  # 生成 HMAC-SHA256 签名 | Generate HMAC-SHA256 signature
        """生成 HMAC-SHA256 签名 | Generate an HMAC-SHA256 signature.

        参数 | Parameters:
        - params (dict[str, Any]): 待签名的请求参数 | Request params to sign.

        返回 | Returns:
        - str: 十六进制签名字符串 | Hex signature string.
        """
        query = urlencode(params)  # 将参数编码为查询串 | Encode params into a query string
        return hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()  # 用密钥对查询串做 HMAC-SHA256 并转十六进制 | HMAC-SHA256 the query with secret, hex-digest

    def place_market_order(  # 下市价单 | Place a market order
        self,  # 实例自身引用 | Instance reference
        symbol: str,  # 交易对，如 BTCUSDT | Trading symbol, e.g. BTCUSDT
        side: str,  # BUY / SELL | BUY / SELL
        qty: float,  # 数量 | Quantity
        *,  # 强制后续参数为关键字参数 | Force following params to be keyword-only
        client_order_id: str = "",  # 自定义订单号，便于幂等 | Custom order id for idempotency
    ) -> dict[str, Any]:  # 返回币安订单结果 | Return Binance order result
        """下市价单（同步，调用方用 asyncio.to_thread 包裹） | Place a market order (synchronous; wrap with asyncio.to_thread).

        参数 | Parameters:
        - symbol (str): 交易对，如 BTCUSDT | Trading symbol.
        - side (str): BUY / SELL（做空在现货模式下由上层拒绝）| BUY/SELL (shorting rejected upstream in spot).
        - qty (float): 数量 | Quantity.
        - client_order_id (str): 自定义订单号，便于幂等 | Custom order id for idempotency.

        返回 | Returns:
        - dict: 币安返回的订单结果 | Order result returned by Binance.

        流程 | Flow:
        校验数量 → 映射方向 → 组装签名参数 → 签名 → 设置请求头 →
        POST 下单并校验 → 解析结果返回；异常时记录并抛自定义异常。
        Validate qty → map side → build signed params → sign → set headers →
        POST and check → parse result; on error log and raise CustomException.
        """
        if qty <= 0:  # 数量非正时拒绝下单 | Reject non-positive quantity
            raise CustomException(msg="下单数量必须大于 0")  # 抛出数量非法异常 | Raise invalid-quantity error
        binance_side = "BUY" if side in {"buy", "buycover"} else "SELL"  # 将策略方向映射为币安 BUY/SELL | Map strategy side to Binance BUY/SELL
        params: dict[str, Any] = {  # 组装待签名请求参数 | Build params to be signed
            "symbol": symbol,  # 交易对 | Trading symbol
            "side": binance_side,  # 币安方向 BUY/SELL | Binance side
            "type": "MARKET",  # 订单类型为市价单 | Order type is market
            "quantity": f"{qty:.8f}".rstrip("0").rstrip("."),  # 格式化数量并去掉多余 0 与小数点 | Format qty and trim trailing zeros/dot
            "timestamp": int(time.time() * 1000),  # 毫秒级时间戳 | Millisecond timestamp
            "recvWindow": 5000,  # 接收窗口 5000 毫秒 | Receive window of 5000 ms
        }
        if client_order_id:  # 提供自定义订单号时写入参数 | Add custom order id when provided
            params["newClientOrderId"] = client_order_id[:36]  # 截断到 36 字符以符合币安限制 | Truncate to 36 chars per Binance limit
        params["signature"] = self._sign(params)  # 对整个参数集计算签名并写入 | Sign the whole param set and store signature
        headers = {"X-MBX-APIKEY": self.api_key, "User-Agent": "FastTradeQuant/1.0"}  # 设置 API Key 与 UA 请求头 | Set API key and UA headers
        try:  # 捕获网络/解析异常并统一抛自定义异常 | Catch network/parse errors and wrap
            with httpx.Client(timeout=20.0, headers=headers) as client:  # 创建带超时与请求头的客户端并自动关闭 | Create client with timeout/headers, auto-close
                response = client.post(BINANCE_ORDER_URL, params=params)  # 发起 POST 下单请求 | Issue POST order request
                response.raise_for_status()  # HTTP 错误时抛异常 | Raise on HTTP error status
                result = response.json()  # 解析订单结果 JSON | Parse order result JSON
        except (httpx.HTTPError, ValueError) as exc:  # 捕获 HTTP 错误或 JSON 解析错误 | Catch HTTP/JSON errors
            # 不记录密钥；仅记录状态码与简短原因，便于排查
            # Do not log secrets; only status/short reason for troubleshooting.
            logger.error(f"❌ 币安下单失败：{exc}")  # 记录脱敏错误日志 | Log redacted error
            raise CustomException(msg=f"币安下单失败：{exc}") from exc  # 包装为自定义异常并上抛 | Wrap and re-raise as CustomException
        return result  # 返回币安订单结果字典 | Return the Binance order result dict

    # ------------------------------------------------------------------
    # 订单生命周期：下单 → 回查 → 撤单 → 成交明细
    # Order lifecycle: place -> query -> cancel -> trade list
    # ------------------------------------------------------------------
    def get_order(  # 查询单笔订单 | Query one order
        self,
        symbol: str,  # 交易对 | Symbol
        *,
        order_id: int | str | None = None,  # 币安订单号 | Binance order id
        client_order_id: str | None = None,  # 自定义订单号（幂等键）| Client order id (idempotency key)
    ) -> dict[str, Any]:  # 返回订单详情 | Return the order details
        """查询单笔订单状态与成交量 | Query one order's status and executed quantity.

        目的 | Purpose:
            下单返回只能说明"请求被受理"，真实是否成交、成交多少必须回查才能确认；
            否则一次网络重试就会在账本里记下并不存在的仓位。
            The order response only proves the request was accepted; whether and how much it
            filled must be confirmed by a query, otherwise a retry books a phantom position.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.
            - order_id (int | str | None): 币安订单号 | Binance order id.
            - client_order_id (str | None): 自定义订单号 | Client order id.

        返回 | Returns:
            - dict[str, Any]: 订单详情（status/executedQty/cummulativeQuoteQty 等）| Order details.

        流程 | Flow:
            校验至少一个标识 → 组装参数 → 签名 GET → 返回结果。
            Require at least one identifier → build params → signed GET → return the result.
        """
        params: dict[str, Any] = {"symbol": symbol}  # 交易对为必填 | The symbol is mandatory
        if order_id not in (None, ""):  # 有订单号则优先使用 | Prefer the order id when present
            params["orderId"] = order_id  # 写入订单号 | Set the order id
        elif client_order_id:  # 否则用自定义订单号 | Otherwise use the client order id
            params["origClientOrderId"] = str(client_order_id)[:36]  # 截断到币安长度限制 | Truncate to the Binance limit
        else:  # 两个标识都没有 | Neither identifier is present
            raise CustomException(msg="查询订单需要 order_id 或 client_order_id")  # 抛出参数缺失异常 | Raise a missing-parameter error
        return self._signed_request("GET", BINANCE_ORDER_URL, params)  # 发起签名查询 | Issue the signed query

    def cancel_order(  # 撤销订单 | Cancel an order
        self,
        symbol: str,  # 交易对 | Symbol
        *,
        order_id: int | str | None = None,  # 币安订单号 | Binance order id
        client_order_id: str | None = None,  # 自定义订单号 | Client order id
    ) -> dict[str, Any]:  # 返回撤单结果 | Return the cancel result
        """撤销一笔尚未成交的订单 | Cancel an order that has not filled.

        目的 | Purpose:
            回查超时仍非终态时必须撤单，否则订单可能在下一次 tick 之后成交，
            造成"账本没记、交易所却已成交"的错位。
            When a query times out without a terminal status the order must be cancelled,
            otherwise it may fill after the tick and desync the ledger from the exchange.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.
            - order_id (int | str | None): 币安订单号 | Binance order id.
            - client_order_id (str | None): 自定义订单号 | Client order id.

        返回 | Returns:
            - dict[str, Any]: 撤单结果 | Cancel result.

        流程 | Flow:
            组装标识 → 签名 DELETE → 返回结果；订单已成交时交易所会报错，由上层容错。
            Build identifiers → signed DELETE → return; a filled order errors out and the caller tolerates it.
        """
        params: dict[str, Any] = {"symbol": symbol}  # 交易对为必填 | The symbol is mandatory
        if order_id not in (None, ""):  # 优先用订单号 | Prefer the order id
            params["orderId"] = order_id  # 写入订单号 | Set the order id
        elif client_order_id:  # 其次用自定义订单号 | Fall back to the client order id
            params["origClientOrderId"] = str(client_order_id)[:36]  # 截断 | Truncate
        else:  # 无标识无法撤单 | Cannot cancel without an identifier
            raise CustomException(msg="撤单需要 order_id 或 client_order_id")  # 抛出参数缺失异常 | Raise a missing-parameter error
        return self._signed_request("DELETE", BINANCE_ORDER_URL, params)  # 发起签名撤单 | Issue the signed cancel

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:  # 查询当前挂单 | List current open orders
        """查询某交易对当前所有未成交挂单 | List every open order for a symbol.

        目的 | Purpose:
            进程崩溃、下单超时或人工干预都会留下挂单；这些挂单占用资金且可能在
            之后成交，对账时必须看到它们。
            Crashes, order timeouts and manual intervention leave open orders that lock
            capital and may fill later; reconciliation must see them.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.

        返回 | Returns:
            - list[dict[str, Any]]: 挂单列表 | Open-order list.

        流程 | Flow:
            签名 GET → 非列表时返回空 → 返回列表。
            Signed GET → return empty for a non-list payload → return the list.
        """
        data = self._signed_request("GET", BINANCE_OPEN_ORDERS_URL, {"symbol": symbol})  # 请求挂单 | Request open orders
        return list(data) if isinstance(data, list) else []  # 归一化为列表 | Normalize into a list

    def my_trades(  # 查询成交明细 | List trade fills
        self,
        symbol: str,  # 交易对 | Symbol
        *,
        order_id: int | str | None = None,  # 限定订单号 | Restrict to one order
        limit: int = 500,  # 返回条数上限 | Maximum rows
    ) -> list[dict[str, Any]]:  # 返回成交明细列表 | Return the fill list
        """查询成交明细，用于手续费回填与盈亏复核 | List fills for fee backfill and PnL audit.

        目的 | Purpose:
            下单响应里的 ``fills`` 有时缺失（或手续费以 BNB 计价），需要从成交流水
            拿到真实的 ``commission``，否则账本里的手续费会长期偏离交易所。
            The order response sometimes omits ``fills`` (or denominates the commission in BNB);
            the trade list carries the real ``commission`` so ledger fees cannot drift forever.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.
            - order_id (int | str | None): 只取该订单的成交 | Only fills of this order.
            - limit (int): 返回条数 | Number of rows.

        返回 | Returns:
            - list[dict[str, Any]]: 成交明细 | Fill list.

        流程 | Flow:
            组装参数（含订单号）→ 签名 GET → 归一化为列表。
            Build params (with the order id) → signed GET → normalize into a list.
        """
        params: dict[str, Any] = {"symbol": symbol, "limit": max(1, min(int(limit), 1000))}  # 币安上限 1000 | Binance caps at 1000
        if order_id not in (None, ""):  # 限定订单 | Restrict to the order
            params["orderId"] = order_id  # 写入订单号 | Set the order id
        data = self._signed_request("GET", BINANCE_MY_TRADES_URL, params)  # 请求成交明细 | Request the fills
        return list(data) if isinstance(data, list) else []  # 归一化为列表 | Normalize into a list

    def place_market_order_verified(  # 下单并回查确认真实成交 | Place an order and confirm the real fill
        self,
        symbol: str,  # 交易对 | Symbol
        side: str,  # BUY / SELL | BUY / SELL
        qty: float,  # 数量 | Quantity
        *,
        client_order_id: str = "",  # 自定义订单号（幂等）| Custom order id (idempotency)
        timeout: float = 8.0,  # 等待终态的最长秒数 | Max seconds to wait for a terminal status
        poll_interval: float = 0.4,  # 轮询间隔秒数 | Poll interval in seconds
    ) -> dict[str, Any]:  # 返回带真实成交量/成交额/手续费的订单结果 | Order result with true qty/notional/fee
        """下市价单并回查确认成交，超时未终态则撤单再查一次 | Place a market order, confirm the fill, cancel on timeout.

        目的 | Purpose:
            只相信下单响应是不够的：网络抖动、部分成交、撮合延迟都会让
            ``executedQty`` 与请求量不一致。这里按"下单 → 轮询回查 → 超时撤单 →
            再回查"的顺序拿到真实终态，账本只按终态记账。
            Trusting the placement response is not enough: network jitter, partial fills and
            matching delays all make ``executedQty`` differ from the request. This method
            places, polls, cancels on timeout and re-queries so the ledger books only the
            real terminal state.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.
            - side (str): BUY / SELL | BUY / SELL.
            - qty (float): 数量 | Quantity.
            - client_order_id (str): 自定义订单号 | Client order id.
            - timeout (float): 等待终态的最长秒数 | Max seconds to wait for a terminal status.
            - poll_interval (float): 轮询间隔 | Poll interval.

        返回 | Returns:
            - dict[str, Any]: 订单结果（status/executedQty/cummulativeQuoteQty/fills）| Order result.

        流程 | Flow:
            下单 → 非终态则轮询回查 → 超时撤单 → 再回查 → 返回最后一次结果。
            Place → poll while non-terminal → cancel on timeout → re-query → return the last result.
        """
        result = self.place_market_order(symbol, side, qty, client_order_id=client_order_id)  # 先下单 | Place the order first
        status = str(result.get("status") or "").upper()  # 读取状态 | Read the status
        order_id = result.get("orderId")  # 读取订单号 | Read the order id
        deadline = time.monotonic() + max(0.0, float(timeout))  # 计算等待截止时刻 | Compute the deadline
        while status not in ORDER_FINAL_STATUS and time.monotonic() < deadline:  # 未到终态且未超时就轮询 | Poll while non-terminal and before the deadline
            time.sleep(max(0.05, float(poll_interval)))  # 等待一个轮询间隔 | Wait for one poll interval
            try:  # 回查失败（限流/网络）时继续重试直到超时 | Retry on query failures until the deadline
                result = self.get_order(symbol, order_id=order_id, client_order_id=client_order_id or None)  # 回查订单 | Query the order
                status = str(result.get("status") or "").upper()  # 更新状态 | Update the status
            except CustomException as exc:  # 查询失败不应直接放弃 | A failed query must not abort immediately
                logger.warning(f"订单回查失败({symbol} {order_id})：{exc}")  # 记录告警 | Log a warning
        if status not in ORDER_FINAL_STATUS:  # 超时仍未到终态 | Timed out without a terminal status
            logger.warning(f"订单 {order_id} 超时未终态（status={status}），执行撤单")  # 记录超时 | Log the timeout
            try:  # 撤单可能因已成交而失败 | Cancelling may fail because the order already filled
                result = self.cancel_order(symbol, order_id=order_id, client_order_id=client_order_id or None)  # 撤单 | Cancel it
                status = str(result.get("status") or "").upper()  # 更新状态 | Update the status
            except CustomException as exc:  # 撤单失败时保留最后一次查询结果 | Keep the last query result when cancelling fails
                logger.error(f"订单撤单失败({symbol} {order_id})：{exc}")  # 记录错误 | Log the error
            try:  # 撤单后必须再查一次，拿到最终成交量 | Re-query after cancelling to get the final executed quantity
                result = self.get_order(symbol, order_id=order_id, client_order_id=client_order_id or None)  # 再次回查 | Query again
            except CustomException as exc:  # 仍失败则返回已有结果，由上层按 0 成交处理 | Return what we have otherwise
                logger.error(f"订单撤单后回查失败({symbol} {order_id})：{exc}")  # 记录错误 | Log the error
        if str(result.get("status") or "").upper() in {"CANCELED", "EXPIRED", "REJECTED"} and float(result.get("executedQty") or 0.0) <= 0:  # 完全未成交 | Nothing filled at all
            raise CustomException(msg=f"订单未成交（status={result.get('status')}），已撤销")  # 抛异常让上层放弃记账 | Raise so the caller books nothing
        return result  # 返回最终结果 | Return the final result

    # ------------------------------------------------------------------
    # 对账与下单规整：让实盘账本与交易所真实余额保持一致
    # Reconciliation and order normalization: keep the live ledger equal to the real account.
    # ------------------------------------------------------------------
    def _signed_request(  # 发起带 HMAC 签名的任意请求 | Issue any HMAC-signed request
        self,
        method: str,  # HTTP 方法：GET/POST/DELETE | HTTP method: GET/POST/DELETE
        url: str,  # 接口地址 | Endpoint URL
        params: dict[str, Any] | None = None,  # 查询/表单参数 | Query or form parameters
    ) -> Any:  # 返回解析后的 JSON | Return parsed JSON
        """发起带签名的 HTTP 请求（GET/POST/DELETE 通用）| Issue a signed HTTP request for any method.

        目的 | Purpose:
            下单、查单、撤单、查成交都需要同一套"补时间戳 → 签名 → 带 API Key"流程，
            收敛成一个方法后，新增接口只需换 URL 与 method。
            Placing, querying, cancelling and listing trades all need the same
            timestamp-sign-header pipeline; one method serves them all.

        参数 | Parameters:
            - method (str): HTTP 方法 | HTTP method.
            - url (str): 接口地址 | Endpoint URL.
            - params (dict | None): 请求参数 | Request parameters.

        返回 | Returns:
            - Any: 解析后的 JSON | Parsed JSON.

        流程 | Flow:
            补时间戳与窗口 → 签名 → 带 API Key 头请求 → 校验状态 → 返回 JSON。
            Add timestamp/window → sign → request with the API key header → check status → return JSON.
        """
        payload = dict(params or {})  # 复制参数，避免修改调用方字典 | Copy params to avoid mutating the caller's dict
        payload["timestamp"] = int(time.time() * 1000)  # 毫秒时间戳 | Millisecond timestamp
        payload["recvWindow"] = 5000  # 接收窗口 5000 毫秒 | Receive window of 5000 ms
        payload["signature"] = self._sign(payload)  # 对整个参数集签名 | Sign the whole param set
        headers = {"X-MBX-APIKEY": self.api_key, "User-Agent": "FastTradeQuant/1.0"}  # 设置鉴权与 UA 头 | Set auth and UA headers
        try:  # 捕获网络/解析异常 | Catch network/parse errors
            with httpx.Client(timeout=20.0, headers=headers) as client:  # 创建客户端并自动关闭 | Create a client with auto-close
                response = client.request(method, url, params=payload)  # 按方法发起请求 | Issue the request with the given method
                response.raise_for_status()  # HTTP 错误时抛异常 | Raise on HTTP error status
                return response.json()  # 返回解析后的 JSON | Return parsed JSON
        except (httpx.HTTPError, ValueError) as exc:  # 捕获 HTTP 或 JSON 错误 | Catch HTTP or JSON errors
            logger.error(f"❌ 币安查询失败({method} {url})：{exc}")  # 记录脱敏错误 | Log a redacted error
            raise CustomException(msg=f"币安查询失败：{exc}") from exc  # 包装为业务异常 | Wrap into a business exception

    def _signed_get(self, url: str, params: dict[str, Any] | None = None) -> Any:  # 发起签名 GET 请求 | Issue a signed GET request
        """发起带 HMAC 签名的 GET 请求（保留旧接口）| Issue a signed GET request (legacy wrapper).

        参数 | Parameters:
            - url (str): 接口地址 | Endpoint URL.
            - params (dict | None): 查询参数 | Query parameters.

        返回 | Returns:
            - Any: 解析后的 JSON | Parsed JSON.
        """
        return self._signed_request("GET", url, params)  # 复用通用签名请求 | Delegate to the generic signed request

    def balances(self) -> dict[str, float]:  # 查询账户可用余额 | Fetch available balances
        """查询账户各资产的可用余额 | Fetch available (free) balances per asset.

        返回 | Returns:
            - dict[str, float]: 资产 → 可用数量 | Asset to free quantity.

        流程 | Flow:
            请求账户接口 → 遍历 balances → 取 free 字段转 float。
            Request the account endpoint → iterate balances → read the free field as float.
        """
        data = self._signed_get(BINANCE_ACCOUNT_URL)  # 请求账户信息 | Request account info
        result: dict[str, float] = {}  # 初始化余额字典 | Init the balance dict
        for item in data.get("balances", []) or []:  # 遍历资产列表 | Iterate over balances
            asset = str(item.get("asset") or "")  # 读取资产名 | Read the asset name
            if not asset:  # 跳过缺失资产名 | Skip entries without an asset
                continue  # 继续下一条 | Continue
            try:  # 自由余额可能为空字符串 | The free balance may be an empty string
                result[asset] = float(item.get("free") or 0.0)  # 记录可用余额 | Record the free balance
            except (TypeError, ValueError):  # 无法转换时记为 0 | Record 0 when unconvertible
                result[asset] = 0.0  # 容错为 0 | Fall back to 0
        return result  # 返回余额字典 | Return the balance dict

    def symbol_info(self, symbol: str) -> dict[str, float | str]:  # 查询交易对规则 | Fetch symbol trading rules
        """查询交易对的资产、过滤器规则（带进程内缓存）| Fetch assets and exchange filters (cached).

        目的 | Purpose:
            只解析 LOT_SIZE 与 MIN_NOTIONAL 是不够的：忽略 MARKET_LOT_SIZE 会让市价单
            因精度被拒，忽略 PRICE_FILTER 会让止损价带上非法小数位，忽略 maxQty
            会让大单被拒且无法提前发现。这里把下单真正用得到的过滤器全部解析出来。
            Parsing only LOT_SIZE and MIN_NOTIONAL is not enough: ignoring MARKET_LOT_SIZE
            gets market orders rejected on precision, ignoring PRICE_FILTER produces illegal
            stop prices, and ignoring maxQty hides oversized orders until the exchange rejects
            them. Every filter that actually gates an order is parsed here.

        参数 | Parameters:
            - symbol (str): 交易对，如 BTCUSDT | Symbol, e.g. BTCUSDT.

        返回 | Returns:
            - dict: base_asset/quote_asset/tick_size/step_size/min_qty/max_qty/
              market_step_size/market_min_qty/market_max_qty/min_notional/max_notional
              | Assets plus every order-gating filter.

        流程 | Flow:
            命中缓存直接返回 → 否则请求 exchangeInfo → 解析过滤器 → 写入缓存。
            Return from cache when present → otherwise call exchangeInfo → parse filters → cache.
        """
        cached = getattr(self, "_symbol_cache", {}).get(symbol)  # 读取缓存 | Read the cache
        if cached:  # 命中缓存直接返回 | Return immediately on a cache hit
            return cached  # 返回缓存结果 | Return the cached rules
        if not hasattr(self, "_symbol_cache"):  # 首次调用时初始化缓存字典 | Initialize the cache on first use
            self._symbol_cache: dict[str, dict[str, float | str]] = {}  # 创建缓存容器 | Create the cache container
        info: dict[str, float | str] = _empty_symbol_rules()  # 默认规则：全部按"无约束"处理 | Default rules: everything unconstrained
        try:  # 规则查询失败不应阻塞交易，仅退化为不规整 | Rule lookup failure must not block trading
            with httpx.Client(timeout=20.0, headers={"User-Agent": "FastTradeQuant/1.0"}) as client:  # 创建客户端 | Create a client
                response = client.get(BINANCE_EXCHANGE_INFO_URL, params={"symbol": symbol})  # 请求交易规则 | Request the trading rules
                response.raise_for_status()  # 校验状态 | Check status
                payload = response.json()  # 解析 JSON | Parse JSON
            item = (payload.get("symbols") or [{}])[0]  # 取第一个交易对 | Take the first symbol entry
            info = _parse_symbol_item(item)  # 复用统一解析器，避免两处逐字段漂移 | Reuse the shared parser so both paths stay identical
        except (httpx.HTTPError, ValueError, IndexError) as exc:  # 查询失败时记录并退化为默认规则 | Log and fall back on failure
            logger.warning(f"交易规则查询失败({symbol})：{exc}")  # 记录告警 | Log a warning
        self._symbol_cache[symbol] = info  # 写入缓存 | Store in the cache
        return info  # 返回规则 | Return the rules

    def normalize_qty(self, symbol: str, qty: float, *, market: bool = True) -> float:  # 按步长规整下单数量 | Round a quantity down to the exchange step
        """按交易所数量过滤器规整数量，避免因精度或越界被拒单 | Normalize a quantity against exchange filters.

        目的 | Purpose:
            市价单应按 MARKET_LOT_SIZE 规整（与 LOT_SIZE 常常不同），并把数量裁剪到
            [minQty, maxQty]；低于最小量时返回 0，由调用方取消订单并给出明确原因。
            Market orders must follow MARKET_LOT_SIZE (often different from LOT_SIZE), and the
            quantity must be clamped into [minQty, maxQty]; below the minimum this returns 0 so
            the caller can cancel with a clear reason.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.
            - qty (float): 原始数量 | Raw quantity.
            - market (bool): 是否按市价单规则（默认 True）| Whether to use market rules (default True).

        返回 | Returns:
            - float: 规整后的数量；不满足最小量时为 0 | Normalized quantity, 0 when below the minimum.

        流程 | Flow:
            取市价/限价步长 → 向下取整到步长整数倍 → 裁剪上限 → 校验下限。
            Pick the market/limit step → floor to a multiple → clamp the maximum → check the minimum.
        """
        rules = self.symbol_info(symbol)  # 读取规则 | Read the rules
        step = float(rules.get("market_step_size" if market else "step_size") or 0.0)  # 取对应步长 | Pick the matching step
        if step <= 0:  # 市价规则缺失时回退默认步长 | Fall back to the default step when the market rule is missing
            step = float(rules.get("step_size") or 0.0)  # 使用 LOT_SIZE 步长 | Use the LOT_SIZE step
        min_qty = float(rules.get("market_min_qty" if market else "min_qty") or 0.0)  # 取对应最小量 | Pick the matching minimum
        max_qty = float(rules.get("market_max_qty" if market else "max_qty") or 0.0)  # 取对应最大量 | Pick the matching maximum
        if qty <= 0:  # 数量非法时直接返回 0 | Return 0 for invalid quantities
            return 0.0  # 无法下单 | Unorderable
        normalized = round(qty, 8) if step <= 0 else round(math.floor(qty / step) * step, 8)  # 无步长只裁精度，有步长则向下取整 | Trim precision or floor to the step
        if max_qty > 0 and normalized > max_qty:  # 超过最大量时裁剪到上限（上限本身按步长对齐）| Clamp oversized orders to the maximum
            normalized = round(math.floor(max_qty / step) * step, 8) if step > 0 else round(max_qty, 8)  # 按步长对齐上限 | Align the cap to the step
        if min_qty > 0 and normalized < min_qty:  # 低于最小量时视为不可下单 | Below the minimum means unorderable
            return 0.0  # 返回 0 | Return 0
        return max(normalized, 0.0)  # 返回非负结果 | Return a non-negative result

    def normalize_price(self, symbol: str, price: float) -> float:  # 按 tick 规整价格 | Round a price to the tick size
        """按 PRICE_FILTER 的 tickSize 向下规整价格 | Round a price down to PRICE_FILTER.tickSize.

        目的 | Purpose:
            止损价、限价单价格若带上非法小数位会被交易所拒绝；向下规整对卖出更保守。
            Stop and limit prices with illegal decimals are rejected; rounding down is the
            conservative choice for sells.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.
            - price (float): 原始价格 | Raw price.

        返回 | Returns:
            - float: 规整后的价格 | Normalized price.
        """
        tick = float(self.symbol_info(symbol).get("tick_size") or 0.0)  # 读取价格精度 | Read the tick size
        if tick <= 0 or price <= 0:  # 无精度约束或价格非法 | No tick constraint or invalid price
            return round(max(price, 0.0), 8)  # 仅裁精度 | Trim precision only
        return round(math.floor(price / tick) * tick, 8)  # 向下取整到 tick 整数倍 | Floor to a tick multiple

    def check_order(self, symbol: str, qty: float, price: float) -> tuple[bool, str]:  # 下单前校验 | Pre-trade order validation
        """按交易所过滤器校验一笔市价单是否会被接受 | Validate a market order against exchange filters.

        目的 | Purpose:
            把"数量过小 / 名义额不足 / 名义额过大"的拒绝提前到本地，避免打到交易所
            再因 -1013/-2010 拒单，也便于在日志里给出可读原因。
            Move "too small / below or above notional" rejections locally instead of hitting
            the exchange and getting -1013/-2010, and produce a readable reason for the logs.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.
            - qty (float): 已规整数量 | Normalized quantity.
            - price (float): 参考成交价 | Reference execution price.

        返回 | Returns:
            - tuple[bool, str]: (是否通过, 原因) | (accepted, reason).

        流程 | Flow:
            数量校验 → 名义额校验 → 返回结果。
            Check quantity → check notional → return.
        """
        if qty <= 0:  # 规整后为 0 说明低于最小步长/最小量 | A zero qty means below the step or minimum
            return False, f"下单数量 {qty} 低于交易所最小要求"  # 返回数量原因 | Return the quantity reason
        if price <= 0:  # 无参考价时只校验数量，避免误判名义额 | Without a reference price only validate the quantity
            return True, ""  # 放行，由交易所按真实成交价判定 | Let the exchange judge at the real fill price
        notional = qty * max(price, 0.0)  # 计算名义额 | Compute the notional
        rules = self.symbol_info(symbol)  # 读取规则 | Read the rules
        min_notional = float(rules.get("min_notional") or 0.0)  # 最小名义额 | Minimum notional
        max_notional = float(rules.get("max_notional") or 0.0)  # 最大名义额 | Maximum notional
        if min_notional > 0 and notional < min_notional:  # 名义额不足 | Notional below the floor
            return False, f"下单名义额 {notional:.8g} 低于交易所最小要求 {min_notional}"  # 返回名义额原因 | Return the notional reason
        if max_notional > 0 and notional > max_notional:  # 名义额超过上限 | Notional above the cap
            return False, f"下单名义额 {notional:.8g} 超过交易所上限 {max_notional}"  # 返回超限原因 | Return the cap reason
        return True, ""  # 校验通过 | Accepted

    def min_notional(self, symbol: str) -> float:  # 查询最小名义额 | Fetch the minimum notional
        """查询交易对的最小下单名义额 | Fetch the minimum order notional for a symbol.

        参数 | Parameters:
            - symbol (str): 交易对 | Symbol.

        返回 | Returns:
            - float: 最小名义额，0 表示无限制 | Minimum notional, 0 when unlimited.
        """
        return float(self.symbol_info(symbol).get("min_notional") or 0.0)  # 返回最小名义额 | Return the min notional


# ----------------------------------------------------------------------
# 下单结果解析：真实成交量/成交均价/手续费
# Order-result parsing: true filled quantity, average price and commission.
# ----------------------------------------------------------------------
def order_fill_summary(  # 汇总订单真实成交信息 | Summarize the real fill of an order
    result: dict[str, Any],  # 币安订单结果 | Binance order result
    *,  # 强制关键字参数 | Force keyword-only args
    fallback_qty: float = 0.0,  # 成交量缺失时的回退 | Fallback quantity
    fallback_price: float = 0.0,  # 成交价缺失时的回退 | Fallback price
    quote_asset: str = "",  # 计价资产，用于识别手续费的计价币种 | Quote asset used to identify the fee currency
) -> dict[str, Any]:  # 返回 {qty, price, fee, fee_known, fee_assets} | Return the summary dict
    """解析订单结果里的真实成交量、成交均价与手续费 | Parse the true quantity, average price and commission.

    目的 | Purpose:
        下单响应、回查响应、撤单响应的字段完整度不同，统一在这里解析，避免各处
        各写一套并出现"成交量取错、手续费漏算"的分歧。
        Placement, query and cancel responses carry different field sets; parsing them in one
        place avoids divergent logic and wrong quantities or missing fees.

    参数 | Parameters:
        result (dict[str, Any]): 订单结果 | Order result.
        fallback_qty (float): 成交量缺失时的回退值 | Fallback quantity.
        fallback_price (float): 成交价缺失时的回退值 | Fallback price.
        quote_asset (str): 计价资产（手续费计价币种）| Quote asset (fee currency).

    返回 | Returns:
        dict[str, Any]: qty/price/fee/fee_known/fee_assets | Summary fields.

    流程 | Flow:
        读 executedQty → 用成交额反算均价 → 汇总 fills 的手续费 → 标注手续费是否完整。
        Read executedQty → derive the average price from the quote quantity → sum the fill
        commissions → flag whether the fee is complete.
    """
    try:  # 字段可能为字符串或缺失 | Fields may be strings or missing
        qty = float(result.get("executedQty") or 0.0)  # 真实成交量 | True executed quantity
    except (TypeError, ValueError):  # 转换失败按 0 处理 | Treat unconvertible values as 0
        qty = 0.0  # 无成交量 | No quantity
    if qty <= 0:  # 成交量缺失时回退请求量 | Fall back to the requested quantity
        qty = max(0.0, float(fallback_qty))  # 回退值非负 | Keep the fallback non-negative
    try:  # 成交额同样可能缺失 | The quote quantity may also be missing
        quote_qty = float(result.get("cummulativeQuoteQty") or 0.0)  # 累计成交额 | Cumulative quote quantity
    except (TypeError, ValueError):  # 转换失败按 0 处理 | Treat unconvertible values as 0
        quote_qty = 0.0  # 无成交额 | No notional
    price = quote_qty / qty if qty > 0 and quote_qty > 0 else 0.0  # 成交额除成交量得均价 | Average price from notional / quantity
    fills = result.get("fills")  # 成交明细 | Fill details
    total_fee = 0.0  # 累计手续费 | Accumulated fee
    fee_assets: set[str] = set()  # 手续费计价币种集合 | Fee currencies seen
    if isinstance(fills, list):  # 明细存在时逐笔汇总 | Sum per fill when the list exists
        for item in fills:  # 遍历每笔成交 | Iterate over each fill
            if not isinstance(item, dict):  # 跳过异常项 | Skip malformed entries
                continue  # 继续下一笔 | Continue
            asset = str(item.get("commissionAsset") or "")  # 手续费币种 | Commission asset
            try:  # 手续费可能是字符串 | The commission may be a string
                commission = float(item.get("commission") or 0.0)  # 该笔手续费 | Fee for this fill
            except (TypeError, ValueError):  # 转换失败跳过 | Skip unconvertible values
                continue  # 继续下一笔 | Continue
            if asset:  # 记录币种 | Record the currency
                fee_assets.add(asset)  # 加入集合 | Add to the set
            if not quote_asset or asset.upper() == quote_asset.upper():  # 只累加计价币种的手续费 | Only sum fees in the quote currency
                total_fee += commission  # 累加 | Accumulate
            if price <= 0:  # 没有成交额时用明细反算均价 | Derive the price from fills when the notional is missing
                try:  # 明细字段同样可能异常 | Fill fields may be malformed too
                    fill_qty = float(item.get("qty") or 0.0)  # 该笔数量 | Fill quantity
                    fill_price = float(item.get("price") or 0.0)  # 该笔价格 | Fill price
                except (TypeError, ValueError):  # 跳过异常项 | Skip malformed entries
                    continue  # 继续下一笔 | Continue
                if fill_qty > 0 and fill_price > 0:  # 两项都有效才有意义 | Both must be valid
                    price = fill_price if price <= 0 else price  # 取首笔有效价（市价单各笔差异极小）| Use the first valid price (market fills barely differ)
    if price <= 0:  # 仍无价格时回退参考价 | Fall back to the reference price
        price = max(0.0, float(fallback_price))  # 回退值非负 | Keep the fallback non-negative
    fee_known = bool(fills) and (not quote_asset or not fee_assets or fee_assets <= {quote_asset})  # 手续费是否完整且以计价币计 | Whether the fee is complete and quote-denominated
    return {  # 返回汇总结果 | Return the summary
        "qty": qty,  # 真实成交量 | True quantity
        "price": price,  # 成交均价 | Average price
        "fee": total_fee,  # 计价币手续费 | Fee in the quote currency
        "fee_known": fee_known,  # 手续费是否可信 | Whether the fee is trustworthy
        "fee_assets": sorted(fee_assets),  # 手续费币种列表（审计用）| Fee currencies (for auditing)
    }


def sum_trade_commission(trades: list[dict[str, Any]] | None, quote_asset: str = "") -> float:  # 汇总成交流水手续费 | Sum commissions from a trade list
    """汇总成交明细中计价币种的手续费 | Sum quote-denominated commissions from a trade list.

    目的 | Purpose:
        下单响应缺 ``fills`` 时（或手续费以 BNB 支付未记入）用成交流水补齐手续费。
        When the order response lacks ``fills`` (or the fee was paid in BNB), the trade list
        supplies the commission.

    参数 | Parameters:
        trades (list[dict] | None): 成交流水 | Trade list.
        quote_asset (str): 计价资产 | Quote asset.

    返回 | Returns:
        float: 累计手续费 | Total commission.
    """
    total = 0.0  # 累计值 | Accumulator
    for item in trades or []:  # 遍历每笔成交 | Iterate over the trades
        if not isinstance(item, dict):  # 跳过异常项 | Skip malformed entries
            continue  # 继续下一笔 | Continue
        asset = str(item.get("commissionAsset") or "")  # 手续费币种 | Commission asset
        if quote_asset and asset.upper() != quote_asset.upper():  # 币种不符则跳过 | Skip other currencies
            continue  # 继续下一笔 | Continue
        try:  # 字段可能是字符串 | The field may be a string
            total += float(item.get("commission") or 0.0)  # 累加 | Accumulate
        except (TypeError, ValueError):  # 转换失败跳过 | Skip unconvertible values
            continue  # 继续下一笔 | Continue
    return total  # 返回累计手续费 | Return the total
