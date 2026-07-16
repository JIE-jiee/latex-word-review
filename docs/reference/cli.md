# CLI 与运行目录契约

`latex-word-review` 是 v0.1 的权威编排入口。Python API、本地审批页和 Codex
Skill 复用同一套库逻辑；Skill 不另行实现转换、修订解析或补丁。

## 稳定边界

- 命令名、位置参数顺序、JSON Schema、`ErrorCode` 和 `ExitCode` 在 0.1.x 内按
  公开契约管理。
- 成功执行的业务命令在 stdout 输出单行 UTF-8 canonical JSON 摘要；运行期契约错误
  以单行 JSON 写入 stderr。`--help`、`--version`、无参数调用和 argparse 在进入业务
  逻辑前发现的语法错误是面向人的纯文本例外。JSON 摘要不记录工作目录的绝对路径。
- 安全关键输出坚持 no-clobber：输出已存在时不覆盖；只有部分明确定义的
  完全相同请求可幂等复用。
- v0.1 不读取隐式的用户级配置文件，也不从项目文本执行配置。后端、超时、
  隐私分类和外部工具均通过命令行明示给出，并被 sealed 对象或策略哈希绑定。
- 相对路径存入契约；绝对本机路径只用于当前调用，不进入可分享对象。

## 命令面

| 命令 | 主要产物 | 是否修改 LaTeX |
|---|---|---:|
| `new-run` | UUIDv7 `run_id` | 否 |
| `doctor` | 路径脱敏的工具/包诊断 | 否 |
| `snapshot` | 只读快照、`SourceManifest` | 否 |
| `export` | 带 bookmark 的 DOCX、capabilities、IR、SourceMap、报告 | 否 |
| `inspect` | 只读 DOCX/OOXML 结构统计 | 否 |
| `reader-capabilities` | 修订读取器 `BackendCapabilities` | 否 |
| `archive` | 返回 Word 原件和归档清单 | 否 |
| `ingest` | 完整 `ChangeSet` | 否 |
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

Pandoc 适配器目前是显式选择的 baseline/degraded 后端，而不是与固定
`tex2word==1.0.5` 同等级的持续验证默认值；缺少 Pandoc 或 pandoc-crossref 时必须由
`doctor` 和能力对象报告，不能静默降级。

## 建议运行目录

```text
run-root/
├── snapshot/                    # 只读 LaTeX 快照
├── objects/
│   ├── source-manifest.json
│   ├── reader-capabilities.json
│   ├── changeset.json
│   └── run-manifest.json
├── export/
│   ├── review.docx
│   └── objects/                 # capabilities/IR/SourceMap/report
├── returned/                    # 只读归档副本和 manifest
├── approvals/                   # approval-r1.json, approval-r2.json, ...
├── plan/                        # patch-plan.json + changes.patch
├── revised/                     # apply 唯一允许创建的 LaTeX 副本
├── verification/                # clean source/PDF, latexdiff, report, logs
├── ledger/                      # ledger.json + ledger.html
└── audit.zip
```

目录名可以改，但同一步的输入树、输出树和权威源必须彼此分离。返回的
Word 先经 `archive`，`ingest` 只读归档副本。

## 两道人工闸门

1. `approve ...` 或 `approve serve ...` 只记录每一项的 accepted/rejected/manual/
   conflict 意图，生成 final `ApprovalSet`；它无权读写 `.tex`。
2. `plan` 重新验证 source/change/approval 并生成 dry-run diff。用户审阅后另行执行
   `apply`；`apply` 再次重算计划，且只能发布到全新目录。

`accepted` 不等于可自动应用。公式、引用、标签、命令、环境、格式修订、move、
comment 和不精确定位在 v0.1 中即使被接受，也会进入 `accepted_but_blocked`。

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
uv sync --frozen --group fixture --python 3.12
uv run --frozen pytest -q tests/test_e2e_public_roundtrip.py -k bounded-command-double
```

若本机同时安装 `latexmk` 和 `latexdiff`，可把编译替身换成真实工具：

```console
uv run --frozen pytest -q tests/test_e2e_public_roundtrip.py -k installed-latexmk-latexdiff
```

该测试实际完成快照、tex2word 导出、带修订 DOCX、不可变归档、ChangeSet、
逐项批准、PatchPlan、新工作副本、编译/latexdiff、账本与可离线验证审计包，
并断言原 LaTeX 与收到的 Word 零字节变化。
