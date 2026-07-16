# 安全补丁计划与原子应用

状态：P8 最小核心，`v1alpha`。本层只处理经过审批的普通正文局部变更，不提供整篇
Word → LaTeX 回转，也不扩大 ApprovalSet 所记录的权限。

## 两道独立闸门

`plan_patch()` 是纯 dry-run：它读取并复核不可变来源，返回 sealed `PatchPlan` 与确定性
unified diff 字节，但不写任何文件。`apply_patch_plan()` 是单独的显式写入入口；它必须
再次取得同一 sealed ChangeSet、最终 ApprovalSet、PatchPlan 和 diff，重新生成计划并
逐字节比较，随后才可创建全新的工作副本。

```python
planned = plan_patch(
    snapshot_root,
    source_tree_sha256=source_tree_sha256,
    changeset=changeset,
    approval=final_approval,
    generated_at="2026-07-16T15:00:00+09:00",
)

applied = apply_patch_plan(
    snapshot_root,
    revised_root,
    patch_plan=planned.document,
    unified_diff=planned.unified_diff,
    changeset=changeset,
    approval=final_approval,
)
```

## v0.1 自动应用白名单

一项 accepted/accepted_with_edit 变更只有同时满足以下条件才会生成 PatchOperation：

- kind 为 insertion、deletion 或 replacement；
- `safety_class=plain_text_candidate`；
- 定位状态为 exact，候选不歧义，置信度至少 `0.99`；
- unit、相对路径、UTF-8 字节 span 均存在；
- 当前源树、目标文件和目标 slice 哈希与 ChangeSet 完全一致；
- `before` 与当前 slice 的 UTF-8 文本逐字相同；
- replacement 不含反斜杠、`{}$%&#_^~`、CR 或 LF，且不会形成空操作；
- 同一计划的 byte span 不重叠；零长度插入点重复或落在另一范围边界也视为冲突。

被 rejected、manual 或 conflict 的变更进入 `excluded_changes`。已接受但不满足白名单的
变更进入 `accepted_but_blocked`，并把整个 PatchPlan 状态设为 `blocked`；安全项不会因
同一计划中存在不安全项而被静默应用。重叠直接返回 `E_PATCH_OVERLAP`，来源/切片漂移
直接拒绝，而不是生成猜测性补丁。

每个 operation 内容派生稳定 ID，并绑定目标文件 SHA、目标 slice SHA、before、
replacement、unit slice、上下文指纹和单 hunk SHA。PatchPlan 同时绑定 source tree、
ChangeSet payload、ApprovalSet payload 与固定策略哈希。计划使用公共 C2 Schema 密封并
校验；相同输入和相同 `generated_at` 得到完全相同的 plan 与 diff。

## 应用与回滚

Applier 首先验证 PatchPlan integrity、内容 ID、策略、diff artifact 及审批链，然后从
当前来源重新运行 planner。只有重算结果与提交的 plan/diff 完全相等时才继续。

写入流程为：

1. 记录来源全部普通文件的相对路径、原始字节与树指纹，并拒绝 symlink、junction 和
   特殊文件；
2. 在目标父目录创建同文件系统临时目录并复制来源；
3. 仅在临时副本中把同一文件的操作按 byte span 降序应用；
4. 复核目标文件/切片哈希、实际 unified diff、完整输出文件集及逐文件字节；
5. 写入 canonical `.latex-word-review-applied.json` marker 并 `fsync` 文件；
6. 再次复核来源不变后，以 rename 原子发布到一个未使用的目标目录；
7. 发布后再次核对输出和来源。

任何异常都会清理本次调用拥有的临时目录；若异常发生在发布后的最终复核，则清理本次
刚发布的目标。权威来源始终不是写入目标。目标已经存在时不覆盖：只有 marker 与重新
计算的 plan、diff、文件集和全部字节完全一致才返回 `reused=true`，否则
fail-closed。这既防止重复 insertion，也防止仅凭伪造 marker 跳过核验。

## 错误与边界

- `E_APPROVAL_NOT_FINAL`：审批未最终覆盖全部 ChangeSet；
- `E_HASH_CHANGESET_MISMATCH` / `E_HASH_APPROVAL_MISMATCH`：sealed 对象或绑定失效；
- `E_PATCH_UNSAFE_KIND` / `E_MAP_*`：accepted 项不满足自动应用白名单；
- `E_PATCH_ACCEPTED_BUT_BLOCKED`：试图应用 blocked plan；
- `E_PATCH_OVERLAP`：byte span 冲突；
- `E_PATCH_SOURCE_DRIFT`：计划后来源、文件或 slice 改变；
- `E_HASH_PATCHPLAN_MISMATCH`：plan、diff、marker 或实际输出无法重现；
- `E_APPLY_PARTIAL_WRITE`：临时写入、清理或原子发布失败。

本阶段不处理公式、命令、环境、标签、引用、资源路径、表格、格式修订、move、comment
或 unknown，不生成 `latexdiff`，也不编译验证。它只提供后续 verify 阶段可以审计的最小
局部补丁和原子工作副本。测试语料全部为合成文本，覆盖 accepted/rejected/blocked、
tamper、漂移、重叠、UTF-8 byte span、Schema、重复 apply 和发布失败回滚。
