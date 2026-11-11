# 文献元数据接口（M3-08）

核对日期：2026-11-12。仅访问 Crossref、OpenAlex 官方元数据 API；不下载全文、不打开数据库返回的文献 URL，不保证全文内容或论断真实。自动化测试全部使用 `httpx2.MockTransport`。

## 官方认证、免费额度与限制

| 数据库 | 本实现使用方式 | 官方当前说明 |
| --- | --- | --- |
| Crossref | 公共匿名 HTTPS REST，不配置收费 Metadata Plus Key，不伪造联系邮箱 | 无需注册；公共池 5 请求/秒、并发 1；polite 池需要真实联系邮箱。限流为 429，封禁可返回 403；实际限制也由响应头给出。 |
| OpenAlex | 默认匿名 HTTPS REST；仅显式传入有效 `SecretStr` 才使用 `Authorization: Bearer …` | 当前基础查询允许无 Key，匿名预算 $0.10/天、10 请求/秒；免费且邮箱已验证的 Key 为 $1/天、30 请求/秒。DOI/ID 单条查询免费，搜索当前为 $0.001/次，预算每 UTC 午夜重置。超限返回 429。 |

OpenAlex 旧文档入口现转向 Help Center；以当前官方认证与价格页为准。无需为此实现注册新账号、申请 Key 或购买额度。客户端不会读取环境变量、`.env`、浏览器、用户账户或 Key 文件，也不会注册或激活 Key。可选 Key 只进入 OpenAlex Authorization 请求头，不进入 URL、审核报告或错误信息。

官方依据：

- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) 与 [Access and authentication](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/)。
- [Crossref 官方 REST API 参数文档](https://github.com/CrossRef/rest-api-doc)：DOI 与参数值应编码；`query.title` 已弃用，使用 `query.bibliographic`。
- [OpenAlex Authentication](https://help.openalex.org/api/authentication/)、[Example costs](https://help.openalex.org/access/example-costs/) 与 [Get Singleton](https://help.openalex.org/api/get-single-entities/)。

## 实现边界

`CitationDatabaseClient(transport=None, openalex_api_key=None, timeout_seconds=10, max_response_bytes=1048576)` 是同步内部客户端。所有数据请求均为固定 `https://api.crossref.org/works` 或 `https://api.openalex.org/works` 下的 GET。DOI 先校验并完整编码为路径标识符；搜索文字作为编码后的参数，不能改变主机或注入额外参数。禁止重定向和环境代理；TLS 保持验证；不跟随响应内任何 URL。

每库先做一次 DOI 或标题查询。DOI 返回 404 且具有足够标题、作者、年份时，最多再做一次标题查询，以免把 DOI 拼错当成文献不存在。因此每条文献最多 4 次 HTTP 请求（每库最多 2 次），没有自动重试、分页、模型调用、全文请求或并行 fanout。429、超时或其它失败立即保留未核验状态，不等待 `Retry-After` 后重试。

每请求连接、读、写、连接池超时均受 `timeout_seconds` 约束，并检查响应读取的总时限；可配置范围大于 0、不超过 30 秒。体积上限最多 1 MiB，既检查 Content-Length，也在流式读取时检查累计字节；请求 identity 编码并拒绝压缩体。JSON 必须为 UTF-8 对象，拒绝重复键、NaN、超过 32 层的嵌套、错误类型及畸形数据库记录。实际请求在本进程按库串行并以 Crossref 0.21 秒、OpenAlex 0.11 秒最小间隔节流，MockTransport 不等待。多进程共用出口仍由上游公共配额约束；限流只产生未核验。

标题检索每库最多读 5 条候选；不翻页。未命中且数据库总数表明候选已截断时标“未能核验”，不把有限页面当完整排除。只保留匹配需要的标题、作者、年份、DOI 与固定格式的数据库标识，不保留响应体、摘要或任意落地 URL。

## 核验结果约定

`CitationQuery`、`ProviderEvidence`、`CitationVerification` 均为 frozen dataclass；结果通过 `to_dict()` 转成 JSON 字段。provider 依次为 `crossref`、`openalex`；单库状态为 `matched`、`no_match`、`unverified`、`unavailable`，外层状态为 `verified`、`not_found`、`unverified`。`reason` 来自固定中文文案，不拼接外部响应、输入、URL 或异常。

- `verified`：至少一个库强匹配且另一库没有弱匹配或冲突；两库不同 DOI 也标未核验。已有 DOI 必须完全对应，提供的标题、作者、年份均须兼容；标题查询还须有足够长标题、作者和年份。标题只规范大小写、Unicode、标点和空白；作者允许姓前置和给名缩写，保持给定作者顺序。不是模糊相似度自动通过。
- `not_found`：非中文、信息充分、两库查询成功并明确无匹配。DOI 404 本身不够；标题、作者、年份不足时标未核验，充分时还要求两库的补充标题检索也明确无匹配。
- `unverified`：401/403/429/5xx、重定向、超时、网络错误、体积/结构/元数据异常、弱或冲突候选、候选截断、信息不足。中文文献无可信自动匹配时始终保持此状态，交人工核对。

数据库元数据匹配只证明本次检索与收录记录相符，不证明数据库完整覆盖、作者身份、全文真实性、引用是否支持正文论断或不存在撤稿。对应正文引用的模型检查由文献审核业务层完成。

## 手动公开接口冒烟

以下命令必须人工明确运行；不读取 `.env`，不提供 Key，不调用模型，只查询一篇已发表公开论文的两条元数据接口。最多 4 次请求；若没有网络或数据库拒绝，打印未核验而非假报成功。工作目录为 `platform/`：

```sh
PYTHONPATH=backend backend/.venv/bin/python - <<'PY'
import json
from app.review.citation_databases import CitationDatabaseClient, CitationQuery

query = CitationQuery(
    title="The hallmarks of cancer",
    authors=("Douglas Hanahan", "Robert A. Weinberg"),
    year=2000,
    doi="10.1016/S0092-8674(00)81683-9",
    is_chinese=False,
)
result = CitationDatabaseClient(timeout_seconds=8).verify(query)
print(json.dumps(result.to_dict(), ensure_ascii=False))
PY
```

本轮实际执行结果见本任务的 `PROGRESS.md`；MockTransport 测试通过不等同真实网络冒烟成功。
