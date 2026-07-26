# ADR-0001：转换与修订解析上游策略

- 状态：已接受；平台范围部分由 ADR-0002 取代
- 日期：2026-07-16
- 决策者：项目维护者
- 关联：`docs/compat/e0-contract-protocol.md`、`docs/compat/upstream-dependency-matrix.md`、
  `0002-windows-only-support.md`

## 背景

项目需要高质量 LaTeX→DOCX、完整 Word 修订/批注证据读取和安全局部回填。现有项目已经覆盖转换与部分修订解析；本项目必须避免重新实现通用 LaTeX parser、OMML writer 或 DOCX package builder，同时不能把安全审批语义锁进某个后端 AST。

## 决策驱动因素

1. 正文、OMML、字段、图表和引用无静默丢失；
2. 能读取删除文本、作者、时间、批注范围和原始证据；
3. Windows 与 Linux 可安装、可测试；这是当时的跨平台驱动因素，当前维护范围已由
   ADR-0002 收敛为 Windows-only；
4. 许可证允许开源分发，并明确“Python 依赖”与“外部可执行程序”边界；
5. 公共接口可被适配器隔离，后端可替换；
6. 上游维护风险与贡献路径清楚；
7. 相同输入具有可验证的语义确定性。

## 已考虑方案

### 转换

- tex2word；
- Pandoc + pandoc-crossref；
- Mingzefei/latex2word（PyPI `tex2docx`）作为封装与模板参考；
- 自行实现转换器（原则上拒绝，除非 E0 证明不存在可复用途径）。

### 修订与批注读取

- Pandoc DOCX reader 的 `--track-changes=all`；
- docx-revisions；
- docx-mcp 的底层能力或设计；
- 本项目最小原始 OOXML 读取层；
- 先接受修订再比较文档（拒绝作为账本来源，因为会丢失证据）。

## E0 证据摘要

实验使用完全合成的 `tests/fixtures/e0-minimal-paper/` 和人工独立
`expected-changeset.json`。返回 DOCX 的 SHA-256 为
`d1d71a408f817daf33c34a26a23d624bab04d2b02b4e2085a6a9cd44283a44c5`。
完整命令、stdout/stderr、版本和产物哈希保存在
`build/upstream-eval/command-log.jsonl` 与 `build/upstream-eval/results.json`；
可提交的结论见 `docs/compat/upstream-contract-results.md`。

| 候选 | 安装/平台 | 转换或提取结果 | 证据完整性 | 决策 |
|---|---|---|---|---|
| tex2word 1.0.5 | Windows；Python 3.12.13/3.14.4 均实测 | 34 段、5 OMML、1 表、1 图、9 书签、11 活字段；规范化重复输出稳定 | IR/labels 可用，但无 source span/稳定 unit ID；round-trip 不保留逐项修订证据 | **adopt + wrap，主后端** |
| Pandoc 3.9 | Windows 便携可执行程序 | 正确 cwd 下 32 段、4 math、1 表、1 图、6 书签、0 活字段 | `--track-changes=all` 保留普通 ins/del 与批注元数据，但移动被扁平化、格式修订和修订 ID 丢失 | **wrap，对照后端与解析交叉核验** |
| pandoc-crossref 0.3.24 | 与 Pandoc 3.9 精确匹配 | 公开 fixture 结构计数未优于无过滤器输出 | 不能单独把原始 LaTeX 引用变成 Word 活字段 | **optional，预处理依赖** |
| docx-revisions 0.1.5 | Windows；Python 3.12.13/3.14.4 均实测 | 4 个直接 `w:ins/w:del` 的文本、空格、ID、作者和时间完全正确 | 不发现 move、格式修订或 comments | **窄范围 helper/oracle，不作唯一解析器** |
| canonical OOXML reader | E0 只读 observer 已实测 | 完整发现 ins/del/move/format/comments 及原始元数据 | 仍需在 R6 产品化安全读取和格式 before/after 重建 | **self-build，生产证据权威路径** |
| docx-mcp | 第一轮源码/设计调研 | 修订、批注和审计设计可借鉴 | 引入 MCP 运行时且不能解决 LaTeX SourceMap | **reference/未来可选集成** |

## 决策

1. **主转换后端**：采用并包装 `tex2word 1.0.5`。复用其 parser、IR、
   OMML、图片/表格、活字段、manifest 和报告，不复制转换核心。
2. **对照后端**：包装 `Pandoc 3.9`。适配器必须将 cwd 固定到 LaTeX
   源根目录；`pandoc-crossref` 只作为版本匹配、依赖预处理的可选组件。
3. **生产修订读取路径**：本项目实现最小、安全、只读的 canonical OOXML
   evidence reader。它负责原始 part/node 证据、ID、作者、带时区时间、
   move pair、格式修订和 comment range；不接受修订、不改写原件。
4. **交叉核验**：Pandoc `--track-changes=all` 与 `docx-revisions` 作为两条
   独立 oracle/helper。任一路径与 canonical 结果不一致时生成诊断，不静默选边。
5. **测试预言机**：人工独立 `expected-changeset.json` 加只读
   `scripts/e0_inspect_docx.py`，不使用被测工具自己的输出定义期望。
6. **Python 范围**：F1 以 Python 3.12 为最低版本；封版元数据为
   `>=3.12,<3.14`，只覆盖阻塞 CI 中的 3.12/3.13。3.14 虽曾在窄实验中运行，
   但不进入正式矩阵；上限是项目发布范围，不是对上游兼容性的反向声明。
7. **fork 策略**：当前不 fork。优先向 tex2word 上游贡献 source span、稳定
   review-unit、无损 revision event API 和 invalid escape `SyntaxWarning` 修复；
   只有贡献失败且适配层无法维持安全契约时重新审议 fork。
8. **禁止路径**：不使用 tex2word reconcile、Pandoc 或其他整篇
   Word→LaTeX 结果覆盖权威原稿。任何回填必须经过
   `ChangeSet → ApprovalSet → PatchPlan` 并只写新工作副本。

## 2026-07-25 补充：展示 LaTeX 中已有的批改

[CTAN `changes`](https://ctan.org/pkg/changes) 定义了 `\added[options]{新文字}`、
`\deleted[options]{旧文字}` 和 `\replaced[options]{新文字}{旧文字}`。这些规范命令只有在已绑定
项目依赖中静态可见标准 `changes` 宏包声明、已绑定项目树未发现同名 `changes.sty` 且未检测到
`\input@path` 搜索覆盖时才接受；裸调用以及直接或动态定义/重定义规范命令全部 fail closed，
不按 TeX 加载顺序猜测。

短命令仍使用独立的保守来源规则：`\add` 只有在唯一来源是静态
[`trackchanges`](https://trackchanges.sourceforge.net/help_stylefile.html) 宏包声明，或唯一、可证明
直接转发到 `\added` 的本地包装时才接受；`\delete` 只有在唯一、可证明直接转发到 `\deleted`
的本地包装时才接受。`\delete` 不是 `trackchanges` 文档声明的命令。宏包声明来源还必须没有
已绑定项目树中的同名 `trackchanges.sty` 或检测到的 `\input@path` 搜索覆盖；包装目标仍须
通过上述静态 `changes` 声明门。裸短别名调用、冲突定义、多重来源、签名不兼容或动态条件中的
定义全部 fail closed。

该门只证明“源码中静态可见标准宏包声明，且已绑定项目树未发现同名 `.sty`，也未检测到
`\input@path` 搜索覆盖”。它不执行 TeX 或 `kpsewhich`，不解析系统/用户 TEXMF 树或
`TEXINPUTS`，也不绑定 TeX 实际加载的宏包路径、版本和哈希。因此这里的 provenance 是有界
语法来源证据，不是对已安装宏包文件身份的证明。

决策：继续采用并包装锁定的 `tex2word==1.0.5` 后端。程序只在内存中注入定义，复用其宏展开、IR、`textcolor` / `sout` 支持和 Word writer，生成两种视图：

- **clean**：新增与替换保留新文字，删除去除旧文字；
- **display**：新增为 `0000FF` 蓝字，删除为 `0000FF` 蓝色单删除线，替换为旧文字蓝色单删除线后紧接新文字蓝字。本功能不添加高亮。

这样无需重建通用 TeX parser、通用转换 IR 或 OMML writer。本项目仍自行负责有界词法/平衡分组扫描器和独立 OOXML 验收层，因为产物身份与 fail-closed 安全属于本地责任。

`review.docx` 是密封的 clean 基线。只有它的精确可编辑另存副本属于可发给审阅者、并可在
返回后导入的角色。`existing-changes-display.docx` 只是静态展示辅助文件：它不是 Word
原生 Track Changes，不启用 Track Changes，也绝不能导入。

展示稿完成字段处理后，会在标准 `word/settings.xml` 文档变量中写入角色
`latex_changes_display_docx`、配置 `lwr-existing-changes-display-v1` 和当前 run ID；最终摘要
另作为 non-returnable 证据密封。receive 先拒绝摘要完全相同的展示稿，再拒绝仍带嵌入角色的
另存或重新打包副本。蓝色不是角色判据，正常审阅稿中的蓝色修订仍走 clean 基线核验。

扫描器只识别直接字面量调用；别名来源另在已绑定的 UTF-8 `.tex`、`.sty`、`.cls` 中做静态
provenance 审计。主文件限于 `document` 正文，其他已绑定的 UTF-8 `.tex` 文件扫描全文。
注释、支持的 verbatim/代码形式、定义命令正文和动态条件的全部分支不会被当作调用；目标规范
命令与短别名的定义仍接受来源和冲突审计。除可证明的短别名直接包装外，普通包装宏、`\csname`
和 TeX 条件不会展开或求值。允许普通 Unicode 文本、
平衡分组、嵌套批改调用、转义字面量和固定的简单格式 allowlist。直接识别到的参数若包含
公式、引用/文献引用、图片、环境、脚注、标签、段落分隔、未转义结构字符或其他命令，会在
后端执行前 fail closed。

空批改内容（如 `\added{}`、`\deleted{}`、`\replaced{}{}`）仍计入源库存，但不能形成非空
展示证据，因此展示稿导出以 `E_SCHEMA_INVALID` fail closed。

只要识别到至少一个宏，两份 Word 就都是同一次暂存导出的必需成员。展示验收从每个最上层
宏树生成诊断用源调用哈希、可见文字和逐字符删除线掩码，并要求 DOCX 提供足够的不重叠、
`0000FF` 蓝色且删除线状态精确相符的片段。对删除内容，还必须由同一段落的相邻可见正文
唯一证明删除线边界；上下文不足或候选不唯一时拒绝生成。验收同时要求 clean/display 新增的
蓝字、删除线和高亮数量与源预期相等，并对照公式、图片、表格、字段、关系和 story parts 等
非批改结构，避免论文别处相同蓝字补偿遗漏实例。完整 OPC 部件名集合，以及
`[Content_Types].xml`、`styles.xml`、`numbering.xml`、`fontTable.xml`、全部
`word/theme/*.xml` 和常规 `settings.xml` 也必须通过规范语义对照；只允许已列明的保存标识、
解析后的编号定义标识、Track Changes 角色差异和完整精确的展示角色 `docVars`。任一产物、
实例、结构、受保护部件或角色标记验收失败都会阻止发布，因此当前 `export/` 不会出现半套结果。

同段上下文证明只用于拒绝错误的删除位置。源调用哈希和偏移仍是诊断来源，不是 LaTeX 调用到
Word 坐标的密码学位置绑定；该展示流程也不为宏派生文字新增精确 SourceMap，自动回填仍依赖
独立的 clean 审阅书签和 SourceMap 契约。

## 边界

无论最终选择哪个后端，下列能力固定由本项目所有：

- 不可变 run、来源和原件哈希；
- 后端无关的版本化 Schema；
- `unit_id`、`SourceMap`、`ChangeSet`、`ApprovalSet` 和 `PatchPlan`；
- 风险分类、逐条审批、漂移/冲突检测和安全局部应用；
- 编译、引用、资源与 `latexdiff` 综合核验。

## 结果与后续动作

E0 上游选择完成，F1 可以开始。后续必须：

- 冻结后端能力协议，显式声明 cwd、OMML、活字段、manifest 和降级项；
- 在 R6 产品化 canonical OOXML reader，并以 Pandoc/docx-revisions 做差异报警；
- 保留 GPL 外部可执行程序与 Python wheel 的分发边界；
- 在具备 LibreOffice/soffice 后补做 DOCX 页面 PNG 渲染验收；
- 在公开发布 fixture 前确认其许可证。
