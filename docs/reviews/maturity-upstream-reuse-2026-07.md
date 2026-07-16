# 成熟化阶段上游复用决策（2026-07）

## 范围与结论

本轮针对三个发布阻断面重新核查上游：Word 修订完整性、局部修订到 LaTeX
UTF-8 字节位置的映射，以及 PDF/SVG/EPS 图像预览。结论仍遵守
adopt → wrap → contribute → self-build 顺序：通用 OOXML、比较器和渲染器不重写；
只自行实现本项目独有的不可变基线、LaTeX provenance、安全策略和审计契约。

## Word 修订与基线漂移

Microsoft 的 [`w:trackRevisions`](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.trackrevisions?view=openxml-3.0.1)
说明明确规定：该元素缺失时，应用不应生成修订标记。因此生产导出必须主动启用，
不能只依赖交付说明或 Word 用户偏好。

| 上游 | 许可证与维护状态 | 可复用测试/能力 | 明确差距 | 决策 |
|---|---|---|---|---|
| [Open XML SDK](https://github.com/dotnet/Open-XML-SDK) | MIT；Microsoft/.NET Foundation；2026 年活跃 | 强类型 OOXML 与 OpenXmlValidator | .NET 运行时不能直接提供本项目的 LaTeX 映射和 Python 审计链 | wrap 为 Windows CI 规范 oracle，不作为默认 Python 运行依赖 |
| [Open XML PowerTools](https://github.com/OpenXmlDev/Open-Xml-PowerTools) | MIT；功能成熟但正式版较旧、仓库寻求维护者 | RevisionProcessor Accept/Reject、WmlComparer；覆盖多 story、move 和属性变化 | 维护风险，不具备本项目不可变对象和 SourceMap | 参考实现并作为差分 oracle |
| [Docxodus](https://github.com/JSv4/Docxodus) | MIT；PowerTools 的活跃后继 | 文档 atomization、比较、接受/拒绝视图 | .NET；不提供 LaTeX byte provenance | 可选 oracle，后续合同稳定后再评估 sidecar |
| [docx-revisions](https://github.com/balalofernandez/docx-revisions) | MIT；2026 年活跃 | Python original/accepted text 与基础 accept/reject | 主要覆盖 body/table 的 `w:ins/w:del`，缺 move、格式、页眉脚注 | wrap 为窄范围单元测试 oracle |
| [Pandoc track changes](https://pandoc.org/MANUAL.html#reader-options) | GPL-2.0-or-later；成熟活跃 | `accept/reject/all` 三视图，保留普通修订作者/时间 | AST 丢失部分 Word 结构和原生 ID | 外部进程差分 oracle，不进入公共 Schema |
| [docx-mcp](https://github.com/SecurityRonin/docx-mcp) | MIT；活跃 | 修订、批注、保护和比较 API | 当前设置修订的实现使用非标准 `w:trackChanges`；比较范围偏正文纯文本 | 暂不采用；优先提交小型上游 issue/PR |

生产不变量为：

```text
semantic(exported_baseline) == semantic(reject(returned_docx))
```

不相等时只能诚实报告 `accepted_or_untracked_drift`。最终 OOXML 无法可靠区分
“Accept All”和“关闭修订后的普通编辑”，不得猜测作者、时间或补丁。另需对账：

```text
delta(reject(returned_docx), accept(returned_docx)) == normalized ChangeSet
```

本项目采用已有公开 fixture 中经过 Word 验证的 `w:trackRevisions` 写入逻辑；自行实现
只读语义投影、不可变基线绑定和 fail-closed 门禁。Word
[`CompareDocuments`](https://learn.microsoft.com/en-us/office/vba/api/word.application.comparedocuments)
仅作为交互桌面上的可选恢复/人工验收工具，不用于无人值守 CI。

## 局部修订与 UTF-8 provenance

局部映射的权威依据是导出基线和 OOXML 文档顺序，不是文本搜索。官方语义依据包括
[ECMA-376](https://ecma-international.org/publications-and-standards/standards/ecma-376/)、
[InsertedRun](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.insertedrun?view=openxml-3.0.1)
和 [BookmarkStart](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.bookmarkstart?view=openxml-3.0.1)。

| 上游/思路 | 许可证与维护状态 | 可复用点 | 决策 |
|---|---|---|---|
| [safe-docx](https://github.com/UseJunior/safe-docx) | Apache-2.0；活跃 | atomization、分层 LCS、书签语义比较与 accept/reject 往返测试 | 借鉴 token/测试设计；不复制其与 LaTeX 无关的整套编辑层 |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | MIT；活跃 | 漂移诊断、人工 diff 候选排序 | 只作诊断，永不决定自动补丁位置 |
| [diff-match-patch](https://github.com/google/diff-match-patch) | Apache-2.0；已归档 | 可视化模糊 diff | reject；模糊 patch 语义与精确回填冲突 |
| [python-docx](https://github.com/python-openxml/python-docx) | MIT；活跃 | 常规 DOCX/OPC 生成 | 不依赖高层 run API 推导修订坐标 |
| [python-docx-replace](https://github.com/ivanbicalho/python-docx-replace) | MIT | 跨 run 字符映射思路 | 仅参考，不能承担修订证据和 source byte 映射 |

采用的算法：

1. 导出时为每个 SourceUnit 记录压缩 provenance segment：review 字符范围、源 UTF-8
   byte 范围、转换类型及是否可自动回填。
2. 返回件内同时构造 baseline/reject/accept token view；只有 reject 与不可变导出基线
   完全一致时继续。
3. 沿 OOXML 顺序维护 baseline cursor：普通文本和删除消耗基线，插入为零宽位置；
   相邻删除和插入满足严格规则才合并为 replacement。
4. 通过 provenance 把局部 review span 缩窄为源 byte span。只允许 identity segment；
   空白折叠、宏渲染、field、OMML、drawing 和未知 token 均转人工。
5. 多补丁从右向左应用；重叠和同位置顺序无法证明时 fail closed。

Unicode 不做隐式 NFC/NFKC；UTF-16、Python code point 和 UTF-8 byte 坐标不得混用。
组合字符、ZWJ、variation selector 等边界未能证明为完整 grapheme cluster 时转人工，依据
[UAX #15](https://unicode.org/reports/tr15/) 与
[UAX #29](https://unicode.org/reports/tr29/)。

后续真实 Word 合同通过后，可评估 content control（SDT）+ bookmark 双锚点；在此之前
不把未经验证的 SDT 锁定行为加入自动应用承诺。

## PDF、SVG 与 EPS 图像预览

| 上游 | 许可证与维护状态 | Windows/已有能力 | 关键差距 | 决策 |
|---|---|---|---|---|
| [tex2word 1.0.5](https://github.com/yfyang86/tex2word) | MIT；2026 年新项目，活跃但维护集中 | 主转换后端；PDF extra 可调用 pypdfium2 | PDF 固定首页/200 DPI；忽略 `graphicspath`；缺 page/pagebox；graphicx 选项顺序丢失；PDF 自然尺寸按 96 DPI 解释 | adopt + wrap；不 fork；贡献 image resolver/materializer hook |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | Apache-2.0 OR BSD-3-Clause；活跃 | Windows x64/ARM64 wheel；页选择、box、scale、rotation | 输出依赖 PDFium 版本；crop/rotation 语义不等同 graphicx | adopt 为锁版本的窄 PDF renderer |
| [resvg](https://github.com/linebender/resvg) | Apache-2.0 OR MIT；成熟活跃 | 官方 Windows 二进制；大量 SVG→PNG regression tests | 字体与本地依赖必须显式锁定；不执行动画/脚本 | adopt 为安全静态 SVG renderer |
| [Pandoc](https://github.com/jgm/pandoc) | GPL-2.0-or-later；成熟活跃 | 路径/尺寸/DOCX relationship baseline；SVG fallback | LaTeX AST 丢 page/trim/clip/angle；PDF/EPS 不转 PNG | wrap 为外部 oracle |
| [Ghostscript](https://ghostscript.com/) | AGPLv3 或商业；成熟 | EPS/PS 栅格化 | PostScript 可执行语言、安全面与许可边界较大 | 仅显式配置的现代外部工具；默认 manual，不捆绑、不自动安装 |

tex2word PDF 实现见
[`raster.py`](https://github.com/yfyang86/tex2word/blob/v1.0.5/src/tex2word/backend/raster.py#L15-L60)，
graphicx 权威语义见
[`grfguide.pdf`](https://tug.ctan.org/macros/latex/required/graphics/grfguide.pdf)。项目不直接调用
屏幕截图，而生成确定性 canonical PNG：原 PDF/SVG 仍留在 LaTeX，Word 只嵌审阅预览。

短期集成采用派生 overlay，不修改原稿：安全解析静态 `graphicspath`、显式或无扩展图片和
有序 graphicx 选项，栅格化后只把 overlay 中的图片引用替换为相对 PNG。所有
page/crop/rotation/resize 在 PNG 中烘焙，避免 Word 成为第二个变换渲染器。

默认 review profile：目标 200 effective PPI、最低 150 PPI、单图不超过 12 MP、长边
不超过 6000 px；软预算触发明确降级，低于最低清晰度或超过硬预算则失败。缓存 key 绑定
源文件、页/box、有序操作、最终像素、renderer/PDFium/Pillow 版本及二进制 hash；同时记录
PNG bytes hash 和解码后 raw-pixel hash。

质量门禁要求 LaTeX 图片实例数与 DOCX drawing 实例数一致，或每个缺失项都有稳定错误；
不得再把“源有图片、Word 为 0 图片”报告为成功。EPS 默认明确为 manual。

## 公开测试与退出门槛

新增 fixture 必须完全自制并明确 Apache-2.0：

- 多页 PDF：不同 page size、MediaBox/CropBox 和不对称 landmark；
- 无字体依赖的 SVG landmark；
- `graphicspath`、显式/无扩展、同名多扩展歧义；
- page/pagebox、trim+clip、angle、width/height/scale 及操作顺序；
- 中文、emoji、组合字符、重复文本、多局部修订；
- Track Changes off、Accept All、bookmark 缺失/重复和未知 revision；
- 损坏/加密/超大 PDF，外链/DOCTYPE/junction escape 与资源上限。

只有生产修订开关、基线漂移门禁、局部 provenance 映射、PDF 图像实例对账及真实 Word
编辑保存合同均通过后，才可把当前 beta 晋级为公开 prerelease。
