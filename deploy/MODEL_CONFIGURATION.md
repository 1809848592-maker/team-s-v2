# 模型配置与费用估算

模型接口及价格快照核实日期：2026-11-11；数据条款复核日期：2026-11-12。本文件说明模型接入层的配置与边界，不替代完整部署指南。项目告知、同意及管理员操作见 [数据外发告知与用量管理](DATA_DISCLOSURE_AND_USAGE.md)。

## 当前执行范围

M3-07 至 M3-09 的规范、文献、逻辑及衔接审核员已接入本层，worker 按固定流水线执行并持久化审核结果、用量及额度。M3-15 的平台辅助起草规范也通过本层调用一次模型，生成结果交给负责人编辑后手动发布，操作说明见 [规范辅助起草](SPEC_DRAFTING.md)。本阶段自动化及隔离容器验证均使用固定响应替身或禁用真实调用；真实模型验证只留待 M3 最终验收。后台任务完成本身不代表审核通过，交付状态由审核汇总规则决定。

`LLM_ENABLE_LIVE_CALLS=false` 是默认值。保持该值即可关闭本层真实请求。即使已填写 Key，也不会因此启用真实调用。M3-13 已实现版本化告知和负责人知情同意：整个审核流水线运行前和模型预算预留前均检查项目是否同意当前告知，辅助起草也在外发前检查，未同意则不外发。启用真实审核或起草须同时满足有效配置、真实调用开关及当前版本项目同意。

## 配置与密钥

审核员配置位于 `backend/app/review/models.toml`。每名审核员分别设置模型、服务地址、密钥环境变量名、思考开关、输出上限、请求超时和价格快照。

| 审核员配置名 | 模型 | 思考模式 | `max_tokens` |
|---|---|---|---:|
| `spec_compliance` | `deepseek-flash` | `false` | 4096 |
| `citation_extract` | `deepseek-flash` | `false` | 4096 |
| `citation_alignment` | `deepseek-flash` | `false` | 4096 |
| `logic` | `deepseek-v4-pro` | `true` | 8192 |
| `linkage` | `deepseek-v4-pro` | `true` | 16384 |
| `spec_draft` | `deepseek-flash` | `false` | 4096 |

前五项用于交付审核员；`spec_draft` 用于已接入的平台辅助起草规范。起草指令由平台固定资源及版本控制，调用方不能指定指令路径或模型。表中思考模式为 TOML 的 `thinking` 布尔值，发送时映射为 API 的 `enabled` / `disabled`。`provider` 为 `deepseek`，`base_url` 为 `https://api.deepseek.com`，`key_env` 为 `DEEPSEEK_API_KEY`，普通审核员及规范起草的 `timeout_seconds` 为60秒，启用思考的逻辑和衔接审核员为120秒。M3真实验收曾出现逻辑审核60秒超时，因此延长这两项请求时限；这一调整不改变模型、输出上限或计价快照。输出和超时限额可按审核员分别调整；整轮仍受 worker 的执行时间预算约束，超时只能记为未完成。最终验收使用900秒整轮时限；部署时应按文档规模调整 `REVIEW_JOB_TIMEOUT_SECONDS`（默认300秒），不能靠增加请求时限绕过整轮限制。

2026-11-12，发起人在 Q-M3-14-02 的提高付费输出上限建议后回复“继续工作”，据此仅将 `linkage.max_tokens` 从8192提高到16384。第四轮真实验收中该审核员输出达到8192并被截断，平台保留了未完成结论。其余审核员上限、全部模型、思考开关、价格快照及各自请求时限保持原值，逻辑和衔接仍为120秒；截断仍拒绝，不会自动扩容或重复付费。随后第5轮明确限制为最多24次账本调用、300000保守token，衔接已实际成功，逻辑8192截断，M3真实验收仍未通过。逻辑的新上限候选与待决定项见`../QUESTIONS.md`的Q-M3-14-03，正式配置仍如上表。

真实 Key 仅写入未纳入版本控制的 `platform/.env`，由 Compose 注入 web/worker 环境；Settings 通过 `DEEPSEEK_API_KEY` 读取并使用 `SecretStr` 保存。TOML 只写环境变量名，不写 Key。不要把 Key 放入前端、模型输出、日志、截图或提交内容。本文件和示例配置均不需要真实 Key。

## 官方接口与响应

使用 OpenAI 兼容的 `POST /chat/completions`，基址为 `https://api.deepseek.com`，凭据使用 Bearer 认证。当前文档列出 `deepseek-flash` 和 `deepseek-v4-pro`；旧 `deepseek-chat` / `deepseek-reasoner` 已公告于 2026-07-24 停用，不应作为新配置默认值。[首次调用](https://api-docs.deepseek.com/)、[变更记录](https://api-docs.deepseek.com/updates/)

本层采用非流式响应。接入时读取 `choices[0].message.content` 并校验完整 JSON；`finish_reason=length`、空白内容或不符合输出结构，都不能当作审核完成。JSON 模式需 `response_format={"type":"json_object"}`，提示中同时要求 JSON 并给出示例；官方说明即使停止原因为 `stop`，也可能返回仅含空格的内容。[JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)

显式设置 `thinking.type`，避免依赖厂商默认的思考模式。开启时推理过程位于 `reasoning_content`；它不是待展示的审核意见。思考模式下 `temperature` 不生效。[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)

用量保存 `prompt_tokens`、`completion_tokens`、`total_tokens`，以及厂商返回的缓存命中、未命中细分。DeepSeek 的 `prompt_tokens` 等于 `prompt_cache_hit_tokens + prompt_cache_miss_tokens`，`prompt_tokens_details.cached_tokens` 与缓存命中数同义；`completion_tokens_details.reasoning_tokens` 已包含在输出总数中，不应重复累加计费。本层会校验该推理细分的合法性，当前数据库不单独持久化此项。[响应字段](https://api-docs.deepseek.com/api/create-chat-completion/)

请求可能先收到空白行保活，官方“10 分钟关闭”仅描述尚未开始推理的连接，并非完整请求的完成承诺。平台仍需自己的超时边界。官方建议 500/503 稍后重试、429 降速；400/401/402/422 应先解决格式、凭据、余额或参数问题。重试要受 worker 次数与预算约束，不能悄悄重复付费请求。[连接与限速](https://api-docs.deepseek.com/quick_start/rate_limit/)、[错误码](https://api-docs.deepseek.com/quick_start/error_codes/)

## 价格快照与估算口径

以下人民币价格均为每百万 token，来自核实当日官方中文价表。`deepseek-flash` 对应 DeepSeek-V4.1-Flash-1017，`deepseek-v4-pro` 对应 DeepSeek-V4-Pro-0813；两者均支持 1M 上下文、最大 384K 输出。[模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

| 模型 | 时段 | 缓存命中输入 | 缓存未命中输入 | 输出 |
|---|---|---:|---:|---:|
| `deepseek-flash` | 空闲 | ¥0.02 | ¥1 | ¥4 |
| `deepseek-flash` | 高峰 | ¥0.04 | ¥2 | ¥8 |
| `deepseek-v4-pro` | 空闲 | ¥0.15 | ¥4.5 | ¥13.5 |
| `deepseek-v4-pro` | 高峰 | ¥0.30 | ¥9 | ¥27 |

北京时间周一至周五（中国法定节假日除外）09:00–12:00、14:00–18:00为高峰，其余为空闲。价格可能调整；TOML 的 `effective_date="2026-11-11"` 记录核实快照日期，`estimate_basis="peak_ceiling"` 表示平台统一使用高峰价估算，不在本项实现节假日计价。`prices.currency="CNY"`，`input_cache_hit`、`input_cache_miss`、`output` 使用十进制字符串保存每百万 token 单价。

缓存细分完整时，估算为：

`(缓存命中输入 × 命中单价 + 缓存未命中输入 × 未命中单价 + 输出总数 × 输出单价) / 1_000_000`

缓存细分未知时，费用估算记为 `NULL`，不能伪造命中率或冒充已核对账单。高峰价估算不等于实付金额；厂商折扣、实际时段和价目变更都可能导致差异，报销以 DeepSeek 平台账单为准。

按当前输出上限，三个普通审核员的单次输出各最多估算¥0.032768，逻辑审核员为¥0.221184，衔接审核员为¥0.442368。每位审核员各调用一次，输出部分合计最多¥0.761856；每位均再结构修复一次时为¥1.523712，均另加实际输入费用。该估算只使用2026-11-11高峰价格快照，不等于完整审核账单或平台余额保证。

项目和全平台均有每日额度，日界线按 UTC。`LLM_PROJECT_DAILY_REVIEWS` 默认20，`LLM_PLATFORM_DAILY_REVIEWS` 默认200，`LLM_PLATFORM_DAILY_TOKENS` 默认1,000,000；`LLM_MAX_CALLS_PER_REVIEW` 默认64，限制单份审核调用总数，达到此上限会确定性失败以防止循环，不按次日额度暂停处理。令牌预算在外发前保守预留，收到有效用量后结算；一份正式交付审核任务占一个项目名额和一个平台名额，不能按每名审核员重复扣减。每次辅助起草也各占一个名额，与交付审核共用额度及 token 账本；它使用独立起草来源，不创建交付审核任务或 `ReviewAdmission`。

每日额度不足时交付审核暂停，尚无审核结论；暂停不消耗执行失败重试次数，后端给出可再次尝试时间。仅剩暂停任务时，页面每分钟读取进度，不发起业务重试或判定通过。起草额度不足则直接返回429及可重试时间，不调用模型。每个起草请求最多调用一次模型，模型失败或结构不合格均不自动重试；负责人重新发起时使用新的请求标识。模型已经产生的有效用量仍计账，即使草稿最终未通过条目校验或未交付给页面。

## 缓存与数据条款

官方上下文磁盘缓存默认开启，采用尽力命中方式，缓存前缀须匹配已持久化的完整单元；不能预先保证缓存折扣。缓存不再使用后通常数小时至数天清理，该描述不等于所有请求日志的保留承诺。本层将项目ID的 SHA-256 哈希作为 `user_id`，利用官方提供的 KVCache 隔离能力；不会发送用户名或邮箱，也不能据此宣称“不留存”。[缓存](https://api-docs.deepseek.com/guides/kv_cache/)、[隔离](https://api-docs.deepseek.com/quick_start/rate_limit/)

2026-11-12 复核了 [DeepSeek 开放平台服务协议](https://cdn.deepseek.com/policies/zh-CN/deepseek-open-platform-terms-of-service.html)（2026-04-22更新，2026-04-29生效）、[通用隐私政策](https://cdn.deepseek.com/policies/zh-CN/deepseek-privacy-policy.html)（2026-02-10更新并生效）及[用户协议](https://cdn.deepseek.com/policies/zh-CN/deepseek-terms-of-use.html)（2025-09-05更新并生效）。开放平台协议第3.5条要求开发者承担下游终端用户数据告知及适用的同意责任，第5.5条区分开发者自身与下游终端用户的数据处理范围。

截至该复核日，未核实 API 请求数据明确不用于训练的承诺、API 专属日志保留期限或训练退出配置。通用条款中的训练描述不能直接推定 API 数据必然用于训练，消费产品内的退出开关也不是本平台已核实的 API 保障。辅助起草仅将负责人填写的起草要求正文作为业务输入发送给 DeepSeek，不附带既有草稿、项目正文或模板目录。当前告知版本为 `deepseek-2026-11-12.2`，新增起草要求外发范围；旧 `.1` 同意需要重新阅读并确认。

按设计方案14.7，创建项目时的同意默认不勾选；负责人也可稍后在任务页或规范页阅读并同意，提交人首次正式提交时另行阅读告知。旧项目不会自动补记同意。完整查证范围及操作说明见 [数据外发告知与用量管理](DATA_DISCLOSURE_AND_USAGE.md)。
