# FastTrade 量化模块操作文档

本目录集中说明量化模块的安装、数据、策略、回测、命令行与 API。所有页面与命令的操作步骤都在这里维护，不再散落到 README 或聊天记录中。

## 1. 功能总览

侧栏「量化」目录已拆分为三个独立市场，结构一致、共用同一套策略与执行引擎：

| 市场目录 | 数据目录 | 数据表前缀 | 柜台/行情源 | 页面 |
| --- | --- | --- | --- | --- |
| **web3-量化** | `user_data/data/binance` | `quant_web3_*` | 币安 REST / 公开镜像 / 历史归档 | 下载、数据图表、回测、模拟盘、实盘 |
| **量化-A股** | `user_data/data/astock` | `quant_astock_*` | 中泰 XTP（可插拔，默认模拟） | 数据、数据图表、回测、模拟盘、实盘 |
| **量化-期货** | `user_data/data/futures` | `quant_futures_*` | CTP（可插拔，默认模拟） | 数据、数据图表、回测、模拟盘、实盘 |

| 能力 | 页面 | 主要命令 | 说明 |
| --- | --- | --- | --- |
| 币安数据下载 | web3-量化-下载 | `python main.py quant download` | 官方 REST / 公开镜像 / 历史归档，下载 OHLCV 到 `user_data/data/binance/*.csv` |
| 本地 CSV 导入 | 各市场-数据 | `python main.py quant import` / API | 导入符合列规范的 CSV 并登记数据集 |
| 数据图表 | 各市场-数据图表 | 页面 | ECharts 绘制 K 线 + 成交量 + 明细表格 |
| 策略自动发现 | 量化-回测 | `python main.py quant strategies` | 扫描 `user_data/strategies/*.py` |
| 策略回测 | 各市场-回测 | `python main.py quant backtest` | 事件/信号两种策略模式，输出收益与成交流水 |
| 模拟盘 | 各市场-模拟盘 | `python main.py quant live-create/live-tick` | 实时行情 + 虚拟资金，本地即时撮合 |
| 实盘 | 各市场-实盘 | `python main.py quant live-create` | 复用同策略，默认 dry-run |

> **一套策略，三种模式、三个市场**：`user_data/strategies/` 下的同一份策略可被 web3 / A股 / 期货共用；回测、模拟盘、实盘共用 `api.buy/sell/sellshort/buycover/close_all` 接口，仅执行器与柜台不同。详见 [统一策略接口](./07-统一策略接口.md) 与 [三市场与数据图表](./08-三市场与数据图表.md)。

## 2. 环境与数据库

- PostgreSQL：数据库 `fast_trade`，用户 `postgres`，密码已在本地 `backend/env/.env.dev` 中配置（该文件已加入 `.gitignore`，**不要提交**）。
- Redis：项目启动依赖 Redis，默认 `localhost:6379`，请先启动本机 Redis。
- Python：≥ 3.12（当前 `.python-version` 为 3.14，已验证可用）；推荐 `uv` 管理依赖。
- PostgreSQL 驱动 `asyncpg` / `psycopg` 已列入核心依赖，`uv sync` 默认安装，无需再带 `--extra postgres`。
- SQL 解析使用纯 Python 的 `sqlglot`，**不要改回 `sqlglot[rs]`**：其 Rust 扩展在 Python 3.14 下无预编译 wheel，会触发源码编译导致 `uv sync` 卡在 `Building sqlglotrs`。

首次启动会自动建表并初始化基础数据。量化相关表由 Alembic 迁移生成（迁移文件在 `backend/app/alembic/versions/`），共 12 张：

- web3：`quant_web3_dataset`、`quant_web3_backtest`、`quant_web3_live_strategy`、`quant_web3_trade_record`
- A股：`quant_astock_dataset`、`quant_astock_backtest`、`quant_astock_live_strategy`、`quant_astock_trade_record`
- 期货：`quant_futures_dataset`、`quant_futures_backtest`、`quant_futures_live_strategy`、`quant_futures_trade_record`

> 旧版 `quant_*` 表已在迁移中重命名为 `quant_web3_*` 并保留历史数据。

若从旧版本升级后侧栏缺少「数据图表 / 量化-A股 / 量化-期货」菜单，执行一次幂等补菜单命令（既有库不会自动重导种子数据）：

```bash
python main.py quant init-menu --env=dev
```

```bash
cd backend
uv sync
uv run main.py run --env=dev


## 🚀 5–15 分钟本地跑起来

```bash
# 1. 克隆
git clone https://gitee.com/stylesmile/fasttrade.git

# 2. 配置环境
cp backend/env/.env.example backend/env/.env.dev
cp frontend/web/.env.development.example frontend/web/.env.development

# 3. 启动后端（首次自动建表 + 初始化数据）
cd backend && uv sync && uv run main.py run --env=dev

# 4. 启动前端
cd ../frontend/web && pnpm install && pnpm run dev

# ✅ 浏览器打开 http://127.0.0.1:5173，用 admin/123456 登录
```
```

> 注意：`uv run` 默认会重新同步并修剪 optional extra。本项目所需驱动均已在核心依赖中，直接用 `uv run` 即可；若日后新增 extra 依赖，需用 `uv run --extra <name>` 或 `uv run --no-sync`。

接口文档：<http://127.0.0.1:8001/docs>

## 3. 前端页面

开发模式：

```bash
cd front-web
pnpm install
pnpm dev
```

页面入口（均由后端 `sys_menu` 动态注册到侧栏，父目录为 web3-量化 / 量化-A股 / 量化-期货）：

- web3-量化：`/quant/download`、`/quant/chart`、`/quant/backtest`、`/quant/paper`、`/quant/live`
- 量化-A股：`/quant-astock/data`、`/quant-astock/chart`、`/quant-astock/backtest`、`/quant-astock/paper`、`/quant-astock/live`
- 量化-期货：`/quant-futures/data`、`/quant-futures/chart`、`/quant-futures/backtest`、`/quant-futures/paper`、`/quant-futures/live`

若新菜单未出现，重新登录以刷新菜单。普通用户访问 API 需要以下权限码；超级管理员默认放行：

| 权限码 | 用途 |
| --- | --- |
| `module_quant:data:query` | 查询数据集/本地文件 |
| `module_quant:data:download` | 下载币安数据 |
| `module_quant:data:import` | 导入本地 CSV |
| `module_quant:strategy:query` | 查询策略列表 |
| `module_quant:backtest:run` | 执行回测 |
| `module_quant:backtest:query` | 查询回测记录 |
| `module_quant:live:create` | 创建实时策略实例 |
| `module_quant:live:query` | 查询实例/成交流水 |
| `module_quant:live:update` | 更新实例配置 |
| `module_quant:live:start` | 启动/停止实例 |
| `module_quant:live:run` | 手动运行一次 tick |
| `module_quant:live:delete` | 删除实例 |

A股/期货的权限码前缀分别为 `module_quant_astock:*` / `module_quant_futures:*`（如 `module_quant_astock:data:query`、`module_quant_futures:live:query`）。

## 4. 目录约定

```text
user_data/
├── data/
│   ├── binance/                  # 币安下载数据（现货；不入库到 Git）
│   │   └── futures/              # 币安 U 本位合约数据
│   ├── astock/                   # A股导入数据
│   ├── futures/                  # 期货导入数据
│   └── *.csv                     # 导入的本地数据（供页面选择）
└── strategies/                   # 三市场共用策略目录
    ├── sma_cross_strategy.py     # 事件模式示例
    └── rsi_signal_strategy.py    # 信号模式示例

backend/app/modules/
├── quant/                        # web3-量化
│   ├── data/                     # 下载、导入、数据集、K线接口
│   ├── strategy/                 # 策略基类、api 封装、自动发现
│   ├── backtest/                 # 回测引擎、记录接口
│   ├── execution/                # 统一执行层（账本/策略API/币安网关/运行时引擎）
│   ├── runtime/                  # 实时策略实例服务与接口（模拟盘/实盘）
│   ├── model.py / schema.py / cli.py
│   └── data/binance_download.py  # REST / 镜像 / 历史归档下载器
└── quant_market/                 # A股 + 期货通用模块（按 market key 参数化）
    ├── config.py                 # 市场配置（表名/数据目录/柜台类型）
    ├── broker.py                 # XTP / CTP 可插拔柜台适配层（默认模拟）
    ├── model.py / schema.py / service.py / controller.py
front-web/src/views/
├── module_quant/                 # web3 页面（含 chart 数据图表）
├── module_quant_astock/          # A股页面（薄包装）
├── module_quant_futures/         # 期货页面（薄包装）
└── module_quant_market/          # A股/期货共享工作区组件
```

## 5. 文档索引

1. [数据下载与导入](./01-数据下载与导入.md)
2. [策略开发](./02-策略开发.md)
3. [回测操作](./03-回测操作.md)
4. [命令行](./04-命令行.md)
5. [API 参考](./05-API参考.md)
6. [模拟盘与实盘](./06-模拟盘与实盘.md)
7. [统一策略接口（回测/模拟/实盘）](./07-统一策略接口.md)
8. [三市场与数据图表](./08-三市场与数据图表.md)
9. [执行架构与一致性](./09-执行架构与一致性.md)

## 6. 边界说明

- 回测按「当前 K 线收盘产生信号 → 下一根 K 线开盘成交」撮合，最后一根 K 线的信号默认按收盘价成交
  （`final_fill_basis="skip"` 可改为直接放弃）；末尾未收盘的 K 线会被丢弃并记入 `dropped_unclosed`，
  避免用尚未走完的收盘价产生信号。
- 模拟盘/实盘同样按「收盘出信号 → 下一根开盘成交」执行：行情由币安 WebSocket（kline + bookTicker）推送，
  K 线收盘瞬间驱动 tick，WebSocket 不可用时退回 REST 轮询；成交量受 L1 盘口深度约束，
  被裁掉的差额记入 `unfilled` 并由模拟盘在下一 tick 补成交。
- 实盘默认 `dry_run=True`，不连接交易所、不处理真实资金；仅当显式关闭 dry-run 且配置币安 API Key/Secret 后才会真实下单。真实下单会回查订单、超时撤单、回填真实手续费，并定期与交易所余额/成交/挂单对账。**币安现货不支持做空**，实盘真实模式下 `sellshort`/`buycover` 会被拒绝。
- 已支持杠杆与维持保证金：`leverage>1` 时按保证金记账，净值跌破维持保证金触发强平（`liquidation`）；
  尚未实现资金费率、盘口多档深度、部分成交排队等期货细节，深度只用到最优一档（bookTicker）。
- 密钥使用 Fernet 加密落库，任何接口都不会返回密钥明文。
- CSV 使用标准列 `date, open, high, low, close, volume`。
