# M4-06 全新 Linux 部署验收

本轮依据开发计划 M4-06，从全新的 Ubuntu 24.04 ARM64 虚拟机安装 Docker、复制干净源码、构建镜像、迁移空数据库，再运行 M1–M3 验收。最终结果及失败记录见 [PROGRESS.md](../PROGRESS.md) 的 M4-06 条目；本任务完成后等待发起人验收。

## 环境边界

- 使用独立 Lima 实例 `teams-m4-06`，2 核、4 GiB 内存、30 GiB 磁盘；不是原 Colima Docker daemon 的另一个 Compose 项目。
- Ubuntu 官方 minimal cloud image：`release-20261105/ubuntu-24.04-minimal-cloudimg-arm64.img`，SHA-256 `3dc8370d923978d4b610fa7964d1b36ba040eefb43198809ca5bc8ac51777b18`。
- 应用部署前确认没有宿主目录挂载，Docker 镜像、容器、卷均为零。源码由 `git archive` 导出，不复制 `.env`、旧数据库、存储、工具或验收现场。
- 原开发与验证虚拟机正常暂停，保留全部卷；任务结束后比较原数据库及文件摘要并恢复。新验收虚拟机停止保存现场。
- 使用 [生产 Compose](compose.production.yaml) 和原样 [Caddyfile](Caddyfile)。验收域名 `teams-m406.localhost` 只用于本机，Caddy 自动生成本地 CA，并显式加入 Linux 系统及 Chromium 信任库。所有 HTTPS 请求正常验证证书，没有使用忽略证书错误选项。
- 没有学校公网域名或主机，因此本轮不验证学校入口、备案或公共 ACME 证书。正式上线仍按 [部署指南](DEPLOY.md) 配置。
- `DEEPSEEK_API_KEY` 为空，`LLM_ENABLE_LIVE_CALLS=false`。M3 模型和文献查询均为固定响应替身，不重复付费真实验收；原 M3 真实模型结果与发起人验收结论仍在 `PROGRESS.md`。

## 复跑主线

只在独立验收环境执行。先按 [DEPLOY.md](DEPLOY.md) 安装 Docker 官方软件包、创建外部配置文件并构建、启动四服务。正式环境使用公共受信证书；本地 CA 验收须先安装其信任根，不能跳过 TLS 验证。

Linux 验收 Python 使用 3.12 虚拟环境，按 `backend/requirements.lock` 校验哈希安装依赖，额外安装 `playwright==1.65.0`、`pytest==9.0.2`、`httpx==0.28.1`。浏览器由 `python -m playwright install --with-deps chromium` 安装。M2 使用官方 `@anthropic-ai/claude-code@2.1.322` 的 Linux ARM64 可执行文件和经官方 SHA-256 校验的 Node 24.22.3，模型由脚本自带本地替身提供。

下列命令在 `platform/` 源码目录执行，路径按实际机器修改。验收输出含测试账号、令牌及 trace，全部放在 Git 忽略目录中。官方 CLI 的本地 CA 包装器只设置 `NODE_EXTRA_CA_CERTS`、`SSL_CERT_FILE` 后 `exec` 官方可执行文件，不改 MCP 调用或模型响应。

```sh
export DEEPSEEK_API_KEY=''
export LLM_ENABLE_LIVE_CALLS=false
export NO_PROXY=localhost,127.0.0.1,::1,teams-m406.localhost
export no_proxy="$NO_PROXY"
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export NODE_EXTRA_CA_CERTS="$SSL_CERT_FILE"

backend/.venv/bin/python scripts/m1_acceptance.py \
  --base-url https://teams-m406.localhost --output-dir .verification/M4-06/M1
backend/.venv/bin/python scripts/m1_ui_regressions.py \
  --base-url https://teams-m406.localhost
backend/.venv/bin/python scripts/m2_acceptance.py \
  --base-url https://teams-m406.localhost \
  --mcp-python /opt/teams-platform/backend/.venv/bin/python \
  --claude /opt/teams-platform/.verification/M4-06/claude-with-ca \
  --output-dir .verification/M4-06/M2
backend/.venv/bin/python scripts/m2_draft_regressions.py \
  --base-url https://teams-m406.localhost
```

M2 主脚本调用 `m2_mcp_acceptance.py` 和 `m2_claude_acceptance.py`，分别保留 SDK、真实 CLI 报告；不能只用 HTTP 调用代替 Claude Code。

另建 `teams_m406_accept` 测试数据库及专用角色。验收覆盖文件只给 PostgreSQL 增加 **Linux guest 的** `127.0.0.1:15432:5432` 映射，不改生产 Compose，也不转发到宿主。M3 驱动在这个库里新建随机 schema 和独立文件存储，结束后检查清理结果；它的临时 HTTP 服务与进程内 worker 验证完整流水线，和生产 Compose 的常驻 worker 分别记录。

```sh
cd backend
M406_DB=postgresql+psycopg://teams_accept:teams_m406_test_only@127.0.0.1:15432/teams_m406_accept
.venv/bin/python ../scripts/accept_m3_revision.py --mode mock \
  --database-url "$M406_DB" --output-dir ../.verification/M4-06/M3-16-revision
.venv/bin/python ../scripts/accept_m3.py --mode mock --milestone M3-17 --layer e2e \
  --database-url "$M406_DB" --output-dir ../.verification/M4-06/M3-17-e2e
.venv/bin/python ../scripts/accept_m3.py --mode mock --milestone M3-17 --layer recovery \
  --database-url "$M406_DB" --output-dir ../.verification/M4-06/M3-17-recovery
```

第一条命令包含五位审核员各自的替身检查和 M3-16 完整流程；M3-17 单独证明同稿修订、未改动片段新意见降级、不采纳去重、正常进入终审，以及断网后公开重试恢复。替身 token/估算费用属于合成账本，真实新增费用为零，不能当作本轮真实模型质量证据。

M1、M2 还需检查输出的桌面/手机截图，M3 检查审核报告、标注、重试、终审及管理员用量页面；页面替身与完整审核流水线证据分别记录。全量后端、前端和部署回归结果也应记录，不能以旧里程碑的通过记录代替本轮运行。

本轮重跑修正了三处旧验收资产：M1 等待后台任务稳定后比较事件，并核对平台执行身份；M2 提交快照比较排除两个动态审核字段，用户智能体事件仍按精确 ID 核验；M3 引用对应替身定位到预埋论据缺口的第 3 段。原人类权限、提交字段、严重度和真实验收判据均未放宽。M3 新增 subprocess 回归先复现失败，再验证五位审核员各两次命中及清理、零真实外发。

## 本地证据

`.verification/M4-06/` 保留源码包摘要、新机身份及空 Docker 状态、安装与重试、构建/启动/迁移、各里程碑原报告、页面截图、回归及环境恢复证据。失败尝试保留原日志，修复或重试后另存结果。真实 Key、生产环境文件、原文调试记录和测试凭据不提交到 Git。
