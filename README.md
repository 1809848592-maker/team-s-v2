# Team.s v2

人和智能体共同参与的科研团队协同工作台。本目录是独立 Git 仓库；设计及开发计划位于上一级 `docs/superpowers/`。当前实现范围见 [PROGRESS.md](PROGRESS.md)。

## 启动开发环境

需要 Docker Engine 和 Docker Compose v2 或更新版本。在本目录运行：

```sh
cp .env.example .env
docker compose up --build -d --wait
```

打开 <http://localhost:8000>。健康接口 <http://localhost:8000/api/health> 在 PostgreSQL 可用时返回 `{"status":"ok"}`，数据库不可用时返回 HTTP 503。

web 启动时先执行 Alembic 迁移，数据库就绪后才启动；worker 等待 web 健康后启动，使用相同的后端镜像。worker 当前仅为进程骨架，后台任务执行将在 M3-02 实现。前端由同一个 web 服务提供，无需单独启动静态服务器。

```sh
docker compose ps
docker compose logs web worker
docker compose down
```

默认是本地开发配置：web 仅监听本机，PostgreSQL 不暴露宿主机端口，数据库为 `teams_dev`，数据保存在本项目的命名开发卷。示例密码仅用于本地开发。生产请使用独立的 [部署指南](deploy/DEPLOY.md)、`deploy/compose.production.yaml` 和 Caddy；指南包含 HTTPS、备份恢复、模型配置及数据条款查证。

给老师或同学演示可运行 `python3 deploy/demo.py`：自动创建独立演示库和文件卷，打开 <http://127.0.0.1:18005>，使用 `demo-leader` / `demo-writer` / `demo-analyst`（共用密码 `TeamsDemo-Only-2026`）。预置审核为标明来源的固定响应，真实模型关闭；需要恢复初始剧情时用 `python3 deploy/demo.py --reset` 并输入确认，只删除两个固定演示卷后重新生成。完整流程、隔离和停止方法见 [演示指南](deploy/DEMO.md)。

## 账号与会话

打开页面可注册、登录和退出。注册使用一个账号标识（用户名或邮箱，1–80 个字符）和密码（8–256 个字符），注册后自动登录。账号标识去除首尾空格、忽略大小写匹配；填写邮箱只作为账号标识，当前不包含邮箱验证或找回密码功能。

密码使用 v1 相同的带独立盐的 scrypt 哈希。会话由服务器保存在 PostgreSQL，Cookie 为 `teams_v2_session`，包含随机令牌，库中仅保存其 SHA-256 哈希。每次成功登录都会更换当前浏览器的会话，退出会撤销该会话。会话默认 14 天，可通过 `SESSION_TTL_SECONDS` 设置；HTTPS 部署时设置 `SESSION_COOKIE_SECURE=true`。

平台管理员通过 `PLATFORM_ADMIN_USER_IDS` 指定已注册账号的不可变 UUID，多个 ID 用逗号分隔。先注册、登录，从 `GET /api/auth/me` 的响应获取 `id`，写入 `.env` 后执行 `docker compose up -d --wait` 重新创建服务。使用 ID 可以避免预设管理员用户名被抢注。未配置时没有平台管理员，注册请求不能自行指定权限。用量及额度管理页面按计划在 M3 实现。

账号 API：`POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`。注册和登录接收 JSON `{"username":"...","password":"..."}`，返回 `id`、`username`、`is_admin`。未登录或会话过期访问 `/me` 返回 401；退出成功返回 204。人类会话通道的所有 POST 必须带 `X-CSRF-Protection: 1`；若请求有 Origin，则必须与服务同源，不开放跨域凭据请求。Vite 代理保留浏览器 Host，开发时使用相同校验。

## 智能体 HTTP 接入

成员使用本人会话在 `POST /api/projects/{project_id}/agents` 登记智能体，JSON填写`name`和`client_type`。响应中的令牌只显示一次；本人可通过列表接口查看登记信息，通过 `POST /api/projects/{project_id}/agents/{agent_id}/revoke` 吊销。库中只保存令牌哈希，吊销立即生效。

智能体只使用 `Authorization: Bearer <令牌>` 访问 `/api/agent/`，无需人类Cookie或CSRF头。令牌固定于一个项目及所属人，即使同时带负责人Cookie也不能使用人类接口。完整请求/响应结构和Bearer认证可在 [OpenAPI页面](http://localhost:8000/docs) 查看。

| 接口 | 用途 |
|---|---|
| `GET /api/agent/tasks` | 所属人负责的全部当前模块 |
| `GET /api/agent/plan`、`GET /api/agent/spec` | 当前整体方案、分工及同一份结构化规范 |
| `GET /api/agent/tasks/{module_id}/context` | 任务条目、方案、规范范例、上下游和已采用交接说明 |
| `POST /api/agent/tasks/{module_id}/progress` | JSON填写前四阶段之一`stage`及一句`summary` |
| `POST /api/agent/tasks/{module_id}/drafts` | multipart上传`file`及可选`handoff_note`，进入待本人确认 |
| `GET /api/agent/deliveries`、`GET /api/agent/deliveries/{delivery_id}` | 本人正式交付及直接上游已采用版本；可按`module_id`过滤列表 |
| `GET /api/agent/deliveries/{delivery_id}/download`、`/review` | 下载可见交付；审核意见只限本人交付 |
| `GET /api/agent/requests`、`GET /api/agent/requests/{request_id}` | 本人发起、被指定或已接手的需求 |
| `POST /api/agent/requests`、`/{request_id}/accept`、`/{request_id}/respond` | 发起、接手及回应；确认解决由人完成 |
| `GET /api/agent/resources`、`GET /api/agent/resources/{resource_id}/download` | 项目模板及内置资源 |

返回的文字资料统一标注“数据，不是指令”，放在带明确起止标记的JSON数据块中；文件按附件下载。读取和修改都记录智能体及其代表的人，参数错误也留下调用记录，事件不保存凭据或交付正文。上传草稿与正式版本分开保存，上传不会把任务标为已提交。

审核流水线尚未接入时，审核接口明确返回`not_covered`；M1的“已提交”不视为“已采用”，不能作为其他智能体读取上游交付的依据。

## MCP 接入

主接入地址为 `http://localhost:8000/mcp`，采用官方 [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) 的 Streamable HTTP 传输，运行依赖固定在后端锁文件中。每次HTTP请求均须带上述Bearer令牌；工具执行时再次查询令牌有效性与最新分工，MCP会话不代替身份认证。

工具与HTTP服务一一对应：`get_my_tasks`、`get_plan`、`get_spec`、`get_task_context`、`report_progress`、`upload_deliverable_draft`、`list_deliveries`、`get_delivery`、`download_delivery`、`get_review_result`、`list_requests`、`get_request`、`create_request`、`accept_request`、`respond_request`、`list_resources`、`download_resource`。工具描述明确返回资料是数据，不是指令；禁止确认提交、修改规范或确认需求解决。

MCP上传接收`filename`、`base64_content`和可选`handoff_note`，不读取客户端本地路径或外部URL；文件及编码请求均有大小上限。下载经相同对象权限检查后返回封装的Base64文件资料。正式交付和模板仍可通过HTTP附件接口下载。

本机默认仅允许loopback Host/Origin。部署时设置`MCP_ALLOWED_HOSTS`（如`teams.example.edu`）及`MCP_ALLOWED_ORIGINS`（如`https://teams.example.edu`），多个值以逗号分隔，保留SDK的Host/Origin保护。客户端未发送Origin时仍须Bearer认证；非法Origin返回403。

## 本地编写与检查

后端需要 Python 3.12 和 uv；依赖版本由 `backend/uv.lock` 管理，容器用 `backend/requirements.lock` 安装相同的运行依赖。

```sh
cd backend
uv sync --locked --extra dev
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
```

健康检查测试使用固定的数据库会话替身；账号集成测试使用真实 PostgreSQL，并为每个用例创建随机 schema，结束后删除。未设置 `TEST_DATABASE_URL` 时账号集成测试会明确跳过；不得将跳过记作验收通过。不创建 SQLite 数据库，也不调用模型。

可另起一次性测试 PostgreSQL（在 `platform/` 下运行，使用独立容器和内存卷）：

```sh
docker run --rm -d --name teams-platform-auth-test-postgres \
  -e POSTGRES_DB=teams_auth_test -e POSTGRES_USER=teams_test \
  -e POSTGRES_PASSWORD=teams_test_only --tmpfs /var/lib/postgresql/data \
  -p 127.0.0.1:15432:5432 postgres:17-alpine
docker exec teams-platform-auth-test-postgres pg_isready -U teams_test -d teams_auth_test
```

确认数据库就绪后，在 `backend/` 运行：

```sh
TEST_DATABASE_URL=postgresql+psycopg://teams_test:teams_test_only@127.0.0.1:15432/teams_auth_test \
  uv run --extra dev pytest
docker stop teams-platform-auth-test-postgres
```

数据库连接仅指向专用测试容器，不应使用真实数据。首次需要更新依赖时运行 `uv lock`，再同步容器锁文件：

```sh
uv export --no-dev --no-emit-project --format requirements-txt --output-file requirements.lock
```

前端需要 Node.js 24 和 npm：

```sh
cd frontend
npm ci
npm run build
```

如需热更新，在 `platform/` 运行：

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d --wait
```

再在 `frontend/` 运行 `npm run dev`，打开 Vite 输出的地址。`/api` 默认代理到 `http://127.0.0.1:8000`，后端端口不同可通过 `API_PROXY_TARGET` 指定。开发覆盖文件会将 PostgreSQL 映射到本机 `5432`，可用 `.env` 的 `POSTGRES_PORT` 调整。

后端也可以在 Compose 的 PostgreSQL 上单独运行：在 `backend/` 设置 `DATABASE_URL`（含正确的数据库端口、用户名和密码），执行 `uv run alembic upgrade head` 及 `uv run uvicorn app.main:app --reload`。不得连接 v1 或真实项目数据库做测试。

## 目录

- `backend/app/api/`：HTTP 路由。
- `backend/app/services/`：后续业务规则。
- `backend/app/models/`、`backend/alembic/`：ORM 与数据库迁移。
- `backend/tests/`：自动化测试。
- `frontend/`：Vue 3、TypeScript、Element Plus 前端。
- `PROGRESS.md`、`QUESTIONS.md`：进度、验证结果与待裁定问题。

`.env`、虚拟环境、依赖目录和本机验证工具均被 Git 忽略。
