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

## 本地编写与检查

后端需要 Python 3.12 和 uv；依赖版本由 `backend/uv.lock` 管理，容器用 `backend/requirements.lock` 安装相同的运行依赖。

```sh
cd backend
uv sync --locked --extra dev
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
```

测试使用固定的数据库会话替身，不创建 SQLite 数据库，也不调用模型。首次需要更新依赖时运行 `uv lock`，再同步容器锁文件：

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
