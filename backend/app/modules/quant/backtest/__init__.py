"""量化回测子模块 | Quantitative backtesting subpackage.

目的 | Purpose:
    提供策略回测的完整能力：从 CSV 加载 K 线、驱动事件/信号两种回测模式、
    计算收益与回撤指标，并通过服务层持久化结果、通过控制器对外暴露 API。
    Provides full backtesting capability: load candles from CSV, drive event/signal
    backtest modes, compute return and drawdown metrics, persist results via the
    service layer, and expose APIs through the controller.

主流程 | Main flow:
    1. engine.load_candles 读取并校验标准 OHLCV CSV | Read and validate OHLCV CSV.
    2. engine.run_backtest 按策略逐根 K 线撮合，生成净值与成交明细 | Run strategy bar-by-bar.
    3. service.create_backtests 校验数据集并落库回测记录 | Validate datasets and persist.
    4. controller 提供 /run、/list、/detail/{id} 接口 | Expose HTTP endpoints.
"""
