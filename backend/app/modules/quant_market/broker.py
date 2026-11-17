"""柜台交易可插拔适配层（中泰 XTP / CTP）。

设计目标：上层策略/回测/模拟盘/实盘不感知具体柜台 SDK。每个柜台实现一个
:class:`BrokerAdapter`，默认 :class:`SimulatedBroker` 用本地撮合，保证没有券商
账号与 SDK 时也能跑通完整流程。后续接入真实 SDK 时，只需实现同接口并在
:func:`get_broker` 中注册，无需改动业务代码。

安全约束：密钥类配置不落业务日志；真实报单默认关闭（dry_run=True）。
"""

from typing import Any, Protocol

from app.core.exceptions import CustomException


class BrokerAdapter(Protocol):
    """柜台适配器统一接口。"""

    name: str

    def connect(self, config: dict[str, Any]) -> None:
        """连接柜台。"""
        ...

    def disconnect(self) -> None:
        """断开连接。"""
        ...

    def latest_price(self, symbol: str) -> float:
        """返回最新价。"""
        ...

    def place_order(self, symbol: str, side: str, qty: float, price: float | None = None) -> dict[str, Any]:
        """报单，返回成交回报。"""
        ...


class SimulatedBroker:
    """离线/模拟柜台：用行情价格直接撮合，不下真实单。"""

    name = "simulated"

    def __init__(self) -> None:
        """初始化模拟柜台。"""
        self._prices: dict[str, float] = {}

    def connect(self, config: dict[str, Any]) -> None:
        """读取可选的初始价格表。"""
        prices = config.get("prices") or {}
        if isinstance(prices, dict):
            self._prices = {str(k): float(v) for k, v in prices.items()}

    def disconnect(self) -> None:
        """无连接可断开。"""
        self._prices.clear()

    def latest_price(self, symbol: str) -> float:
        """返回模拟价格；未配置时给出稳定占位价并报错提示。"""
        if symbol not in self._prices:
            raise CustomException(
                msg=f"模拟柜台未配置 {symbol} 的价格，请在 broker_config.prices 中提供，或导入本地数据后使用离线 tick"
            )
        return self._prices[symbol]

    def place_order(self, symbol: str, side: str, qty: float, price: float | None = None) -> dict[str, Any]:
        """模拟成交。"""
        traded = price if price is not None else self.latest_price(symbol)
        return {"symbol": symbol, "side": side, "qty": qty, "price": traded, "status": "filled", "simulated": True}


class XTPBroker(SimulatedBroker):
    """中泰 XTP 适配器占位：未安装 SDK 时退化为模拟，接入时替换实现。"""

    name = "xtp"


class CTPBroker(SimulatedBroker):
    """CTP 适配器占位：未安装 SDK 时退化为模拟，接入时替换实现。"""

    name = "ctp"


_BROKERS: dict[str, type] = {
    "simulated": SimulatedBroker,
    "xtp": XTPBroker,
    "ctp": CTPBroker,
}


def get_broker(broker_type: str) -> BrokerAdapter:
    """按类型创建柜台适配器。

    未知类型直接报错，避免静默降级造成"以为在实盘其实在模拟"的风险。
    """
    cls = _BROKERS.get(broker_type)
    if cls is None:
        raise CustomException(msg=f"未知柜台类型：{broker_type}，可选：{list(_BROKERS)}")
    return cls()
