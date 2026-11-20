# FastTrade vs QuantDinger 对比与差距分析

> 对比基线：FastTrade（本仓库，`backend` 3.2.0 + `front-web`）与 QuantDinger `v5.6.1`（`D:\code\quant\QuantDinger-github-1118-3`）。
> 结论只陈述**可核对的事实**，标注来源路径；对无法确认的能力不做推断。
> 调研方式：双方均以静态阅读（源码 / SQL / 配置 / 文档）为准，未启动服务、未抓取运行时指标。

---

## 0. 一句话结论

FastTrade 的强项是**管理后台底座完整**（权限/组织/存储/运维/代码生成/任务调度 UI 开箱可用）与**前端源码完整可改**；QuantDinger 的强项是**交易专业度、分布式运行时、工程化治理与文档体系**。

差距最集中的三个方向：

| 优先级 | 方向 | 一句话 |
| --- | --- | --- |
| **P0** | 工程化与质量门禁 | FastTrade **完全没有 CI**，无安全扫描、无契约测试、无部署产物；这是所有差距里最容易补齐、收益最高的一块 |
| **P0** | 交易能力深度 | FastTrade 仅币安现货实盘，A股/期货柜台为占位；回测仅 4 个指标，无参数优化、无因子研究、无组合策略 |
| **P1** | 架构与可观测性 | FastTrade 单进程单体 + 无指标暴露；QuantDinger 8 种进程角色 + 租约/栅栏/inbox + Prometheus 栈 |

---

## 1. 规模与形态对比

| 维度 | FastTrade | QuantDinger | 说明 |
| --- | --- | --- | --- |
| 后端 Python 文件 | 515 | 997 | FastTrade 含大量双语文档字符串，实际业务代码量差距小于文件数比 |
| 后端测试文件 | 212（其中 `*integration*` 49） | 393（其中 `*contract*` 22） | 数量接近，**类型分布差异极大** |
| 前端源码 | 443 个 `.vue`/`.ts`（`front-web/`） | **不在本仓库**（预构建镜像） | FastTrade 前端可改，QuantDinger 前端闭源 |
| 文档 | 20 篇（`docs/`） | 141 个文件（118 篇 Markdown / 16,478 行） | 6 倍以上差距 |
| CI 流水线 | **0**（无 `.github/` 目录） | **8 条** | 最显著的工程化差距 |
| 数据库迁移 | Alembic（15 个版本文件） | 手写幂等 SQL + `qd_bootstrap_migrations` 内容校验 | 两种范式，各有取舍（见 §6.3） |
| 部署产物 | 无 Dockerfile / 无 docker-compose | Dockerfile + 9 个 compose 文件 + install.sh/ps1 + AWS Marketplace 模板 | FastTrade 无法一键部署 |

---

## 2. P0 差距一：工程化与质量门禁

### 2.1 CI 完全缺失

FastTrade 仓库根目录**没有 `.github/` 目录**（实测 `Get-ChildItem .github` 为空），意味着：

- 没有自动 lint / 测试 / 覆盖率
- 没有依赖漏洞扫描、密钥扫描、SAST
- 没有 PR 模板、CODEOWNERS、Dependabot
- 没有任何发布流程自动化

QuantDinger 的 8 条流水线可直接作为补齐模板：

| Workflow | 作用 | FastTrade 对应缺口 |
| --- | --- | --- |
| `basic-ci.yml` | ruff + 编译检查 + 分片 pytest + 覆盖率 + compose 配置校验 + 版本/编码一致性 | 全部缺失 |
| `security-ci.yml` | `pip-audit` 依赖审计 + `bandit` 源码扫描 + `gitleaks` 全历史密钥扫描 + `codeql` | 全部缺失 |
| `openapi-ci.yml` | OpenAPI 导出漂移 diff + Spectral lint + `oasdiff` 破坏性变更门禁 | 全部缺失（FastTrade 只有 FastAPI 自动 `/docs`，无契约文件） |
| `mcp-ci.yml` | MCP 包多版本（3.10/3.12/3.13）测试 + 构建 | 不适用（FastTrade 无 MCP 包） |
| `docker-publish.yml` | 多架构镜像 + SBOM + provenance + Sigstore keyless 签名 | 全部缺失（且无镜像可发布） |
| `dco.yml` | PR 强制 `Signed-off-by` | 缺失 |
| `reuse.yml` | REUSE 许可证元数据合规 | 缺失 |
| `scorecard.yml` | OpenSSF Scorecard 评分 | 缺失 |

> 参考实现：`QuantDinger/.github/workflows/`（8 个文件，全部 workflow 顶层 `permissions: contents: read`，所有 `uses:` 以 40 位 commit SHA 固定）。

### 2.2 无契约测试（这是隐性风险最高的缺口）

FastTrade 有 212 个测试文件，但 **0 个契约测试**（`*contract*` 匹配数为 0）。前后端一致性目前完全依赖人工纪律——AGENTS.md 要求"权限码前后端必须完全一致""改接口契约三处必须同一次改动内对齐"，但**没有任何自动化校验**。

QuantDinger 的做法（可直接借鉴）：

| 契约校验 | 断言内容 | 来源 |
| --- | --- | --- |
| OpenAPI 导出漂移 | CI 重新导出 spec 并与已提交 YAML `diff`，不一致直接 fail | `openapi-ci.yml` L54-68 |
| Agent 契约对齐 | OpenAPI 的 (method, path) 集合**完全等于**路由装饰器解析出的集合 | `tests/test_ai_agent_contract_alignment.py` L88-98 |
| scope 对齐 | 每个 operation 的 `x-scope-class` **完全等于**路由声明的 scope | 同上 L140-157 |
| MCP 工具对齐 | MCP 工具名（AST 解析）== 后端工具注册表；所有工具 route 必须出现在 OpenAPI 中 | 同上 L160-173 |
| 破坏性变更 | `oasdiff breaking --fail-on ERR` | `openapi-ci.yml` L92-98 |

**对 FastTrade 的直接映射建议**：写一个 `tests/test_permission_contract.py`，用 AST 解析 `backend/app/**/controller.py` 中所有 `Security(AuthPermission([...]))` 字符串，与 `backend/sql/sys_menu.json` 中所有 `permission` 字段做集合比对——这正是本次管理后台文档人工核对过一遍的工作（`docs/admin/07-权限与安全.md`），完全可以自动化。

### 2.3 无安全扫描与依赖审计

| 能力 | FastTrade | QuantDinger |
| --- | --- | --- |
| 依赖锁定 | `uv.lock`（有，但无 CI 校验） | `requirements.lock` + `check_requirements_lock.py` CI 校验"每个直接生产依赖都被 lock 覆盖" |
| 依赖漏洞审计 | 无 | `pip-audit -r requirements.lock --no-deps` |
| 源码 SAST | 无 | `bandit -lll -ii`（high severity + high confidence）+ CodeQL |
| 密钥扫描 | 无 | `gitleaks` 全历史 + `--redact` + 18 条白名单 |
| 供应链评分 | 无 | OpenSSF Scorecard 每周 |

### 2.4 无 Docker / 无部署产物

FastTrade 无 `Dockerfile`、无 `docker-compose*.yml`、无安装脚本。部署只能靠 AGENTS.md 手写的 `uv sync` + `pnpm build` 流程。

QuantDinger 提供：`Dockerfile`（非 root UID 10001、基础镜像固定 digest）+ 9 个 compose 文件（开发/生产/可观测性/GHCR 预构建/AWS Marketplace）+ `install.sh`/`install.ps1` 一键安装 + `deploy/aws-marketplace/` CloudFormation 模板。

### 2.5 治理文件几乎全缺

| 文件 | FastTrade | QuantDinger |
| --- | --- | --- |
| `AGENTS.md` | ✅ 有（且比对方更详细） | ✅ 有（46 行） |
| `SECURITY.md`（漏洞披露 + SLA） | ❌ | ✅ 203 行，含 72h 确认 / 7d 初评 / 14d 修复 SLA + 3 条历史 Advisory |
| `CONTRIBUTING.md` | ❌ | ✅ 281 行 |
| `GOVERNANCE.md` | ❌ | ✅ 53 行 |
| `CODE_OF_CONDUCT.md` | ❌ | ✅ 116 行 |
| `MAINTAINERS.md` / `CONTRIBUTORS.md` | ❌ | ✅ 有 |
| `ROADMAP.md` / `CHANGELOG.md` | ❌ | ✅ 有 |
| `TRADEMARKS.md` / `SUPPORT.md` | ❌ | ✅ 有 |
| `CODEOWNERS` / `dependabot.yml` / PR 模板 / Issue 模板 | ❌ 全无 | ✅ 全有 |
| `REUSE.toml` / `LICENSES/` | ❌ | ✅ 有 |
| `codecov.yml` / `sonar-project.properties` | ❌ | ✅ 有 |

> FastTrade 目前只有 `LICENSE`、`README.md`、`README.en.md`、`AGENTS.md`、`.gitignore` 五个根文件。

---

## 3. P0 差距二：交易能力深度

### 3.1 券商 / 交易所接入

**FastTrade 现状**：

- 加密货币：**仅币安现货**（`backend/app/modules/quant/execution/exchange.py`，`BINANCE_ORDER_URL` = `api.binance.com/api/v3/order`）
- A股：中泰 XTP —— **占位实现**，`XTPBroker(SimulatedBroker)` 未接真实 SDK（`backend/app/modules/quant_market/broker.py`）
- 期货：CTP —— 同为**占位**，退化为 `SimulatedBroker` 本地撮合

**QuantDinger 现状**：

| 类别 | 接入对象 | 能力要点 |
| --- | --- | --- |
| 加密交易所（6 家） | Binance、OKX、Bitget、Bybit、Gate、HTX | spot + swap；私有 WS 成交流；Bitget 支持 UTA V3 / Classic V2 双实现；Gate 支持交易所直连股票 |
| 传统券商（4 家） | IBKR、Alpaca、Tiger、Webull | IBKR 原生 bracket 单 + `readonly` 只读模式；Alpaca bracket + 盘前盘后 + 碎股；Tiger 美股+港股；Webull 仅美股 |
| 统一策略层 | `services/live_trading/capabilities.py` + `factory.py` | 能力矩阵集中声明，`validate_exchange_environment` 校验 live/demo/testnet |
| 券商×市场×方向矩阵 | `services/broker_market_policy.py`（唯一真源） | 券商 `LONG_ONLY_BROKERS`；grid/martingale 仅 Crypto；对外只读接口 `GET /api/policy/broker-market` |

> 注：QuantDinger 文档提到 Futu/Moomoo，但**代码中无适配器**，按代码事实为 4 家券商。

**差距定性**：这不是"少接几个柜台"，而是**执行层抽象不足**。FastTrade 的 `BrokerAdapter` Protocol 设计方向正确，但只有 1 个真实实现 + 2 个占位；QuantDinger 已把"能力声明—环境校验—市场/方向策略—部署校验"做成四层可扩展结构。

### 3.2 回测指标

**FastTrade**（`backend/app/modules/quant/backtest/engine.py`）只输出：

`total_return`、`annualized_return`、`max_drawdown`、`win_rate`，外加成交流水与净值曲线。

**QuantDinger**（`services/backtest/metrics.py`，30+ 函数）输出：

- 基础：收益、回撤、胜率、Profit Factor、Sharpe、Sortino、Calmar、下行标准差
- 回撤分析：回撤生命周期（时长/恢复）、VaR、CVaR、Omega、Tail Ratio、偏度峰度
- **基准相对**：信息比率、Beta、Jensen Alpha、Treynor、相关系数/R²、上涨/下跌捕获率、捕获比率、M² 及 M² Alpha、双重 Beta（上涨/下跌 Beta）、Batting Average
- 基准对齐细节：按基准原生时间点对齐权益曲线，避免向前填充制造虚假跟踪误差

**差距定性**：FastTrade 的回测结果不足以支撑策略筛选决策——没有风险调整收益，无法与基准比较，无法判断"是策略好还是市场好"。

### 3.3 参数优化 / 策略进化

| 能力 | FastTrade | QuantDinger |
| --- | --- | --- |
| 参数搜索 | ❌ 无 | ✅ `random` / `grid` / `tpe` 三种 |
| 参数空间来源 | — | 从 `# @param` 声明自动推导 |
| 过拟合防护 | ❌ 无 | ✅ 训练/验证/盲测切分 + embargo + PBO + Deflated Sharpe + Monte-Carlo bootstrap |
| 成本压力测试 | ❌ 无 | ✅ 费用×1/1.5/2/3 四档 |
| 结果可视化 | — | ✅ 参数热力图、收敛曲线 |

> 注意 QuantDinger 自身缺陷：`EvolutionConfig.folds` 硬编码为 1（`strategy_evolution/models.py:94`），实际是单次切分而非多折 walk-forward，文档称"walk-forward"存在夸大。FastTrade 若要对标，应实现真正的多折滚动。

### 3.4 因子研究

**FastTrade**：无因子体系。

**QuantDinger**：
- 内置技术因子 + 时点基本面因子
- TA-Lib 0.8.1（201 个函数，`factors/talib_adapter.py`）
- Qlib Alpha158 / Alpha360（`factors/qlib_adapter.py`）
- 自定义公式因子（受限 DSL，≤2000 字符 / 120 AST 节点）
- 因子研究：IC / RankIC / 分组收益 / 换手 / 行业中性化 / 覆盖率
- 因子基准排名（OOS + FDR 控制）

### 3.5 策略范式

| 维度 | FastTrade | QuantDinger |
| --- | --- | --- |
| 策略模式 | 事件模式 / 信号模式（两种） | CTA（单标的）/ Portfolio（多标的 + `on_rebalance`） |
| 策略接口 | `api.buy/sell/sellshort/buycover/close_all` | `initialize` / `handle_data` / `on_rebalance` / `before_trading_start` / `after_trading_end` + `run_daily/weekly/monthly` |
| 标的池 | 单标的为主 | `set_universe` 支持静态/指数/池；候选池上限分钟级 600 / 日周级 1200 |
| 杠杆 | 支持（`leverage>1` 按保证金记账） | 仅全 Crypto swap，上限 100 |
| 保护单 | 策略类属性声明 `stoploss` / `trailing_stop` / `max_drawdown` | 订单级 `protection` / `stop_loss_pct` / `take_profit_pct` / `trailing_stop_pct` / `time_limit_seconds` + 交易所原生保护单 + 券商 bracket |
| 执行模型 | 收盘出信号 → 下一根开盘成交（与对方一致） | 同 |

> FastTrade 的 `execution/risk.py` 设计有亮点：风控以策略类属性声明、不需改数据库、跳空按最坏情况成交。但**只有 3 个风控参数**，且无订单级保护单、无交易所原生挂单。

### 3.6 实盘安全闸门

**FastTrade**（现有）：

1. `dry_run=True` 默认不连交易所
2. 策略类属性风控（止损 / 移动止损 / 最大回撤熔断）
3. 币安现货不支持做空，`sellshort`/`buycover` 在真实模式被拒绝
4. 密钥 Fernet 加密，接口不回传明文
5. 订单回查 + 超时撤单 + 定期对账（`execution/reconcile.py`）

**QuantDinger**（22 道闸门，按执行链顺序）：全局实盘开关 → 场地白名单 → 源码契约校验 → 方向能力守卫 → 部署账户/产品校验 → 默认停止+显式启动 → 启动预检 → 腿冲突锁 → 执行模式二段式 → **订单预算守卫（fail-closed）** → **持仓归属守卫**（账户仓位=策略分配+用户保护+未知差额，超容差暂停开仓） → 方向/腿一致性 → AI 决策过滤 → 杠杆配置 fail-closed → 价格保护 → 原生保护单 → 服务端止损止盈 → 幂等与串行化 → 重试与自动停机 → Agent 侧硬门 → 紧急停止。

**差距定性**：FastTrade 的闸门是"防止误操作"级别；QuantDinger 是"多策略共享账户时防止互相踩踏"级别。后者在单用户单策略场景下显得过度设计，但一旦出现"同一账户跑多个策略"就是刚需——FastTrade 的 `quant_account` 模块刚起步，这个问题会很快暴露。

---

## 4. P1 差距三：架构与运行时

### 4.1 进程模型

| 维度 | FastTrade | QuantDinger |
| --- | --- | --- |
| 形态 | FastAPI 单进程（uvicorn）+ 进程内 APScheduler | 8 种进程角色，由 `QD_PROCESS_ROLE` 选择 |
| 角色 | 无区分 | `api` / `trading` / `scheduler` / `celery` / `strategy-dispatcher` / `strategy-evaluator` / `kafka-audit` / `migration` / `legacy` |
| 策略执行位置 | API 进程内（`runtime/service.py`） | 独立 `trading` worker；API 进程只写命令表 |
| 横向扩展 | 不支持（无状态外置，但调度/策略都在进程内） | 各 worker 独立 `*_REPLICAS` 扩副本 |
| 部署顺序 | 无编排 | Compose `service_completed_successfully` 强制 migration → worker → api |

> FastTrade 的 APScheduler 已有 Redis 分布式锁选主（`ap_scheduler.py` L65 `fasttrade:scheduler:lock`，TTL 30s / 续期 10s / 争抢 5s），Redis 不可用时退化为单机直跑。这是正确的第一步，但只解决了"调度不重复"，没解决"策略执行不重复"。

### 4.2 分布式一致性机制

| 机制 | FastTrade | QuantDinger |
| --- | --- | --- |
| 调度选主 | ✅ Redis 锁 | ✅ `qd_process_leases` 表租约 |
| 策略运行时租约 | ❌ | ✅ `qd_strategy_runtime_leases`（owner_id + fencing_token） |
| Fencing token 贯穿订单 | ❌ | ✅ `runtime_fencing_token` 写入 intent 与 pending_orders，下单网关二次校验 |
| 事件收件箱（inbox 去重） | ❌ | ✅ `qd_event_inbox`，未完成不提交 Kafka offset |
| 持久命令队列 + 幂等键 | ❌ | ✅ `qd_strategy_commands`，唯一索引防重复启停 |
| 订单幂等 | 部分（有 `reconcile.py` 对账） | ✅ `pending_orders.idempotency_key` UNIQUE + `ON CONFLICT DO NOTHING` |
| 事件总线 | ❌ 无 | ✅ Kafka（`acks=all` + 幂等 producer + 手动提交 + `read_committed`） |
| Outbox 模式 | ❌ | ❌（V5.4 文档列为前置条件，代码未实现） |

> QuantDinger 的 V5.4 文档诚实声明了 Kafka 不能替代订单幂等，列出 client order id / DB inbox / outbox / per-account fencing / 对账五项前置——这种"先写清约束再实现"的做法值得借鉴。

### 4.3 可观测性

| 能力 | FastTrade | QuantDinger |
| --- | --- | --- |
| 指标暴露 | ❌ 无 `/metrics` | ✅ `GET /metrics`（prometheus-client） |
| HTTP 指标 | ❌ | ✅ 请求数/耗时直方图/在途数（按 method+route+status） |
| 业务指标 | ❌ | ✅ worker 健康、命令状态、DB 连接池、构建信息 |
| 结构化日志 | ❌ loguru 文本格式（有 correlation_id 注入 `extra.ctx`） | ✅ JSON 格式，含 `process_role` / `request_id` / 异常栈 |
| 请求 ID | ✅ 有（`set_correlation_id`，日志中 `cid=` 前 8 位） | ✅ `X-Request-ID` 校验正则 + 回写响应头 + ContextVar |
| 分布式追踪 | ❌ | ❌（仅有 `trace_id` 字段占位，未导出到任何后端） |
| 告警 | ❌ | ✅ 7 条 Prometheus 规则 + Alertmanager |
| 面板 | ❌ | ✅ Grafana 自动供给 + 1 个 Runtime Overview 面板（6 panel） |
| 健康检查 | ✅ `/check` + `/stream`（SSE 30s） | ✅ liveness / readiness（PG+Redis 探测）/ workers 三态 + 容器级 `worker_health` |
| 日志采集 | ❌ 无集中采集 | ❌ 无集中采集（json-file driver，无 Loki/Promtail） |

> 双方都没有分布式追踪和集中日志，这块不算差距。真正的差距是**指标与告警**：FastTrade 无法回答"现在 API 错误率多少""哪个 worker 卡住了"。

---

## 5. P1 差距四：AI 能力

| 能力 | FastTrade | QuantDinger |
| --- | --- | --- |
| 对话 | ✅ WS 流式 + HTTP 非流式 | ✅ SSE 流式 |
| 模型配置 | ✅ 用户级 Redis 隔离 + 多配置 + 激活 + 连通性测试 | ✅ 系统设置页配置 |
| 工具调用 | ✅ 5 个只读工具（服务器/存储/文件/任务/Redis 键） | ✅ 59 个 MCP 工具（含写类 21 个） |
| 只读安全设计 | ✅ 明确不移植 `run_host_command`（=RCE）、`_assert_no_injected_params` 禁注入 | ✅ scope 分级（R/W/B/N/T/C）+ 幂等 + 限额 + 脱敏 |
| Agent Gateway | ❌ 无 | ✅ `/api/agent/v1`，53 paths / 63 operations，6 种 scope |
| MCP Server | ❌ 无 | ✅ 独立 pip 包 `quantdinger-mcp` 0.6.2，59 工具，stdio/SSE/streamable-http |
| AI 研究流水线 | ❌ 无 | ✅ plan→gather→assess→repair→synthesize，工具返回证据不返回指令 |
| AI 交易决策过滤 | ❌ 无 | ✅ JEV/LLM 预交易决策门（仅拦入场单，退出绕过，fail-open） |
| AI 复盘 | ❌ 无 | ✅ 后交易分析 V2（确定性账本证据 + 4 项诊断 check + AI 仅追加解释） |
| 代码生成 | ✅ 有（10 个 Jinja2 模板 + 三重安全闸门） | ✅ 有（积分计价） |

> FastTrade 的 AI 助手定位是"运维 Copilot"，QuantDinger 是"研究 + 交易全链路 Agent"。前者安全边界更保守（工具全只读），后者能力更强但需要 scope/幂等/限额三层约束。**这不是必须补齐的差距**，取决于产品定位。

---

## 6. P2 差距五：文档体系

### 6.1 文档规模与覆盖面

| 主题 | FastTrade | QuantDinger |
| --- | --- | --- |
| 总量 | 20 篇 | 118 篇 Markdown / 16,478 行 |
| 快速开始 | 无（只有 README 里的 5 分钟片段） | ✅ `getting-started/`（安装总览、30 分钟上手、首次运行、安装后配置、测试策略） |
| 部署运维 | **无** | ✅ `deployment/` 46 篇 / 5,770 行：Windows/Linux/macOS 安装、云部署、AWS Marketplace、SaaS、多用户、备份恢复迁移、升级回滚、兼容性矩阵、生产加固、可观测性、分布式扩容、移动端、通知配置（邮件/SMS/Telegram）、OAuth、配置参考 |
| 架构文档 | 无（散落在 AGENTS.md） | ✅ `architecture/` 20 篇：架构总览、模块边界、并发模型、进程职责、扩展指南、API 约定、文档维护规范、V5.1→V5.7 演进记录、V6 规划 |
| 交易文档 | `docs/quant/` 11 篇 | ✅ `trading/` 11 篇 / 4,483 行：策略开发（881 行）、指标开发（600 行）、IBKR 指南、Alpaca 费用对账、实盘安全、点时点股票池 |
| API 契约 | 无（依赖 FastAPI 自动 `/docs`） | ✅ `docs/api/openapi.yaml`（9,417 行 / 319 paths）+ ReDoc 查看器 + CI 校验 |
| 故障排查 | 无 | ✅ `ERROR_KNOWLEDGE_BASE`（分层诊断）+ `INSTALL_TROUBLESHOOTING`（症状-恢复）+ `INSTALLATION_FAQ` |
| 合规文档 | 无 | ✅ `compliance/` 8 篇（OpenChain ISO 5230/18974 自评 34+38 条全 Yes） |
| 安全文档 | 部分（`docs/admin/07-权限与安全.md`） | ✅ `security/` 5 篇（安全模型、依赖策略、安全开发、安全评估） |
| 多语言 | 中文为主（`README.en.md` 一个英文） | ✅ 11 语言 README + 45 个 `_CN` + 7 个 `_EN` 文档 |

### 6.2 文档维护机制

| 机制 | FastTrade | QuantDinger |
| --- | --- | --- |
| 维护规范 | AGENTS.md 提到"不主动新增 md" | ✅ `DOCUMENTATION_MAINTENANCE.md`：信息架构表 + 6 条 PR DoD + 写作标准 + 截图标准 + 版本审查 |
| CI 校验 | ❌ 无 | ✅ `scripts/check_docs.py`（链接/代码围栏/截图引用/UTF-8）+ `check_version.py` + `check_mojibake.py` |
| 契约对齐 | ❌ 无 | ✅ Agent OpenAPI ↔ 路由 ↔ scope ↔ MCP 工具四处 pytest 硬对齐 |
| 文档总索引 | ⚠️ `docs/README.md` 存在但 13 条链接全部失效（见 §6.4） | ✅ `docs/README.md` 按 6 组分类 + 目标导航表 |

> **FastTrade 需立即修正的一处**：`docs/README.md` 由原 `docs/quant/README.md` 平移而来（提交 `2839ca4` 删除原文件，内容复制到 `docs/README.md`），但**内部相对链接未同步调整**，13 条分册链接全部指向不存在的 `docs/01-*.md`。详见 §6.4。

### 6.4 已确认的文档死链（✅ 已修复，commit `fa66f5f`）

> 2026-11-20 处理完成：`docs/README.md` 已升级为全量文档总索引，13 条链接改为 `./quant/` 前缀，
> 补齐管理后台与对比分析入口，并纳入 git；全仓 docs 相对链接死链数实测 **13 → 0**。
> 以下为修复前的问题记录，保留作为决策依据。

| 问题 | 证据 | 影响 |
| --- | --- | --- |
| `docs/README.md` 13 条分册链接全部失效 | 该文件用 `./01-数据下载与导入.md` 等相对路径，但分册实际在 `docs/quant/` 下；实测 13 个目标均不存在 | 文档总索引不可用 |
| `docs/quant/README.md` 已不存在 | 提交 `2839ca4`（`README.md update`）删除该文件（-172 行），内容改为 `docs/README.md` | 所有指向 `../quant/README.md` 的链接失效 |
| `docs/README.md` 未被 git 跟踪 | `git ls-files docs/README.md` 为空；`git status` 显示 `?? docs/README.md` | 他人克隆仓库后无量化索引 |
| `docs/quant/10`、`11` 未提交 | `git status` 显示 `?? docs/quant/10-币安密钥配置.md`、`?? docs/quant/11-三市场密钥与持仓管理.md` | 根 README 的量化文档区未列 09/10/11 |

**修复建议**（按最小改动）：

1. 把 `docs/README.md` 改为**全量文档总索引**：同时索引 `docs/admin/` 与 `docs/quant/`，相对链接改为 `./admin/...` 与 `./quant/...`；
2. 或保留其量化索引定位，把所有 `./NN-*.md` 改为 `./quant/NN-*.md`，并另建 `docs/admin/README.md` 的入口（已存在）；
3. 提交 `docs/README.md` 与 `docs/quant/10`、`11`，并在根 README 文档区补齐 09/10/11 三条链接。

### 6.3 数据库迁移范式对比

| 维度 | FastTrade（Alembic） | QuantDinger（手写幂等 SQL） |
| --- | --- | --- |
| 版本标识 | 迁移文件链 + `alembic_version` 表 | `qd_bootstrap_migrations`（组件名 + SHA-256 校验和） |
| 内容变更 | 不可变（禁止改历史文件） | 可重跑（内容变了就重跑，因此每个脚本必须幂等） |
| 新库安装 | 逐条 replay 全部迁移 | `init.sql` 直接给最终 schema，不回放历史 |
| 并发控制 | 单实例执行 | Compose `migration` 服务单写者 + 其它服务 `SKIP_AUTO_MIGRATE=true` |
| 超时控制 | 无 | `lock_timeout` 15s / `statement_timeout` 180s |
| 风险 | 迁移链断裂、replay 慢 | 幂等性靠人工保证，写错会静默出错 |

> **结论：Alembic 范式更规范，不建议改**。QuantDinger 的方案是在"39 个 SQL 文件只有 19 个被登记"的混乱中演化出的妥协，不值得对标。

---

## 7. FastTrade 相对优势（不应丢掉的）

对比不是单向的。FastTrade 有以下 QuantDinger 明显不足或没有的能力：

| 能力 | FastTrade | QuantDinger |
| --- | --- | --- |
| **管理后台完整度** | ✅ 用户/角色/菜单/部门/岗位/字典/参数/公告/工单/版本 11 页 + 权限码体系 + 操作日志自动脱敏 | ⚠️ 有设置页，但无同等粒度的 RBAC 后台文档 |
| **存储管理** | ✅ 8 种协议（FTP/FTPS/SFTP/S3/OBS/OSS/COS/LOCAL）+ 文件浏览 + 传输流程画布 + 分片 | ❌ 无 |
| **运维工具** | ✅ SSH 终端（配额/审计）+ SQL 控制台（只读校验/打码）+ 代码生成 | ⚠️ 无 SSH/SQL 控制台 |
| **任务调度 UI** | ✅ 调度器监控 + 节点管理 + 执行日志 + 控制台 + 分布式锁选主 | ⚠️ Celery + 命令表，无同等可视化 |
| **前端源码** | ✅ 完整 Vue 3 源码可改 | ❌ 预构建镜像，前端不在仓库 |
| **AGENTS.md 详细度** | ✅ 非常详细（提交规范/TDD/安全禁区/常用命令） | ✅ 有但更简短（46 行） |
| **双语注释风格** | ✅ 后端代码中英双语注释 | ⚠️ 仅英文注释 |
| **Alembic 迁移** | ✅ 规范 | ⚠️ 手写幂等 SQL |

---

## 8. 差距清单与建议优先级

### P0（建议立即补，收益最高）—— 全部已处理 ✅

> 处理记录（2026-11-20）：以下 5 项已依次完成并提交，详见下方「处理状态」列。

| # | 差距 | 建议动作 | 处理状态 |
| --- | --- | --- | --- |
| 1 | 无 CI | 建 `.github/workflows/ci.yml`：ruff + pytest + 前端 type-check/test + 文档链接校验 | ✅ `ci.yml`（commit `b1ea89d`）：3 job，Python 3.12/3.13 矩阵 + Node20/pnpm9 + docs 链接 |
| 2 | 无权限码契约测试 | 写 `tests/test_permission_contract.py`：AST 解析 controller 权限码 ↔ `sys_menu.json` 比对 | ✅ `test_permission_contract.py`（commit `33aa6ba`）：核心域双向一致，量化域前缀校验，3 passed |
| 3 | 无安全扫描 | 加 `security.yml`：`pip-audit` + `bandit` + `gitleaks` + Dependabot | ✅ `security.yml` + `dependabot.yml`（commit `a269f53`）：3 job + 3 生态周检 |
| 4 | 回测指标贫乏 | 补 Sharpe / Sortino / Calmar / Profit Factor / VaR / CVaR | ✅ `backtest/metrics.py` + engine 接入（commit `f3d2ce9`）：7 项指标，10 个 TDD 测试通过。基准相对指标仍属 P1 |
| 5 | 文档死链 | `docs/README.md` 13 条链接失效 + 未跟踪 | ✅ `docs/README.md` 升级为全量索引（commit `fa66f5f`）：死链 13→0，纳入 git |

**验证汇总**：13 个单元测试通过（`--noconftest`）；ruff 检查通过；docs 死链 0；3 个 CI/security YAML 均可解析。

**遗留说明**：
- CI 的 ruff 当前只校验新增契约测试（历史 935 条既有告警待逐步治理）；
- pytest 在 `--noconftest` 下运行（仓库 conftest 在 Windows 沙箱下触发 `ProcessPoolExecutor` WinError 5，既有问题）；
- CI 端到端需 push 后在 GitHub Actions 页确认。

### P1（产品化必需）

| # | 差距 | 建议动作 |
| --- | --- | --- |
| 6 | 无 Docker / 部署产物 | 加 `Dockerfile` + `docker-compose.yml`（backend + postgres + redis + frontend）+ 安装脚本 |
| 7 | 无 `/metrics` | 引入 `prometheus-client`，暴露 HTTP 指标 + 业务指标；加结构化 JSON 日志开关 |
| 8 | A股/期货柜台占位 | 要么接真实 SDK（XTP/CTP 需券商授权，成本高），要么在文档与 UI 明确标注"仅模拟" |
| 9 | 无参数优化 | 在回测引擎上加 `grid` / `random` 搜索 + 训练/验证切分 |
| 10 | 多策略共享账户无保护 | 借鉴"持仓归属守卫"：账户仓位 = 策略分配 + 用户保护 + 未知差额，超容差暂停开仓 |
| 11 | 无部署/运维文档 | 补 `docs/deployment/`：安装、配置参考、升级回滚、备份恢复、生产加固 |

### P2（按产品定位取舍）

| # | 差距 | 说明 |
| --- | --- | --- |
| 12 | 无因子研究体系 | 仅在定位为"研究平台"时才需要；若定位为"交易执行平台"可跳过 |
| 13 | 无 Agent Gateway / MCP | 若有外部 AI 客户端接入需求再做；FastTrade 现有只读工具已覆盖运维 Copilot 场景 |
| 14 | 无网格/DCA/马丁引擎 | 取决于是否要做"策略模板市场" |
| 15 | 无分布式事件总线 | 单机部署下 Kafka 是过度设计；仅在多机多策略场景才需要 |
| 16 | 无合规文档（REUSE/DCO/OpenChain） | 仅在对外开源运营时才需要 |
| 17 | 无多语言文档 | 取决于是否有海外用户 |

---

## 9. 关键认知：哪些"差距"其实不是差距

避免盲目对标，以下几点需要明确：

1. **QuantDinger 的分布式架构是为 10 万策略目标设计的**，其 V6 文档明确写"The 100,000-strategy target is not yet certified"。FastTrade 若定位为单机/小团队自用，引入 Kafka + 多进程角色是纯负担。
2. **QuantDinger 也有大量未闭环**：`account_risk` 模块定义完整但无生产调用方；`order.intent.v1` 等 3 个事件契约只有定义无生产/消费代码；DLQ topic 只创建不投递；`strategy_evolution` 的 walk-forward 实际是单折。
3. **QuantDinger 前端闭源**，FastTrade 前端完整可改——这是 FastTrade 的实质优势，不应为了"对齐"而放弃。
4. **QuantDinger 有 12 处文档与代码不一致**（见附录），说明其文档也存在漂移，不必视为完美模板。

---

## 10. 附录：QuantDinger 已确认的文档-代码不一致

供对标时避免被其文档误导：

| # | 文档说法 | 代码事实 | 来源 |
| --- | --- | --- | --- |
| 1 | MCP 有 58 个工具 | 实际 59 个（`MCP_TOOL_NAMES` 59、测试断言 59） | `docs/agent/MCP_SETUP.md` vs `mcp_server/src/quantdinger_mcp/server.py` |
| 2 | 支持 Futu/Moomoo | 无任何适配器代码 | `docs/trading/LIVE_TRADING_SAFETY.md` vs 全库检索 |
| 3 | 内置告警覆盖 "job Redis memory pressure" | `alerts.yml` 无该规则 | `docs/deployment/OBSERVABILITY.md` vs `ops/prometheus/alerts.yml` |
| 4 | 策略进化是 walk-forward | `folds` 硬编码为 1，单次切分 | 文档 vs `services/strategy_evolution/models.py:94` |
| 5 | 网格模板版本 6 | 实际 7 | 文档 vs `services/strategy_runtime/robot_v2.py:202` |
| 6 | 双均线模板 `system_seed version=12` | 迁移 SQL 中为 13 | 文档 vs `migrations/strategy_v2_templates.sql` |
| 7 | 有 `services/backtest_engine/` 目录 | 不存在 | `README_CN.md` 目录树 |
| 8 | 要求先读 `docs/REFACTOR_PLAN_4_0.md` | 文件不存在 | `docs/architecture/ARCHITECTURE.md:154` |
| 9 | 账户风控会拒绝订单 | `account_risk.py` 无生产调用方 | 文档 vs 全库检索 |
| 10 | 有官网生成器需运行 | 仓库内无任何站点生成配置 | `DOCUMENTATION_MAINTENANCE.md` vs 全库检索 |
| 11 | `VERSION` = 5.6.1 | git 标签已到 v5.9.3 | `VERSION` vs `git tag` |
| 12 | Agent 规格有破坏性变更检查 | `oasdiff` 仅覆盖人类 API | `openapi-ci.yml` |

---

## 11. 参考

- 本文档所引 QuantDinger 事实均来自 `D:\code\quant\QuantDinger-github-1118-3`（`v5.6.1`，HEAD `78723e4`）
- FastTrade 管理后台能力详见 [`docs/admin/README.md`](./admin/README.md)
- FastTrade 量化能力详见 [`docs/quant/01-数据下载与导入.md`](./quant/01-数据下载与导入.md) 起的 11 篇分册（`docs/quant/` 下无 README 索引文件）
- FastTrade 权限码全表详见 [`docs/admin/07-权限与安全.md`](./admin/07-权限与安全.md)
