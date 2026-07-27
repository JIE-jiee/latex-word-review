<p align="center">
  <strong>Language / 语言 / 言語</strong><br>
  <strong>简体中文</strong> ·
  <a href="README.en.md">English</a> ·
  <a href="README.ja.md">日本語</a>
</p>

# LaTeX Word Review

让只习惯 Word 的导师或合作者审阅论文，也让 LaTeX 作者能逐条确认修改，再安全地写回新的 LaTeX 副本。

LaTeX Word Review 是一个只在 Windows 本机运行的论文审阅助手。它不要求审阅者学习 LaTeX，也不会用修改后的 Word 重建整篇论文。

> [!IMPORTANT]
> **系统要求：仅支持 64 位 Windows 与 64 位 Windows PowerShell 5.1。PowerShell 7（`pwsh`）、32 位 Windows 和 32 位 Windows PowerShell 均不受支持。** 双击启动器会自动调用正确的 Windows PowerShell，无需手动选择命令行。

## 你是否遇到过这种情况

你用 LaTeX 写论文，导师或合作者却只习惯 Microsoft Word。

把论文转成 Word 并不难。真正麻烦的是 Word 返回以后：几十处修订要手工比对，公式、引用和 LaTeX 命令又不能随便覆盖。复制少一处会漏改，复制错一处可能破坏原稿。论文越长，这件事越难核对。

这个项目把 Word 当作审阅工具，而不是新的论文源文件。程序会读取返回 Word 中的修订和批注，列出每一项变化，让你决定采用还是拒绝。只有能够准确定位、风险较低且经过你确认的正文修改，才会写入新的 LaTeX 副本。原稿始终保留。

## 它适合谁

- 用 LaTeX 写论文，但需要和 Word 用户协作的学生、研究人员与作者。
- 希望继续用 Word 的“修订”和“批注”功能审稿的导师、合作者或编辑。
- 不愿把未发表论文上传到在线转换网站，希望在本机完成审阅流程的人。
- 需要知道“改了什么、采用了什么、为什么没有自动写回”的谨慎用户。

## 它能帮你做什么

| 你要做的事 | 程序的处理方式 |
|---|---|
| 把 LaTeX 交给 Word 用户审阅 | 从 `main.tex` 生成便于审阅的 Word 文件 |
| 查看 LaTeX 中已有的批改标记 | 普通纯文本批改会精确显示为统一蓝色；含公式、引用或格式等结构化内容时，干净审阅 Word 仍会生成，展示稿按项提示并尽力呈现 |
| 收回审阅结果 | 读取 Word 的插入、删除、作者、时间和批注信息 |
| 决定哪些修改要采用 | 在本机页面中逐条采用或拒绝，也可批量采用已确认安全的普通正文修改 |
| 防止误改原稿 | 先显示文件级差异，再由你第二次确认；结果只写入新副本 |
| 保留核对依据 | 保存修改记录、审批结果、差异和审计材料 |
| 生成最终核验材料 | 工具齐全时生成新 LaTeX、PDF 和带修订标记的 `latexdiff.pdf` |

## 本次更新

- LaTeX 原稿里已有的 `\added`、`\deleted`、`\replaced` 等批改，现在可以另存为统一蓝色的对照 Word；删除内容使用蓝色删除线。
- 某一处批改含有公式、引用或格式时，不会再只因为它难以完整显示就卡住整篇审阅 Word。程序会明确提示，并在安全范围内尽量生成对照稿。
- 日常操作更简单：统一从根目录双击启动，可以自行选择审阅 Word 的保存位置，也可以删除不再需要的最近任务。

## 四步完成一次审阅

1. **选择论文**：点击“新建审阅”，选择论文的主 `.tex` 文件。
2. **生成并收回 Word**：程序生成干净的审阅 Word。请先“另存可编辑 Word”，把这个精确副本发给审阅者，并在同一轮任务中导入它的返回稿；可选的批改展示 Word 只供对照，不能导入。
3. **逐条审批**：查看每项修改的前后文本、作者、时间和风险，决定采用或拒绝。
4. **预览并生成结果**：检查 LaTeX 文件差异，再次确认后生成新的论文副本和可用的核验材料。

原 LaTeX 和返回的 Word 原件都不会被覆盖。关闭浏览器后，任务仍保存在本机，可以继续处理。

## 原 LaTeX 已经带有批改标记时

程序可以识别 `\added{新文字}`、`\deleted{旧文字}`、`\replaced{新文字}{旧文字}`，并在来源
能够静态确认时兼容 `\add`、`\delete`。

- **普通纯文本批改**会精确展示：新增为统一蓝字，删除为蓝色单删除线，替换为
  “旧文字蓝色单删除线 + 新文字蓝字”。
- **批改参数中含公式、引用、格式或其他结构化内容**时，不再只因为这一项难以精确着色就
  阻断整篇论文。`review.docx` 仍是正式的干净审阅稿；程序会为每个无法完整显示的项目给出提示，
  `existing-changes-display.docx` 只做尽力展示，若无法安全生成则可能不提供。

| 文件 | 作用 |
|---|---|
| `review.docx` 及程序生成的精确可编辑副本 | 干净的审阅稿；只有这个审阅角色的另存副本可以发出并作为返回 Word 导入 |
| `existing-changes-display.docx` | 可选的只读对照稿；普通纯文本批改精确显示为蓝字和蓝色单删除线，含公式或引用的复杂批改会逐项提示并尽量展示。本功能不添加高亮 |

展示稿不是 Word 原生“修订/Track Changes”，不能作为返回 Word 导入，也不参与自动回填。
自动回填的规则也没有放宽：只有返回 Word 中经过逐项批准、能够精确定位的普通正文才可能
写入新的 LaTeX 副本，绝不会用整篇 Word 覆盖 LaTeX。

真正涉及安全或正文完整性的问题仍会停止导出，例如危险或越界路径、无法静态确认的动态宏、
以及干净审阅 Word 对原稿中的图片、公式、表格或引用发生可证明的静默丢失。程序不会把这些
问题降成普通提醒。

<details>
<summary>静态识别与展示验收的技术边界</summary>

规范命令需要受支持的静态 `changes` 宏包声明；已绑定项目树中若发现同名
`changes.sty`、`\input@path` 搜索路径覆盖、直接或动态定义/重定义，程序会在转换前停止。
`\add`、`\delete` 使用更保守的静态声明或直接包装规则；`trackchanges.sty` 阴影、来源冲突
或无法确认时不猜测。这个检查不会运行 TeX，也不证明系统/用户 TEXMF 或 `TEXINPUTS`
最终加载的包文件、版本或哈希。

对纯文本批改，程序会精确核对源文字、蓝字、删除线及删除位置的同段唯一上下文；失败时不会
发布错误的展示稿。对结构化批改，精确纯文本子集仍会核验，无法精确证明的项绑定到源位置并
逐项警告；若整个展示稿不能通过有界验收，只保留已通过完整性检查的干净审阅 Word。空批改
内容（如 `\added{}`、`\deleted{}`、`\replaced{}{}`）无法形成展示证据，会以
`E_SCHEMA_INVALID` 停止。

这项功能不会建立“某个 LaTeX 调用到某个 Word 坐标”的新 SourceMap。扫描范围、别名来源
证明、结构内容限制和完整验收门槛见
[导出与 DOCX 验收说明](docs/reference/export-and-inspection.md)。

</details>

## Windows 上怎样开始

目前公开的是源码双击启动方式，还不是签名安装包。

1. 下载本仓库的 [源码 ZIP](https://github.com/JIE-jiee/latex-word-review/archive/refs/heads/main.zip)。
2. 在资源管理器中选择“全部解压”。不要直接在 ZIP 预览窗口里运行。
3. 双击解压目录中的 **`Start-Latex-Word-Review.cmd`**。
4. 第一次启动时保持联网并等待准备完成。浏览器随后会自动打开本机页面。以后继续双击同一个文件即可启动。

普通用户不需要预先安装 Python、Git 或 uv，也不需要管理员权限。第一次启动会下载并校验程序所需的私有运行环境，因此会比以后启动慢。

> [!IMPORTANT]
> 当前 GitHub 首页提供简体中文、English 和日本語三份说明。点击页面最上方的语言名称，会进入对应的完整 README。应用程序本身目前仍是简体中文界面，切换 README 不会改变应用语言。

[查看完整中文使用指南](docs/guide.zh-CN.md) | [Windows 快速入门](docs/quick-start-windows.md)

## 使用前需要知道

项目当前是 `0.2.0b1` beta 候选，仍可能遇到 LaTeX 模板兼容、Word 排版、复杂修订识别或性能问题。开始前请备份论文，并检查每一次输出。

- **Word 是审阅稿，不是排版成品。** 它优先保留语义和可审阅性，不会复制 LaTeX PDF 的投稿版式。公式、复杂表格、引用和图片需要在交付前检查。
- **它不会把整篇 Word 转回 LaTeX。** 这样做很容易破坏宏、标签、引用和模板。程序只尝试回填已经批准并能准确定位的普通正文。
- **批改展示 Word 只供静态对照。** 普通纯文本批改会精确显示为统一蓝色；含公式、引用或格式等结构时只做逐项警告的尽力展示，必要时可以不生成展示稿，但干净审阅 Word 仍可交付。它不是 Word 原生 Track Changes，绝不能作为返回稿。
- **复杂不等于不安全，静默丢失才会阻断。** 结构化批改的显示困难不会单独卡住整篇论文；危险/越界路径、无法确认的动态宏，以及图片、公式、表格或引用的可证明静默丢失仍会硬性停止。
- **识别到修改，不等于一定能自动写回。** 对复杂 Word，程序可能读出修订，却无法证明它在 LaTeX 中的唯一位置。这些修改会留在记录里供你手工处理，而不会冒险改稿。
- **审阅者需要 Microsoft Word for Windows。** 原生“修订”证据应由 Word 产生。包含 Word 活字段的稿件，在生成端也可能需要 Word 来刷新字段。
- **最终 PDF 需要本机 TeX 工具。** 首次启动不会安装 Microsoft Word、MiKTeX、TeX Live、Pandoc、`latexmk` 或 `latexdiff`。缺少相关工具时，程序仍可完成部分流程，并明确说明哪些结果没有生成。
- **目前没有公开的签名安装包。** Windows 可能对从互联网下载的 `.cmd` 显示安全提示。请只从本仓库下载，不要通过关闭系统安全功能来运行来历不明的副本。

## 你会得到哪些结果

一次完整任务通常包含：

- 供审阅者使用的 Word 文件；
- `existing-changes-display.docx`（仅当检测到已有 LaTeX 批改宏时生成，只供对照）；
- 返回 Word 中提取出的逐条修改记录；
- 你的采用和拒绝决定；
- 修改前后的 LaTeX 文件差异；
- 不覆盖原稿的新 LaTeX 副本；
- 环境允许时生成的干净 PDF 和带修订标记的 `latexdiff.pdf`；
- 便于复查与归档的审计材料。

程序默认把任务放在 `%LOCALAPPDATA%\LatexWordReview`。你也可以在页面中把 Word 另存到自己选择的位置，或删除不再需要的最近任务。删除任务不会删除原论文，也不会删除你另存到其他位置的文件。

## 隐私与安全边界

应用只绑定本机地址 `127.0.0.1`，浏览器页面不是云端网站。程序不会主动上传论文。首次准备运行环境时需要联网下载依赖，论文处理则留在本机。

原 LaTeX、导出的基线和返回 Word 原件都会保持不变。任何自动回填都必须经过逐条审批和最终差异确认，并且只能写入新的工作副本。

请不要在公开 Issue 或 Pull Request 中上传私人论文、导师返回的 Word、审稿人信息或未脱敏的修改记录。

## 这个项目如何产生

项目来自一个真实的 LaTeX 与 Word 协作问题，主要由维护者通过 Vibe Coding 与 OpenAI Codex 协作完成。维护者提出需求、反馈实际使用问题、设定安全边界并决定发布范围；AI 编程 Agent 参与调研、设计、编码、测试和文档整理。

“由 Vibe Coding 产生”是在说明开发过程，不是质量保证。项目仍可能存在未发现的问题，也会在后续版本继续修正。欢迎通过 [Issue](https://github.com/JIE-jiee/latex-word-review/issues) 报告可复现的问题，或通过 [Pull Request](https://github.com/JIE-jiee/latex-word-review/pulls) 改进兼容性、文档、测试和安全性。提交前请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 项目状态

[![CI](https://github.com/JIE-jiee/latex-word-review/actions/workflows/ci.yml/badge.svg)](https://github.com/JIE-jiee/latex-word-review/actions/workflows/ci.yml)
[![Double-click bootstrap](https://github.com/JIE-jiee/latex-word-review/actions/workflows/windows-source-bootstrap.yml/badge.svg)](https://github.com/JIE-jiee/latex-word-review/actions/workflows/windows-source-bootstrap.yml)
[![Windows](https://img.shields.io/badge/platform-Windows-0078D4)](docs/compat/platform-support.md)
[![Python 3.12 | 3.13](https://img.shields.io/badge/python-3.12%20%7C%203.13-3776AB)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

项目只支持 Windows。当前尚未创建 GitHub Release，也未发布到 PyPI。安装器和 portable ZIP 已通过候选构建测试，但因为 Windows 原生依赖的公开再分发材料尚未完成闭环，仓库暂不发布二进制文件。

<details>
<summary>开发者、审计人员和高级用户入口</summary>

### 文档导航

- [文档总览](docs/README.md)
- [完整中文使用指南](docs/guide.zh-CN.md)
- [Windows 快速入门](docs/quick-start-windows.md)
- [CLI 参考](docs/reference/cli.md)
- [核心契约与安全模型](docs/architecture/domain-contracts.md)
- [威胁模型](docs/security/threat-model.md)
- [上游复用与差距评估](docs/reviews/maturity-upstream-reuse-2026-07.md)
- [依赖供应链审计](docs/reviews/dependency-supply-chain-audit.md)
- [Windows 二进制许可证审计](docs/reviews/windows-binary-license-audit-2026-07.md)
- [发布流程与门禁](docs/release/release-process.md)

### 从命令行运行

```powershell
git clone https://github.com/JIE-jiee/latex-word-review.git
Set-Location latex-word-review
uv lock --check
uv sync --frozen --no-default-groups --extra pdf-figures --python 3.12
uv run --no-sync latex-word-review app
```

核心业务逻辑、安全约束和 Schema 位于独立库与 CLI 中，图形界面和 Skill 只负责编排。详细命令、产物契约、测试方式和恢复流程请从上面的文档入口继续阅读。

</details>

## 许可证

项目代码采用 [Apache-2.0](LICENSE) 许可证。第三方组件及测试素材按各自许可证使用，详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
