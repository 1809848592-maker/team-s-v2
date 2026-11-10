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

默认是本地开发配置：web 仅监听本机，PostgreSQL 不暴露宿主机端口，数据库为 `teams_dev`，数据保存在本项目的命名开发卷。示例密码仅用于本地开发。生产部署、HTTPS、备份将在 M4-04 完成。

## 账号与会话

打开页面可注册、登录和退出。注册使用一个账号标识（用户名或邮箱，1–80 个字符）和密码（8–256 个字符），注册后自动登录。账号标识去除首尾空格、忽略大小写匹配；填写邮箱只作为账号标识，当前不包含邮箱验证或找回密码功能。

密码使用 v1 相同的带独立盐的 scrypt 哈希。会话由服务器保存在 PostgreSQL，Cookie 为 `teams_v2_session`，包含随机令牌，库中仅保存其 SHA-256 哈希。每次成功登录都会更换当前浏览器的会话，退出会撤销该会话。会话默认 14 天，可通过 `SESSION_TTL_SECONDS` 设置；HTTPS 部署时设置 `SESSION_COOKIE_SECURE=true`。

平台管理员通过 `PLATFORM_ADMIN_USER_IDS` 指定已注册账号的不可变 UUID，多个 ID 用逗号分隔。先注册、登录，从 `GET /api/auth/me` 的响应获取 `id`，写入 `.env` 后执行 `docker compose up -d --wait` 重新创建服务。使用 ID 可以避免预设管理员用户名被抢注。未配置时没有平台管理员，注册请求不能自行指定权限。用量及额度管理页面按计划在 M3 实现。

账号 API：`POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`。注册和登录接收 JSON `{"username":"...","password":"..."}`，返回 `id`、`username`、`is_admin`。未登录或会话过期访问 `/me` 返回 401；退出成功返回 204。所有 POST 必须带 `X-CSRF-Protection: 1`；若请求有 Origin，则必须与服务同源，不开放跨域凭据请求。Vite 代理保留浏览器 Host，开发时使用相同校验。

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
