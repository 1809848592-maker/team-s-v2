# 文本抽取测试样例

本目录全部为专用测试内容，不含真实交付、个人数据或密钥。测试在本地读取原始文件，不向外部服务发送内容。

- `authored-paragraphs-table.docx`：用 bundled python-docx 1.2.0 独立生成，包含中文、空段落、显式换行和制表符、2×2 表格、单元格内第二段、表后正文及页眉。
- `authored-reordered-slides.pptx`：用 bundled python-pptx 1.0.2 独立生成，12 张幻灯片的演示顺序为原 `slide12.xml`、`slide2.xml` 至 `slide11.xml`、`slide1.xml`；包含空段落、表格、分组文字及备注。
- `authored-bom-crlf.md`：UTF-8 BOM 和 CRLF，包含中文、连续正文行、代码围栏、空行及 Markdown 表格。
- `pdf-mixed-pages.pdf`：用 ReportLab 4.4.9 独立生成，依次包含两行英文、空白页、图像页及中文文字层；中文使用非嵌入的 STSong CID 字体引用，没有打包系统字体。生成参数和固定内容见 PDF 专项测试，ReportLab 和图像生成库不属于后端运行依赖。

Office 生成库仅用于制作这些静态样例，不属于应用或测试运行依赖。回归测试直接通过公开 `extract_text` API 检查文件内容和关键位置，不使用生成库或完整输出快照。
