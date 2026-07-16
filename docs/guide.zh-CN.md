# Windows 完整使用指南

本指南面向第一次使用 LaTeX Word Review 的论文作者。它覆盖从安装、导出 Word、接收导师
修订、逐条审批、预览补丁，到生成新的 LaTeX 副本和审计交付物的完整流程。

当前维护范围仅限 Windows 与 Python 3.12/3.13。命令示例使用 PowerShell。项目仍是 beta
候选，建议先跑公开样例，再处理论文副本。

## 先理解四条规则

1. 原 LaTeX 项目永远不改。`workflow init` 会创建只读 snapshot，后续只从 snapshot 工作。
2. 返回的 Word 原件永远不改。工具先归档，再从归档副本读取修订。
3. 审批不会写 LaTeX。只有 `apply` 可以创建新的 LaTeX 工作副本。
4. 输出目录都遵守 no-clobber。目标已存在时命令会停止，不会覆盖旧证据。

真实运行的主链如下：

```text
原 LaTeX
  -> snapshot
  -> export/review.docx
  -> 导师在 Word 中修订
  -> receive/original/returned-original.docx
  -> receive/changeset.json
  -> approvals/approval-rN.json
  -> plans/plan-r1/changes.patch
  -> revised-clean/
  -> verification/ + ledger/ + audit.zip
```

## 1. 准备环境

### 必需项

- Windows；
- Python 3.12 或 3.13；
- Git；
- [uv](https://docs.astral.sh/uv/)；
- 真实审阅时，审阅者使用 Microsoft Word for Windows。

Microsoft Word 不必安装在运行 CLI 的同一台电脑上，但返回文件必须保留原生 Track Changes
证据。LibreOffice 可以用于额外查看，不是本项目的审阅合同应用。

### 完整 PDF 交付的可选项

如果还要生成 `revised-clean.pdf` 和 `latexdiff.pdf`，本机需要：

- `latexmk`；
- 论文对应的 TeX 引擎，例如 XeLaTeX；
- `latexdiff`；
- 论文实际使用的 LaTeX 包和字体。

CLI 不会替你静默安装这些外部程序。缺少工具时，核心审阅和回填仍可完成，完整验证阶段会
明确报告 blocked 或 degraded。

## 2. 安装 CLI

当前没有正式 GitHub Release 或 PyPI 包。推荐从公开仓库取得源码，并固定到你审核过的
commit：

```powershell
git clone https://github.com/JIE-jiee/latex-word-review.git
Set-Location latex-word-review
$ReviewedCommit = "PASTE_THE_REVIEWED_40_CHARACTER_COMMIT_SHA_HERE"
git checkout --detach $ReviewedCommit
if ((git rev-parse HEAD).Trim() -ne $ReviewedCommit) { throw "Commit verification failed" }
uv sync --frozen --group fixture --extra pdf-figures --python 3.12
$VenvScripts = (Resolve-Path .\.venv\Scripts).Path
$env:PATH = "$VenvScripts;$env:PATH"
$Lwr = (Resolve-Path "$VenvScripts\latex-word-review.exe").Path
& $Lwr --version
& $Lwr doctor
```

把 `$ReviewedCommit` 替换为你在 GitHub 上审核过的完整 40 位 commit SHA。之后复现实验或
报告问题时，这个 commit 比“我用的是 main”更有用。

`$Lwr` 保存了 CLI 的绝对路径，后面即使切换到运行目录也可以继续使用。如果你已从 wheel
安装，可以改用：

```powershell
$Lwr = "latex-word-review"
```

### 怎样读 doctor 结果

`doctor` 只做诊断，不改文件。至少确认：

- Python 版本属于 3.12/3.13；
- 默认 `tex2word` 后端可导入；
- 使用 PDF 图片时，pypdfium2/Pillow 可用；
- 需要完整 PDF 交付时，`latexmk`、TeX 引擎和 `latexdiff` 可发现。

可选工具缺失不代表核心流程完全不可用。先判断你需要的是“Word 审阅与安全回填”，还是还
需要“编译、latexdiff 和审计包”。

## 3. 先跑一次公开样例

公开 E0 样例不读取私人论文，也不需要 Microsoft Word。它会动态生成一条确定性 Word 修订，
然后实际调用 CLI 完成 snapshot、export、ingest、approve、plan 和 apply。

快速核心闭环：

```powershell
Set-Location C:\path\to\latex-word-review
uv run --frozen python scripts/run_public_e0_cli_demo.py --skip-verification
```

若本机已安装 TeX 工具，运行完整闭环：

```powershell
uv run --frozen python scripts/run_public_e0_cli_demo.py
```

默认输出位于新的 `build/public-e0-cli-demo/<run-id>/`。检查：

- `demo-summary.json`；
- `plan/changes.patch`；
- `revised-clean/`；
- 完整模式下的 `verification/`、`ledger/` 和 `audit.zip`。

看到 `full_pass` 才表示真实编译、latexdiff、账本和审计包都完成。`core_pass` 或
`core_pass_degraded` 只表示核心审阅链成功。公开样例的实现细节见
[Public E0 CLI tutorial](tutorial-public-e0.md)。

## 4. 准备真实 LaTeX 项目

运行前做一次人工检查：

- 找到主文件，例如 `main.tex`；
- 确认项目当前能在自己的常规环境编译；
- 把审阅轮次目录放在源项目之外；
- 不要把审阅轮次目录放进云盘的自动冲突合并区；
- 确认没有把密码、私钥或无关私人文件混进项目树；
- 确认源文件使用严格 UTF-8，主文件和依赖能被保守静态发现器识别；
- 若图片使用 PDF，安装 `pdf-figures` extra；
- 若项目依赖运行脚本、shell escape 或动态生成资源，先预期它们可能转人工或在验证阶段被阻止。

示例约定：

```text
C:\research\paper\           # 原 LaTeX 项目，只读来源
C:\review-runs\paper-r1\    # 本轮运行目录，开始前必须不存在
C:\received\reviewed.docx   # 导师返回文件，放在运行目录之外
```

这些只是示例路径。公共配置和输出不会写入你的个人绝对路径。

## 5. 创建不可变运行

在任意目录执行：

```powershell
& $Lwr workflow init C:\research\paper C:\review-runs\paper-r1 `
  --main main.tex --confidentiality local_private
Set-Location C:\review-runs\paper-r1
& $Lwr workflow status .
```

成功后至少会出现：

```text
paper-r1/
├── snapshot/
└── objects/
    └── source-manifest.json
```

`snapshot/` 是后续转换和应用的权威输入副本。`source-manifest.json` 记录允许的源文件、
相对路径、大小和哈希。不要编辑这两个对象。原项目和 snapshot 任何一处漂移都会使后续绑定
失败。

如果目标运行目录已存在，请换一个新名称，例如 `paper-r2`。不要为了“重跑”覆盖旧轮次。

## 6. 导出 Word 审阅稿

```powershell
& $Lwr workflow export . --backend tex2word --confidentiality local_private
& $Lwr workflow status .
```

主要产物：

```text
export/review.docx
export/objects/backend-capabilities.json
export/objects/review-ir.json
export/objects/source-map.json
export/objects/export-report.json
```

`review.docx` 会包含稳定 bookmark，并在设置中启用 Track Changes。SourceMap 保存 Word 审阅
单元与 LaTeX UTF-8 字节范围的关系。

### PDF 图片会发生什么

启用 `pdf-figures` 后，工具只在派生 overlay 中把静态可解释的 PDF 页面渲染为 PNG。原 PDF
和原 `.tex` 不变。页码、裁剪、旋转、尺寸、源哈希和 PNG 哈希会进入导出证据。`pagebox`
当前不在自动物化合同内，遇到时转人工。

Word 中看到的是栅格化预览，所以适合审阅，不适合从 Word 取回矢量图。SVG、EPS、动态路径
或不能安全解释的图片操作不会被偷偷执行，会明确转人工。

### 发给导师时附上的说明

可以把下面这段一起发送：

> 请使用 Microsoft Word 打开附件，并保持“审阅 > 修订”开启。正文修改请直接使用修订，
> 讨论性内容请使用批注。请不要“接受所有修订”、删除书签、另存为旧 `.doc` 格式或通过会
> 扁平化修订的编辑器处理。完成后请返回 `.docx` 文件。

不要编辑或替换本地的 `export/review.docx`。它是返回稿 reject-view 对账所需的不可变基线。

## 7. 接收并归档返回稿

把收到的文件放在运行目录之外，然后执行：

```powershell
& $Lwr workflow receive . C:\received\reviewed.docx `
  --confidentiality local_private
& $Lwr workflow status .
```

该命令按顺序完成：

1. 把收到的字节归档到 `receive/original/returned-original.docx`；
2. 生成归档清单并绑定 SHA-256；
3. 计算返回稿的 reject-changes 语义视图；
4. 与本轮 `export/review.docx` 基线对账；
5. 读取插入、删除、替换、move、格式和批注证据；
6. 写出 `receive/changeset.json`。

成功后不要再打开并保存归档原件。需要查看时，请复制一份到运行目录之外。

### receive 为什么可能停止

常见原因：

| 现象 | 含义 | 正确处理 |
|---|---|---|
| reject-view 与基线不同 | 可能关闭过 Track Changes，或接受了修订 | 向审阅者索取保留原始修订证据的文件，或把本轮转人工 |
| bookmark 缺失、重复或损坏 | Word 到 LaTeX 的锚点不再可信 | 不要手改 JSON，重新取得未损坏的返回稿 |
| 返回稿来自另一轮导出 | run、基线或 SourceMap 不匹配 | 使用对应轮次的 run，不能混用 |
| 不支持或不完整的 OOXML 结构 | 证据无法安全解释 | 保留诊断并转人工复核 |

工具无法可靠区分“接受所有修订”和“关闭修订后普通编辑”，所以不会猜作者、时间或补丁。
这项基线证明只覆盖 `verified_for_text_patch` 所需的可见文字、结构、字段、bookmark 和修订
语义。格式、OMML、图片、超链接目标、content control、custom XML 和嵌入对象仍需人工
完整性复核，即使正文基线通过也不能省略。

## 8. 在本机逐条审批

先创建第一版 ApprovalSet：

```powershell
& $Lwr approve init receive\changeset.json approvals\approval-r1.json `
  --actor-id paper-author --actor-name "Paper Author"
```

启动本地审批页：

```powershell
& $Lwr approve serve receive\changeset.json `
  approvals\approval-r1.json approvals --open-browser
```

页面只绑定随机本地端口的 `127.0.0.1`，不会监听局域网或公网。命令窗口保持运行时，浏览器
才能继续提交决定。用完后回到 PowerShell 按 `Ctrl+C` 关闭服务。

### 每一项要看什么

页面会显示：

- change ID 和类型；
- Word 记录的作者和时间；
- before 和 after；
- SourceMap 定位、置信度和安全分类；
- raw event IDs、fingerprint 和诊断；
- 当前决定。

不要只看 after。至少同时检查 before、上下文、源位置、作者、风险和是否涉及公式或引用。

### 五种决定

| 页面按钮 | 何时使用 | 是否保证自动应用 |
|---|---|---:|
| Accept | 完全采用返回文字 | 否 |
| Accept edited | 同意修改方向，但要填写自己的最终文字 | 否 |
| Reject | 保留原 LaTeX 文字 | 不应用 |
| Manual | 证据有效，但需要在 LaTeX 副本中人工处理 | 不应用 |
| Conflict | 证据、语义或上下文存在冲突，当前不能决策 | 不应用 |

`accepted` 只表示你的意图。只有 exact、置信度至少 0.99、纯正文且通过结构字符和 Unicode
边界检查的项目，才可能进入 PatchPlan。接受公式或格式修改不会提高自动补丁权限。

每次有效操作都会生成新的 `approval-r2.json`、`approval-r3.json` 等文件。旧版不覆盖。
所有项目决定后，点击 **Finalize approval**。页面进入只读状态。记住最终文件名，以下示例
写作 `approvals\approval-rN.json`。

若浏览器返回 409，通常是页面过期或另一标签页先提交了决定。刷新页面，检查最新 revision，
不要重复覆盖。

## 9. 生成并检查 dry-run 补丁

```powershell
& $Lwr plan snapshot objects\source-manifest.json receive\changeset.json `
  approvals\approval-rN.json plans\plan-r1 --confidentiality local_private
```

`plan` 不写 `.tex`。检查：

```text
plans/plan-r1/patch-plan.json
plans/plan-r1/changes.patch
```

重点核对：

- 每个 operation 是否对应你批准的 change；
- 文件路径和 UTF-8 byte span 是否正确；
- before 与源文件当前文字是否一致；
- replacement 是否是你最终决定的文字；
- 是否出现 `accepted_but_blocked`；任何一项都会使计划状态为 `blocked`；
- diff 是否包含任何未批准文件或额外改动。

可以用文本编辑器查看 `changes.patch`，也可以在 PowerShell 中运行：

```powershell
Get-Content -LiteralPath plans\plan-r1\changes.patch -Encoding utf8
```

如果结果不对，或计划状态为 `blocked`，不要执行 `apply`。回到最新 ApprovalSet，把相关项目
改为 `manual` 或 `rejected`，再生成新的 approval revision、重新 finalize，并把计划写到另一个
新目录，例如 `plans\plan-r2`。只有 `ready` 或 `noop` 计划可以进入下一步。

final ApprovalSet 在浏览器中是只读的。若最终文件是 `approval-r8.json`，可先用 CLI 生成一版
新的 draft，再重新打开审批页：

```powershell
& $Lwr approve set receive\changeset.json `
  approvals\approval-r8.json approvals\approval-r9.json `
  --change-id "chg_replace_with_actual_id" --decision manual `
  --reason "当前类型不允许安全自动回填"
& $Lwr approve serve receive\changeset.json `
  approvals\approval-r9.json approvals --open-browser
```

完成并 finalize 后，用新的最终 ApprovalSet 创建 `plans\plan-r2`。旧审批和旧计划保留为审计
证据，不要覆盖。

## 10. 跨过第二道闸门

只有在你明确确认当前 PatchPlan 后，才执行：

```powershell
& $Lwr apply snapshot plans\plan-r1 receive\changeset.json `
  approvals\approval-rN.json revised-clean
```

`apply` 会重新读取 source、ChangeSet、ApprovalSet 和 PatchPlan，复算哈希、字节范围、重叠和
补丁内容。成功后发布新的 `revised-clean/`。它不会修改：

- 原 LaTeX 项目；
- `snapshot/`；
- `receive/original/returned-original.docx`；
- 已存在的 ApprovalSet 或 PatchPlan。

此时不要在 `revised-clean/` 中加入 manual、conflict 或其他人工修改。下一步核验要求它的
实际字节差异严格等于 PatchPlan；任何额外修改都会触发 `E_VERIFY_DIFF_MISMATCH`。先完成自动
应用树的验证，再从它复制新的人工工作副本。

## 11. 编译并核验实际差异

这一阶段需要 `latexmk`、相应 TeX 引擎和 `latexdiff`。输出目录 `verification` 必须尚不存在：

```powershell
& $Lwr verify snapshot revised-clean `
  receive\original\returned-original.docx `
  objects\source-manifest.json receive\changeset.json `
  approvals\approval-rN.json plans\plan-r1\patch-plan.json verification
```

核验器会在启动外部工具前证明实际 LaTeX 字节差异与 PatchPlan 一致，再编译干净版本和
带标记版本。主要产物：

```text
verification/verification-report.json
verification/actual.diff
verification/revised-clean.pdf
verification/latexdiff.tex
verification/latexdiff.pdf
```

请同时检查干净 PDF 和带标记 PDF。`latexdiff` 只负责可视化原稿与修订稿差异，不会伪造
Word 的作者、时间和修订来源。Word 证据仍以 ChangeSet 和账本为准。

验证成功后，如果还要处理 manual、conflict 或 accepted-but-blocked 项，另建目录：

```powershell
if (Test-Path -LiteralPath revised-with-manual) { throw "revised-with-manual already exists" }
Copy-Item -LiteralPath revised-clean -Destination revised-with-manual -Recurse -ErrorAction Stop
```

只在 `revised-with-manual/` 中进行人工修改，并单独编译、复核和披露。当前
VerificationReport、PatchPlan 和自动审阅账本不会授权或证明这些额外字节；不要把人工副本
替换进已生成的 verification 或 audit bundle 后重新命名为自动验证结果。

## 12. 生成审阅账本

```powershell
& $Lwr ledger receive\changeset.json approvals\approval-rN.json `
  plans\plan-r1\patch-plan.json `
  verification\verification-report.json ledger
```

得到：

```text
ledger/ledger.json
ledger/ledger.html
```

`ledger.html` 是便于人阅读的自包含页面，`ledger.json` 适合机器复核。两者都绑定同一轮
ChangeSet、最终 ApprovalSet、PatchPlan 和 VerificationReport。

## 13. 创建并离线验证审计包

审计包只收集明确列出的文件。先建立全新的 delivery 目录并复制准备交付的产物：

```powershell
if (Test-Path -LiteralPath delivery) { throw "delivery already exists; choose a new directory" }
New-Item -ItemType Directory -Path delivery -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath verification\verification-report.json -Destination delivery -ErrorAction Stop
Copy-Item -LiteralPath verification\actual.diff -Destination delivery -ErrorAction Stop
Copy-Item -LiteralPath verification\latexdiff.tex -Destination delivery -ErrorAction Stop
Copy-Item -LiteralPath verification\latexdiff.pdf -Destination delivery -ErrorAction Stop
Copy-Item -LiteralPath verification\revised-clean.pdf -Destination delivery -ErrorAction Stop
Copy-Item -LiteralPath ledger\ledger.json -Destination delivery -ErrorAction Stop
Copy-Item -LiteralPath ledger\ledger.html -Destination delivery -ErrorAction Stop
```

生成 RunManifest。以下 `local_private` 表示内容包含私人论文或审稿信息，不能当作公开 fixture：

```powershell
& $Lwr run-manifest objects\source-manifest.json delivery\run-manifest.json `
  --object export\objects\review-ir.json `
  --object export\objects\source-map.json `
  --object export\objects\export-report.json `
  --object receive\changeset.json `
  --object approvals\approval-rN.json `
  --object plans\plan-r1\patch-plan.json `
  --object verification\verification-report.json `
  --backend export\objects\backend-capabilities.json `
  --backend receive\revision-reader.json `
  --phase verified --status completed `
  --artifact ledger.json review_ledger_json application/json local_private `
  --artifact ledger.html review_ledger_html text/html local_private
```

创建 allowlist ZIP：

```powershell
& $Lwr bundle delivery audit.zip `
  delivery\run-manifest.json verification\verification-report.json `
  --item run-manifest.json run_manifest delivery\run-manifest.json `
  --item verification-report.json verification_report verification\verification-report.json `
  --item actual.diff actual_diff verification\verification-report.json `
  --item latexdiff.tex latexdiff_tex verification\verification-report.json `
  --item latexdiff.pdf latexdiff_pdf verification\verification-report.json `
  --item revised-clean.pdf revised_clean_pdf verification\verification-report.json `
  --item ledger.json review_ledger_json delivery\run-manifest.json `
  --item ledger.html review_ledger_html delivery\run-manifest.json `
  --classification local_private
```

最后离线复核：

```powershell
& $Lwr verify-bundle audit.zip `
  --run-manifest delivery\run-manifest.json `
  --verification verification\verification-report.json
```

审计包不会自动把整个运行目录塞进 ZIP。未列入 `--item` 的私人中间文件不会进入包。即便使用
`local_private`，分享前仍需人工检查版权、隐私、作者姓名、批注和路径信息。

## 14. 暂停、恢复和安全清理

在 receive 之前，可以用：

```powershell
& $Lwr workflow status .
```

它只识别 `snapshotted`、`exported` 和 `ingested`。审批开始后，它不会发现新的 ApprovalSet、
PatchPlan、revised tree 或 verification。恢复时应找到每条命令输出的最新 sealed JSON 和 receipt，
必要时运行：

```powershell
& $Lwr validate approvals\approval-rN.json --schema ApprovalSet
& $Lwr validate plans\plan-r1\patch-plan.json --schema PatchPlan
& $Lwr <command> --help
```

检查工具拥有的废弃暂存目录：

```powershell
& $Lwr workflow clean .
```

默认只是预览。确认列表后才执行：

```powershell
& $Lwr workflow clean . --execute
```

它只能清理由工具严格命名的暂存目录，不能删除 snapshot、导出、返回原件、ChangeSet、审批、
计划、revised tree 或审计交付物。

## 15. 常见问题

### Word 中的修订会出现在重新生成的 LaTeX 里吗

经过批准且安全应用的文字会出现在 `revised-clean/`。LaTeX 源文件本身不会带 Word 那种修订
气泡。审阅标记由 `verification/latexdiff.tex` 和 `latexdiff.pdf` 展示，作者、时间和批注等
Word 原始证据保存在 ChangeSet 与 ledger 中。

### 能不能把导师的所有修订一次接受

审批库有受限的 `accept_all_safe` 能力，但只允许 exact plain-text candidate。Skill 和默认
工作流不会把“处理返回稿”理解为接受全部。公式、结构、move、格式、批注和冲突项仍需逐条
或人工处理，`apply` 仍是独立确认。

### 为什么已经 Accept，计划里却显示 blocked

Accept 表示用户同意内容，不表示工具有权自动写入。定位不精确、涉及 LaTeX 结构、包含危险
字符、破坏 grapheme 边界或属于非纯正文类型时，planner 会保留接受决定但阻止自动补丁。

### 能否直接把 PDF 图放进 Word

Word 对 LaTeX 常用 PDF 图的直接审阅体验不稳定。本项目在派生副本中把指定 PDF 页光栅化为
PNG，再嵌入 Word。原 PDF 仍留在 LaTeX 项目中，Word 只拿到审阅预览。

### 可以在 Linux 或 macOS 上用吗

纯 Python 包可能碰巧能安装，但不属于支持、CI、发行和故障排查范围。本项目只维护 Windows。

### 能否修改 sealed JSON 后继续

不能。Schema、payload hash、document hash、run ID 和跨对象引用会在后续阶段重新验证。需要
改变决定时应生成新的 ApprovalSet revision，而不是编辑旧 JSON。

### 命令失败后能否复用同一个输出目录

安全关键输出坚持 no-clobber。先保留失败目录用于诊断，再换新的 revision 或目录。不要覆盖
可能已经包含部分证据的旧路径。

## 16. 报告问题时带什么

公开 issue 中不要上传私人论文、返回 Word、审稿人信息或未脱敏账本。建议提供：

- 项目 commit；
- `latex-word-review --version`；
- `doctor` 的脱敏结果；
- 失败命令的 stderr JSON、退出码和稳定 `error.code`；
- 用自制最小 fixture 复现的步骤；
- 是否安装 Word、MiKTeX、latexmk 和 latexdiff；
- 预期行为与实际行为。

非敏感缺陷可提交到 [GitHub Issues](https://github.com/JIE-jiee/latex-word-review/issues)。安全问题
请使用[私密漏洞报告](https://github.com/JIE-jiee/latex-word-review/security/advisories/new)。

更精确的参数、退出码和对象约束见 [CLI 与运行目录契约](reference/cli.md)、
[审批状态机](reference/approval.md)、[计划与安全应用](reference/plan-and-apply.md) 和
[编译核验、账本与审计包](reference/verification-and-bundle.md)。
