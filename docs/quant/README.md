# FastTrade 量化模块操作文档

本目录集中说明量化模块的安装、数据、策略、回测、命令行与 API。所有页面与命令的操作步骤都在这里维护，不再散落到 README 或聊天记录中。

## 1. 功能总览

| 能力 | 页面 | 主要命令 | 说明 |
| --- | --- | --- | --- |
| 币安数据下载 | 量化-下载 | `python main.py quant download` | 下载 OHLCV 到 `user_data/data/binance/*.csv` |
| 本地 CSV 导入 | 量化-下载 | `python main.py quant import` | 导入符合列规范的 CSV 并登记数据集 |
| 策略自动发现 | 量化-回测 | `python main.py quant strategies` | 扫描 `user_data/strategies/*.py` |
| 策略回测 | 量化-回测 | `python main.py quant backtest` | 事件/信号两种策略模式，输出收益与成交流水 |
| 记录查询 | 量化-回测 | API | 回测记录、净值曲线、成交明细入库 |

## 2. 环境与数据库

- PostgreSQL：数据库 `fast_trade`，用户 `postgres`，密码已在本地 `backend/env/.env.dev` 中配置（该文件已加入 `.gitignore`，**不要提交**）。
- Redis：项目启动依赖 Redis，默认 `localhost:6379`，请先启动本机 Redis。
- Python：≥ 3.12（当前 `.python-version` 为 3.14，已验证可用）；推荐 `uv` 管理依赖。
- PostgreSQL 驱动 `asyncpg` / `psycopg` 已列入核心依赖，`uv sync` 默认安装，无需再带 `--extra postgres`。
- SQL 解析使用纯 Python 的 `sqlglot`，**不要改回 `sqlglot[rs]`**：其 Rust 扩展在 Python 3.14 下无预编译 wheel，会触发源码编译导致 `uv sync` 卡在 `Building sqlglotrs`。

首次启动会自动建表并初始化基础数据；新增的 `quant_dataset`、`quant_backtest` 两张表会在 dev 环境由 Alembic 自动迁移生成。

```bash
cd backend
uv sync
uv run main.py run --env=dev
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

页面入口：

- `/quant/download`：量化-下载
- `/quant/backtest`：量化-回测

页面为静态路由，登录后可见。普通用户访问 API 需要以下权限码；超级管理员默认放行：

| 权限码 | 用途 |
| --- | --- |
| `module_quant:data:query` | 查询数据集/本地文件 |
| `module_quant:data:download` | 下载币安数据 |
| `module_quant:data:import` | 导入本地 CSV |
| `module_quant:strategy:query` | 查询策略列表 |
| `module_quant:backtest:run` | 执行回测 |
| `module_quant:backtest:query` | 查询回测记录 |

## 4. 目录约定

```text
user_data/
├── data/
│   ├── binance/                  # 币安下载数据（不入库到 Git）
│   └── *.csv                     # 导入的本地数据
└── strategies/
    ├── sma_cross_strategy.py     # 事件模式示例
    └── rsi_signal_strategy.py    # 信号模式示例

backend/app/modules/quant/
├── data/                         # 下载、导入、数据集接口
├── strategy/                     # 策略基类、api 封装、自动发现
├── backtest/                     # 回测引擎、记录接口
├── model.py                      # ORM 模型
├── schema.py                     # Pydantic 模型
└── cli.py                        # 命令行入口
```

## 5. 文档索引

1. [数据下载与导入](./01-数据下载与导入.md)
2. [策略开发](./02-策略开发.md)
3. [回测操作](./03-回测操作.md)
4. [命令行](./04-命令行.md)
5. [API 参考](./05-API参考.md)

## 6. 边界说明

- 当前仅支持**回测**，不连接交易所下单，不处理真实资金。
- 回测按「当前 K 线收盘产生信号 → 下一根 K 线开盘成交」撮合，最后一根 K 线的信号按收盘价成交。
- 未实现资金费率、杠杆维持保证金、订单簿深度、部分成交等期货细节；`sellshort` / `buycover` 为简化保证金模型。
- CSV 使用标准列 `date, open, high, low, close, volume`。
