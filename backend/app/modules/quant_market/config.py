"""量化市场配置：A 股（中泰 XTP）与期货（CTP）的参数化定义。

两个市场共用同一套 service/controller，仅在以下维度不同：
- 表名前缀（quant_astock_* / quant_futures_*）
- 本地数据目录（user_data/data/astock / futures）
- 柜台适配器类型（xtp / ctp）

策略目录三市场共用 user_data/strategies，保证同一套策略可用于 web3 回测、
模拟盘、实盘以及 A股/期货的回测与实时 tick（tick 引擎固定从共享目录加载）。

柜台适配器当前默认离线/模拟；XTP/CTP SDK 的具体实现在 broker.py 中以可插拔
接口预留，后续填入 SDK 即可切换为真实报单，无需改动上层业务代码。
"""

from dataclasses import dataclass
from pathlib import Path

from app.config.path_conf import QUANT_ASTOCK_DIR, QUANT_FUTURES_DIR, QUANT_STRATEGY_DIR


@dataclass(frozen=True)
class MarketConfig:
    """单个市场的静态配置。"""

    key: str
    label: str
    table_prefix: str
    data_dir: Path
    strategy_dir: Path
    broker_type: str
    symbol_example: str

    @property
    def dataset_table(self) -> str:
        """数据集表名。"""
        return f"{self.table_prefix}_dataset"

    @property
    def backtest_table(self) -> str:
        """回测表名。"""
        return f"{self.table_prefix}_backtest"

    @property
    def live_table(self) -> str:
        """实时策略表名。"""
        return f"{self.table_prefix}_live_strategy"

    @property
    def trade_table(self) -> str:
        """成交流水表名。"""
        return f"{self.table_prefix}_trade_record"


MARKETS: dict[str, MarketConfig] = {
    "astock": MarketConfig(
        key="astock",
        label="A股（中泰XTP）",
        table_prefix="quant_astock",
        data_dir=QUANT_ASTOCK_DIR,
        strategy_dir=QUANT_STRATEGY_DIR,
        broker_type="xtp",
        symbol_example="600519.SH",
    ),
    "futures": MarketConfig(
        key="futures",
        label="期货（CTP）",
        table_prefix="quant_futures",
        data_dir=QUANT_FUTURES_DIR,
        strategy_dir=QUANT_STRATEGY_DIR,
        broker_type="ctp",
        symbol_example="rb2410.SHFE",
    ),
}


def get_market(key: str) -> MarketConfig:
    """按 key 取市场配置，未知市场抛 ValueError。"""
    if key not in MARKETS:
        raise ValueError(f"未知市场 {key}，可选：{list(MARKETS)}")
    return MARKETS[key]
