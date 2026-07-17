# 文档索引

这里仅收录需要随公开源码长期维护的使用说明、接口契约、设计决策和可复现验证记录。本地
任务日志、私人论文、构建目录、缓存和一次性输出不属于 GitHub 文档。

## 开始使用

- [Windows 完整使用指南](guide.zh-CN.md)：从安装、Word 审阅、逐条审批到 PDF、账本和审计包。
- [English Windows Quick Start](quick-start-windows.md)：较短的英文命令路径。
- [公开 E0 教程](tutorial-public-e0.md)：用自制 fixture 重跑核心或完整闭环。
- [开发来源与 Vibe Coding 记录](development-provenance.md)：维护者与 AI 的分工、责任和验证边界。

## 工作流与公共接口

- [CLI 与运行目录](reference/cli.md)
- [项目发现、快照与环境诊断](reference/snapshot-and-doctor.md)
- [导出后端与 DOCX 结构验收](reference/export-and-inspection.md)
- [返回原件归档、修订读取与 ChangeSet](reference/revision-ingest.md)
- [ApprovalSet 审批状态机](reference/approval.md)
- [本地审批浏览器](reference/review-server.md)
- [补丁计划与原子应用](reference/plan-and-apply.md)
- [编译核验、账本与审计包](reference/verification-and-bundle.md)
- [公共契约](reference/contracts.md)
- [Python API 稳定性边界](reference/python-api.md)

## 架构、范围与上游决策

- [v1alpha 领域契约](architecture/domain-contracts.md)
- [v0.1 支持与自动应用范围](compat/v0.1-scope.md)
- [Windows 平台和工具支持矩阵](compat/platform-support.md)
- [ADR-0001：上游策略](adr/0001-upstream-strategy.md)
- [ADR-0002：Windows-only](adr/0002-windows-only-support.md)
- [上游依赖矩阵](compat/upstream-dependency-matrix.md)
- [上游契约实验规范](compat/e0-contract-protocol.md)
- [上游契约实验结果](compat/upstream-contract-results.md)
- [成熟化阶段复用审查](reviews/maturity-upstream-reuse-2026-07.md)
- [威胁模型](security/threat-model.md)

## 维护、发布与验证记录

- [发布流程](release/release-process.md)
- [发布检查表](release/release-checklist.md)
- [发布物证据边界](release/artifact-evidence.md)
- [依赖与供应链审计](reviews/dependency-supply-chain-audit.md)
- [公开 fixture clean-room 生成审查](reviews/e0-cleanroom-generator.md)
- [公开 fixture 独立审计](reviews/e0-fixture-audit.md)
- [Microsoft Word 视觉检查](reviews/e0-word-visual-qa.md)
- [Microsoft Word 合同工具](reviews/word-contract-harness.md)
- [私人复杂样本聚合结果](reviews/private-complex-stress.md)
- [Codex Skill 前向测试](reviews/codex-skill-forward-test.md)

版本变化见仓库根目录的 [CHANGELOG](../CHANGELOG.md)。发布前检查以目标 commit 的 GitHub
Actions、可复现命令和人工晋级决定为准，不能仅凭历史记录判断当前状态。
