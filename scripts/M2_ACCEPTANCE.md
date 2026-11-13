# M2 智能体接入验收

仅对**独立测试数据库与独立存储卷**运行，不能指向生产或实际项目。脚本会注册随机 A/B/C 账号、新建项目和文件，所有业务写入均限于这些新账号；不自动删除数据。本阶段不调用付费模型。

完整验收由三个脚本组成：`m2_acceptance.py` 通过 Playwright 操作网页；`m2_mcp_acceptance.py` 使用项目锁定的官方 Python MCP SDK；`m2_claude_acceptance.py` 使用真实 Claude Code CLI，通过本机固定响应替身驱动 MCP 调用。替身只替代模型响应，MCP 的鉴权、工具调用、进度和稿件写入都经过实际容器服务。

需要启动平台服务、安装项目后端依赖、准备 Python Playwright/Chromium 及官方 Claude Code 可执行文件。可以复用 M1 的 Playwright 环境；官方 CLI 可局部安装到忽略目录，脚本不会安装或升级它。示例：

```sh
python scripts/m2_acceptance.py --help
python scripts/m2_acceptance.py \
  --base-url http://127.0.0.1:18001 \
  --mcp-python /absolute/path/to/platform/backend/.venv/bin/python \
  --claude /absolute/path/to/claude \
  --output-dir .verification/M2
```

若使用本机 Chrome，添加 `--browser-executable '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'`。先执行单任务测试并提交 M2-07，然后再次运行这个命令作为里程碑最终验收。退出码 0 表示自动检查通过；仍需查看页面截图与报告。

验收覆盖：

1. A/B/C 通过网页注册、邀请和入组，发布有分工/依赖的方案及规范和模板；C 提交尚未采用的交付，并建立另一个隔离项目。
2. B 在“我的智能体”登记，复制一次性令牌与三个配置片段；核对 JSON/TOML 语法、精确环境变量格式、复制结果、列表及他人隔离，离页/刷新后令牌不再出现。
3. 官方 MCP 客户端发现 17 个工具，读取 B 的任务/方案/规范/上下文，回报进度、上传私稿，尝试读取 C 未采用交付及跨项目数据，尝试自行确认、修改规范和标记完成，全部拒绝。
4. 真实 Claude Code 使用网页复制的 `.mcp.json`，在隔离配置目录下完成 `mcp list` 连接检查，以及读取任务/规范、回报进度、上传私稿。模型请求只到 `127.0.0.1` 固定响应替身，不访问第三方模型。使用 `--bare`、严格 MCP 配置、关闭持久会话及非必要外连，不使用已有用户认证、Keychain、插件或项目指令。
5. B 在网页填写必选 AI 声明并确认真实 Claude Code 上传的稿件，检查正式版本对成员共享、原字节及声明留存、人类确认身份；Bearer 同时携带人类 Cookie 也不能确认或改规范。
6. 核对拒绝事件的具体交付、跨项目 ID、操作者与代表的人；查看和筛选网页时间线。注入智能体列表和吊销 500，恢复后吊销，HTTP/MCP 均立即 401；检查 390px 页面及桌面截图。

M3 后台审核开启时，交付的 `review_status`、`review_progress` 会继续推进；私稿确认前后的共享检查逐项比较提交记录的其余字段，并仍核对版本数量、声明、绑定、提交人及原字节。用户智能体事件按本次登记的精确 ID 核对，平台审核后台事件不计入其代表 B 的操作。审核状态和后台事件另由 M3 验收覆盖。

输出目录含 `report.json`、`mcp-report.json`、`claude-report.json`、事件及截图和浏览器 trace。`credentials.json`、`agent-token.json`、trace 或 HTML 含测试凭据，仅保留在忽略目录；令牌文件为 600 权限。CLI 的临时配置、脱敏 transcript 和本地替身证据也留在本次目录。脚本不会把原始令牌放进命令行、日志或 MCP 配置片段。SDK 报告只保存固定检查项和对象 ID。

此外，`m2_draft_regressions.py` 可独立复跑 M2-06 的私稿确认失败重试与原子提交浏览器检查；其准备数据中的登记/上传走 HTTP，不能替代上述真实 Claude Code 的完整里程碑验收。

配置语法核对日期：2026-11-11。依据：[Claude Code MCP](https://code.claude.com/docs/en/mcp)、[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Cursor MCP](https://cursor.com/docs/mcp)。Claude Code 使用 `Bearer ${TEAMS_AGENT_TOKEN}`，Codex 使用 `bearer_token_env_var = "TEAMS_AGENT_TOKEN"`，Cursor 使用 `Bearer ${env:TEAMS_AGENT_TOKEN}`。Cursor 未安装在本机，本阶段核实其官方配置并验证 JSON 与复制结果；未声称在 Cursor 应用内执行。
