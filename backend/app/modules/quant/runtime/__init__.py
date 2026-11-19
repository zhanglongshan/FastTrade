"""量化实时策略运行包：模拟盘与实盘共用策略与执行层。

目的 | Purpose:
    本包（runtime）承载“实时策略实例”的运行能力，是模拟盘（paper）与实盘
    （live）共用的策略层与执行层的聚合入口。它把策略加载、账户账本、执行器、
    tick 驱动、状态轮询与持久化等能力组织在一起，对上层接口暴露统一的服务
    （service）与控制器（controller）。
    This package (runtime) hosts the runtime capabilities of "live strategy
    instances". It is the aggregate entry point where the paper (simulated)
    and live (real) trading modes share the same strategy layer and execution
    layer. It groups strategy loading, account ledger, executors, tick driving,
    status polling and persistence, exposing a unified service and controller
    to the upper API layer.

主要流程 | Main flow:
    1. 通过 :mod:`service` 创建 / 更新 / 查询 / 删除策略实例，并将 API 密钥加密落库。
       Create / update / query / delete strategy instances via ``service`` and
       store API secrets encrypted.
    2. :class:`RuntimeManager` 为每个运行中的实例维护一个 asyncio 任务，按周期
       定时驱动 ``run_strategy_tick``。
       ``RuntimeManager`` keeps one asyncio task per running instance and drives
       ``run_strategy_tick`` periodically according to its interval.
    3. 每次 tick 复用同一套策略逻辑，仅执行器不同（PaperExecutor 本地虚拟撮合，
       LiveExecutor 视 dry_run 决定是否真实下单）。
       Each tick reuses the same strategy logic; only the executor differs
       (PaperExecutor for local virtual matching, LiveExecutor which may place
       real orders depending on dry_run).
    4. tick 结果（账本、最近结果、成交流水、错误、状态）持久化到数据库，服务重启
       后由 ``restore`` 自动恢复运行中的实例。
       Tick results (ledger, latest result, trade records, errors, status) are
       persisted to the database, and ``restore`` resumes running instances
       after a service restart.

模块内容 | Module contents:
    - :mod:`service`: 实例服务与调度器实现 | Instance service and scheduler.
    - :mod:`controller`: FastAPI 路由接口 | FastAPI routing endpoints.
"""
