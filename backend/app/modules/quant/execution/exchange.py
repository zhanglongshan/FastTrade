"""币安行情与下单网关。

- 行情：公开 REST ``/api/v3/klines``，与数据下载模块共用同一接口，
  模拟盘与实盘都从这里拉取最新 K 线喂给策略。
- 下单：签名 REST ``/api/v3/order``（HMAC-SHA256），仅实盘模式使用，
  且受 ``dry_run`` 开关约束——默认只撮合不下单。

密钥经 CryptoUtil 加密落库，运行时才解密到内存，不写入任何日志。
"""

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.exceptions import CustomException
from app.core.logger import logger

from ..data.binance_download import BINANCE_API_KLINES_URL, row_from_raw
from ..strategy.api import Candle

# 实盘下单接口；与行情接口同域，便于统一出口白名单审计
BINANCE_ORDER_URL = "https://api.binance.com/api/v3/order"
DEFAULT_KLINE_LIMIT = 300


def fetch_klines(
    symbol: str,
    interval: str,
    *,
    limit: int = DEFAULT_KLINE_LIMIT,
    client: httpx.AsyncClient | None = None,
) -> list[Candle]:
    """拉取最新 K 线（同步执行，调用方用 asyncio.to_thread 包裹）。

    参数:
    - symbol: 交易对，如 BTCUSDT（无斜杠）。
    - interval: K 线周期。
    - limit: 拉取根数，至少 2。
    - client: 可复用的 httpx 客户端；为 None 时临时创建。

    返回:
    - list[Candle]: 按时间升序的 K 线。
    """
    params = {"symbol": symbol, "interval": interval, "limit": max(2, int(limit))}
    own_client = client is None
    target = client or httpx.Client(timeout=20.0, headers={"User-Agent": "FastTradeQuant/1.0"})
    try:
        response = target.get(BINANCE_API_KLINES_URL, params=params)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CustomException(msg=f"币安行情获取失败：{exc}") from exc
    finally:
        if own_client:
            target.close()  # type: ignore[union-attr]

    if not isinstance(payload, list) or not payload:
        raise CustomException(msg="币安未返回 K 线数据")
    candles = [Candle(**row_from_raw(row)) for row in payload]
    return sorted(candles, key=lambda item: item.date)


async def fetch_klines_async(symbol: str, interval: str, limit: int = DEFAULT_KLINE_LIMIT) -> list[Candle]:
    """异步拉取最新 K 线。"""
    import asyncio

    return await asyncio.to_thread(fetch_klines, symbol, interval, limit=limit)


class BinanceOrderClient:
    """币安现货下单客户端（仅实盘使用）。"""

    def __init__(self, api_key: str, api_secret: str) -> None:
        """初始化签名客户端。

        参数:
        - api_key: 币安 API Key（明文，由调用方从加密存储解密）。
        - api_secret: 币安 API Secret（明文）。
        """
        if not api_key or not api_secret:
            raise CustomException(msg="实盘模式必须配置 API Key 与 Secret")
        self.api_key = api_key
        self.api_secret = api_secret

    def _sign(self, params: dict[str, Any]) -> str:
        """生成 HMAC-SHA256 签名。"""
        query = urlencode(params)
        return hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        client_order_id: str = "",
    ) -> dict[str, Any]:
        """下市价单（同步，调用方用 asyncio.to_thread 包裹）。

        参数:
        - symbol: 交易对，如 BTCUSDT。
        - side: BUY / SELL（做空在现货模式下由上层拒绝）。
        - qty: 数量。
        - client_order_id: 自定义订单号，便于幂等。

        返回:
        - dict: 币安返回的订单结果。
        """
        if qty <= 0:
            raise CustomException(msg="下单数量必须大于 0")
        binance_side = "BUY" if side in {"buy", "buycover"} else "SELL"
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": binance_side,
            "type": "MARKET",
            "quantity": f"{qty:.8f}".rstrip("0").rstrip("."),
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id[:36]
        params["signature"] = self._sign(params)
        headers = {"X-MBX-APIKEY": self.api_key, "User-Agent": "FastTradeQuant/1.0"}
        try:
            with httpx.Client(timeout=20.0, headers=headers) as client:
                response = client.post(BINANCE_ORDER_URL, params=params)
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # 不记录密钥；仅记录状态码与简短原因，便于排查
            logger.error(f"❌ 币安下单失败：{exc}")
            raise CustomException(msg=f"币安下单失败：{exc}") from exc
        return result
