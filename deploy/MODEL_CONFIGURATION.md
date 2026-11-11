# M3-03 模型配置与费用估算

核实日期：2026-11-11。本文件说明模型接入层的配置与边界，不替代完整部署指南。

## 当前执行范围

本项使用固定响应替身验证接入、用量记录和额度控制，不向模型厂商发送交付内容，也不执行真实付费调用。当前 worker 的审核 runner 尚未调用模型，后续审核员框架接入后才会使用本层；不能将后台任务完成视为审核通过。

`LLM_ENABLE_LIVE_CALLS=false` 是默认值。保持该值即可关闭本层真实请求。即使已填写 Key，也不会因此启用真实调用。M3-13 需要完成负责人知情同意及相应告知后，才能启用 AI 审核；单独修改此开关不代表已满足启用条件。

## 配置与密钥

审核员配置位于 `backend/app/review/models.toml`。每名审核员分别设置模型、服务地址、密钥环境变量名、思考开关、输出上限、请求超时和价格快照。

| 审核员配置名 | 模型 | 思考模式 | `max_tokens` |
|---|---|---|---:|
| `spec_compliance` | `deepseek-flash` | `false` | 4096 |
| `citation_extract` | `deepseek-flash` | `false` | 4096 |
| `citation_alignment` | `deepseek-flash` | `false` | 4096 |
| `logic` | `deepseek-v4-pro` | `true` | 8192 |
| `linkage` | `deepseek-v4-pro` | `true` | 8192 |
| `spec_draft` | `deepseek-flash` | `false` | 4096 |

这些是平台默认分配，不表示本项已实现各审核员。表中思考模式为 TOML 的 `thinking` 布尔值，发送时映射为 API 的 `enabled` / `disabled`。`provider` 为 `deepseek`，`base_url` 为 `https://api.deepseek.com`，`key_env` 为 `DEEPSEEK_API_KEY`，`timeout_seconds` 默认60秒。输出和超时限额可按审核员分别调整；不能超过 worker 的执行时间预算。

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

项目和全平台均有每日额度，日界线按 UTC。`LLM_PROJECT_DAILY_REVIEWS` 默认20，`LLM_PLATFORM_DAILY_REVIEWS` 默认200，`LLM_PLATFORM_DAILY_TOKENS` 默认1,000,000；`LLM_MAX_CALLS_PER_REVIEW` 默认64，限制单份审核调用总数，达到此上限会确定性失败以防止循环，不按次日额度暂停处理。令牌预算在外发前保守预留，收到有效用量后结算；每日审核名额按正式交付审核任务计，不能按每名审核员重复扣减。每日额度不足时审核暂停，尚无审核结论；暂停不消耗执行失败重试次数，后端给出可再次尝试时间。仅剩暂停任务时，页面每分钟读取进度，不发起业务重试或判定通过。

## 缓存与数据条款

官方上下文磁盘缓存默认开启，采用尽力命中方式，缓存前缀须匹配已持久化的完整单元；不能预先保证缓存折扣。缓存不再使用后通常数小时至数天清理，该描述不等于所有请求日志的保留承诺。本层将项目ID的 SHA-256 哈希作为 `user_id`，利用官方提供的 KVCache 隔离能力；不会发送用户名或邮箱，也不能据此宣称“不留存”。[缓存](https://api-docs.deepseek.com/guides/kv_cache/)、[隔离](https://api-docs.deepseek.com/quick_start/rate_limit/)

已核对 [DeepSeek 开放平台服务协议](https://cdn.deepseek.com/policies/zh-CN/deepseek-open-platform-terms-of-service.html)（2026-04-22更新，2026-04-29生效）。协议第3.5条要求开发者告知终端用户相关个人信息处理及委托处理规则，第5.5条说明其通用隐私政策不覆盖下游应用终端用户的个人信息处理规则。[通用隐私政策](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)也在引言作出同样的范围说明。

截至核实日，在所查公开 API 文档和协议中未找到 API 输入“明确不用于训练”的承诺，也未核实到 API 专属的日志保留期限或零留存选项。不能从通用隐私政策中的训练描述直接推定 API 数据必然用于训练，也不能宣称其不用于训练。按设计方案14.7，本项不外发；M3-13 须向负责人说明 DeepSeek 厂商名称、条款链接及上述不确定性，并取得知情同意后才能启用 AI 审核。本文件只记录查证结果，不提前实现或替代该同意流程。
