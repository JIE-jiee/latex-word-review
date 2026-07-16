# GitHub 生态调研与复用决策

> 调研快照：2026-07-16。上游状态会变化，实施前必须重新核验版本、许可证、维护状态和接口。
>
> 2026-07 成熟化阶段对 Word 修订双视图、局部 UTF-8 provenance 与 PDF/SVG/EPS
> 图像路线的增量调研及 adopt/wrap/contribute/self-build 决策，见
> [`reviews/maturity-upstream-reuse-2026-07.md`](reviews/maturity-upstream-reuse-2026-07.md)。

## 调研结论

没有发现一个项目同时完成“高质量 LaTeX→Word、完整保留 Word 修订证据、结构化账本、逐条批准局部回填 LaTeX、编译与 latexdiff 核验”的全部闭环。

但转换和 DOCX 修订解析已经有质量较高的开源基础。本项目不应重新实现完整转换器，而应成为这些能力之上的可审计审阅工作流与安全回填层。

## 候选矩阵

| 项目 | 可直接借鉴或复用 | 与目标的主要差距 | 当前决策 |
|---|---|---|---|
| [yfyang86/tex2word](https://github.com/yfyang86/tex2word) / [PyPI](https://pypi.org/project/tex2word/) | MIT、Python、原生 OMML、Word 活字段、IR、reference.docx、转换报告、OOXML 校验、round-trip manifest、确定性输出 | 项目很新且主要由单维护者推进；README 的稳定性声明仍需独立验证。当前 DOCX reader 会接受插入并丢弃删除，未形成保留作者、时间和删除证据的逐项账本 | **优先 adopt/wrap**：主转换后端候选；通用缺陷优先向上游贡献 |
| [Mingzefei/latex2word](https://github.com/Mingzefei/latex2word)（PyPI 包名 `tex2docx`） | MIT；Pandoc + pandoc-crossref + Lua + reference.docx 的成熟封装；中文、子图合成和模板经验 | 自述仍约有 5% 内容需人工修复；核心能力受 Pandoc/pandoc-crossref 版本约束；不处理审阅回流 | **wrap/reference**：保留为 Pandoc 基线，借鉴预处理和样式模板，不复制实现 |
| [jgm/pandoc](https://github.com/jgm/pandoc) | 成熟通用转换器；DOCX reader 的 [`--track-changes=all`](https://github.com/jgm/pandoc/blob/main/MANUAL.txt) 能输出插入、删除、批注及作者和时间 | LaTeX→DOCX 的编号、活交叉引用和复杂宏有限；其 AST 不能成为本项目永久公共接口 | **adopt**：独立基线和修订提取候选，通过适配器隔离 |
| [lierdakil/pandoc-crossref](https://github.com/lierdakil/pandoc-crossref) | 图、表、公式编号和交叉引用过滤器 | 与 Pandoc 版本耦合；不能解决 Word 审阅账本和回填 | **optional dependency**：仅属于 Pandoc 后端 |
| [balalofernandez/docx-revisions](https://github.com/balalofernandez/docx-revisions) | MIT、Python；读取/写入 `w:ins`、`w:del`，暴露作者和日期，支持接受/拒绝 | 版本仍早期、范围聚焦修订；批注、复杂移动、跨段落和全部 OOXML 部件需验证 | **evaluate**：作为 Python 修订解析器候选和契约测试对象 |
| [SecurityRonin/docx-mcp](https://github.com/SecurityRonin/docx-mcp) | MIT；修订、批注、作者/日期、变更摘要、书签和结构审计；测试与 MCP/Skill 组织可借鉴 | 产品定位是通用 DOCX MCP，不负责 LaTeX 源映射；直接依赖会扩大运行时与接口面 | **reference/optional integration**：借鉴审计和测试，评估是否复用底层模块 |
| [ItMeDiaTech/docXMLater](https://github.com/ItMeDiaTech/docXMLater) | MIT；强调现有 DOCX 的修订、批注和书签 round-trip 保真；OOXML 架构与测试思路完善 | TypeScript/Node 运行时，与拟定 Python 核心形成双栈；不处理 LaTeX | **reference**：借鉴保真和结构验证设计，暂不作为核心依赖 |
| [transpect/docx2tex](https://github.com/transpect/docx2tex) | 可研究 DOCX→LaTeX 的结构映射经验 | 整篇反向转换与“局部、可确认回填”的安全目标不一致 | **do not adopt as apply path** |

## `tex2word` 重点核查

`tex2word` 1.0.5 已覆盖原计划中最昂贵的一批转换能力，因此不应在本项目中重写其 LaTeX parser、OMML writer、活字段、IR 或 OOXML package builder。

不过其当前 round-trip 语义与本项目目标不同：

- [`roundtrip.py`](https://github.com/yfyang86/tex2word/blob/main/src/tex2word/roundtrip.py) 使用 manifest-biased reconcile，目标是生成合并后的 LaTeX，而不是输出待批准的逐项审阅证据；
- [`docx_reader.py`](https://github.com/yfyang86/tex2word/blob/main/src/tex2word/frontend/docx_reader.py) 对 `w:ins`/`w:moveTo` 取接受结果，对 `w:del`/`w:moveFrom` 直接丢弃；
- 批注目前恢复文本和作者，但没有构成包含时间、锚点范围、原始 XML 证据及处理状态的完整账本；
- 对混合公式、引用或复杂块的编辑采取保守保留 manifest，适合避免破坏，但不能替代“告诉用户具体改了什么并逐条批准”。

因此推荐的边界是：

1. 让 `tex2word` 负责 LaTeX→DOCX、IR、OMML、活字段和基础 manifest；
2. 本项目在导出前后增加不可变运行记录、稳定审阅单元和统一能力报告；
3. 本项目独立读取原始 OOXML 修订与批注，生成 `ChangeSet`；
4. 只把已批准变更转换成针对原 LaTeX 切片的局部补丁；
5. 如果上游愿意，贡献“不自动接受修订的事件 API”、批注日期/范围和更稳定的 manifest/source-map 扩展点；
6. 不先 fork；依赖适配和上游 PR 失败且存在明确长期维护理由时才考虑 fork。

## 复用决策规则

每个依赖进入生产路径前必须通过：

1. 许可证和可选依赖审计；
2. 活跃度、维护者集中度和发布机制检查；
3. 受支持 Windows 环境及目标 Python 版本安装测试；
4. 自制最小语料的契约测试；
5. 私有复杂论文的压力测试；
6. 作者/时间/插入/删除/移动/批注保真测试；
7. 锚点、OMML、交叉引用和未知结构的降级报告测试；
8. 可替换性评估，避免核心 Schema 被单个依赖锁死。

README 中的功能声明只用于筛选候选，不作为验收结论。

## Adopt / Wrap / Contribute / Self-build

### Adopt

- `tex2word` 的转换核心；
- Pandoc 的独立基线及 `--track-changes=all`；
- LaTeX 编译链、`latexdiff` 和标准 Schema/OOXML 工具。

### Wrap

- 所有转换后端；
- 修订解析器；
- LaTeX 模板/Profile 和外部验证器。

### Contribute

- `tex2word` 的原始修订事件读取、批注元数据和 manifest 扩展点；
- 能被上游接受的通用解析、验证和测试修复。

### Self-build

- 不可变运行与归档模型；
- `ReviewIR`、`SourceMap`、`ChangeSet`、`PatchPlan` 和统一报告 Schema；
- 跨后端稳定 `unit_id`；
- 置信度定位、冲突分类和人工批准状态机；
- 只修改工作副本的局部补丁与综合核验；
- 独立 CLI 和薄 Codex Skill。

这些部分才是本项目相对于现有 GitHub 项目的主要贡献。
