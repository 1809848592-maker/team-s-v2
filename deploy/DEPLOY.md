# Team.s v2 单机生产部署

适用范围：一台专用 Ubuntu 24.04 LTS Linux 主机，建议至少 2 核 / 4 GiB，磁盘按交付文件、审核调试记录和备份增长预留空间。学校服务器或国内云主机均可；需要 sudo、域名管理和公网端口配置权限。以下源码目录是 **platform 独立仓库**，不是 v1 仓库。Docker 内构建前后端，主机不需要 Node 或应用 Python 虚拟环境。

生产使用独立的 `compose.production.yaml`，不要叠加开发 Compose：PostgreSQL 17、web、独立 worker 和 Caddy。只有 Caddy 发布 80/TCP、443/TCP、443/UDP；web、数据库和 Caddy 管理接口均不发布宿主端口。所有服务有重启策略和日志轮转；数据库、文件、Caddy 证书/配置分别使用四个命名卷。模型请求仍由原有配额、知情同意和权限门禁控制。

## 1. 先确定域名与学校接入方式

- 与学校确认实际主机、使用期限、DNS 名称、校外访问方式、证书由谁维护，以及数据外发要求。域名备案是否覆盖本服务应由学校/接入商确认，不能因“学校服务器”就假定已覆盖。
- `PUBLIC_DOMAIN` 必须是独立的完整主机名，例如 `teams.example.edu.cn`，不含 `https://`、端口、路径或通配符。本配置在域名根路径提供网页、`/api/`、`/mcp`，不支持挂到某个路径前缀。
- DNS 的 A 记录指向主机公网地址；若有 AAAA，IPv6 也必须可达。安全组、学校网关及主机防火墙允许 80/TCP、443/TCP，443/UDP 用于可选的 HTTP/3。保留管理员的 SSH 入口。仅修改 ufw 不足以限制 Docker 已发布的端口，详见 [Docker 防火墙说明](https://docs.docker.com/engine/install/ubuntu/#firewall-limitations)。
- Caddy 根据域名自动申请和续期证书，并将 HTTP 重定向到 HTTPS；须保证 DNS 正确、80/443 从公网可达，主机可访问 ACME 服务。不能只用裸 IP 验证生产证书，也不能把本地开发 CA 当作公共证书。[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)
- 如果学校已在前置网关终止 HTTPS，先与其管理员确认端口转发及证书安排，再调整此部署；不要同时抢占学校已有的 80/443，也不要直接暴露 web 来绕过 Caddy。

截至 **2026-11-12** 查证：现行《非经营性互联网信息服务备案管理办法》（2024 修订）要求境内非经营性互联网信息服务履行备案；公益性网络接入单位可以代办，网站开通时还需展示备案编号及查询链接。先请学校/接入商确认主体、域名、接入及展示方式，再开通公网服务；本仓库不预填或虚构备案号。[工信部规章原文（陕西省通信管理局发布）](https://shxca.miit.gov.cn/xxgk/zcwj/wjfb/art/2024/art_a2e7f67bec17456798da4bb12b8a2b29.html)、[工信部备案系统](https://beian.miit.gov.cn/)

## 2. 在全新 Ubuntu 安装运行环境

以下是官方 apt 仓库方式；若已有旧版 Docker/发行版冲突包，先按官方文档处理。不要在已有业务主机上直接卸载软件。国内网络访问镜像仓库受限时使用学校批准的镜像来源，保留相同版本，不要改用来历不明的镜像。[Docker Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/)

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

这些命令在服务器由运维人员执行；`docker` 权限相当于主机管理员权限。使用当前 Compose v2 插件；不用旧的 `docker-compose` v1。

## 3. 取得版本和配置密钥

将 platform 独立仓库的获准版本部署到 `/opt/teams-platform`（将下面两个占位参数替换为实际地址和提交）。也可复制该版本的干净源码包；不要带入 `.env`、`.verification` 或开发数据库。

```sh
sudo git clone <platform独立仓库地址> /opt/teams-platform
cd /opt/teams-platform
sudo git checkout <已批准的提交或标签>
sudo install -d -m 0700 /etc/teams-platform
sudo install -m 0600 deploy/production.env.example /etc/teams-platform/production.env
python3 -c 'import secrets; print(secrets.token_hex(32))'
sudoedit /etc/teams-platform/production.env
```

填入域名、ACME 通知邮箱和生成的 **64 位十六进制数据库密码**。数据库 URL 使用此密码，不能填未经 URL 编码的 `@:/?#%$` 等字符；生成命令避开了这些问题。首次启动后不要只修改环境文件中的数据库密码：已有数据卷不会随之修改角色口令，应在维护窗口通过 PostgreSQL `ALTER ROLE` 同步轮换并更新配置。不要使用开发密码。

密钥只保存在主机的 `/etc/teams-platform/production.env`，不写入仓库、命令行参数、截图或日志。仓库和构建上下文排除了 `*.env`，但密钥仍应放在源码目录外。初次部署保持 `DEEPSEEK_API_KEY` 为空、`LLM_ENABLE_LIVE_CALLS=false`。不要 source 环境文件或把完整 `docker compose config` 输出贴到工单，它会包含密钥。使用 `config --quiet` 验证即可。

在此 shell 定义后续命令的简写（重开 shell 后需重定义）：

```sh
dc() {
  sudo docker compose \
    --env-file /etc/teams-platform/production.env \
    -f /opt/teams-platform/deploy/compose.production.yaml "$@"
}
dc config --quiet
dc build web worker
dc up -d --wait
dc ps
```

构建需要访问官方 Node/Python 基础镜像和依赖仓库。内存紧张时可在同架构构建机生成带版本标签的应用镜像，传到服务器 `docker load`，将 `PLATFORM_IMAGE` 设为该标签，再用 `dc up -d --no-build --wait`。不要为了构建中断已有生产数据库。web 启动前自动执行 Alembic 迁移，数据库健康后才启动 web，web 健康后启动 worker 和 Caddy。[Compose 生产部署](https://docs.docker.com/compose/how-tos/production/)

## 4. 检查 HTTPS、会话及智能体入口

将示例域名替换为实际域名；正式验证必须正常信任证书，不使用 `curl -k`。

```sh
curl -I http://teams.example.edu.cn/
curl --fail https://teams.example.edu.cn/api/health
dc logs --tail=80 web worker caddy
```

HTTP 应重定向到 HTTPS，health 返回 `{"status":"ok"}`，四个容器持续运行，web、数据库和 Caddy 健康。Caddy 的健康检查只证明配置管理进程存活，不能代替公网 DNS/证书/连通性检查。首次证书签发失败时检查 DNS、AAAA、NAT、80/443 占用及 Caddy 日志，修好后重新检查；不要通过关闭 TLS 掩盖失败。

在 HTTPS 页面注册测试账号，刷新后仍登录；浏览器的 `teams_v2_session` 应有 Secure、HttpOnly、SameSite=Lax。web 信任内部代理头，Caddy 保留 Host 并自行设置转发头，因此同源 POST 能通过原有 CSRF 检查，跨站 Origin 仍被拒绝。web 不能单独发布端口，也不能把它接入其他不受信任的共享 Docker 网络。[Caddy 反向代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)、[Uvicorn 代理设置](https://uvicorn.dev/settings/)

测试新建项目、上传和下载同一文件、时间线；将网页提供的智能体配置地址设为 `https://实际域名/mcp`，用官方 MCP 客户端验证初始化和工具调用。Caddy 不缓冲 MCP 流式响应。MCP Host/Origin 只允许这个公网域名；增设别名域名需一并审核白名单，不能为排错改为全开放。

平台管理员使用不可变账号 UUID：先在 HTTPS 上注册、登录，从浏览器 `GET /api/auth/me` 取得 `id`，写到 `PLATFORM_ADMIN_USER_IDS`（逗号分隔），然后 `dc up -d --no-build --wait web worker`。用户名不能授予管理员，未填则没有管理员。管理员页面用于额度调整及按时间导出 CSV，管理员身份本身不代替项目负责人权限。

## 5. DeepSeek 配置与数据条款查证

平台统一 Key，由发起人支付；不要给每个项目另填 Key。先配置额度和管理员，再在主机环境文件中填 `DEEPSEEK_API_KEY` 并显式设 `LLM_ENABLE_LIVE_CALLS=true`，重新创建 web、worker。两进程必须使用同一镜像和环境。Key、live 开关、项目负责人确认当前版本的数据外发告知三者缺一不能启用真实模型功能；关闭真实调用不会把“未完成/失败”变成通过。自动化测试始终关闭真实调用并使用替身。

模型配置在镜像中的 `backend/app/review/models.toml`：

| 角色 | 模型 | 思考模式 | 输出上限 / 超时 |
|---|---|---|---|
| spec_compliance、citation_extract、citation_alignment、spec_draft、project_summary | deepseek-flash | 关闭 | 4096 tokens / 60 秒 |
| logic、linkage、project_linkage、plan_self_check | deepseek-v4-pro | 开启 | 16384 tokens / 120 秒 |

API 基址 `https://api.deepseek.com`，环境变量名 `DEEPSEEK_API_KEY`。模型、指令或价格快照调整后重建并同时更新 web/worker，不能只改一个容器。详见 [模型配置](MODEL_CONFIGURATION.md)、[数据外发与用量](DATA_DISCLOSURE_AND_USAGE.md)。

默认项目每日 20 次审核、平台每日 200 次审核、平台每日 100 万 tokens、单审核最多 64 次模型调用；环境文件可以调整默认值，管理员可以调整各项目额度。`BOTTLENECK_RETURN_DAYS` 默认 3 天，为正整数。用量明细是估算，配额不是人民币硬上限：管理员导出的 CSV 需与 DeepSeek 账单核对。

**查证日期：2026-11-12；以下是条款查证结果，不能替代学校的数据外发决定。**

| 官方来源 | 核对结果及部署含义 |
|---|---|
| [开放平台服务协议](https://cdn.deepseek.com/policies/zh-CN/deepseek-open-platform-terms-of-service.html)（2026-04-22 更新，2026-04-29 生效） | 输入需具有相应权利，输入输出权属条款不等于厂商承诺不训练。协议引用隐私政策；未找到 API 请求数据明确不用于训练、固定保留天数或 API 专用退出参数的承诺。 |
| [隐私政策](https://cdn.deepseek.com/policies/zh-CN/deepseek-privacy-policy.html)（2026-02-10 更新/生效） | 适用范围包含 API，说明可能以加密、去标识后的输入输出改善模型；保存期限按必要性及法定义务决定，未给 API 专属固定期限。产品界面的体验优化开关不能推断为 API 不训练保证。 |
| [模型及价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) | 查证时 Flash 对应 V4.1-Flash-1017，Pro 对应 V4-Pro-0813。高峰每百万 tokens 的缓存命中/未命中/输出分别为 ¥0.04/2/8 和 ¥0.30/9/27，与当前配置快照相符；官方说明闲时半价，实际结算以平台账单为准。 |

因此保留设计 §14.7 的**负责人知情同意**要求，不宣称“API 数据绝不用于训练”。现行告知版本 `deepseek-2026-11-12.4` 写明向 DeepSeek 外发的正文、交接说明、规范、方案/历史、上游摘要、完整衔接和未发布自检草稿等数据；向 Crossref/OpenAlex 发送文献题名、作者、年份和 DOI。当前告知与上述查证一致，无需放宽门禁或换模型。后续供应商条款变化应重新查证、更新告知并让负责人确认新版同意。学校未允许外发的数据不要上传后再开启 AI。

## 6. 一致备份

附件本体不在数据库里。**必须在同一停写窗口备份 PostgreSQL 和完整文件卷**（包含审核调试记录）；仅 `pg_dump` 或仅复制卷都不完整。维护时不要同时运行导入、其他备份、数据库工具或第二套访问相同卷的服务。备份/恢复工具按环境文件加互斥锁；同一部署始终使用同一份环境文件。

```sh
cd /opt/teams-platform
sudo install -d -m 0700 /srv/teams-backups
sudo python3 deploy/maintenance.py backup /srv/teams-backups/2026-11-12-before-upgrade \
  --env-file /etc/teams-platform/production.env
sudo python3 deploy/maintenance.py verify /srv/teams-backups/2026-11-12-before-upgrade
dc ps
```

目录名每次换成唯一时间/事件，已存在的目录会拒绝覆盖。工具确认 postgres、web、worker 正在运行，停止 web/worker，先生成 PostgreSQL 自定义格式 `database.dump`，再归档 `storage.tar`，校验后写出带 SHA-256 和应用镜像 ID 的 `manifest.json`，最后恢复 web/worker。失败会返回非零，部分文件不发布为完整备份；恢复服务失败也返回非零，此时需检查 `dc ps`。维护窗口可能达到正在运行审核的时长，worker 默认给予 330 秒优雅退出；若上调审核超时也应相应调整停止等待。Caddy 在此期间可能返回 502，应提前告知使用者维护时间。

备份包含交付正文、用户数据、会话/智能体令牌哈希及调试材料，目录权限为 0700；只交给授权运维人员，传输和异地存储使用加密。建议每天及每次升级前备份，至少保留若干日常和升级前版本，并定期在隔离环境演练恢复；同机备份不能防止整机损坏。校验摘要用于发现损坏，不是来源签名，只恢复可信来源备份。

环境文件另行加密离线保存，不由工具复制，勿跟数据包一起公开。保留对应源码提交和应用镜像（可 `docker save`）及部署文件。Caddy 的 `/data`、`/config` 卷正常重建容器时保留；灾难恢复可以重新签发公共证书，应用恢复不依赖旧私钥。若学校要求保留证书私钥，另在停掉 Caddy 后安全归档这两个卷，再启动 Caddy，私钥按密钥管理。

官方依据：[PostgreSQL 17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)、[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)、[Caddy 持久卷](https://caddyserver.com/docs/running#docker-compose)。

## 7. 恢复到空目标

优先在新主机按第 1–3 节安装并取回**备份时的应用版本/镜像**、部署配置及安全保存的环境文件。不要先启动 web，它会迁移出表；不要把开发卷当作恢复目标。准备好同一 PostgreSQL 17 镜像和应用镜像后：

```sh
dc config --quiet
dc build web worker  # 或加载备份时的带标签镜像；不要重建成当前最新版
sudo python3 deploy/maintenance.py verify /srv/teams-backups/2026-11-12-before-upgrade
sudo python3 deploy/maintenance.py restore /srv/teams-backups/2026-11-12-before-upgrade \
  --env-file /etc/teams-platform/production.env
```

工具先校验摘要及归档路径，要求 web/worker 已停止，仅启动 PostgreSQL；目标数据库和文件卷有任何应用对象/文件都拒绝覆盖。恢复文件时使用镜像的非 root 应用用户，数据库恢复使用单事务并遇错退出。恢复结束**保持 web/worker 停止**，便于运维核对版本后再启动。恢复失败也不要启动应用，保留失败目标排查，换新的空目标重试；工具不提供删除现有数据的开关。

```sh
dc run --rm --no-deps -T --entrypoint alembic web current
dc up -d --no-build --wait
curl --fail https://teams.example.edu.cn/api/health
dc ps
```

用备份中的测试账号检查项目/方案版本、事件日志和交付记录，下载附件并与原文件 SHA-256 对比；检查权限隔离、旧版仍可下载及 worker 处理/恢复审核任务。租约恢复沿用原有任务规则，不能把失败任务记为通过。最后再切换 DNS/入口流量。如果在原机器演练，使用另一项目名（Compose 和工具的 `--project-name` 必须相同）、独立数据库/文件卷及不同入口；绝不能对现有生产卷执行清空或 `down -v`。

## 8. 升级、故障定位与验证边界

升级前完成第 6 节备份，记录源码提交和镜像 ID，在隔离环境验证迁移与恢复。维护窗口先 `dc stop web worker`，切换到获准版本，`dc build web worker`，`dc up -d --wait`；失败保留日志和备份，不要在已经升级的数据库上直接换回旧代码。需要回退时按第 7 节在空目标恢复升级前数据。普通停止用 `dc stop`，不删除卷；重启主机后 Docker 和 unless-stopped 策略恢复原先运行的服务，人工停止的服务需要 `dc start --wait`。

排查时先看 `dc ps` 和有限的 `dc logs --tail=100 web worker caddy`，再检查磁盘、配额、Key/live/项目同意和域名白名单。不要导出完整环境，也不要把受限审核调试记录贴到公共日志。配置文件改动通过 `dc up -d --no-build --wait` 重新创建；模型和指令文件改动须重建镜像。`dc restart` 不会加载新的容器环境。

Docker 客户端可能向容器注入 HTTP 代理。Caddy 明确让内部 `web` 上游绕过代理，回环健康探测也关闭代理，外部 ACME 请求仍可沿用运维配置的代理；不要删除内部旁路配置，否则可能出现容器健康但网页 502。

仓库内可运行的部署专项测试：

```sh
python3 deploy/test_deploy.py
```

此测试解析真实 Compose 配置（不需要 daemon），检查必填配置、端口/卷/构建边界、两进程安全配置和额度传递，并验证备份摘要与不安全归档拒绝。M4-04 另外用现有隔离验证环境测试本地 CA 的真实 TLS、会话/CSRF/MCP、备份恢复和 Compose 启动，证据记于 `PROGRESS.md`。这不能代替学校公网 DNS、可信公共证书或全新 Linux 主机验证；按计划 **M4-06** 再从零部署并跑全部里程碑验收。
