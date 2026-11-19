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
DEFAULT_KLINE_LIMIT = 300  # 默认拉取 K 线根数 | Default number of candles to fetch


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
