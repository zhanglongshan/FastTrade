"""量化策略子模块 | Quantitative strategy subpackage.

目的 | Purpose:
    作为量化回测策略功能的包入口，聚合策略 API、指标、加载器与查询控制器，
    为上层提供统一的“策略发现 -> 策略执行 -> 回测成交”能力。
    Acts as the package entry point for quantitative backtesting strategies, aggregating
    the strategy API, indicators, loader and query controller to offer a unified
    "strategy discovery -> strategy execution -> backtest fills" capability.

主要流程 | Main flow:
    1. loader.py 扫描 user_data/strategies/*.py 自动发现策略类 | loader.py scans strategy files to auto-discover strategy classes.
    2. api.py 提供 TradeAPI 供策略下单并驱动逐 K 线撮合 | api.py provides TradeAPI for orders and per-candle matching.
    3. indicators.py 提供仅依赖标准库的轻量技术指标 | indicators.py provides lightweight stdlib-only technical indicators.
    4. controller.py 暴露 FastAPI 路由供前端查询可用策略 | controller.py exposes FastAPI routes for querying available strategies.

子模块 | Submodules:
    - api: 策略基类、成交/订单数据结构与交易 API | Strategy base class, trade/order data structures and trading API.
    - indicators: SMA / EMA / RSI 等技术指标 | SMA / EMA / RSI and other technical indicators.
    - loader: 策略自动发现与动态加载 | Strategy auto-discovery and dynamic loading.
    - controller: 策略查询 HTTP 接口 | Strategy query HTTP endpoints.
"""
