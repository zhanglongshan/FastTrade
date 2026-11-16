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
| `limit` | query | 默认 100，最大 500 |

### GET `/api/v1/quant/data/files`

列出可导入的本地 CSV 文件，返回 `path`、`name`、`size`、`modified_time`。

### GET `/api/v1/quant/data/detail/{dataset_id}`

查询单个数据集。

### POST `/api/v1/quant/data/download`

下载币安数据。

```json
{
  "symbol": "BTC/USDT",
  "interval": "1h",
  "start_time": "2024-01-01",
  "end_time": "2024-06-30",
  "limit": 1000,
  "description": "回测用数据"
}
```

返回：`name`、`symbol`、`interval`、`file_path`、`downloaded`、`row_count`、`dataset_id`。

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

执行回测。

```json
{
  "dataset_id": 1,
  "strategy_name": "SmaCrossStrategy",
  "name": "BTC-1h-均线测试",
  "initial_cash": 10000,
  "fee_rate": 0.001,
  "slippage": 0.0005
}
```

返回 `QuantBacktestOutSchema`，其中 `result` 包含收益、回撤、成交流水与净值曲线。

### GET `/api/v1/quant/backtest/list`

查询回测记录，支持 `limit` 参数。

### GET `/api/v1/quant/backtest/detail/{backtest_id}`

查询回测详情。失败记录的 `status=2`，原因在 `error` 字段。

## 4. 权限码

| 接口 | 权限码 |
| --- | --- |
| 数据查询类 | `module_quant:data:query` |
| 下载 | `module_quant:data:download` |
| 导入 | `module_quant:data:import` |
| 策略查询 | `module_quant:strategy:query` |
| 执行回测 | `module_quant:backtest:run` |
| 回测查询 | `module_quant:backtest:query` |

给普通角色授权时，在「系统管理 → 角色管理」勾选对应权限；超级管理员默认拥有全部权限。

## 5. 调用示例

```bash
# 需要先获取登录 token
curl -X POST http://127.0.0.1:8001/api/v1/system/auth/login \
  -d "username=admin&password=<your-password>"

curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8001/api/v1/quant/strategies
```
