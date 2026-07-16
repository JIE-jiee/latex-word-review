# E0 上游契约实验结果

## 结论

E0 已完成上游选型，可以进入 F1。证据来自完全合成的公开 fixture、人工独立预言机、只读 OOXML observer 和 `build/upstream-eval/command-log.jsonl`。机器可读汇总位于 `build/upstream-eval/results.json`。

- 主转换后端：采用并包装 `tex2word 1.0.5`；
- 对照后端：包装 `Pandoc 3.9`，固定工作目录为 LaTeX 源根目录；
- 修订权威读取：本项目实现最小、安全、只读的 canonical OOXML evidence reader；
- 交叉核验：Pandoc `--track-changes=all` 与 `docx-revisions 0.1.5`；
- `pandoc-crossref 0.3.24` 只作为 Pandoc 后端的可选组件，不能单独解决 Word 活字段；
- 不采用任何整篇 Word→LaTeX 结果覆盖原稿的路径。

## 环境与输入

在 Windows AMD64 上分别实测 Python 3.12.13 和 3.14.4。`tex2word` 上游声明
`Python >=3.12`，分类器只列 3.12/3.13。封版审计后，本项目发布元数据明确采用
`>=3.12,<3.14`：3.14 的窄契约实验只保留为历史观察，不构成本项目完整闭环支持。

公开 fixture：`tests/fixtures/e0-minimal-paper/`。返回 Word 的 SHA-256 为 `d1d71a408f817daf33c34a26a23d624bab04d2b02b4e2085a6a9cd44283a44c5`，独立预言机包含：2 个插入、2 个删除、1 对移动、1 个格式修订和 2 条批注。所有解析前后原件哈希不变。

## 转换契约

| 候选 | 公开 fixture 实测 | 关键差距 | 判定 |
|---|---|---|---|
| tex2word 1.0.5 | 34 段、5 个 OMML、1 表、1 图、9 书签、11 个 `REF/SEQ` 字段；报告为 5/5 OMML | manifest 有 IR/labels，但没有 source span 或稳定 `unit_id`；生成时间导致字节非确定，规范化语义稳定 | 主后端，adopt + wrap |
| Pandoc 3.9 | 正确 cwd 下为 32 段、4 个 math、1 表、1 图、6 书签、0 活字段 | 仓库根目录运行会找不到 3 个 `\input` 并生成不完整 DOCX；公式标签和 Word 活字段不足 | 独立基线，wrap |
| pandoc-crossref 0.3.24 | 与 Pandoc 3.9 精确匹配；公开 fixture 结构计数仍为 32/4/1/1/6/0 | 对原始 LaTeX 输入不能自行补成活 `REF/SEQ` 字段 | 可选，依赖预处理 |

Pandoc 的适配器必须把 cwd 固定为主 `.tex` 所在源根目录；`--resource-path` 不能替代 `\input` 的正确工作目录。

`tex2word` 在 3.12 与 3.14 上产生相同的规范化 DOCX。原始 ZIP 不同只因 `word/tex2word/manifest.json` 的 `generated` 时间。首次导入在两版 Python 均出现无功能影响的 invalid escape `\d` `SyntaxWarning`，应提交上游修复。

## 修订解析契约

| 路径 | 保留能力 | 丢失或改写 | 判定 |
|---|---|---|---|
| 原始 OOXML observer | 完整发现 2 ins、2 del、moveFrom/moveTo、rPrChange、2 comments、ID/作者/带时区时间及批注锚点 | E0 observer 尚未重建格式修订所在 run 的完整 before/after | canonical 生产路径基础 |
| Pandoc `--track-changes=all` | 4 个普通文本修订、作者/时间；移动作为额外 insertion/deletion；批注 ID、作者、时间及 start/end 位置 | 丢失修订 ID；移动语义和配对 ID丢失；格式修订元数据丢失；事件边界空格被规范化 | 强交叉核验，不能独立入账 |
| docx-revisions 0.1.5 | 4 个直接 `w:ins/w:del` 的文本、尾随空格、ID、作者、时间完全正确；3.12/3.14 结果一致 | 0 move、0 格式修订、0 批注 | 窄范围 helper/oracle，不能独立入账 |

因此，生产 `ChangeSet` 必须从只读 OOXML 证据层构建；Pandoc 和 docx-revisions 用于差异检测与回归交叉核验，不能成为唯一来源。

## Round-trip 安全结论

对带修订的 tex2word DOCX，`to-latex` reconcile 会生成接受后全文，并丢失删除内容、作者、时间及逐项审批证据；`--no-reconcile` 只返回 manifest 基线。该实测直接证明：两者都不能替代 `ChangeSet → ApprovalSet → PatchPlan`，更不能覆盖权威 LaTeX 原稿。

## 许可证与维护边界

- tex2word 1.0.5：MIT，早期 Beta、维护者集中；不 fork，优先提交 source-map/修订事件扩展；
- docx-revisions 0.1.5：MIT，范围明确但版本早期；保持可替换；
- Pandoc 与 pandoc-crossref：GPL-2.0-or-later 外部可执行程序；本项目不把二进制并入 Python wheel；
- pandoc-crossref 官方 0.3.24 构建与 Pandoc 3.9 匹配，版本必须成对锁定和检测。

## 后续清单

1. 向 tex2word 上游提 source span/稳定 review-unit 扩展、无损 revision event API 和 `SyntaxWarning` 修复建议；
2. F1 后端协议必须声明 cwd、OMML、活字段、manifest 和降级能力；
3. R6 实现 canonical OOXML reader，并用两条候选解析路径做差异报警；
4. 当前缺少 LibreOffice/soffice，DOCX 页面 PNG 渲染 QA 仍为阻塞项；结构、包打开、隐私和确定性检查已通过；
5. 合成 fixture 发布前仍须由仓库所有者确认 fixture 许可证。
