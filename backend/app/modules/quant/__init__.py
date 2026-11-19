"""量化模块：币安数据下载、本地数据导入、策略加载与回测 | Quantitative module: Binance data download, local data import, strategy loading and backtesting.

模块划分 / Module layout:
- data：数据下载与 CSV 元数据管理 | Data download and CSV metadata management
- strategy：策略基类、交易 API 封装、策略自动发现 | Strategy base classes, trading API wrappers and strategy auto-discovery
- backtest：回测引擎与回测记录 | Backtest engine and backtest records

主要流程 / Main flow:
1. 通过 data 子模块下载或导入 K 线数据并登记数据集 | Download or import candles via the data submodule and register datasets.
2. 通过 strategy 子模块自动发现并加载用户策略 | Auto-discover and load user strategies via the strategy submodule.
3. 通过 backtest 子模块运行回测并保存结果摘要 | Run backtests via the backtest submodule and persist result summaries.
"""
