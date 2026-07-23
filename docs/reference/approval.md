# ApprovalSet 审批状态机

`latex_word_review.approval` 是审批闸门的 v1alpha 库实现。它只把用户意图记录为不可变、可验证的 `ApprovalSet` revision；不读取或修改 `.tex`，不生成补丁，不触发 plan/apply。

## 公开处理链

```text
sealed ChangeSet
  -> create_approval_set          # revision 1, draft, 全部 pending
  -> record_decision              # 逐条决定或撤回为 pending
  -> record_bulk_decision         # 受限安全批量操作
  -> finalize_approval_set        # 全部已决定后 final
  -> write_approval_json          # 显式新路径，原子发布
```

最小用法：

```python
from latex_word_review.approval import (
    create_approval_set,
    finalize_approval_set,
    record_decision,
    write_approval_json,
)

approval = create_approval_set(
    changeset,
    decided_by={"id": "paper-author", "display_name": "Paper Author"},
)
approval = record_decision(
    changeset,
    approval,
    change_id="chg_...",
    decision="accepted_with_edit",
    final_text="final wording",
    decision_source="local_ui",
)
# 其余 change 也必须逐条决定
approval = finalize_approval_set(changeset, approval)
write_approval_json(changeset, approval, "review/approval-r3.json")
```

调用方每次都同时传入 ChangeSet 和当前 ApprovalSet。这样写盘前仍会复核完整授权绑定，而不是只相信一个脱离账本的审批 JSON。

## 状态与 revision

每个 change 的用户状态为：

```text
pending
  -> accepted
  -> accepted_with_edit
  -> rejected
  -> manual
  -> conflict
```

`pending` 用 `undecided_change_ids` 表示，不伪造一条 Decision。其他五种状态对应 `decisions` 中的唯一记录。`accepted_with_edit` 必须有非 `null` 的 `final_text`；其他状态的 `final_text` 必须为 `null`。

每次有效状态或决定内容变化都生成新 revision，并同时填写：

- `supersedes_payload_sha256 = 前一版 ApprovalSet.payload_sha256`；
- `audit.previous_payload_sha256 = 同一前序哈希`；
- 由新 payload 身份前像派生的 `apr_...` stable ID。

重试完全相同的决定意图是幂等的：返回原 ApprovalSet，不增加 revision 或批量审计记录。比较包括 decision、`final_text`、reason、risk acknowledgement 和 decision source，不把重试传入的新 `decided_at` 当成新意图。

`final` 要求 ChangeSet 的每个 change 都已决定，否则以 `E_APPROVAL_NOT_FINAL` 拒绝。对 final 对象修改决定不会原地改写；它会生成一个 superseding `draft` revision，需重新 finalize。

## 强绑定

创建、转移、finalize 和写盘均 fail closed 执行：

- 完整验证 ChangeSet 与 ApprovalSet Schema、语义约束和 envelope 双层哈希；
- `ApprovalSet.changeset_sha256` 精确等于当前 sealed `ChangeSet.payload_sha256`；
- run ID 和 `source_manifest_sha256` 一致；
- 每条 Decision 的 `change_fingerprint` 精确等于被绑定 ChangeSet 中的对应值；
- decided 与 pending IDs 不重复、不遗漏，且严格按 ChangeSet 顺序分割；
- revision 1 无前序，后续 revision 的两个前序哈希完全一致；
- `approval_set_id` 与排除自身 ID 后的 payload 重算一致。

因此，仅对篡改后 ApprovalSet 重新 seal 不能绕过逐项指纹绑定；替换或重新 seal ChangeSet 也会使旧 ApprovalSet 失效。

## 批量安全策略

`record_bulk_decision` 只支持 Schema 定义的三种操作：

- `accept_all_safe` → `accepted`；
- `reject_selected` → `rejected`；
- `mark_manual` → `manual`。

三种批量操作都只处理尚未决定的项目，绝不覆盖既有决定。传入显式 `change_ids` 时，函数会先拒绝重复或未知 ID，再从中排除已有决定；主动改判仍只能逐项调用 `record_decision`。如果过滤后没有可处理 ID，则幂等返回原 ApprovalSet，不增加 revision 或审计记录。

`accept_all_safe` 未传 ID 时只选择尚未决定的 exact `plain_text_candidate`；`reject_selected` 必须显式给出 ID，且实际处理的尚未决定项也必须满足同一 exact 普通正文条件。move、format、comment、unknown、未匹配或冲突项不能经这两种操作接受或拒绝，否则以 `E_PATCH_UNSAFE_KIND` 拒绝。

`mark_manual` 是单向降权操作：未传 ID 时只选择尚未决定且不满足 exact 普通正文条件的项目；显式给出 ID 时可以把任何尚未决定项目标为 manual。它不会接受文字、提升补丁权限或写入 LaTeX。

每次非幂等批量操作都进入 `audit.bulk_operations`，但只记录本次实际处理的 change ID 和带时区时间；已决定而被跳过的 ID 不进入审计集合。

## 写盘边界

`write_approval_json(changeset, approval, output_path)` 要求调用方给出一个显式、父目录已存在、后缀为 `.json` 的新版本路径。它：

1. 在写入前再次复核 ChangeSet/ApprovalSet 全部绑定；
2. 在目标同目录写入并 `fsync` 一个临时文件；
3. 用同文件系统的原子 no-clobber hard-link 发布；
4. 清理临时名。

目标已存在、为链接、不是 `.json`、父目录不存在或原子发布失败时均拒绝；永不覆盖旧 revision。该 API 不接受源目录或 `.tex` 路径，也不会自动推导输出文件名。

## 主要拒绝码

| 代码 | 条件 |
|---|---|
| `E_HASH_CHANGESET_MISMATCH` | ChangeSet payload/run 绑定或逐项指纹不匹配 |
| `E_HASH_APPROVAL_MISMATCH` | ApprovalSet 完整性、revision chain 或 stable ID 失效 |
| `E_HASH_SOURCE_MISMATCH` | source manifest 绑定不匹配 |
| `E_APPROVAL_CHANGE_UNKNOWN` | 决定引用未知/遗漏 change |
| `E_APPROVAL_FINAL_TEXT_REQUIRED` | `accepted_with_edit` 的 `final_text` 为 `null` |
| `E_APPROVAL_NOT_FINAL` | 存在 pending change 却请求 final |
| `E_PATCH_UNSAFE_KIND` | 批量接受或拒绝的实际目标不是 exact plain-text candidate |

输出必须通过包内 `ApprovalSet` JSON Schema 和 C2 语义校验；未知决定、decision source 或批量操作 fail closed，不做 best effort 映射。
