# Microsoft Word 保存往返合同工具

`scripts/run_word_contract_qa.ps1` 是一个仅面向 Windows 桌面版 Microsoft Word 的本地合同测试工具。它不把 Office COM 当成生产依赖，也不在 CI 或服务器上静默启动 Word；它只用于维护者在公开、自制 DOCX 上取得真实 Word 保存证据。

## 它生成什么

工具从调用方显式给出的同一份公开/合成 baseline 创建五份独立工作副本，并让 Word 以 DOCX 格式 `SaveAs2`：

| case | 目的 |
| --- | --- |
| `roundtrip_unchanged` | 不改正文，仅经过一次真实 Word 打开与另存 |
| `tracked_review` | 开启 `TrackRevisions`，生成局部插入、删除、替换和一条批注 |
| `negative_untracked_drift` | 关闭 `TrackRevisions` 后改动正文，作为未跟踪漂移负例 |
| `negative_accepted_all` | 先生成修订，再接受全部修订，作为“修订证据已消失”负例 |
| `negative_broken_bookmark` | 删除一个 bookmark，作为锚点损坏负例 |

输出目录还包含 UTF-8（无 BOM）的 `manifest.json`。清单只记录 schema/status、baseline SHA-256、Word 版本，以及各 case 的相对文件名、SHA-256、状态和结构计数；不写用户名、绝对路径或文档正文。

## 运行

前提：Windows PowerShell 5.1 或 PowerShell 7、已安装并完成首次启动配置的 Microsoft Word 桌面版。baseline 必须是公开、自制或明确获准再分发的 `.docx`，必须至少含一个 bookmark 和一段不少于 12 个普通字符的正文，且不得已有修订。

```powershell
if (-not (Test-Path -LiteralPath .\build)) {
    New-Item -ItemType Directory -Path .\build | Out-Null
}

.\scripts\run_word_contract_qa.ps1 `
    -BaselineDocx .\tests\fixtures\e0-minimal-paper\base\review-base.docx `
    -OutputDir .\build\word-contract-qa
```

`-OutputDir` 的父目录必须已经存在，而且目标目录本身必须不存在。工具不会覆盖既有输出。若任一 case 失败，脚本返回非零，关闭所有已打开文档、退出 Word，并删除本次未完成的同级 stage；baseline 在运行前后还会再次核对 SHA-256。

普通 pytest 只运行静态安全合同和“既有输出不得覆盖”测试，不会启动 Word。显式开启真实 COM 测试：

```powershell
$env:LWR_RUN_WORD_COM_QA = "1"
uv run --frozen pytest tests/test_word_contract_harness.py -q
Remove-Item Env:\LWR_RUN_WORD_COM_QA
```

若没有设置该环境变量、不是 Windows，或 Word 没有注册，真实测试会以明确原因 skip；不会伪报通过。

## 安全边界与限制

- Word 永远只打开先复制到本次私有 stage 的工作副本；不会通过 COM 打开 baseline 原件。
- 所有 case 使用不同副本与不同目标文件，Word 警报关闭，VBA 自动化安全在打开任何文档前强制设为禁用。
- 所有文档都在 `finally` 中以“不保存额外改动”关闭；Word 进程也在 `finally` 中退出并释放 COM RCW。
- Word 自己可能把本机 Office 身份写入生成的 DOCX 元数据或批注作者。清单不会复制这些字段，但 DOCX 仍是本地 QA 证据，发布前必须另做隐私扫描；工具不会自动提交这些输出。
- Microsoft Office 桌面自动化依赖交互式用户配置、桌面会话和 Office 自身状态，不适合作为无人值守 CI、Windows 服务或服务器端转换器。本项目的自动化 CI 应继续依赖 OOXML 静态合同；本工具仅补充维护者的真实 Word 证据。
- 脚本不接受私人论文作为默认样本，也不会遍历用户目录寻找 DOCX。输入和输出范围完全由两个显式参数决定。
