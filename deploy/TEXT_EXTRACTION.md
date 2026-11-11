# M3-04 本地文本抽取

核实日期：2026-11-11。抽取器读取 DOCX、PPTX、PDF、Markdown 原文件，返回文本、来源位置和覆盖范围。它不生成审核结论，也不改变交付版本、审核任务或项目终审状态。当前审核 runner 仍为 `pipeline_not_ready`，M3-06 再接入审核流水线；不能因为单独抽取成功而显示“审核通过”。

## 调用与输出

应用内入口为 `app.review.extract.extract_text(data: bytes, filename: str, *, limits: ExtractionLimits | None = None) -> ExtractedDocument`。文件名仅用于选择格式，支持大小写不敏感的 `.docx`、`.pptx`、`.pdf`、`.md` 扩展名；正文仍须通过相应解析器校验。旧式 `.doc`、`.ppt` 和其他扩展名不支持。

```python
from app.review.extract import extract_text

result = extract_text(original_bytes, "delivery.docx")
payload = result.to_dict()
```

结果对象不可变；`to_dict()` 可交给 JSON 序列化器，包含以下字段：

| 字段 | 含义 |
|---|---|
| `format` | `docx`、`pptx`、`pdf` 或 `md` |
| `sha256` | 输入原始字节的 SHA-256；不以规范化文本代替原文件指纹 |
| `segments` | 有文字的片段序列，每项为 `text` 和 `location` |
| `warnings` | 覆盖范围提示，每项为固定 `code`、`message` 和可选 `location` |
| `coverage` | `text_extracted`、`partial` 或 `no_text` |

`text_extracted` 表示本抽取器支持的内容已抽取且未产生提示，`partial` 表示有文字但存在提示，`no_text` 表示没有可用文字。这三个值只描述文本覆盖情况，不能替代审核状态。空白片段不进入 `segments`，但源文件中的空段落、空行和空页仍保留在后续位置编号中。达到处理上限会报错，不返回截断后冒充完整的结果。

## 位置约定与支持范围

`TextLocation` 含 `part`，以及适用时的 `page`、`slide`、`paragraph`、`table`、`row`、`column`、`shape_id`、`line_start`、`line_end`；不适用的字段为 `None`。数字序号从 1 开始，`shape_id` 保留文件内原始字符串。

| 格式 | 来源位置 | 抽取内容 |
|---|---|---|
| DOCX | `part` 为正文 XML 部件路径；`paragraph` 为正文遍历顺序中的全局段落号，包含表格和空段落；表内另有 `table/row/column` | 正文、表格单元格、超链接的可读文字；保留显式换行、制表符及可读缓存域文字 |
| PPTX | `slide` 为演示关系中的实际顺序；`part` 为对应幻灯片 XML 路径；`shape_id` 为图形 ID；`paragraph` 在该文本框或单元格内编号；表格另有 `table/row/column` | 文本框、形状文字、分组内文字、表格；幻灯片次序不依赖 `slide1.xml` 等文件名排序 |
| PDF | `part="pdf"`；`page` 为文件内物理页序号；`paragraph` 与 `line_start=line_end` 为该页抽取结果中的行号 | 文件已有的文字层，每个非空抽取行作为片段 |
| Markdown | `part="markdown"`；每片段对应一个非空源行，`line_start=line_end` | UTF-8 源文本，兼容 BOM、CRLF 和 CR；保留 Markdown 标记、代码围栏、HTML 和链接文字 |

DOCX 不计算 Word 渲染页码。表格行列表示原 XML 的单元格顺序，合并单元格不推算视觉网格。PPTX 的图形顺序采用源 XML 顺序，不重建屏幕阅读顺序。PDF 的页码不等于页内打印的页码标签；行号是抽取结果行，不是视觉排版行或作者语义段落。Markdown 仅作为源文字读取，不执行代码、HTML 或链接。

DOCX 的页眉、页脚、脚注、尾注、批注、绘图、文本框、公式和嵌入对象不进入正文结果；自动编号不生成，动态域不重新计算。修订仅保留当前可读的非删除文字，并附提示。PPTX 的演讲者备注、母版与版式继承文字、图表、SmartArt、图片和嵌入对象不识别。相应内容被发现时，结果附覆盖范围提示。

PDF 不进行 OCR。扫描页或图像中的文字不能据此视为已读取；表格、公式、多栏、字体映射和复杂版面的文字顺序可能不完整。所有 PDF 均附 `pdf_layout` 提示，因此有文字时 `coverage` 为 `partial`；空白页和扫描页另有 `pdf_page_no_text`，整份没有文字时为 `no_text`。Markdown 中的图片、音视频和图形仅保留源标记，并附未识别提示。调用者须保留 `warnings`，不能只传递拼接后的文字并丢弃覆盖情况。

## 正式交付读取

`app.services.text_extraction.extract_delivery_text(sessions, *, project_id, delivery_id, storage, limits=None)` 供可信审核流水线使用。它先按项目和正式交付版本共同查询文件名、大小及指纹，关闭数据库 Session 后再读取存储原文件，核对文件大小和 SHA-256，最后调用抽取器。

此服务不新增公开 HTTP/MCP 入口或读取权限，不处理共享材料及私稿，也不自动触发模型调用。调用者应先使用既有权限和正式交付流程取得版本标识。抽取结果包含交付正文，后续接入日志、缓存或审核员时仍须遵守正文和密钥不进入日志的既有约束。

## 处理上限与失败

默认 `ExtractionLimits` 如下，调用者可传入更小的正整数限额；布尔值、零、负值和非整数均不接受。

| 项目 | 默认值 |
|---|---:|
| 原文件字节数 `max_file_bytes` | 100 MiB |
| ZIP 条目数 `max_archive_entries` | 10,000 |
| ZIP 全部条目解压后总字节数 `max_uncompressed_bytes` | 128 MiB |
| XML 部件合计字节数 `max_xml_bytes` | 16 MiB |
| XML 嵌套深度 `max_xml_depth` | 128 |
| 文本片段数及覆盖提示数 `max_segments` | 各 20,000 |
| 已收集文字字符总数 `max_text_chars` | 2,000,000 |
| PDF 页数或 PPTX 幻灯片数 `max_pages` | 1,000 |
| PDF 文字、字体映射及相关流累计解码字节数 `max_pdf_content_bytes` | 32 MiB |

OOXML 包仅在内存中读取，不解压到磁盘。拒绝重复或不安全条目路径、符号链接、加密 ZIP、不支持的压缩算法、DTD、实体和超限 XML；外部关系只记录未读取提示，目标不作为磁盘或网络路径打开。defusedxml 用于 XML 解析；ZIP、字节数和深度限制由本应用另行实施。

PDF 采用严格解析，拒绝加密文件，并在解码过程中应用 pypdf 的输出和工作量限制；不同页、字体及表单流合计计入解码预算。只检查图片类型，不读取图片像素或运行外部解码器。解析器原始日志在当前抽取上下文中被屏蔽；修复告警会转成固定的失败结果，避免损坏文件被悄悄当作完整文本。

同步抽取 API 自身不提供硬时间或进程内存隔离。后续正式审核接入时，应在既有 worker 的子进程及硬超时边界内调用；当前 worker 未配置进程内存上限，不要将同步解析直接放进公开请求处理路径。PDF 解码前后的大小限制也不能代替进程级资源边界。

| `ExtractionError.code` | 含义 |
|---|---|
| `unsupported_format` | 扩展名不支持 |
| `invalid_document` | 包、XML、PDF 或声明格式无效，或文件损坏 |
| `encrypted_document` | 文件加密，未尝试使用密码解密 |
| `limit_exceeded` | 超过配置的处理上限 |
| `invalid_encoding` | Markdown 非 UTF-8 |
| `source_unavailable` | 正式交付不存在、项目不匹配或原文件不可读 |
| `source_mismatch` | 原文件大小或 SHA-256 与正式交付记录不符 |

异常对外使用固定中文消息，不拼入原文、文件名、XML、解析器异常或存储路径。无文字是有覆盖提示的 `no_text` 结果；解析失败是异常，二者不可合并为“审核通过”。

## 依赖选择与官方资料

后端锁定 `pypdf 7.0.1` 和 `defusedxml 0.7.1`，无需 Office、浏览器、OCR 或模型服务。真实验收样例的 python-docx/python-pptx 只用于本地生成测试文件，不属于后端运行依赖。

pypdf 使用 BSD-3-Clause 许可证；当前稳定版要求 Python 3.10 及以上，与本项目 Python 3.12 兼容。官方基础 API 为 `PdfReader(...).pages` 和 `page.extract_text()`。官方说明文字抽取不含 OCR，复杂版面坐标与段落推断有限，解析整页流可能占用较多内存。[项目及许可证](https://pypi.org/project/pypdf/)、[抽取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[PdfReader API](https://pypdf.readthedocs.io/en/stable/modules/PdfReader.html)

7.0.1 包含图像缓冲区安全修复及文字流处理改进；7.0.0 增强了对象流、XFA 和图像限制。版本升级仍须重跑本地样例和错误边界测试。[官方变更记录](https://pypdf.readthedocs.io/en/stable/meta/CHANGELOG.html)

defusedxml 使用 Python Software Foundation License。其 XML API 默认禁止实体和外部引用，但默认允许 DTD；本实现显式启用三项禁止标志，并补充字节、深度和归档限制。[项目 API 与安全说明](https://pypi.org/project/defusedxml/)、[许可证](https://github.com/tiran/defusedxml/blob/main/LICENSE)

## 本地验证

在 `platform/backend/` 执行 `.venv/bin/pytest -q tests/test_extract.py tests/test_extract_ooxml.py tests/test_extract_pdf.py tests/test_extract_samples.py tests/test_text_extraction.py`，验证四种格式的内容与位置，以及无文字、扫描页、加密、损坏、畸形 XML、外部关系、大小限制和正式交付来源校验。测试不读取真实 `.env`，不调用模型，不发送交付内容。

独立 Office 生成库制作的真实 DOCX/PPTX 和 BOM/CRLF Markdown 已纳入 `backend/tests/fixtures/extract/`，`test_extract_samples.py` 固定断言主要正文顺序、空段号、表格位置、幻灯片重排及源行位置；运行测试无需 Office 生成库。忽略目录 `.verification/M3-04-fixtures/` 另存本地生成脚本、`expectations.json`、SHA-256 和验收报告，作为本地证据，不作为生产资源。
