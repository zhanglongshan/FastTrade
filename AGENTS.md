# AGENTS.md — FastTrade 协作规范

本文件约束所有 AI Agent（Claude Code、CodeBuddy、Trae、Cursor 等）在本仓库内的工作方式。**执行任何任务前先读本文件**；与仓库内其他文档冲突时，以本文件为准。

---

## 0. 项目速览

FastTrade 是一套**前后端分离**的量化交易后台，代码源自 FastapiAdmin 并按其分层范式二次开发：

| 部分 | 目录 | 技术栈 |
| --- | --- | --- |
| 后端 API | `backend/` | Python 3.12+、FastAPI、SQLAlchemy 2.x（异步）、Pydantic v2、Alembic、APScheduler、Redis |
| 前端管理台 | `front-web/` | Vue 3（`<script setup>`）、Vite 7、TypeScript、Element Plus、Pinia、Tailwind CSS 4、Vitest |

要点：

- 两个目录**各自独立**（无 pnpm/npm workspace）。所有命令必须在对应目录内执行，不要在仓库根目录跑 `pnpm` 或 `pytest`。
- 后端同时承担 API 与前端产物托管（`backend/dist/` 存在时挂载到 `/`），前端开发期靠 Vite 代理联调。
- 仓库存在**上游遗留的不一致**，不要盲从：`front-web/.github/workflows/ci.yml` 的 `working-directory` 仍是 `frontend/web`，README 引用的 `docs/DESIGN-SPEC.md`、`docs/PLUGIN_ARCHITECTURE.md` 在本仓库不存在。命令与路径以本文件为准。

---

## 1. 提交规范（Commit）

### 1.1 小步提交
- 一个可独立验证的需求（或一个功能切片）对应一次 commit；不把不相关改动塞进同一个 commit。
- 单次 commit 应可独立回滚，且不破坏既有功能。

### 1.2 只 commit，不 push
- Agent 只能执行 `git add` + `git commit`，**严禁** `git push`、`git push --force`、`git push --force-with-lease`。
- 当前主分支为 `master`，推送由用户人工完成。完成后提示："已提交到本地，待你确认后推送。"

### 1.3 提交身份
- 本仓库 `.git/config`：`user.name = Stylesmile`，`user.email = 3239866994@qq.com`。
- 提交必须使用该身份，**禁止**用 `git config` 修改仓库级或全局配置；若默认未读到，显式传参：
  ```bash
  git -c user.name="Stylesmile" -c user.email="3239866994@qq.com" commit -m "..."
  ```

### 1.4 Commit Message
- 中文描述，首行 ≤ 50 字；格式 `<type>(<scope>): <简述>`，scope 可省略（建议用 `backend` / `front-web`）。
- `type` 必须取自 `front-web/commitlint.config.cjs` 的枚举（前端启用了 commitlint，越界类型会被拒）：
  `feat`、`fix`、`docs`、`style`、`refactor`、`perf`、`test`、`build`、`ci`、`revert`、`chore`、`wip`。
- 示例：`feat(backend): 新增持仓同步接口`、`fix(front-web): 修复用户表格分页丢失`
- 改动跨端或较复杂时，空一行后补正文：
  ```bash
  git commit -m "$(cat <<'EOF'
  feat(backend): 新增持仓同步接口

  - 新增 position 模块 controller/service/crud/model/schema
  - 在 app/api/v1/routers.py 注册 /system/position 域
  EOF
  )"
  ```

### 1.5 暂存范围
- 按文件精确 `git add <file1> <file2>`，避免 `git add .` / `git add -A`。
- 严禁提交：`backend/env/.env.dev`、`backend/env/.env.prod`（真实密钥）、任何 token/私钥、`front-web/.env.local`、`backend/logs/`、`backend/static/` 下的用户上传物（`backend/static/image/` 等内置素材除外）。
- 严禁提交构建产物与依赖目录：`front-web/dist/`、`front-web/node_modules/`、`backend/.venv/`、`*.pyc`、`.idea/`。
- `front-web/src/types/auto-imports.d.ts`、`components.d.ts` 为自动生成且已被 gitignore，不要提交或手改。

---

## 2. 工作流程：先计划，TDD 测试先行，完成即测

### 2.1 先计划后编码
接到需求后先输出实现计划（可用 TodoWrite / TaskCreate 落成 todo）：
1. 需求拆解为可独立验证的切片；
2. 每个切片涉及的前端 / 后端 / 迁移 / i18n 文件；
3. 每个切片的验收方式（接口返回、页面表现、测试用例）；
4. 依赖顺序（通常：模型/迁移 → CRUD/Service → Controller/路由 → 前端 API → 页面）。

### 2.2 TDD（测试驱动开发）— 强制，不可跳过

本仓库默认采用 TDD：**先写测试，再写实现**。除 2.3.3 列明的例外，任何新增功能、接口、字段、逻辑分支、缺陷修复都必须走完下面三步，不允许"先写实现、事后补测试"。

1. **Red**：先写能复现需求/缺陷的测试，**实际执行并确认它失败**；失败原因必须是"功能缺失 / 行为不符"，而不是语法错、导入错、fixture 错。没有红灯证据就不算进入 TDD。
2. **Green**：写最小实现让测试通过；不顺手加需求外的功能、参数、容错分支。
3. **Refactor**：在测试全绿的保护下重构，重构后必须重跑测试确认仍然全绿。

补充规则：

- 一个切片 = 一组测试 + 一份实现，测试与实现**同一次 commit** 提交；不要"先提交实现、后补测试"。
- 测试断言**行为与契约**（返回结构、状态码、错误码、边界值、权限拒绝、分页与排序），不要只断言"函数被调用过"。
- 修 bug 必须先写一个当前能稳定复现该 bug 的失败测试；修复后该用例转为回归用例长期保留，不得删除。
- 禁止为了"变绿"而修改测试断言去迁就错误实现；调整断言必须有需求依据，并在 commit message 正文说明原因。
- 确实无法写自动化测试时，按 2.3.3 例外条款处理，不允许默默跳过。

### 2.3 测试命令与「开发完成后自动测试」

#### 2.3.1 测试命令

```bash
# 后端：默认 sqlite 临时库 + mock Redis，无需外部服务
cd backend
uv run pytest -q                          # 全量（约 198 个测试文件）
uv run pytest tests/test_<模块>.py -q     # 单文件
uv run pytest tests/test_<模块>.py::test_xxx -q   # 单用例

# 后端：需要真实 PostgreSQL/Redis 的集成用例（默认 skip）
TEST_DB_TYPE=postgres RUN_INTEGRATION=1 TEST_DB_USER=... TEST_DB_PASSWORD=... \
  uv run pytest tests/test_integration_pg_redis.py -q
```

```bash
# 前端：Vitest（jsdom）
cd front-web
pnpm test                                  # vitest run
pnpm test:watch
pnpm vitest run src/__tests__/permission-check.spec.ts
```

#### 2.3.2 开发完成后自动测试（完成即测，不等用户提醒）

开发（含新功能、修 bug、改接口契约、改模型/迁移）完成后，Agent **必须立即自行执行相关测试**，不得把"要不要跑测试"交回用户决定，更不得在未执行测试的情况下宣称任务完成。

执行顺序：按改动范围**由窄到宽**，先窄后宽，失败先定位再扩面。

1. 单用例：`uv run pytest tests/test_<模块>.py::test_xxx -q` / `pnpm vitest run <spec 路径>`
2. 单文件：`uv run pytest tests/test_<模块>.py -q`
3. 受影响范围 + 全量：`uv run pytest -q`、`pnpm test`、`pnpm type-check`
4. 静态检查：`uv run ruff check`、`pnpm lint`（改样式/格式时）

自愈循环（最多 3 轮）：

- 测试失败 → 读报错定位 → 修复 → 重跑，直到全绿；
- 同一失败连续 3 轮修不掉，或失败明显与本次改动无关（既有历史失败、缺外部服务），**停止盲改**，向用户明确报告：失败用例名、报错关键行、判断是本次引入还是既有问题、建议下一步；
- 严禁用 `skip` / `xfail` / 注释掉断言 / 删除用例的方式让测试变绿。

结果汇报要求：

- 交付时给出结论 + 关键数字，例如"后端 pytest 198 passed、前端 vitest 12 passed、ruff 无告警"；
- 存在跳过、既有失败或只跑了部分用例时，必须显式说明范围与原因，不要用"测试已通过"含糊带过。

#### 2.3.3 例外（确实无法写自动化测试时）

- 涉及数据库迁移、类型/约束语义的改动：sqlite 通过不代表正确，必须在 PostgreSQL 上复验（`TEST_DB_TYPE=postgres RUN_INTEGRATION=1`）。
- 纯样式、纯配置、纯文档、纯 i18n 文案等无法自动化验证的改动，允许不写测试，但必须同时满足：
  1. commit message 正文写明"为何无法测试"；
  2. 写出可复现的人工验证步骤（访问哪个页面、点什么、期望看到什么）；
  3. 至少跑通能跑的静态检查（`uv run ruff check` / `pnpm type-check`）。
- 例外只免除"写测试"，不免除"跑测试"：改动涉及的既有测试仍必须执行。

### 2.4 测试位置与风格
- 后端：`backend/tests/test_<主题>.py`，函数 `test_xxx`；复用 `tests/conftest.py` 的 `test_client`、`auth_headers`、`assert_route`。
- 后端测试默认 sqlite，请勿在测试内引入对外部服务的强依赖；必须依赖时用 `pytest.mark.skipif` 按环境变量开关。
- 前端：`front-web/src/__tests__/<主题>.spec.ts`（全局配置 `globals: true`，但现有用例仍显式 `import { describe, it, expect } from "vitest"`，保持一致）。
- 前端单测尽量只测纯函数 / 组合式逻辑；不要为了覆盖率去 mount 整页依赖 Element Plus 的组件。

---

## 3. 代码规范

### 3.1 注释（中文）
- **每个模块、类、公开方法/函数都要有中文说明**；后端沿用现有 docstring 风格，写出「参数 / 返回」：
  ```python
  async def get_user_controller(...) -> JSONResponse:
      """查询单个用户详情。

      参数:
      - auth (AuthSchema): 当前登录用户上下文。
      - db (AsyncSession): 异步数据库会话。

      返回:
      - JSONResponse: 统一响应信封，data 为 UserOutSchema。
      """
  ```
- **每个 Pydantic 字段逐个加 `description`**（业务含义、单位、取值范围、枚举含义），前端 TS 类型/接口字段用 JSDoc：
  ```python
  class UserCreateSchema(BaseModel):
      """创建用户参数"""

      username: str = Field(description="登录账号，全局唯一")
      status: int = Field(default=1, description="状态：0-停用，1-启用")
  ```
- **关键位置必须注释**：业务分支、外部调用（DB/Redis/HTTP/SFTP/S3）、异步任务、魔法数字与阈值、SQL 拼接与白名单校验。
- **不写无信息量的逐行注释**（如 `i += 1  # 加一`）；注释解释"为什么"，而非复述代码。
- 注释、commit message、计划文档统一用**中文**；标识符、日志 key、权限码用英文。

### 3.2 命名与结构
- 后端 snake_case；类名 `XxxModel` / `XxxSchema` / `XxxService` / `XxxCrud` / `XxxRouter`。
- 前端组件目录 kebab-case（`fa-user-table/index.vue`），组件名 PascalCase（`FaUserTable`）；视图、API、i18n 统一 `module_<域>` 前缀，与后端域保持一致。
- 复用优先：先找 `backend/app/core/`、`backend/app/common/`、`front-web/src/components/`、`front-web/src/hooks/` 里既有能力，不重复造。

### 3.3 后端约定（FastAPI 分层）
- 标准分层目录（每个业务子域一份）：`controller.py` / `service.py` / `crud.py` / `model.py` / `schema.py`，需要拆分参数模型时加 `param.py`。
- **新增路由必须注册**：在 `backend/app/api/v1/routers.py` 的 `DOMAIN_CONTROLLERS` 里挂到对应域前缀，否则接口 404。插件式路由放 `backend/app/plugin/module_xxx/controller.py`，由 `core/discover.py` 自动扫描（模块顶层必须有 `APIRouter` 变量）。
- 响应统一走 `app/common/response.py`：成功 `SuccessResponse`，异常 `ErrorResponse`；控制器声明 `response_model=ResponseSchema[XxxOutSchema]`，不要直接 `return dict` / 裸 `JSONResponse`。
- 鉴权统一 `Security(AuthPermission(["module_<域>:<资源>:<动作>"]))`，动作复用既有词表（`query` / `detail` / `create` / `update` / `delete` / `patch` / `import` / `export` / `upload` / `download` 等）；仅需登录态时用 `Security(get_current_user)`。前端按钮权限码必须与后端完全一致。
- 分页查询使用 `app/core/base_schema.py` 的 `PaginationQueryParam` / `PageResultSchema`；查询参数继承 `BaseQueryParam`，不要自造分页协议。
- 时间字段用 `app/core/validator.py` 的 `DateStr` / `TimeStr` / `DateTimeStr`；入 ORM 用 `model_dump(mode="python")`，写 Redis / JSON 用 `model_dump(mode="json")`。
- 枚举与常量集中放 `app/common/enums.py`、`app/common/constant.py`，不散落魔法值；配置读 `app/config/setting.py` 的 `settings`，禁止在业务代码里读 `os.environ`。
- 数据库操作全程 async（`AsyncSession`），不要在异步链路里调用同步阻塞 IO；耗时任务交给 APScheduler 或后台任务。
- 模型变更才需要迁移：`uv run main.py revision --env=dev -m "说明"` → 人工 review 生成文件 → `uv run main.py upgrade --env=dev`。**不要手改历史迁移文件**，也不要删除 `app/alembic/versions/` 下已有脚本。
- 代码风格交给 ruff（`line-length = 200`、双引号、fix 开启）：提交前 `uv run ruff check --fix`。

### 3.4 前端约定（Vue 3 + Vite）
- `AutoImport` 已自动导入 `vue`、`vue-router`、`pinia`、`@vueuse/core`、`vue-i18n`、`src/hooks/core` 以及 `ElMessage`/`ElMessageBox` 等；`Components` 自动注册 `src/components`、`src/layouts`、`src/**/components` 下的 `Fa*` 组件与 Element Plus 组件。**不要为这些能力手写 import**（类型导入、第三方函数除外）。
- 接口封装放 `src/api/module_<域>/<资源>.ts`，统一用 `@utils` 的 `request<T>()`，模块内定义 `API_PATH` 常量；类型就近声明并从 `@api/...` 导出，禁止在 `.vue` 里直连 axios。
- 页面放 `src/views/module_<域>/<资源>/index.vue`，页面私有组件放同级 `components/`；优先复用 `FaTable`、`FaSearchBar`、`FaTableHeader`、`FaWorkbench`、`FaSidePanel` 等既有骨架，不要另起一套布局。
- 权限控制用 `checkPerm` 与组件的 `perm-*` 属性（如 `:perm-create="['module_system:user:create']"`），不要写死角色判断。
- 文案走 `src/locales/langs/zh.json` / `en.json`；已有 i18n 的页面新增文案必须成对补齐中英文。
- 样式用 Tailwind CSS 4 + SCSS，遵循 `.prettierrc.yaml` / `.stylelintrc.cjs`；`pnpm lint` 会自动修复大部分格式问题。
- 不要手改 `src/types/auto-imports.d.ts`、`src/types/components.d.ts`（自动生成）；也不要改 `vite.config.ts` 的代理与别名去绕过联调问题。

### 3.5 最小改动
- 只改与需求相关的文件，不做顺手重构、不升级无关依赖、不调整无关格式。
- 不主动新增 `*.md` 文档，除非用户明确要求。
- 不添加未被要求的功能、配置项、容错分支；不擅自 `pnpm update` / `uv lock --upgrade`。

---

## 4. 安全与禁区

- **Git**：禁止 `push` 与强推；禁止 `reset --hard`、`checkout .`、`clean -fd`、`branch -D`、`rebase -i`（除非用户明确要求）；禁止修改 `.git/config`。
- **密钥**：禁止把真实 DB 密码、Redis 密码、`SECRET_KEY`、`OPENAI_API_KEY`、OAuth 凭据写入受版本控制的文件；新增环境变量先更新 `backend/env/.env.example` 或 `front-web/.env.*.example`。
- **高危能力不可放宽**：`app/modules/ops/`（SSH 终端、数据库控制台）、`app/modules/task/cronjob/`（代码块执行，受 `SCHEDULER_ALLOW_CODE_EXEC` 控制）、`app/modules/generator/`（代码生成落盘）、文件上传与存储适配器，都带有校验/白名单/路径约束。改动时必须保留并覆盖这些保护，不得为"方便调试"临时放开或注释掉校验。
- **SQL**：禁止拼接用户输入；必须动态拼 SQL 的场景（dbconsole、codegen）走既有 `sqlglot` / 白名单校验链路。
- **数据**：不要删除或重写 `backend/sql/` 初始化脚本与 `backend/app/alembic/versions/` 历史迁移；不要提交本地数据库文件、日志、上传文件。
- **编辑纪律**：先读文件再改；长命令（安装、构建、全量测试）放后台执行，避免阻塞会话。

---

## 5. 工程结构与常用命令

| 端 | 目录 | 安装 | 启动 / 构建 | 质量校验 |
| --- | --- | --- | --- | --- |
| 后端 | `backend/` | `uv sync --extra mysql`（或 `postgres` / `sqlite` / `all-db`） | `uv run main.py run --env=dev` | `uv run ruff check` / `uv run pytest -q` |
| 前端 | `front-web/` | `pnpm install`（Node ≥ 20.19，pnpm 9） | `pnpm dev`（端口 5180）/ `pnpm build` | `pnpm lint` / `pnpm type-check` / `pnpm test` |

### 5.1 首次运行前置
- 后端：`cp backend/env/.env.example backend/env/.env.dev`，按本机填写 `DATABASE_*`、`REDIS_*`、`SECRET_KEY`；**首次启动会自动建表并初始化基础数据**，无需先 `upgrade`。接口文档：`http://127.0.0.1:8001/docs`。
- 前端：以 `front-web/.env` 为通用配置，开发覆盖项参考 `.env.development.example`（`VITE_API_BASE_URL=http://127.0.0.1:8001`）；`/api/v1` 由 Vite 代理转发。改 `.env*` 后必须重启 `pnpm dev`。
- 演示账号来自后端初始化数据，测试中固定使用 `admin / admin123`（仅在测试生命周期内重置）。

### 5.2 常见改动落点
- **新增后端业务模块**：`app/modules/<域>/<资源>/` 下建 `controller/service/crud/model/schema` → 在 `app/api/v1/routers.py` 注册路由 → 补 `tests/test_<资源>.py` → 若改了模型则生成迁移 → 前端菜单/权限码同步。
- **新增前端页面**：`src/views/module_<域>/<资源>/index.vue` + `src/api/module_<域>/<资源>.ts` → 路由由菜单驱动（`VITE_ACCESS_MODE=mixed`）或补静态路由 → i18n 中英文 → `pnpm type-check`。
- **改接口契约**：先后端 `schema.py` 与 `response_model`，再前端 `src/api` 类型，最后页面；三处必须同一次改动内对齐。
- **涉及数据库结构**：`model.py` → `revision` → 检查生成脚本（默认值、索引、可空性）→ `upgrade` → 在 PostgreSQL 上跑相关测试。

---

## 6. 执行检查清单

提交前自检：

- [ ] 计划已输出并落成 todo，切片可独立验证
- [ ] TDD 走完 Red → Green → Refactor：先有失败测试、再有实现，测试与实现同一次 commit；无法写测试的已按 2.3.3 说明原因与人工验证步骤
- [ ] 开发完成后已**自动执行测试**（未等用户提醒），失败已在 3 轮内修好或已如实报告卡点
- [ ] 交付时已给出测试结论与关键数字（passed / failed / skipped 及原因）
- [ ] 后端：`uv run ruff check` 通过、相关 `pytest` 通过
- [ ] 前端：`pnpm type-check` 通过、相关 `pnpm test` 通过（改样式再加 `pnpm lint`）
- [ ] 新接口已在 `routers.py` 注册，权限码前后端一致
- [ ] 新字段有 `description`，公开方法有中文 docstring，关键分支有注释
- [ ] 未提交 `.env.dev`、密钥、日志、`dist/`、`node_modules/`
- [ ] 按文件精确 `git add`，commit 使用仓库配置身份
- [ ] 仅 commit 未 push，commit message 类型在 commitlint 枚举内
