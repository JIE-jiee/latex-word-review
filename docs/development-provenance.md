# 开发来源与 Vibe Coding 记录

## 公开说明

LaTeX Word Review 源自一个真实的研究协作问题，并主要通过维护者驱动的 Vibe Coding 与
OpenAI Codex 协作开发。

这里的 Vibe Coding 指一种持续对话式开发过程：维护者用自然语言描述问题、反馈结果、纠正
方向和追加约束，AI 编程 Agent 负责调研方案、实现代码、运行测试、审查失败和整理文档。
它不是“一句话生成整个仓库”，也不表示 AI 输出天然正确。

本页说明项目怎样形成、人与 AI 各自承担什么，以及读者应怎样判断它是否可信。

## 问题从哪里来

论文作者经常用 LaTeX，导师或合作者却习惯 Word 的修订和批注。把 LaTeX 导出到 Word 并不
难，难的是返回：

- Word 中有作者、时间、插入、删除、格式和批注证据；
- 原 LaTeX 中有宏、公式、引用、标签、环境和期刊模板；
- 手工复制容易漏改；
- 整篇 Word 转回 LaTeX 可能破坏原结构；
- 接受全部修订或关闭修订会让证据消失；
- LaTeX 常引用 PDF 图片，Word 审阅时需要另一种表现形式。

项目因此没有把目标定成“做一个更大的双向转换器”，而是把 Word 定位为审阅界面，把
LaTeX 保持为唯一权威源。

## 需求怎样在对话中收敛

开发过程经历过多次方向修订。下面只记录对公共项目有影响的决策，不公开私人论文内容或
个人对话记录。

| 维护者提出或修正的要求 | 形成的工程决策 |
|---|---|
| 不要只处理一篇论文，要成为可复用项目或一套成熟 Skill | 核心业务放在独立 Python 库和 CLI 中，Skill 只做薄编排 |
| 先借鉴 GitHub 上已有项目，不要重复造轮子 | 建立上游矩阵和 ADR，按 adopt、wrap、contribute、self-build 决策 |
| Word 返回稿中的每条修改要能决定是否采用 | 建立 `ChangeSet -> ApprovalSet -> PatchPlan` 三层对象和本地审批页 |
| 用户逐项决定后也不能立刻覆盖论文 | 把审批和应用拆成两道人工闸门，`plan` 默认 dry-run，`apply` 只写新目录 |
| 修订后的 LaTeX 需要可视标记 | 输出干净 PDF 与 `latexdiff.pdf`，同时把 Word 原始修订证据保存在账本中 |
| PDF 图片不方便直接放入 Word | 在派生 overlay 中把 PDF 页规范化为 PNG，原 PDF 和原 LaTeX 保持不变 |
| 只考虑 Windows | 收敛平台、CI、文档和发布承诺到 Windows 与 Python 3.12/3.13 |
| 最终放到 GitHub 供别人参考 | 清理个人路径和私人语料，补齐许可、CI、发行检查、Plugin、Skill 和公开 E0 样例 |

这些修订说明了 Vibe Coding 在本项目中的实际作用：需求不是预先写成一份完整规格后一次
实现，而是在可运行结果、失败证据和维护者反馈之间逐步收敛。

## 人与 AI 怎样分工

### 维护者负责

- 提出真实使用场景和最终目标；
- 决定 LaTeX 必须保持权威、原稿不得覆盖；
- 明确逐条审批、第二次应用确认和 Windows-only 范围；
- 根据实际 Word 使用体验修正 PDF 图片和修订标记需求；
- 决定哪些结果可以公开，以及何时发布到自己的 GitHub；
- 对最终合并、tag、Release 和后续维护负责。

### OpenAI Codex 和协作 Agent 参与

- 搜索和比较上游项目、许可证、维护状态与能力差距；
- 提出领域对象、安全边界和命令行工作流；
- 编写和修改 Python 库、CLI、Schema、测试、GitHub Actions、Skill、Plugin 与文档；
- 运行单元、契约、端到端、真实 Word 和 TeX 工具门禁；
- 对失败路径、隐私、供应链和 bundle 篡改面做对抗性检查；
- 根据测试、代码审查和维护者反馈继续修正。

### 不能从这段分工推导出的结论

- 项目没有因为使用 Codex 就自动获得正确性或安全性；
- 当前不声称经过独立第三方安全审计；
- 当前不声称每一行代码都经过维护者逐行人工重写或复核；
- beta 候选不等于已发布的稳定产品；
- 真实 Word 合同和公开 E0 通过，不代表任意 LaTeX 模板都能无损处理。

## 为什么没有自己重写所有功能

“由 Vibe Coding 产生”不等于“所有代码都从零生成”。项目先查找成熟上游，再把自研范围
限制到本项目特有的审阅安全问题。

| 能力 | 处理方式 |
|---|---|
| LaTeX 解析、OMML、DOCX 基础生成 | 采用并包装 `tex2word==1.0.5` |
| 对照转换 | Pandoc 已接入为可选 baseline 后端；修订读取只做过契约实验，未接入生产 ingest |
| 基础 Word 插入、删除研究 | `docx-revisions` 用于上游契约实验和设计参考，未作为生产依赖 |
| PDF 页渲染 | 采用固定版本 pypdfium2/Pillow |
| OOXML 规范和比较设计 | 参考 Open XML SDK、PowerTools、Docxodus、safe-docx 等项目 |
| LaTeX provenance、reject-view 基线、审批、补丁和审计链 | 由本项目实现，因为上游没有提供这一组合合同 |

具体许可证、版本、实测结果和取舍可查看：

- [ADR-0001：转换与修订解析上游策略](adr/0001-upstream-strategy.md)
- [成熟化阶段上游复用决策](reviews/maturity-upstream-reuse-2026-07.md)
- [上游依赖矩阵](compat/upstream-dependency-matrix.md)
- [GitHub 生态调研](GitHub生态调研.md)

## 质量怎样判断

不要把“AI 写了很多代码”当作质量证据，也不要把 README 中的描述当作实现证明。建议按
下面的顺序判断：

1. 查看目标 commit 的 [GitHub Actions](https://github.com/JIE-jiee/latex-word-review/actions)。
2. 在干净 Windows 环境运行公开 E0 教程。
3. 阅读 `changes.patch`、sealed JSON 和 verification receipt，而不是只看最终 PDF。
4. 检查真实 Word 合同记录、MiKTeX 门禁和供应链审查。
5. 用自制或脱敏的复杂样例做压力测试。
6. 在真实论文上只操作副本，并保留人工复核。

仓库当前公开的证据包括：

- lint、format、mypy 和 pytest；
- Windows Python 3.12/3.13 锁定环境；
- 完全自制的 E0 往返样例；
- Microsoft Word COM 修订合同；
- 固定 MiKTeX 包闭包的真实编译和 latexdiff；
- wheel/sdist clean-install；
- bundle 篡改、路径、隐私和对象绑定测试；
- Codex Skill 与 Plugin 内嵌 Skill 的字节一致性检查。

具体结果应绑定 commit，不在本页写成永久不变的数字。项目尚无正式 tag、GitHub Release 或
PyPI 发布，这一状态不应被文档中的版本字符串掩盖。

## 安全和隐私边界

Vibe Coding 过程中使用过私人论文做本地压力测试，但公共仓库不得包含这些文件。公开测试
语料必须自制、脱敏或有明确再分发许可。`samples/private/` 默认被 Git 忽略。

返回 Word、审批账本和 audit bundle 可能包含：

- 作者或审稿人姓名；
- 修改时间；
- 原文、修订文字和批注；
- 论文中尚未公开的内容；
- 本机路径或工具日志。

因此，工具的隐私扫描只是最后一道窄检查，不能替代分享前的人工审阅。真实论文应使用
`local_private` 分类，不要把返回稿或账本上传到公开 Issue。

核心 CLI 的文件处理在本机执行。可选 Codex Skill 会让所用 Agent 按其产品和组织数据政策
接触被纳入上下文的命令输出或审阅证据；`local_private` 只是一项产物分类，不提供加密、
网络隔离或访问控制。敏感材料应先按适用政策判断能否使用 AI 编排，必要时只运行本地 CLI。

## 对 AI 辅助贡献的要求

本项目接受 AI 辅助贡献，但责任仍由提交者承担。贡献者应：

- 理解自己提交的代码和文档；
- 在 Pull Request 中披露对设计或实现有重要影响的 AI 辅助；
- 说明测试范围、未测试平台和已知风险；
- 不提交模型生成但自己无法解释的安全关键改动；
- 不把私人论文、提示词中的个人信息、凭据或受版权保护材料带入仓库；
- 为用户可见变更提供可复现证据。

维护者可以拒绝无法解释、没有验证或模糊责任归属的 AI 生成变更。是否使用 AI 不决定变更
能否合并，证据、可维护性和安全边界才决定。

## 为什么公开这件事

公开 Vibe Coding 来源有三个目的：

- 让使用者知道代码和文档是怎样形成的；
- 避免把 AI 参与隐藏在传统开发叙事后面；
- 给其他研究者提供一个可审查的例子，观察自然语言需求怎样变成库、CLI、Skill、测试和
  GitHub 工程。

这个项目既可以作为工具使用，也可以作为一份工程案例阅读。可供参考的内容包括最终代码、
需求如何缩小、哪些上游被复用、哪些路径被明确拒绝，以及安全声明如何落到可运行门禁上。
