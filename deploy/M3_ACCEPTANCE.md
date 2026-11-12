# M3 审核验收（M3-16 / M3-17 修订）

执行依据为设计方案 14.9 与开发计划 M3-16。自 2026-11-12 本修订起，真实验收采用“逐审核员真实单测 + 一次真实端到端”的两层方式。旧文档中要求模型生成 `checks`、`finding_links`、位置坐标，或者要求持续追加整轮真实复核的内容，由本修订及 [REVIEW_CONTRACT.md](REVIEW_CONTRACT.md) 取代。原判据、严重度、去重规则和“未完成不能当作通过”继续有效。

M3-17 根据设计 14.10 增加重新提交时的审核收敛。五位审核员的 M3-16 真实单测结果保留；本次新增验收使用同一问题稿完成退回、按意见修改和重新提交，确认修订稿进入终审。断网后恢复只要求 `coverage=completed`，不再要求恢复稿进入终审。收敛规则详见 [REVIEW_CONVERGENCE.md](REVIEW_CONVERGENCE.md)。

旧六轮真实验收的失败、用量及未知费用仍保留在 `PROGRESS.md`、原证据及用量记录中。本修订的新增 ¥10 授权单独建账，旧六轮历史不计入这笔新增额度；这不改变旧记录，也不将旧失败视为通过。

## 运行环境与材料

驱动从 `platform/backend/` 运行。只使用显式指定的本地 PostgreSQL 测试数据库和合成账号、项目、方案、规范与 DOCX，建立随机 schema 及隔离存储。每轮创建人类会话并完成当前版本知情同意，通过正式提交、持久队列、审核流水线和公开 HTTP 接口验证；结束时清理本轮 schema、服务和临时存储，保留验收证据。

数据库名必须包含 `test`、`smoke` 或 `accept`；拒绝非本地地址及 URL 查询参数。验收期间与镜像构建串行运行，避免本机 Docker VM 内存不足影响 PostgreSQL。浏览器报告、标注、重试和终审交互，仍需完成 M3-14 的独立浏览器验证。

固定问题稿保留故意错误的字体、虚构英文参考文献、绑定术语冲突及缺乏实证依据的普遍因果断言。真实字体检查及文献数据库核验规则不变；文献查询网络故障产生的“未能核验”不能代替明确的无匹配结果。修订稿恢复字体与术语、删除虚构文献，将保留命题明确限定为待检验假设；历史标注及原去重规则照常处理。

## 不付费的自动化验证

自动化测试始终使用模型替身。完整端到端替身回归可运行：

```sh
cd /absolute/path/to/Team.s/platform/backend
.venv/bin/python ../scripts/accept_m3.py \
  --mode mock \
  --database-url postgresql+psycopg://teams_test:teams_test_only@127.0.0.1:15432/teams_m1_test
```

替身经过真实 `LLMClient`、输出适配、程序检查、版本绑定、审核汇总、状态转换及用量账本；厂商与文献数据库传输使用固定响应。mock 模式不读取 `.env` 或模型 Key，也不访问真实模型。替身结果仅证明相应实现和链路，真实审核员单测结果单独记录。

## 两层真实验收

发起人已授权按 14.9 执行本修订的真实调用，新增估算费用合计最多 ¥10，尽量安排在工作日晚上或周末。运行修订驱动：

```sh
cd /absolute/path/to/Team.s/platform/backend
.venv/bin/python ../scripts/accept_m3_revision.py \
  --mode live \
  --database-url postgresql+psycopg://teams_test:teams_test_only@127.0.0.1:15432/teams_m1_test \
  --key-file /absolute/path/to/Team.s/platform/.env
```

`--key-file` 必须是本项目 `platform/.env` 的绝对路径。仅显式 live 模式读取其中字面量 `DEEPSEEK_API_KEY`，不执行文件或展开变量，不打印或提交 Key。真实模型传输仅向配置的 DeepSeek API 发送凭据，公开文献接口不接收模型 Key。

第一层逐个验证五个模型审核员：规范、文献抽取、引用对应、逻辑及衔接轻检。每位审核员对固定问题稿最多执行 3 次模型调用，至少 2 次结构合格且命中该审核员负责的预埋问题才通过；JSON 结构修复也占用这 3 次调用额度。已满足两次命中的审核员停止追加调用；失败审核员只使用自己的剩余次数。未完成、结构不合格或没有命中问题均不得计为通过。

第二层只执行一次真实端到端流程，验证：正式提交 → 审核退回 → 人类逐条采纳/不采纳并写理由 → 修订提交 → 人类终审并采用；另在预设的独立模块模拟断网，确认部分失败，再由授权人类经公开重试接口恢复；最后由平台管理员导出 CSV 并核对真实用量账本。智能体不能标注意见、终审或执行人类重试。

若这一次端到端因模型输出的偶然质量未能退回或进入终审，保存真实报告及具体原因，随后用替身补跑同一流程来证明链路。替身补跑的结果须与真实结果分别报告，不把真实未完成改写为通过，不再为此循环追加真实端到端调用。审核员真实单测未达标、预算门禁、环境故障及清理失败也须照实记录。

## 聚合费用账本与重复运行门禁

14.9 与 14.10 的所有真实试验、JSON 修复及端到端调用共享 `platform/.verification/M3-16-live-budget.json`，由 `scripts/accept_m3_budget.py` 管理。账本固定修订号 `M3-16`，以 Decimal 计算人民币金额。M3-17 继承已有 28 次真实调用及 ¥2.0009669600，不另建额度或清空账本。跨进程使用独占文件锁及原子替换，目录权限 0700、账本和锁文件权限 0600。

每次模型外发前，按 `json.dumps(messages, ensure_ascii=False, allow_nan=False)` 的 UTF-8 字节数加 1024 作为输入 token 保守上界，全部按缓存未命中单价计算，再加配置输出 token 上限的费用；人民币单价均按每百万 token 换算。加上此前已知估算费用及未知调用仍占用的预留，超过 ¥10 即停止，不发起该调用。有效用量返回后按实际估算金额结算；超时等未知费用保留原最大预留，不能记为零或通过新进程重置。人民币预算与平台用量/配额、每次输出上限、试验次数及请求超时共同生效。

驱动写入执行 marker 防止误触发重复真实验收。重新运行遇到既有 marker 时应停止并保留现场，不能删除 marker、账本或换路径来重复获得次数和额度。需要恢复时，先核对已有证据、各审核员已用次数、唯一真实端到端是否执行及剩余额度，再决定后续处置。

证据包含各审核员每次实际结果、命中判定、原有 JSON 修复记录、唯一真实端到端结果、必要时的替身补跑结果、用量与费用汇总、marker 及清理结果。语义或结构失败的原始模型输出保存在管理员专用调试记录中；真实验收使用独立的 `platform/.verification/M3-16-review-debug`，由同一存储按 30 天保留期清理，不复制原始响应到验收证据中。详见 [REVIEW_CONTRACT.md](REVIEW_CONTRACT.md)。终端和普通验收汇总不打印 Key 或完整厂商响应。

执行者将本次改造、替身测试、两层真实验收、费用和任何失败原样记入 `PROGRESS.md`，关闭 Q-M3-14-04 后停在 M3，等待发起人验收；不开始 M4。

## M3-17 同稿重新提交与恢复验收

替身端到端使用相同流水线，显式选择 M3-17。其修订稿额外生成一条位于未改动、旧轮无意见片段的模型重要意见，验证平台将其降为建议并保留原严重度，且修订稿进入终审：

```sh
cd /absolute/path/to/Team.s/platform/backend
.venv/bin/python ../scripts/accept_m3.py \
  --milestone M3-17 --mode mock --layer e2e \
  --database-url postgresql+psycopg://teams_test:teams_test_only@127.0.0.1:15432/teams_m1_test \
  --output-dir /absolute/path/to/Team.s/platform/.verification/M3-17-mock
```

真实验收直接运行端到端驱动，沿用已完成的五位审核员单测与同一人民币总账：

```sh
cd /absolute/path/to/Team.s/platform/backend
.venv/bin/python ../scripts/accept_m3.py \
  --milestone M3-17 --mode live --layer e2e \
  --database-url postgresql+psycopg://teams_test:teams_test_only@127.0.0.1:15432/teams_m1_test \
  --key-file /absolute/path/to/Team.s/platform/.env \
  --output-dir /absolute/path/to/Team.s/platform/.verification/M3-17-live \
  --revision-input /absolute/path/to/Team.s/platform/.verification/M3-17-revision-input.json
```

`--revision-input` 路径在启动前应尚不存在。驱动保存首轮报告、完成逐条标注后打印 `READY revision input` 并最多等待 900 秒。执行者依据该轮实际意见修改合成稿，将 JSON 原子写入该路径；只允许 `paragraphs`（1–100 个非空正文字符串）、`handoff_note` 和 `change_note` 三个字段。文件不超过 128 KiB，各字符串不超过 10000 字符且不得含 NUL。输入只作为数据读取；驱动使用修订正文生成字体合规的 DOCX，保留改动说明与输入 SHA-256。不能在收到首轮实际意见前用另一份无关合格稿冒充修改。

本次真实验收写入 `M3-17-live-e2e-started.json`，保留 M3-16 原 marker 与原失败报告；M3-17 marker 仍阻止重复启动本次真实流程。Key、`.env`、合成稿、管理员原文与本地验证证据均不提交进 Git。

验收分别判断两条路径：

- 同模块问题稿首轮 `coverage=completed` 且退回，修订版仍绑定同一方案和规范，经正常完整审核到达 `pending_final_review`，并且 `nonconverged=false`。不能用循环上限旁路代替收敛通过。真实模型没有产生需要降级的新意见时，仍按实际同稿重新提交结果记录；替身测试单独证明降级分支。
- 独立恢复模块先模拟断网得到 `coverage=incomplete` / `ai_review_partial_failure`，再由授权人类公开重试，恢复后 `coverage=completed` 即满足 14.10.6。恢复稿可以因模型意见处于 `returned`，也可以进入终审；未完成或绕过仍不通过。

保留首轮、修订轮、断网和恢复报告，原始严重度、收敛标记、终审上下文、调用/费用、管理员 JSON/CSV、清理结果及具体失败。全部真实费用加未知预留合计达到 ¥10 边界时停止外发并报告；完成后更新 `PROGRESS.md`，停在 M3 等待发起人整体验收。

若唯一真实端到端已执行首稿和修订稿，因修订稿仍退回而在断网恢复之前停止，只能补尚未执行的恢复阶段。使用：

```sh
cd /absolute/path/to/Team.s/platform/backend
.venv/bin/python ../scripts/accept_m3.py \
  --milestone M3-17 --mode live --layer recovery \
  --database-url postgresql+psycopg://teams_test:teams_test_only@127.0.0.1:15432/teams_m1_test \
  --key-file /absolute/path/to/Team.s/platform/.env \
  --output-dir /absolute/path/to/Team.s/platform/.verification/M3-17-recovery
```

恢复层只读固定 `M3-17-live-e2e-started.json` 引用的原报告，要求其为已发生真实调用、已完整清理、明确因 `revised_delivery_did_not_reach_final_review` 失败的 M3-17 端到端，并确认断网及恢复步骤和报告均未生成。新报告的 `continuation` 保存原失败报告路径与 SHA-256；新 marker `M3-17-live-recovery-started.json` 阻止恢复层再次付费运行，继续累计同一 `M3-16-live-budget.json`。

恢复层建立独立测试环境，只提交恢复稿，验证断网失败、授权人类重试、`coverage=completed` 及本层管理员 JSON/CSV 对账；不重复首稿或修订稿，不执行与本层材料无关的虚构文献预检。自动化演练可用相同命令改为 `--mode mock` 并删除 `--key-file`，不会读取真实失败报告或产生外发。恢复层通过只能证明恢复路径，原真实重新提交失败继续保留为失败；完整链路由替身另行补跑并分别记录。
