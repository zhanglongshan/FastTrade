# 05 API 参考

所有接口挂在 `/api/v1/quant` 下，返回项目统一响应信封：

```json
{
  "code": 0,
  "msg": "查询成功",
  "data": [],
  "status_code": 200,
  "success": true
}
```

未登录或缺少权限时返回 401/403；业务错误通过 `msg` 说明原因。

## 1. 数据接口

### GET `/api/v1/quant/data/list`

查询数据集列表。

| 参数 | 位置 | 说明 |
| --- | --- | --- |
| `symbol` | query | 可选，如 `BTCUSDT` |
| `interval` | query | 可选，如 `1h` |
| `market` | query | 可选，`spot`-现货 / `futures`-合约 |
| `limit` | query | 默认 100，最大 500 |

### GET `/api/v1/quant/data/files`

列出可导入的本地 CSV 文件，返回 `path`、`name`、`size`、`modified_time`。

### GET `/api/v1/quant/data/detail/{dataset_id}`

查询单个数据集。

### POST `/api/v1/quant/data/download`

下载币安数据。支持 `api`（官方 REST）、`mirror`（公开 REST 镜像，仅现货）、`archive`（历史归档 ZIP）三种来源。

```json
{
  "symbol": "BTC/USDT",
  "interval": "1h",
  "start_time": "2024-01-01",
  "end_time": "2024-06-30",
  "days": null,
  "source": "archive",
  "market": "spot",
  "erase": false,
  "fill_recent": true,
  "limit": 1000,
  "description": "回测用数据"
}
```

| 字段 | 说明 |
| --- | --- |
| `source` | `api` / `mirror` / `archive`，默认 `api` |
| `market` | `spot` / `futures`，默认 `spot` |
| `days` | 最近 N 天，设置后优先于 `start_time`/`end_time` |
| `erase` | 清空本地已有数据后重新写入 |
| `fill_recent` | 归档方式是否用公开镜像补齐近端数据 |

返回：`name`、`symbol`、`interval`、`source`、`market`、`file_path`、`downloaded`、`row_count`、`dataset_id`、`requests`、`archive_files`、`missing_periods`、`warnings`。

### POST `/api/v1/quant/data/import`

导入本地 CSV。

```json
{
  "file_path": "user_data/data/my_data.csv",
  "symbol": "BTC/USDT",
  "interval": "1h",
  "source": "local"
}
```

## 2. 策略接口

### GET `/api/v1/quant/strategies`

返回自动发现的策略列表：

```json
[
  {
    "name": "SMA均线交叉",
    "file": "user_data/strategies/sma_cross_strategy.py",
    "mode": "event",
    "timeframe": "1h",
    "doc": "5/20 周期均线交叉策略。"
  }
]
```

## 3. 回测接口

### POST `/api/v1/quant/backtest/run`

执行回测，支持一次选择多个数据集（最多 50 个）。`dataset_id` 为旧版单数据集兼容字段，与新字段合并去重。

```json
{
  "dataset_ids": [1, 2, 3],
  "strategy_name": "SmaCrossStrategy",
  "name": "BTC-1h-均线测试",
  "initial_cash": 10000,
  "fee_rate": 0.001,
  "slippage": 0.0005
}
```

返回 `QuantBacktestOutSchema` 列表，每个选中的数据集一条记录；记录中的 `market` 标明现货/合约，现货回测自动禁止开空。单个数据集失败时该条 `status=2`、`error` 有原因，其余记录照常返回。

### GET `/api/v1/quant/backtest/list`

查询回测记录，支持 `limit` 参数。

### GET `/api/v1/quant/backtest/detail/{backtest_id}`

查询回测详情。失败记录的 `status=2`，原因在 `error` 字段。

## 4. 实时策略接口（模拟盘/实盘）

统一前缀 `/api/v1/quant/live`。模拟盘与实盘共用这些接口，以 `mode`（paper/live）区分执行器。完整字段与风险说明见 [06-模拟盘与实盘](./06-模拟盘与实盘.md)。

| 方法 | 路径 | 说明 | 权限码 |
| --- | --- | --- | --- |
| POST | `/create` | 创建实例 | `module_quant:live:create` |
| GET | `/list?mode=&limit=` | 实例列表（可按 paper/live 过滤） | `module_quant:live:query` |
| GET | `/detail/{id}` | 实例详情（不含密钥明文） | `module_quant:live:query` |
| PUT | `/update/{id}` | 更新费率/滑点/dry_run/密钥等 | `module_quant:live:update` |
| POST | `/start/{id}` | 启动轮询 | `module_quant:live:start` |
| POST | `/stop/{id}` | 停止轮询 | `module_quant:live:start` |
| POST | `/tick/{id}` | 手动运行一次 | `module_quant:live:run` |
| GET | `/trades/{id}` | 成交流水 | `module_quant:live:query` |
| DELETE | `/delete/{id}` | 删除实例 | `module_quant:live:delete` |

创建实例 `POST /create` 请求体关键字段：

```json
{
  "name": "BTC-1h-SMA",
  "mode": "paper",
  "strategy_name": "SmaCrossStrategy",
  "symbol": "BTC/USDT",
  "interval": "1h",
  "initial_cash": 10000,
  "fee_rate": 0.001,
  "slippage": 0,
  "dry_run": true,
  "allow_short": true
}
```

`mode=live` 且 `dry_run=false` 时需额外提供 `api_key` 与 `api_secret`（加密存储，响应永不回传明文）。

## 5. 权限码

| 接口 | 权限码 |
| --- | --- |
| 数据查询类 | `module_quant:data:query` |
| 下载 | `module_quant:data:download` |
| 导入 | `module_quant:data:import` |
| 策略查询 | `module_quant:strategy:query` |
| 执行回测 | `module_quant:backtest:run` |
| 回测查询 | `module_quant:backtest:query` |
| 实时策略 | `module_quant:live:create/query/update/start/run/delete` |

给普通角色授权时，在「系统管理 → 角色管理」勾选对应权限；超级管理员默认拥有全部权限。

## 6. 调用示例

```bash
# 需要先获取登录 token
curl -X POST http://127.0.0.1:8001/api/v1/system/auth/login \
  -d "username=admin&password=<your-password>"

curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8001/api/v1/quant/strategies
```
