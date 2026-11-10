# M1 浏览器验收

仅对**独立测试数据库与独立文件存储卷**使用本脚本。它会通过页面注册三个新账号、创建项目并上传文件；不要指向生产、真实试点或开发业务数据。服务需提前启动，脚本不启动/清理容器、不调用模型。

需要 Python 3.12 与标准 `playwright==1.65.0`，以及 Playwright Chromium 或本机 Chrome。使用项目已有运行环境即可，不需要增加后端生产依赖。首次使用 Playwright 自带浏览器时，按该环境的方式安装 Chromium：

```sh
python -m playwright install chromium
```

在 `platform/` 中运行（默认静态前端测试地址为 18000）：

```sh
python scripts/m1_acceptance.py --help
python scripts/m1_acceptance.py
```

本机 Chrome 或 Vite 代理验收示例：

```sh
/Users/ethan/.claude/skills-python/bin/python3 scripts/m1_acceptance.py \
  --base-url http://127.0.0.1:5183 \
  --output-dir .verification/M1-vite \
  --browser-executable '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

完成 Vite 验证后，应对新构建静态前端再执行一次完整验收，例如改为 `--base-url http://127.0.0.1:18000 --output-dir .verification/M1-static`。每次运行均使用随机账号名和随机密码，A/B/C 分别位于独立浏览器 context；不会登录或读取已有用户的项目。输出目录下会新增独立 `run-时间-随机值/`，保留重复运行结果。

另有针对邀请处理中切换项目、时间线请求失败与恢复、退出失败可见性等交互的回归脚本，同样仅用于独立测试环境：

```sh
/Users/ethan/.claude/skills-python/bin/python3 scripts/m1_ui_regressions.py \
  --base-url http://127.0.0.1:5183 \
  --browser-executable '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

脚本的核心步骤均使用页面表单、按钮与下载链接；只读 GET 用于核对页面产生的结构、历史、通知、版本绑定及事件，未用 API 代替注册、发布、邀请、回应或上传操作：

1. A/B/C 注册；A 创建项目，生成两份单次邀请，由 B/C 分别在页面接受。成员入组前置于分配模块，以满足模块负责人必须属于项目的校验。
2. A 发布含研究目标、论证、3 个术语、2 项数据口径和 3 个模块的方案；B/C 分别负责模块，模块依赖为有向无环链。
3. A 选用 GB/T 7714、上外 PPT 和字体范例，修改字体后发布；下载 PPT 检查 ZIP、Office 结构、CRC 与 SHA256；上传并下载自己的模板，比较原字节。
4. B 回报进度，发起给 C 的需求；C 接手并回应，B 在原需求补充，C 再回应，B 确认解决。核对消息顺序、操作者以及 C 没有确认解决按钮。
5. B 提交带明确 AI 参与声明的正式 v1/v2，核对方案/规范绑定、提交人、哈希和阶段；通过页面下载旧版并比较原字节。另由 C 上传并下载共享材料。
6. 发布规范 v2，再查看冻结 v1，确认历史只读且不改变草稿；确认成员没有发布/上传模板/日志导出入口，读取通知并刷新验证会话。
7. 检查项目与任务时间线及操作者；从页面导出完整 JSON 和 CSV，转换 `details`、整数 ID 和空值后逐字段比较。保存桌面看板、方案、依赖图、规范、时间线与手机看板截图；手机看板不得横向溢出。
8. 导出核对后拦截 A 的退出请求，注入一次 HTTP 500；错误必须在工作台可见且账号仍登录。解除拦截后正常退出并重新登录，验证恢复。

退出码 `0` 表示自动断言通过；仍需查看截图确认布局与文字。`report.json` 给出每步结果。失败时退出码为 `1`，保留各账号的页面截图、HTML 和 Playwright trace。可用 `python -m playwright show-trace <trace-A.zip路径>` 检查。

输出中的 `credentials.json`（权限 600）、trace、HTML 可能含测试凭据和邀请链接，仅留本地。默认 `.verification/` 已被 Git 忽略；自定义输出目录也应放在忽略路径。脚本不自动删除项目或账号，避免引入设计尚未支持的删除流程；重复运行继续使用新账号，清理时仅删除独立测试环境的数据库和存储卷。

`pageerrors.json` 必须为空。`console.json` 记录全部控制台信息；新访客读取当前会话所产生的预期 `/api/auth/me` 401，以及明确标记为验收注入阶段的退出 500 单独排除，其余控制台 error 使验收失败，保存在 `unexpected-console-errors.json`。脚本本身不会屏蔽页面异常。
