"""量化数据子模块 | Quantitative data subpackage.

用途 | Purpose:
    负责量化交易数据的完整生命周期管理：从币安（Binance）下载 K 线、公开 REST 镜像拉取、
    历史归档 ZIP 解析、本地 CSV 导入导出，以及数据集元数据的登记、查询与审计。

主要流程 | Main flow:
    1. binance_download.py：封装 REST 分页、镜像与归档下载，统一输出 OHLCV 标准行；
    2. service.py：编排下载/导入/合并落盘、数据集元数据 CRUD 与 CSV 路径安全校验；
    3. controller.py：以 FastAPI 路由形式对外暴露下载、导入、列表、详情与 K 线查询接口。
"""
