# Windows 完整使用指南

本指南面向第一次使用 LaTeX Word Review 的论文作者。你可以把 LaTeX 论文变成便于审阅的
Word，交给不熟悉 LaTeX 的合作者；拿回带修订的 Word 后，再逐条决定哪些改动写入新的
LaTeX 副本。

项目只支持 Windows。原 LaTeX 与导师返回的 Word 原件保持不变；自动修改只写入新的副本。

> [!IMPORTANT]
> 当前 `0.2.0b1` 是 beta 源码 ZIP，不是签名安装包。请只从本仓库下载，保留论文备份，并
> 检查每一次生成结果。

## 1. 先理解这套工作流

正常用户只需要四步：

```text
选择 main.tex
  → 生成 review.docx
  → 审阅者在 Word 中使用修订和批注
  → 选择返回的 .docx
  → 逐条审批（第一道闸门）
  → 核对按文件 diff（第二道闸门）
  → 新 LaTeX 副本 + PDF + ledger + audit.zip
```

四条不可变规则贯穿整个过程：

1. 原 LaTeX 项目不修改。程序只从只读快照工作。
2. 返回 Word 原件不修改。程序先只读归档，再解析归档副本。
3. 审批决定不等于写入。第一道闸门只密封意图。
4. 只有第二次确认的精确计划才能写入全新 LaTeX 副本；已有目录不会覆盖。

## 2. 下载源码 ZIP，双击启动

公开双击入口要求 **64 位 Windows** 和 **64 位 Windows PowerShell**。启动器会自动选择
系统自带的 64 位 Windows PowerShell；不支持 32 位 Windows、32 位 PowerShell 或其他系统。

普通用户不需要预先安装 Python、Git 或 uv，也不需要管理员权限。

1. 下载本仓库的 [源码 ZIP](https://github.com/JIE-jiee/latex-word-review/archive/refs/heads/main.zip)。
2. 在资源管理器中选择“全部解压”。不要直接在 ZIP 预览窗口里运行。
3. 打开解压后的文件夹，双击 **`Start-Latex-Word-Review.cmd`**。
4. 第一次启动时保持联网，不要关闭命令窗口。准备完成后，浏览器会自动打开本机页面。
   以后继续双击同一个文件即可启动。

首次启动会在解压目录内下载并校验专用 Python 环境和已锁定的应用依赖，不会安装到系统
目录。若下载或准备中断，请记下窗口里的稳定错误码，检查网络后再次双击。保留整个解压目录，
下次启动就能复用已经准备好的环境。

浏览器页面只连接随机端口的 `127.0.0.1`，不是云端网站。应用界面目前仍是简体中文；GitHub
首页的中文、English、日本語链接只切换说明文档，不会切换应用界面。

## 3. 使用前要准备什么

首次启动只准备应用自己的私有运行环境，不会静默安装 Word、TeX 发行版或论文宏包。完成
整套审阅时，请留意以下条件：

- 审阅者需要 Microsoft Word for Windows，用原生“修订”功能编辑返回稿；
- 若审阅稿含 `SEQ`、`REF`、`PAGEREF` 等活字段，运行应用的电脑也需要 Word 来刷新字段；
- 生成干净 PDF 需要能编译原论文的 TeX 环境和 `latexmk`；
- 生成带修改标记的 PDF 还需要 `latexdiff`；
- 自动 PDF 核验目前按一套完整 MiKTeX 工具链验证，不会混用多个 TeX 安装。

没有 TeX 工具时，Word 导出、返回稿读取、逐条审批和新 LaTeX 副本仍可继续。结果页会明确
列出未生成的 PDF 或核验材料，不会把部分完成冒充全部成功。

## 4. 启动、数据目录和隐私

当前公开入口是源码 ZIP：打开解压目录，双击根目录中的
`Start-Latex-Word-Review.cmd`。命令窗口会负责准备或复用私有运行环境，然后打开本机页面。
请不要只复制这个启动文件，也不要把它移出解压后的项目目录。

应用只绑定随机端口的 `127.0.0.1`，默认浏览器只是本机界面，不是云网站。程序不会主动上传
论文。

默认数据根：

```text
%LOCALAPPDATA%\LatexWordReview\
└── runs\
    ├── session_<随机标识>\
    └── session_<随机标识>\
```

每轮审阅都是一个独立任务。安装版、正式 portable 和源码入口共用这个位置，所以更换这些
启动方式后仍可看到旧任务。维护者工作区启动器会覆盖为
`output\local-windows\user-data`，确保应用数据和临时文件不离开工作区。高级用户可
显式运行：

```powershell
latex-word-review app --data-root C:\review-data
```

普通用户应保留默认路径。不要把数据根设在论文源目录中，也不要使用会自动冲突合并文件的
云同步目录。

## 5. 第一步：选择 `main.tex`

1. 在首页点击 **新建审阅**。
2. Windows 文件选择器中选择论文的主 `.tex`，例如 `main.tex`。
3. 查看预检页显示的主文件、论文位置和依赖检查。
4. 没有阻断项后点击 **生成审阅 Word**。

程序会：

- 把主文件所在目录作为来源根；
- 保守发现静态 `\input`、`\include`、图像和文献依赖；
- 拒绝越界路径、链接/junction 逃逸和歧义依赖；
- 创建新的随机任务目录；
- 复制不可变快照并记录文件哈希；
- 在后台调用固定的 `tex2word` 路径生成 Word；
- 通过上游公开 `reference_doc` 接口加载代码确定性生成、SHA-256 绑定的
  `academic-review-v1` 样式；若模板未被上游明确确认加载，导出立即失败；
- 统一为 A4 单栏审阅稿，默认使用 Times New Roman/SimSun、明确标题层级、紧凑表格，
  并把普通图片限制在正文宽度内（只缩小、不主动放大）。

原项目不会被写入。后台处理中可以保留进度页；同一任务同时只允许一个写操作。

### 选择前的建议

- 先确认论文能在自己的常规环境编译；
- 主文件与依赖使用严格 UTF-8；
- 不要把密码、密钥或无关私人文件混入项目树；
- 动态脚本、shell escape 或运行时生成资源可能被阻断；
- 复杂宏、期刊类和不明确路径应预期人工复核。

## 6. 第二步：交给 Word 审阅

Word 生成后，页面同时显示 **打开审阅稿** 和 **另存到指定位置**：

- **打开审阅稿**：直接打开程序内部的密封导出基线，适合本机快速检查；
- **另存到指定位置**：弹出 Windows 保存窗口，生成一个可发送、可编辑的 `.docx` 副本。

另存不会移动或改写内部基线，不会覆盖已有文件；取消保存没有副作用。典型内部路径是：


```text
export\review.docx
```

这是便于改字、修订和批注的语义审阅版式，不是 PDF 像素复刻，也不是期刊终稿模板。
LaTeX 中明确声明的字体可以优先于默认字体；分页、浮动体位置和图像栅格化结果仍可能与
PDF 不同，发送前应在 Word 中浏览公式、图片、表格和特殊字段。

建议把下面说明一并发给审阅者：

> 请使用 Microsoft Word 打开文件，并保持“审阅 → 修订”开启。正文修改请直接使用修订，
> 讨论内容请使用批注。请不要“接受所有修订”、删除书签、另存为旧 `.doc` 格式或通过会
> 扁平化修订证据的编辑器处理。完成后请返回 `.docx`。

不要编辑本机保存的导出基线；它用于对照返回稿。

收到文件后：

1. 点击 **选择返回 Word 并读取修改**。
2. 选择返回的 `.docx`；`.docm`、旧 `.doc` 和链接文件不会接受。
3. 等待只读归档、基线核验和 ChangeSet 建立。

程序按顺序：

1. 把收到的字节归档为 `receive/original/returned-original.docx`；
2. 绑定导出与返回文件 SHA-256；
3. 计算返回稿的 reject-changes 语义视图；
4. 与导出基线对账；
5. 读取插入、删除、替换、move、格式和批注；
6. 建立逐条审批清单。

### 为什么返回稿可能被拒绝

| 现象 | 含义 | 正确处理 |
|---|---|---|
| reject-view 与基线不同 | 可能关闭过修订或接受过修改 | 向审阅者索取保留原始修订的文件，或整轮转人工 |
| bookmark 缺失/重复/损坏 | Word 到 LaTeX 的定位不可信 | 重新取得未损坏返回稿，不要手改 JSON |
| 来自另一轮导出 | run、基线或 SourceMap 不匹配 | 回到对应任务，不能混用轮次 |
| OOXML 不支持或超限 | 当前无法安全解释 | 保留错误代码，构造脱敏最小例报告 |

基线核验主要证明可自动正文补丁所需的可见文字、结构、书签和修订语义。格式、OMML、图片、
超链接目标、content control、custom XML 与嵌入对象仍需人工完整性复核。

## 7. 第三步：逐条审批

审批卡会显示：

- 修改类型；
- 修改前/修改后；
- Word 作者和时间；
- 上下文和 LaTeX 相对路径；
- 定位方法、UTF-8 字节范围、置信度；
- 安全类别、指纹和诊断。

至少同时检查 before、after、上下文、作者、来源和风险，不要只看结果文字。

| 按钮 | 何时使用 | 是否保证自动应用 |
|---|---|---:|
| 采用 | 完全采用返回文字 | 否 |
| 修改后采用 | 接受方向，但填写自己的最终文字 | 否 |
| 不采用 | 保留原文 | 否 |
| 留待人工 | 证据有效，但需人工处理 | 否 |
| 无法判断 | 证据或语义存在冲突 | 否 |

“采用全部安全正文修改”只使用核心的 `accept_all_safe` 规则：它只为尚未决定且属于 exact
`plain_text_candidate` 的项目填写“采用”，不会覆盖任何已有决定。公式、引用、结构、move、
格式、批注、低置信度和冲突项不会被混入。

修改较多时，页面每次只渲染 25 项。筛选“待决定 / 可自动 / 需人工 / 冲突”后翻页；
保存单项决定会回到原筛选和原页，不必重新从第一项查找。

“将全部不可自动回填项标为人工”只使用核心的 `mark_manual` 降权规则：它仅处理尚未
决定、且不满足 exact 普通正文条件的项目，不会覆盖已有决定，也不会采用这些文字或写入
LaTeX。完成审批前仍可逐项更改决定。它适合先收拢大量公式、引用、结构和冲突项，再集中
人工处理。

每个决定都会生成新的不可变审批版本。所有项目决定后，点击
**完成审批并预览补丁**。这是第一道闸门：

- 密封最终 ApprovalSet；
- 不修改 `.tex`；
- 不代表所有 accepted 项都可自动应用；
- 从密封输入生成 dry-run PatchPlan。

## 8. 第四步：检查 diff，再明确确认

补丁页分别显示：

- 将自动应用；
- 留待人工；
- 不采用；
- 存在冲突；
- 每个受影响文件的 unified diff。

重点检查：

- 文件是否正确；
- 删除行与新增行是否符合你的决定；
- 没有未批准文件或额外修改；
- 人工项没有混入自动操作；
- 页面没有 `accepted_but_blocked`。

若计划被阻断，不要继续。显式点击页面的 **重新审批**，再把相关修改改为人工处理或不采用；
程序随后生成新的审批/计划版本。已有决定与旧证据保留，不会被批量操作或新计划覆盖。

计划 ready/noop 后，用户必须：

1. 勾选“我已检查补丁摘要”；
2. 点击 **确认并生成全部结果**。

这是第二道闸门。程序使用页面中绑定的当前 PatchPlan 精确哈希，再次验证源哈希、字节范围、
Unicode 边界、重叠和安全策略。只有全部一致才会创建 `revised-clean/`。原稿、snapshot、
返回 Word 原件和旧审批/计划保持不变。

## 9. 结果页怎样读

完整成功时可打开：

| 结果 | 说明 |
|---|---|
| 修订后的 LaTeX | 只包含自动授权补丁的新工作副本 |
| 修订后 PDF | 从上述副本编译的干净版本 |
| 带修订标记的 PDF | `latexdiff` 的人类可视差异 |
| 带修订标记的 LaTeX | 生成差异 PDF 的派生源 |
| 核验报告 | 编译、实际 diff 与完整性检查 |
| 审阅账本 | 作者、时间、决定、补丁和最终状态 |
| 审计包 | allowlist ZIP，可离线复核对象和哈希 |

后台典型路径：

```text
revised-clean\
verification\revised-clean.pdf
verification\latexdiff.tex
verification\latexdiff.pdf
ledger\ledger.json
ledger\ledger.html
delivery\run-manifest.json
audit.zip
```

LaTeX 源文件不会出现 Word 气泡式标记。`revised-clean/` 是干净源；存在实际源差异时，
`latexdiff.tex` 包含派生的增删标记，成功编译后的 `latexdiff.pdf` 才显示可视修订。noop
计划不会制造标记。Word 作者/时间/批注保存在 ChangeSet 与 ledger，不编码进
`latexdiff`；三者不能互相替代。

如果结果为“部分完成”，已安全应用的 LaTeX 不会撤销。结果页会说明缺失项和是否可以
**重新运行核验工具**。重试创建新的 `verification-retries/retry-rN/`，不会覆盖旧记录。

人工项应在完整自动核验结束后，从 `revised-clean/` 复制另一个目录再修改。不要把人工字节
塞回已密封的自动核验目录，并把它冒充为自动证据。

## 10. PDF 图片怎样进入 Word

LaTeX 中的 PDF 图片仍是权威资源。导出 Word 前，程序只在派生 overlay 中解析静态
`\includegraphics`：

- 把指定页规范化为 RGB PNG；
- 烘焙支持的裁剪与旋转；
- 记录源 PDF、页码、渲染器、像素和 PNG 哈希；
- 检查 DOCX 图片实例数，发现明显静默丢图就停止。

原 `.tex` 与 PDF 保持不变。Word 看到的是栅格审阅图，不是可编辑矢量 PDF。SVG、EPS、
`pagebox`、动态路径或不确定操作不会偷偷执行外部转换器，而是转人工。

## 11. 暂停、恢复与退出

### 关闭后继续

重新打开应用，首页显示最近任务。为避免任务较多或论文较大时首页逐个读取全部证据，首页只
在最多 1000 个候选目录中展示最近 20 项，并从受大小与 Schema 限制的密封来源摘要生成
“打开时核验”卡片。点击 **打开并核验** 或执行任何任务操作后，`ApplicationSession` 才会
从该任务的全部密封对象重新计算真实状态：

- 只读快照已完成；
- 等待返回 Word；
- 审批未开始/进行中/待最终化；
- 补丁预览待恢复/被阻断/等待确认；
- 已应用、核验中、部分完成、待账本、待审计包或已完成。

最近任务列表只是入口，不是权威证据。密封对象损坏、哈希漂移或路径越界时会显示稳定错误
代码并停止。

### 删除不再需要的任务

在最近任务卡点击 **删除**，进入独立确认页；只有勾选“我确认永久删除”后才会执行。程序会
先验证目标确实是自身数据根下的直属任务目录，并拒绝符号链接、junction/reparse point、
特殊文件、路径越界和正在运行的任务。删除只影响该任务的程序数据，不修改原论文，也不会
删除你通过“另存到指定位置”保存到其他目录的 Word 副本。删除不可撤销，请先保留需要的
审计包或结果文件。

### 恢复按钮不会越权

- “恢复并开始逐项审批”只建立第一版审批账本，不替你做决定。
- “恢复并生成补丁预览”只从最终审批重新计算 dry-run，不应用补丁。
- “重新运行核验工具”只创建新的核验尝试，不覆盖旧失败证据。

### 正确退出

关闭浏览器标签页不会结束本机服务器。请在首页或结果页点击 **退出程序**。程序停止接受新
操作；若后台任务仍在运行，会等待它安全结束。源码方式也可以在 PowerShell 按 `Ctrl+C`。

## 12. SmartScreen、签名与校验

当前没有公开安装器，因此也没有官方二进制下载入口。未来若公开 Beta 尚无可信
Authenticode 签名，Windows 可能显示 SmartScreen“未知发布者”。正确顺序是：

1. 只从项目 GitHub Release 获取；
2. 确认 tag/commit 与发布说明；
3. 用发布页 `SHA256SUMS.txt` 核对文件；
4. 阅读当期签名状态、SBOM/内容清单和已知限制；
5. 仅在你信任该源码与校验结果时继续。

不要关闭系统安全功能，也不要从网盘或第三方镜像取得同名 EXE。

## 13. 高级启动方式与未来发布计划

以下内容面向需要固定源码版本、参与开发或验收本地冻结候选的用户。普通用户按第 2 节双击
启动即可。

### 固定并运行审核过的 commit

先安装 Git 与 [uv](https://docs.astral.sh/uv/)，再把占位值替换为自己审核过的完整 40 位 SHA：

```powershell
git clone https://github.com/JIE-jiee/latex-word-review.git
Set-Location latex-word-review
$ReviewedCommit = "PASTE_THE_REVIEWED_40_CHARACTER_COMMIT_SHA_HERE"
git checkout --detach $ReviewedCommit
if ((git rev-parse HEAD).Trim() -ne $ReviewedCommit) { throw "Commit verification failed" }
uv sync --frozen --extra pdf-figures --python 3.12
uv run --frozen latex-word-review --version
uv run --frozen latex-word-review doctor
uv run --frozen latex-word-review app
```

占位值会故意失败，避免静默跟随变化中的 `main`。源码控制台需要保持运行。

### 维护者工作区入口

维护者本机可能有 `LaTeX Word Review（双击启动）.lnk`，备用入口为
`output\local-windows\Start-Latex-Word-Review.cmd`。该交付受 Git 忽略，只用于本机冻结候选
验收，不在公开源码 ZIP 中，也不代表 GitHub 已发布二进制。维护者可用
`scripts\deploy-local-windows.ps1` 更新候选；普通用户不需要运行构建或部署脚本。

### 安装器和 portable 的状态

当前没有 GitHub Release、PyPI 发布、签名安装器或公开 portable 包。二进制候选虽已完成
本地构建和运行检查，但原生依赖的许可证与可重链接证据尚未闭环，因此不能公开分发。详情见
[Windows 二进制许可证审计](reviews/windows-binary-license-audit-2026-07.md)。

阻断解除后，计划提供两种普通用户入口：

| 计划入口 | 启动方式 | 需要 Python、Git 或 uv |
|---|---|---:|
| 当前用户安装器 | 安装完成后从桌面或开始菜单启动 | 否 |
| Portable ZIP | 全部解压后双击 `LatexWordReview.exe` | 否 |

计划中的安装器不会请求管理员权限；卸载程序时不会删除论文和审阅任务。计划中的 portable
也必须先完整解压，不能在 ZIP 预览窗口内运行。请勿把这些计划描述理解为当前下载链接。

本地 `0.2.0b1` Windows x64 候选在 2026-07-20 的实测体积为：

| 资产 | 实测大小 | SHA-256 |
|---|---:|---|
| setup | **21,444,947 bytes（20.45 MiB）** | `6028a469f571f29c92219c36e23f2bd47d85515b99065ca50c9d82ef6d532904` |
| portable ZIP | **33,421,203 bytes（31.87 MiB）** | `e95f5b6bb90017c0f0f25f811b3f25b0c882dbe5bfb876ee253304f5d9fe13dd` |
| 安装或解压后的程序目录 | **65,767,086 bytes（62.72 MiB）**（438 个文件） | — |

该候选由固定的 64 位 CPython 3.12.13 冻结。体积会随版本和依赖更新而变化。这些本地数据
不代表公开发布，也不解除许可证阻断。

## 14. 高级 CLI 与异常恢复

普通审阅不要复制旧版十几条命令。以下情况才使用 CLI：

- 无界面自动化；
- 细查 sealed JSON 和稳定错误码；
- 从旧 `0.1.x` granular 运行目录恢复；
- 维护者测试、构建和发行审计。

启动界面的 CLI：

```powershell
latex-word-review app
latex-word-review app --data-root C:\review-data
latex-word-review app --no-browser
```

检查当前高层运行：

```powershell
latex-word-review workflow status <run-root>
```

清理工具拥有的废弃暂存目录必须先预览：

```powershell
latex-word-review workflow clean <run-root>
latex-word-review workflow clean <run-root> --execute
```

`workflow clean` 不能删除 snapshot、导出、返回原件、ChangeSet、审批、计划、revised tree
或交付物。Granular `snapshot → export → archive → ingest → approve → plan → apply →
verify → ledger → run-manifest → bundle → verify-bundle` 的完整契约见
[CLI 与运行目录](reference/cli.md)。不要猜参数，也不要编辑密封 JSON 绕过失败。

## 15. Codex Skill 的边界

`$latex-word-review` Skill 默认只启动 `latex-word-review app`，把文件选择、逐条决定和
第二次确认留给用户在本机界面完成。只有用户明确要求 CLI/Agent 编排或异常恢复时，Skill
才调用 granular 命令。

Skill：

- 不复制 DOCX 解析、哈希、审批、补丁和核验逻辑；
- 不从“帮我处理完”推导批量接受；
- 不代理跨过第一道或第二道闸门；
- 不把能发现的私人论文自动纳入工作；
- 遇到哈希、Schema、路径或能力错误必须停止。

核心程序不会主动上传论文，但 Agent 可能按所用 Codex 产品与组织政策接触命令输出、
before/after、作者或批注。敏感论文应先判断适用政策；不希望 Agent 接触内容时，只用本机
界面。`local_private` 是分类，不是加密或网络隔离。

## 16. 常见问题

### 返回 Word 后生成的 LaTeX 有修订标记吗

`revised-clean/` 是干净源，不带 Word 气泡。只要存在实际采用并安全应用的源差异，
`latexdiff.tex` 会包含派生的增删标记；核验编译成功后，`latexdiff.pdf` 显示可视差异。
noop 任务没有标记，核验未完成时也可能只有 `.tex` 而没有 `.pdf`。Word 作者、时间、批注
和决定在 ledger，不会被伪装成 `latexdiff` 元数据。

### 能不能一键接受所有修改

不能把任意修改一键接受。界面只提供受限的“采用全部安全正文修改”，仅补齐尚未决定的
exact 普通正文候选，绝不覆盖已有决定。公式、结构、move、格式、批注和冲突仍需人工；
第二道应用确认也不会省略。

### 已点“采用”，为什么不能自动写入

“采用”只表达内容意图。定位不精确、涉及 LaTeX 结构、包含危险字符、破坏 grapheme 边界
或属于非普通正文时，计划会保留决定但阻止自动补丁。

### 没装 TeX 能不能用

可以完成 Word 导出、返回稿解析、审批和新 LaTeX 副本。最终 PDF、`latexdiff` 和完整审计
交付会显示部分完成，待安装正确工具后重试。

### 能否直接把 PDF 图放进 Word

程序把指定 PDF 页转为 PNG 审阅预览，原 PDF 继续属于 LaTeX 项目。Word 中不是矢量原件。

### 可以在 Linux/macOS 用吗

不在支持、CI、发行和故障排查范围内。本项目只维护 Windows。

### 能否手改 JSON 后继续

不能。Schema、payload hash、run ID 和跨对象绑定会重新验证。修改决定应生成新的审批版本。

## 17. 报告问题

公开 Issue 不要上传私人论文、返回 Word、审稿人信息或未脱敏账本。建议提供：

- 完整 commit 和 `latex-word-review --version`；
- `doctor` 脱敏结果；
- 稳定错误代码和失败阶段；
- 自制最小 fixture；
- 是否安装 Word、TeX 引擎、`latexmk` 和 `latexdiff`；
- 预期与实际行为。

非敏感缺陷提交到 [GitHub Issues](https://github.com/JIE-jiee/latex-word-review/issues)；
安全问题使用[私密漏洞报告](https://github.com/JIE-jiee/latex-word-review/security/advisories/new)。

继续阅读：

- [English Windows Quick Start](quick-start-windows.md)
- [开发来源与 Vibe Coding 记录](development-provenance.md)
- [Windows 产品体验 ADR](adr/0003-windows-product-experience.md)
- [安全威胁模型](security/threat-model.md)
- [计划与安全应用](reference/plan-and-apply.md)
- [编译、账本与审计包](reference/verification-and-bundle.md)
