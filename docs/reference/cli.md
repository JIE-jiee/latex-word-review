# CLI 与运行目录契约

`latex-word-review` 是 0.2.x 的权威机器编排入口。普通 Windows 用户应优先运行
`latex-word-review app`（冻结版对应 `LatexWordReview.exe`），在本机中文四步应用中完成
主流程；CLI 主要用于自动化、排障和精细恢复。Python API、应用和 Codex Skill 复用同一套
库逻辑；Skill 不另行实现转换、修订解析或补丁。

## 稳定边界

- 命令名、位置参数顺序、JSON Schema、`ErrorCode` 和 `ExitCode` 在 0.2.x 内按
  公开契约管理。
- 成功执行的业务命令在 stdout 输出单行 UTF-8 canonical JSON 摘要；运行期契约错误
  以单行 JSON 写入 stderr。`--help`、`--version`、无参数调用和 argparse 在进入业务
  逻辑前发现的语法错误是面向人的纯文本例外。JSON 摘要不记录工作目录的绝对路径。
- 安全关键输出坚持 no-clobber：输出已存在时不覆盖；只有部分明确定义的
  完全相同请求可幂等复用。
- 0.2.x 不读取隐式的用户级配置文件，也不从项目文本执行配置。后端、超时、
  隐私分类和外部工具均通过命令行明示给出，并被 sealed 对象或策略哈希绑定。
- 相对路径存入契约；绝对本机路径只用于当前调用，不进入可分享对象。

## 命令面

| 命令 | 主要产物 | 是否修改 LaTeX |
|---|---|---:|
| `app` | 本机中文四步应用与可恢复会话；第二道确认前只生成计划 | **是，仅确认后写入新目录** |
| `new-run` | UUIDv7 `run_id` | 否 |
| `doctor` | 路径脱敏的工具/包诊断 | 否 |
| `snapshot` | 只读快照、`SourceManifest` | 否 |
| `export` | 带 bookmark 的 DOCX、capabilities、IR、SourceMap、报告 | 否 |
| `inspect` | 只读 DOCX/OOXML 结构统计 | 否 |
| `reader-capabilities` | 修订读取器 `BackendCapabilities` | 否 |
| `archive` | 返回 Word 原件和归档清单 | 否 |
| `ingest` | 完整 `ChangeSet` | 否 |
| `workflow init/export/receive/status/clean` | 固定目录的安全编排、状态与暂存清理 | 否 |
| `approve init/set/finalize` | 逐版不可变 `ApprovalSet` | 否 |
| `approve serve` | 仅绑定 `127.0.0.1` 的逐条审批页 | 否 |
| `plan` | dry-run `PatchPlan` 和 exact unified diff | 否 |
| `apply` | 全新的已修订工作副本 | **是，仅新目录** |
| `verify` | `revised-clean/`、两份 PDF、`latexdiff.tex`、报告 | 否 |
| `ledger` | canonical JSON 与自包含 HTML 账本 | 否 |
| `run-manifest` | 运行对象/后端/产物清单 | 否 |
| `bundle` / `verify-bundle` | 显式 allowlist 审计 ZIP / 离线验证结果 | 否 |
| `validate` | 单个 sealed JSON 的 Schema/哈希验证 | 否 |

每个子命令的完整参数以 `latex-word-review <command> --help` 为准。

`run-manifest --artifact PATH ROLE MEDIA_TYPE CONFIDENTIALITY` 用于登记 ledger 等在领域对象
之后生成的交付文件，可重复给出。`PATH` 必须相对于 RunManifest 输出目录，读取时执行
portable-path、root containment、symlink/junction、普通文件、64 MiB 上限和稳定哈希检查；
重复路径或读取期漂移均 fail closed。生成的 immutable `ArtifactRef` 进入 sealed
RunManifest `payload.artifacts`，供 AuditBundle selector 离线复核。

Pandoc 适配器目前是显式选择的 baseline/degraded 后端，而不是与固定
`tex2word==1.0.6` 同等级的持续验证默认值；缺少 Pandoc 或 pandoc-crossref 时必须由
`doctor` 和能力对象报告，不能静默降级。

## 首选 CLI 高层工作流

需要脚本化但不需要逐个底层命令时，优先运行 `workflow init`、`workflow export`、
`workflow receive` 与 `workflow status`。普通交互使用仍优先选择 `app`。这些命令只减少
路径和对象传递的重复工作，不绕过任何底层合同：

- `init` 原子创建新 run root、只读 snapshot 与 sealed `SourceManifest`；
- `export` 只使用 snapshot，保存启用 Track Changes 的不可变 Word baseline；
- `receive` 先归档返回原件，再强制比较 baseline 与返回稿 reject view 后生成 ChangeSet；
- `status` 只读重验高层 workflow 已完成的 snapshot/export/receive 阶段及跨对象绑定；它只
  返回 `snapshotted`、`exported` 或 `ingested`，不会发现后续 ApprovalSet、PatchPlan 或
  revised tree。进入 granular 阶段后，以最新 sealed 对象和显式输出路径为准；
- `clean` 默认 dry-run，只允许删除严格命名的工具暂存目录，执行删除必须显式
  `--execute`。

高层命令永不创建审批结论，也永不调用 `plan` 或 `apply`。完整可复制流程见
[Windows Quick Start](../quick-start-windows.md)。

## 固定运行目录

```text
run-root/
├── snapshot/                    # 只读 LaTeX 快照
├── objects/
│   └── source-manifest.json
├── export/
│   ├── review.docx
│   ├── review.docx.image-overlay/  # 需要时生成的 PDF 页 PNG 派生树
│   └── objects/                 # capabilities/IR/SourceMap/report
├── receive/
│   ├── original/
│   │   ├── returned-original.docx
│   │   └── returned-original.manifest.json
│   ├── revision-reader.json
│   └── changeset.json
├── .lwr-staging/                # 仅工具拥有的临时 stage
├── approvals/                   # approval-r1.json, approval-r2.json, ...
├── plans/                       # patch-plan.json + changes.patch
├── revised-clean/               # apply 唯一允许创建的 LaTeX 副本
├── verification/                # clean source/PDF, latexdiff, report, logs
├── ledger/                      # ledger.json + ledger.html
├── delivery/                    # 显式复制、准备进入审计包的 allowlist 根
└── audit.zip
```

`workflow` 管理到 `receive/` 为止；之后的审批、计划、应用、验证和打包仍使用同一批
granular 命令。若不用高层命令，目录名可以改，但同一步的输入树、输出树和权威源必须
彼此分离；返回 Word 仍必须先经 `archive`，`ingest` 只读归档副本。

## 两道人工闸门

1. `approve ...` 或 `approve serve ...` 只记录每一项的 accepted/rejected/manual/
   conflict 意图，生成 final `ApprovalSet`；它无权读写 `.tex`。
2. `plan` 重新验证 source/change/approval 并生成 dry-run diff。用户审阅后另行执行
   `apply`；`apply` 再次重算计划，且只能发布到全新目录。

`accepted` 不等于可自动应用。公式、引用、标签、命令、环境、格式修订、move、
comment 和不精确定位在 0.2.x 中即使被接受，也会进入 `accepted_but_blocked`。

## 进程退出码

| 码 | 类别 |
|---:|---|
| 0 | 成功，或显式警告但无需阻断 |
| 2 | 用法或 Schema |
| 3 | 外部工具/环境 |
| 4 | 不可信输入、路径或哈希 |
| 5 | 后端/导出 |
| 6 | 修订导入 |
| 7 | 审批/计划 |
| 8 | 应用 |
| 9 | 编译验证/审计包 |
| 10 | 未预期的内部不变量 |

机器分支应读取 stderr JSON 的 `error.code`，而不应解析可以改进的人类消息。

## 从新 clone 验证最小闭环

不需要私人论文或 Word 安装的确定性闭环：

```console
uv sync --frozen --group fixture --extra pdf-figures --python 3.12
uv run --frozen pytest -q tests/test_e2e_public_roundtrip.py -k bounded-command-double
```

若本机同时安装 `latexmk` 和 `latexdiff`，可把编译替身换成真实工具：

```console
uv run --frozen pytest -q tests/test_e2e_public_roundtrip.py -k installed-latexmk-latexdiff
```

该测试实际完成快照、tex2word 导出、带修订 DOCX、不可变归档、ChangeSet、
逐项批准、PatchPlan、新工作副本、编译/latexdiff、账本与可离线验证审计包，
并断言原 LaTeX 与收到的 Word 零字节变化。
