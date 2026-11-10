# Team.s v2 开发进度

设计依据：`../docs/superpowers/specs/2026-11-10-teams-v2-design.md`（第 14 节优先）。
执行计划：`../docs/superpowers/plans/2026-11-10-teams-v2-plan.md`（按第 0 节执行）。

## 当前任务

- M1-01 工程骨架：已实现，任务验证通过。
- M1-02 账号与会话：已实现，任务验证通过。
- 下一个未完成任务是 M1-03「项目、成员、邀请」，本次未开始。
- M1 里程碑尚未完成；M1-03 至 M1-10 完成后再运行完整里程碑验收脚本并交发起人验收。

## M1-01：工程骨架（2026-11-10）

### 已实现

- 在 `platform/` 初始化独立 Git 仓库。
- Python 3.12、FastAPI、SQLAlchemy 2、Alembic 基线；业务规则预留 `app/services/`。
- Vue 3、Vite、TypeScript、Element Plus 中文前端空壳，显示真实连接状态，支持重新检查。
- `/api/health` 检查 PostgreSQL，成功返回 `{"status":"ok"}`，失败返回 HTTP 503。
- 独立 worker 进程骨架，支持 SIGTERM/SIGINT 退出；任务队列在 M3-02 实现。
- Compose 包含 web、worker、postgres；前端构建后由 web 同源提供。
- 使用单独的 `teams_dev` 数据库及 Compose 开发卷，不连接 v1 数据。

### 验证

- 后端 `pytest -q --basetemp=../.verification/pytest`：3 passed，覆盖健康返回、数据库失败返回 503、静态前端不遮蔽 API。
- 测试有 1 条依赖弃用警告：Starlette 提示 TestClient 的 httpx 兼容方式将弃用。当前测试全部通过，尚未切换其建议的 httpx2。
- `ruff check .`、`ruff format --check .` 通过。初次检查发现 1 处导入排序问题，已修复。
- 前端 `npm run build`：vue-tsc 类型检查与 Vite 生产构建通过；npm 安装报告 0 vulnerabilities。
- 默认 Compose 与开发覆盖文件的 `docker compose config --quiet` 均通过。
- Docker 多阶段镜像实际构建通过，容器内前端构建及带哈希的 Python 锁定依赖安装通过。
- `docker compose up -d --wait --wait-timeout 90` 成功；postgres 和 web 为 healthy，worker 正常运行。
- 在实际 PostgreSQL 上执行迁移，`docker compose exec -T web alembic current` 返回 `0001_baseline (head)`。
- `curl http://127.0.0.1:8000/api/health` 返回 `{"status":"ok"}`。
- 使用本机 Chrome 的 Playwright 无头浏览器实际打开容器页面；中文空壳与“服务已连接”可见。模拟 health 503 后显示“暂时无法连接”，移除模拟并重新检查后恢复连接；无页面 JavaScript 异常，已检查截图。
- 只读独立复核未发现范围、代码或配置上的实质问题。
- 未调用付费模型，未连接真实数据库或 v1 数据。完整 M1 验收及开发热更新启动未执行（不属于当前单任务验收）。

### 工程选择与遗留

- 健康检查同时检查数据库，避免页面将数据库不可用显示为正常。
- M1-01 不建立业务表，Alembic 基线只建立迁移版本记录。
- 后端依赖由 `uv.lock` 锁定，容器通过导出的 `requirements.lock` 校验包哈希；前端使用 `package-lock.json` 和 `npm ci`。
- 本机原无 Docker。已在忽略目录 `.tools/` 下建立独立 Colima/Lima VZ 运行时（3 GiB 内存），Docker Engine 29.5.2、Compose 5.7.0。工具、VM、配置均在 `platform/`，未全局安装，未修改主机 DNS 或 v1 文件。
- 首次容器环境尝试遇到 VM DNS 文件缺失、公共 DNS 异常及 daemon/构建客户端未继承代理；首次镜像构建因此失败。已仅在本机 `.tools/` 配置 VM 初始化与代理后修复，随后镜像拉取、构建、Compose 启动均成功。代理配置未写入源码或 Compose。
- 当前开发服务保留运行，页面：<http://localhost:8000>。验证日志和截图在忽略目录 `.verification/`。
- 未改变设计方案，无新增产品行为、权限问题。

### 本机复用验证环境

在 `platform/` 下运行（`.tools/` 是本机工具，不随 Git 提交；其他机器请安装正常的 Docker 环境）：

```sh
source .tools/env.sh
# 如 VM 已停止：
colima start teams-m1 --ssh-config=false --activate=false
docker compose up -d --wait
# 停止开发服务：
docker compose down
# 停止本机 VM：
colima stop teams-m1
```

## M1-02：账号与会话（2026-11-10）

### 已实现

- 注册、登录、退出、当前账号查询 API 及中文页面；注册后自动登录，刷新后从服务器恢复会话。
- PostgreSQL 用户及会话表、`0002_accounts` Alembic 迁移。
- 密码采用 v1 的 scrypt 实现，独立随机盐；会话令牌随机生成，数据库只存其哈希。会话保留在服务器端，成功登录轮换当前会话，退出撤销；过期、伪造、缺失令牌均不能访问受保护接口。
- Cookie 设置 HttpOnly、SameSite=Lax、Path=/；Secure 和会话时长由环境变量配置。
- 平台管理员按 `PLATFORM_ADMIN_USER_IDS` 配置已注册账号 UUID。管理员身份由服务端配置决定，注册不能赋予管理员权限。
- POST 要求 CSRF 请求头并检查 Origin；Vite 开发代理保留原 Host，以支持相同校验。
- 注册冲突、账号或密码错误、网络故障都有中文提示；密码和会话令牌不出现在账号响应中，422 校验响应不回显原始输入。

### 验证

- 独立一次性 PostgreSQL 容器、数据库 `teams_auth_test`；每个测试使用随机 schema，执行完删除。不连接开发业务数据或 v1 数据。
- 设置 `TEST_DATABASE_URL` 后执行全部后端测试：**17 passed，无跳过**。覆盖错误密码、未知账号、会话过期、未登录/伪造会话被拒、退出后旧令牌失效、登录轮换、用户名唯一性、环境配置管理员、Cookie 属性、CSRF 及输入校验。
- 在输入校验用例补充敏感输入检查后，首次断言把错误类型 `string_too_short` 中的 `short` 误认为密码回显；已修正为匹配完整 JSON 字符串并检查无 input/ctx 字段，针对该用例复跑通过。产品实现未因此改变。
- 仍有 M1-01 已记录的 Starlette/httpx 弃用警告（1 条），未屏蔽；无测试失败遗留。
- `ruff check .`、`ruff format --check .` 通过。初次检查的导入排序和长行已格式化修复。
- `npm run build` 的 TypeScript 类型检查和 Vite 生产构建通过；Docker 镜像实际构建通过。
- `docker compose up -d --wait --wait-timeout 90` 成功，web/PostgreSQL 健康、worker 正常运行；原有基线数据库升级至 `0002_accounts (head)`。
- 另用独立 Compose 项目 `teams-platform-m1-02-smoke`、独立数据库和存储卷从零启动并自动迁移成功，浏览器测试数据仅写入该项目。
- Chrome/Playwright 分别通过容器静态前端（18000）和 Vite 代理（5183）实际完成：注册并登录 → 刷新恢复 → 退出 → 错误密码提示 → 正确密码登录；缺 CSRF 头的退出被拒且原会话有效。两条路径均无页面 JavaScript 异常，已检查登录页截图。
- 默认开发页面 8000 确认可显示登录表单；未在开发数据库中注册测试账号。
- 独立只读安全复核未发现本任务范围内必须修复的问题。
- 测试及冒烟环境、Vite 临时服务已清理；开发 Compose 服务保留运行供查看。

### 工程选择与遗留

- 沿用 v1 的密码长度（8–256）、scrypt 和 14 天默认会话，便于复用已有安全零件；会话时间可配置。
- 用户名或邮箱用一个账号标识字段接收，保留原显示文本，统一去空格和 casefold 作为唯一键；不增加邮箱验证或密码找回流程。
- 管理员使用不可变 UUID 名单，避免开放注册抢注管理员名称。名单为空时没有管理员；未提前实现 M3 的用量与额度管理。
- 使用独立 Cookie 名 `teams_v2_session`，避免与 v1 的 Cookie 混用；保持服务器端会话，不使用 JWT 或浏览器本地存储。
- 事件日志按计划在 M1-04 加入；项目、成员、邀请仍留给 M1-03。完整 M1 里程碑验收尚未执行。
- 未修改 v1、未调用模型、未花费付费服务。无设计偏离，无新增待裁定问题。
