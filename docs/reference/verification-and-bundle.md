# 编译核验、审阅账本与审计包

状态：`v0.1` 已实现的生产库与 CLI。

`latex_word_review.latex_verify`、`ledger` 与 `bundle` 是 v1alpha 闭环的只读核验和
交付层。它们不会把 Word 整篇反向生成 LaTeX，也不会修改权威 source、applied
source 或 returned DOCX。带标记的 `latexdiff` 仅是派生审阅产物，`revised-clean/`
才是已批准补丁对应的干净工作副本。

## 完整处理链

```text
SourceManifest + ChangeSet + final ApprovalSet + PatchPlan
  -> verify_latex_project
       -> revised-clean/          # manifest allowlist 的已应用源文件副本
       -> revised-clean.pdf       # 干净修订版编译结果
       -> latexdiff.tex/.pdf      # 原稿与修订稿的派生可视化差异
       -> actual.diff             # 实际源差异
       -> VerificationReport      # 编译、对账与不可变性证据
  -> build_ledger                 # canonical JSON + 自包含 escaped HTML
  -> create_audit_bundle          # 显式 allowlist 的确定性 ZIP
  -> verify_audit_bundle          # 离线复核路径、哈希、隐私与对象绑定
```

审批、计划、应用和核验是独立闸门。`accepted` 只表示用户意图；只有 PatchPlan 中
经过安全检查的 operation 才可进入 applied tree，核验器随后再次证明实际字节差异
恰好等于这些 operation。

## CLI 入口

完整交付层已经连接到安装后的 `latex-word-review` 命令：

```console
latex-word-review verify ORIGINAL REVISED RETURNED_ORIGINAL SOURCE_MANIFEST \
  CHANGESET FINAL_APPROVAL PLAN/patch-plan.json VERIFICATION_OUTPUT
latex-word-review ledger CHANGESET FINAL_APPROVAL PLAN/patch-plan.json \
  VERIFICATION_OUTPUT/verification-report.json LEDGER_OUTPUT
latex-word-review run-manifest SOURCE_MANIFEST RUN_MANIFEST \
  --object VERIFICATION_OUTPUT/verification-report.json --phase verified --status completed
latex-word-review bundle BUNDLE_SOURCE_ROOT audit.zip RUN_MANIFEST \
  VERIFICATION_OUTPUT/verification-report.json --classification local_private \
  --item RELATIVE_PATH ROLE SOURCE_OBJECT.json
latex-word-review verify-bundle audit.zip \
  --run-manifest RUN_MANIFEST \
  --verification VERIFICATION_OUTPUT/verification-report.json
```

以上是参数形状示例；每个命令的精确可选项以 `latex-word-review COMMAND --help` 为准。
所有成功摘要均为 stdout 上的 canonical JSON；稳定失败类别使用公共退出码。命令不会
覆盖既有输出，`verify`/`ledger`/`bundle` 均要求显式输入与新目标。

## 编译与 latexdiff API

```python
from pathlib import Path

from latex_word_review.latex_verify import verify_latex_project

result = verify_latex_project(
    Path("snapshot"),
    Path("applied"),
    Path("ingest/returned-original.docx"),
    source_manifest,
    changeset,
    final_approval_set,
    patch_plan,
    Path("verification-r1"),
)
```

调用成功后，`result.report` 是通过公共 Schema 的 `VerificationReport`，
`result.revised_source_tree_sha256` 是实际 applied source tree 哈希。输出目录必须是
尚不存在且与两个输入树完全分离的新路径；它在同文件系统临时目录内完整生成、复核
并清除私有工作区后，才用一次 rename 原子发布。不会覆盖旧输出。

### 授权与字节对账

运行任何外部工具前，核验器会 fail closed 复核：

- 四个对象的 envelope Schema、语义和 payload/document 双层哈希；
- SourceManifest、ChangeSet、final ApprovalSet 与 PatchPlan 的 run/source/change/
  approval 哈希链；
- ApprovalSet 对 ChangeSet 的逐项覆盖和 `change_fingerprint`；
- PatchPlan operation 恰好对应 accepted/accepted-with-edit 项，且 target、unit、
  before/replacement、exact resolution 与 plain-text safety 仍一致；
- 原始 source 的每个 manifest 文件和 source-tree 哈希；
- returned DOCX 的字节数与 SHA-256 等于 ChangeSet 的不可变原件引用；
- 按降序 byte span 重放 operations 后的期望树逐文件等于 applied tree；
- 重新生成的 deterministic unified diff 的长度和 SHA-256 等于 PatchPlan diff。

任何绑定替换、源漂移、遗漏、额外文件、重复/相邻冲突 span 或未批准修改都会在
启动编译器前拒绝，并且不发布输出。命令完成后还会重新读取 original source、applied
source 与 returned DOCX；任何并发变化返回 `E_VERIFY_ORIGINAL_MUTATED` 或
`E_PATCH_SOURCE_DRIFT`。

`VerificationReport` v1alpha 的固定 payload 没有独立的 ChangeSet、ApprovalSet 和
applied-tree 字段。本实现不修改 Schema，而是在合法的 namespaced extension
`org.latex-word-review.verification` 中绑定：

```text
changeset_payload_sha256
approval_set_payload_sha256
patch_plan_payload_sha256
source_manifest_payload_sha256
original_source_tree_sha256
applied_source_tree_sha256
policy_sha256                  # PatchPlan 安全策略哈希
verification_policy_sha256     # 编译/timeout/输出限制策略哈希
commands[]
```

账本会再次复核这些值，而不是把 extension 当作未验证展示数据。

### 外部命令边界

所有命令只在 `_work/` 中的显式副本上运行。returned DOCX 与四个 contract 也先复制
到该私有工作区，但不会传给 TeX 工具或进入最终输出。调用约束为：

- Python list argv；复用项目的 `run_command`，从不经 shell；
- 固定 `latexmk` 参数包含 `-no-shell-escape`、nonstop、halt-on-error 和独立 outdir；
- 源中的 `write18`、pipe input 和 minted shell-escape 依赖在启动前拒绝；
- latexdiff 生成结果为空或重新引入上述危险构造时，不会交给编译器；
- `cwd` 固定为工作副本，main document 使用 `./relative-path`，避免选项注入；
- 环境只透传运行所需的少量系统变量，强制 UTF-8，并设置受限
  `openin_any/openout_any/shell_escape`；
- 单命令 timeout 最大 60 秒，stdout/stderr 分别有上限，超限即终止；
- 日志只保存状态、退出码、timeout/truncation、输出哈希及清洗后的文本，Windows、
  POSIX 绝对路径都替换为占位符。

状态含义：

| 结果 | 含义 |
|---|---|
| `pass` | 实际 diff 对账通过，干净版与 latexdiff 均成功编译，引用检查无失败 |
| `fail` | 输入授权仍有效，但编译、timeout、输出上限、PDF 缺失或引用检查失败 |
| `blocked` | 必需外部工具缺失，或策略明确阻止继续；不伪报通过 |

`VerificationPolicy` 可降低 timeout/输出/文件上限，或把 latexdiff 设为非必需；不能
把 timeout 提高到 60 秒以上，也不能启用 shell escape。工具缺失是报告中的显式
`blocked`，而输入篡改、路径越界和写入失败抛出稳定 `ContractError` 并回滚 staging。

## 审阅账本

```python
from latex_word_review.ledger import build_ledger

ledger = build_ledger(changeset, approval_set, patch_plan, verification_report)
ledger.json_bytes   # RFC 8785 canonical JSON + LF
ledger.html_bytes   # 单文件 UTF-8 HTML
```

账本是四个 sealed 对象的确定性只读投影。构造前会复核 run/source/change/approval/
plan/verification 哈希、final decision 的逐项指纹、PatchPlan planned/blocked/excluded
互斥全集以及 VerificationReport reconciliation。每条记录包含：

- change kind、author/authors、timestamp/timestamps；
- 完整审批决定、决定时间、理由和最终文本；
- `applied_verified`、`accepted_blocked`、`verification_failed` 等最终状态；
- operation ID、blocked code 或 exclusion reason；
- before/after/comment；
- raw event ID、part URI、fragment hash、source location、unit 与 resolution 摘要。

HTML 不加载脚本、字体、图片或网络资源，并设置禁止外联的 CSP。作者、时间、理由、
内容、证据及所有其他动态值均先进行 HTML attribute/text escaping；即使原始 Word
元数据包含 `<script>`、事件属性或引号，也只显示为文本。HTML 和 JSON 都不是授权
来源，授权仍以 sealed ApprovalSet/PatchPlan 为准。

## AuditBundle API

Bundle 只接受调用方逐项给出的 allowlist，不会递归打包整个 run 目录：

```python
from latex_word_review.bundle import BundleItem, create_audit_bundle, verify_audit_bundle

result = create_audit_bundle(
    "verification-r1",
    "release/audit-r1.zip",
    allowlist=[
        BundleItem("verification-report.json", "verification_report", verification_report),
        BundleItem("ledger.json", "review_ledger_json", verification_report),
        BundleItem("ledger.html", "review_ledger_html", verification_report),
        BundleItem("actual.diff", "actual_diff", verification_report),
    ],
    run_manifest=run_manifest,
    verification_report=verification_report,
    content_classification="local_private",
    generated_at="2026-07-16T16:00:00+09:00",
)

verified = verify_audit_bundle(
    result.path,
    expected_run_manifest=run_manifest,
    expected_verification_report=verification_report,
)
```

每个 `BundleItem` 含 portable relative path、role 和一个已验证 domain object，后者
作为 `BundleEntry.source_object` 的完整 payload/document 哈希来源。allowlist 路径
拒绝绝对路径、盘符、UNC、反斜杠、空组件、`..`、保留 metadata 名、重复项、symlink
与 junction；目标必须为源树外尚不存在的显式 `.zip` 文件。

归档固定按路径字典序写入，时间为 `1980-01-01 00:00:00`，Unix regular-file mode
固定为 `0644`，清空 extra/comment，并使用固定 DEFLATE level。内部只增加：

```text
manifest/audit-bundle.json
manifest/privacy-scan.json
```

AuditBundle 的 `entries` 逐项绑定 path/role/size/SHA-256/source object，
`manifest_sha256` 等于 canonical entries 哈希；对象同时绑定 RunManifest 和
VerificationReport payload。创建时先验证临时 ZIP，再以 no-clobber hard link 原子
发布，发布后再次验证，并复核 allowlist 源文件未变化。失败只清理自己创建的临时名和
同 inode 的未完成目标。

离线验证拒绝重复 member、目录项、加密项、非预期压缩、ZIP bomb 比率、过量文件/
字节、symlink mode、非固定 metadata、member 集合漂移、entry/隐私报告哈希篡改和
AuditBundle stable-ID/manifest hash 失效。

### 隐私分类

隐私扫描当前识别用户 home/profile 绝对路径、邮箱、private-key header 和 OpenAI
风格 API key；报告只记录 rule ID、bundle 相对路径和 byte offset，不复制命中秘密。

- `public_fixture`：必须零命中，否则 `E_BUNDLE_PRIVATE_RELEASE`，不创建公开包；
- `local_private`：允许生成但 `privacy_scan.status=fail` 会如实保留，不把私人包误标为
  可公开。

扫描是发布闸门而非数据匿名化工具。它不能替代人工许可证、版权和隐私复核。

## 主要拒绝码

| 代码 | 条件 |
|---|---|
| `E_HASH_INTEGRITY_MISMATCH` | 任一 sealed envelope 的 payload/document 哈希失效 |
| `E_HASH_SOURCE_MISMATCH` | source/returned/bundle 输入哈希漂移 |
| `E_HASH_APPROVAL_MISMATCH` | final ApprovalSet 或逐项指纹绑定失效 |
| `E_HASH_PATCHPLAN_MISMATCH` | plan、operation 或 verification 上游绑定失效 |
| `E_PATCH_UNAPPROVED_CHANGE` | operation 不对应唯一批准项或批准文本 |
| `E_VERIFY_DIFF_MISMATCH` | applied tree 或 actual diff 不等于 PatchPlan |
| `E_VERIFY_COMPILE_FAILED` | 源显式依赖 shell escape 等禁止能力 |
| `E_VERIFY_ORIGINAL_MUTATED` | original source 或 returned DOCX 在核验期间变化 |
| `E_APPLY_PARTIAL_WRITE` | staging/原子发布失败且已回滚 |
| `E_BUNDLE_HASH_MISMATCH` | ZIP metadata/member/entry/manifest/object 绑定失效 |
| `E_BUNDLE_PRIVATE_RELEASE` | public fixture 隐私扫描未通过 |
| `E_PATH_TRAVERSAL` / `E_PATH_LINK_ESCAPE` | allowlist、输出或链接越界 |

定向测试使用合成 source、contract、PDF bytes 和子进程结果，不读取私人论文。真实
latexmk/latexdiff 的安装与版本兼容属于 doctor/support matrix；测试不会通过 shell
执行任意字符串，也不会在 authoritative inputs 内创建辅助文件。
