# E0 上游依赖、许可证与接口证据矩阵

> 核验快照：2026-07-18T04:20:00Z。本文只记录官方 GitHub 仓库、GitHub Release、版本标签源码、PyPI 元数据或项目官方文档中的证据。README 中的功能声明仅作为接口线索；尚未通过本项目 fixture 实测的能力均标为“待契约测试”。
>
> 当前项目支持范围由 ADR-0002 收敛为 Windows-only。本文关于上游提供 Linux/macOS
> 二进制的描述仅是上游事实，不构成本项目的平台支持、CI 或维护承诺。

## 1. 结论先行

| 上游 | 当前核验版本 | 最低运行环境 | 许可证 | E0 决策 | 生产边界 |
|---|---:|---|---|---|---|
| [`yfyang86/tex2word`](https://github.com/yfyang86/tex2word) | GitHub `v1.0.5` / PyPI `1.0.5` | Python `>=3.12`；基础依赖 `lxml>=5.0`、`pylatexenc>=2.10` | MIT | **adopt + wrap + contribute** | 主转换后端候选。只通过公开 Python API/CLI 适配，不复制 IR、OMML、活字段、OOXML package builder 或 round-trip 实现；审阅账本由本项目实现 |
| [`jgm/pandoc`](https://github.com/jgm/pandoc) | `3.10` | 无 Python 要求；优先使用官方预编译可执行文件。源码构建版本测试 GHC 9.6.7/9.8.4/9.10.3/9.12.2 | GPL-2.0-or-later | **adopt + wrap** | 独立外部进程，作为转换基线和修订读取对照；不把 Pandoc AST 变成本项目公共 Schema，不随 wheel 捆绑 |
| [`lierdakil/pandoc-crossref`](https://github.com/lierdakil/pandoc-crossref) | Release `v0.3.24a`，程序版本 `0.3.24` | 无 Python 要求；最新预编译件按 Pandoc `3.9.0.2`、GHC `9.8.4` 构建 | 上游元数据 `GPL-2` | **adopt as optional + wrap** | 仅属于 Pandoc 后端，外部进程，不随 wheel 捆绑；必须与 Pandoc 精确成对验证 |
| [`Mingzefei/latex2word`](https://github.com/Mingzefei/latex2word) / PyPI `tex2docx` | `v1.3.0` / `1.3.0` | Python `>=3.8`；外部 Pandoc；crossref 缺失会降级；子图路径调用 XeLaTeX、`pdftocairo` 且启用 `-shell-escape` | MIT | **reference + optional wrap** | 借鉴预处理、Lua、模板和 fixture；不设为默认依赖。只在不可变快照、显式许可和隔离环境运行高风险子图路径 |
| [`balalofernandez/docx-revisions`](https://github.com/balalofernandez/docx-revisions) | `docx-revisions-v0.1.5` / PyPI `0.1.5` | Python `>=3.10`；`python-docx>=1.1.0`、`lxml>=4.9.0` | MIT | **evaluate + optional wrap + contribute** | 可作为 `w:ins`/`w:del` 读取及派生副本接受/拒绝的契约对象；不能直接充当完整 `ChangeSet` 解析器，不 vendor 源码 |
| [`SecurityRonin/docx-mcp`](https://github.com/SecurityRonin/docx-mcp) / PyPI `docx-mcp-server` | `v0.7.4` / `0.7.4` | Python `>=3.10`；依赖 MCP、lxml、Mistune、Presidio、spaCy | MIT | **reference + optional integration** | 不作为核心依赖；只作为用户自行安装的 MCP 或比较对象。不得误装 PyPI 上属于另一仓库的 `docx-mcp` 包 |

推荐的 v0.1 组合是：

1. 主转换后端先固定 `tex2word==1.0.5`；上游最低 Python 是 `>=3.12`，而本项目发布
   元数据收紧为 `>=3.12,<3.14` 并以 CI 阻塞验证 3.12/3.13。这个项目上限不改写上游
   事实；它避免仅凭上游无上限约束就误称本项目支持 3.14。
2. Pandoc + pandoc-crossref 基线不能使用“当前 latest/latest”。当前 crossref `v0.3.24a` 的包约束是 `pandoc >=3.8.2,<3.10`，Release 说明其预编译件按 Pandoc `3.9.0.2` 构建；因此首轮成对实验应固定 **Pandoc 3.9.0.2 + pandoc-crossref 0.3.24a**。Pandoc 3.10 可另做“不带 crossref”的对照，直到 crossref 有兼容发行。
3. 修订证据的 canonical 路径应是本项目只读解析原始 OOXML 并输出版本化 `ChangeSet`。Pandoc 3.10、`docx-revisions==0.1.5` 和 `docx-mcp-server==0.7.4` 只作为交叉核验或可替换适配器。
4. 所有上游对象均须被本项目的 `Backend`/`RevisionReader` 协议隔离；公共 Schema 不得暴露 `tex2word.ir`、Pandoc JSON AST、`python-docx` 对象或 MCP 工具私有返回结构。

## 2. 关键兼容与供应链门槛

### 2.1 Pandoc 3.10 与当前 pandoc-crossref 发行不兼容

[`pandoc-crossref` 0.3.24 的 `package.yaml`](https://github.com/lierdakil/pandoc-crossref/blob/v0.3.24a/package.yaml#L31-L35) 声明 `pandoc >=3.8.2 && <3.10`；[`v0.3.24a` Release](https://github.com/lierdakil/pandoc-crossref/releases/tag/v0.3.24a) 又明确列出各平台二进制是按 Pandoc `3.9.0.2` 构建。其 README 还要求预编译 filter 与 Pandoc 版本匹配。故 `pandoc 3.10 + pandoc-crossref v0.3.24a` 必须在 `doctor` 中判为不受支持组合，而不是继续运行并等待隐蔽错误。

### 2.2 `docx-mcp` 存在 PyPI 名称碰撞

SecurityRonin 仓库的 [`pyproject.toml`](https://github.com/SecurityRonin/docx-mcp/blob/v0.7.4/pyproject.toml#L5-L28) 声明发行名为 **`docx-mcp-server`**，官方 README 也链接到 [`docx-mcp-server` PyPI 页面](https://pypi.org/project/docx-mcp-server/)。PyPI 的 **`docx-mcp`** 当前指向另一个仓库 `rockcj/Docx_MCP_cj`，不是本矩阵评估的项目。锁文件、安装文档和 `doctor` 必须同时校验 distribution name、版本和 `project_urls.Repository`，不能只按 import 名或相似名称安装。

### 2.3 GPL 工具保持外部进程边界

Pandoc 声明 [`GPL-2.0-or-later`](https://github.com/jgm/pandoc/blob/3.10/pandoc.cabal#L1-L14)，pandoc-crossref 声明 [`GPL-2`](https://github.com/lierdakil/pandoc-crossref/blob/v0.3.24a/package.yaml#L13-L21)。本项目采用保守的工程发布边界：

- wheel/sdist 不包含其二进制或源码；
- `doctor` 只发现并核验用户单独安装的可执行文件；
- 通过受控 `subprocess` 参数调用；
- 记录版本、可执行文件路径和哈希；
- 若未来发行安装器要一并分发二进制，必须单独完成许可证义务复核和 Third-Party Notices。

此处是发布工程边界，不替代正式法律意见。

### 2.4 `tex2docx` 子图路径不是默认安全路径

`tex2docx` 的 [`CompilerOptions`](https://github.com/Mingzefei/latex2word/blob/v1.3.0/tex2docx/constants.py#L96-L105) 明确为 XeLaTeX 加入 `-shell-escape`，子图模板又调用 `pdftocairo`。因此不能把该路径用于不可信 LaTeX，也不能在来源目录直接执行。若保留实验适配器，必须：

- 只在哈希快照中运行；
- 默认禁用该子图编译能力；
- 只有显式配置和隔离执行器才能开启；
- 报告实际调用命令与产物；
- 未来优先向上游贡献可关闭 shell escape 的安全接口。

## 3. 逐项官方证据与接口边界

### 3.1 tex2word 1.0.5

**仓库、发行与运行时**

- 官方仓库：[`yfyang86/tex2word`](https://github.com/yfyang86/tex2word)。
- 最新 GitHub Release：[`v1.0.5`](https://github.com/yfyang86/tex2word/releases/tag/v1.0.5)，发布于 2026-07-12。
- PyPI：[`tex2word 1.0.5`](https://pypi.org/project/tex2word/1.0.5/)，`requires-python = ">=3.12"`，发行状态分类为 Beta。
- [`pyproject.toml`](https://github.com/yfyang86/tex2word/blob/v1.0.5/pyproject.toml#L1-L37) 声明 MIT、基础依赖 `lxml>=5.0` 和 `pylatexenc>=2.10`；PDF、MathML、公式图片和 CSL 是可选 extras。
- [`LICENSE`](https://github.com/yfyang86/tex2word/blob/v1.0.5/LICENSE) 为 MIT。

**可包装的公开接口**

- [`tex2word.__init__`](https://github.com/yfyang86/tex2word/blob/v1.0.5/src/tex2word/__init__.py) 明确导出 `convert_source`、`convert_file`、`ConversionResult`。
- [`ConversionResult`](https://github.com/yfyang86/tex2word/blob/v1.0.5/src/tex2word/pipeline.py#L18-L40) 包含 IR `document`、`ConversionReport` 和 DOCX bytes，适合适配成统一 `ExportReport`，但本项目不得把其类直接暴露为公共 Schema。
- [`convert_file`](https://github.com/yfyang86/tex2word/blob/v1.0.5/src/tex2word/pipeline.py#L208-L235) 是文件级 API；CLI 提供 `convert --report`、`coverage`、`benchmark` 和 `to-latex`。
- `convert_source`/`convert_file` 的公开 `reference_doc` 参数会导入标准 Word 样式、页面尺寸和
  页边距；本项目据此采用内置 `academic-review-v1`，不再自研第二套 DOCX 样式引擎。
- [`roundtrip.py`](https://github.com/yfyang86/tex2word/blob/v1.0.5/src/tex2word/roundtrip.py#L23-L104) 提供 manifest v1、`read_manifest`、`recover_ir` 与 manifest-biased `to_latex(reconcile=True)`。

**为何不能把 round-trip 当审阅账本**

- [`docx_reader._runs`](https://github.com/yfyang86/tex2word/blob/v1.0.5/src/tex2word/frontend/docx_reader.py#L412-L430) 明确把 `w:ins`/`w:moveTo` 当作已接受内容，把 `w:del`/`w:moveFrom` 丢弃；它产出的是接受所有修订后的文档视图，不是修订事件流。
- [`_read_comments`](https://github.com/yfyang86/tex2word/blob/v1.0.5/src/tex2word/frontend/docx_reader.py#L155-L168) 当前只返回 `(author, plain text)`，没有评论日期、范围锚点、回复/解决状态和原 XML 证据。
- reconcile 的目标是保守恢复/合并 LaTeX，不提供逐项 change ID、原文/新文、作者/时间、审批状态或只应用批准项的补丁计划。

**本地契约测试发现的 1.0.5 转换缺口**

- `figure` 直接包含多个 `minipage`、且每个列中有一张图和一个 caption 时，
  1.0.5 的 figure 解析会反复覆盖单一图片槽，最终 DOCX 可能只保留最后一张图。
- raster 图片通过 `\graphicspath` 找到且原命令省略目录时，直接保留原目标会让后端
  无法定位资源。
- 本项目不复制或修改上游 parser。wrapper 只在能力精确匹配 1.0.5 时，对派生树中
  可证明安全的 direct-minipage 形态做带 span/hash 证据的 `subfigure` 归一化，并把
  raster 命令改为已验证的根相对路径；原始 LaTeX 始终不变。任何未覆盖形态仍由
  DOCX 图片计数门禁阻断，而不是猜测补图。后续优先把最小复现和多图数据模型建议
  贡献给上游。
- On Windows with the legacy long-path policy, tex2word 1.0.5 joins `base_dir`
  and LaTeX forward-slash image paths before `os.path.isfile()`. Images beyond
  MAX_PATH are reported as missing even when conversion returns success. The real
  stress test retained only 1/64 images, and the project silent-loss gate blocked it.
- The only temporary private-API exception is confined to the isolated worker and
  guarded by Windows plus the exact `tex2word==1.0.5` version. It supplies an
  extended absolute path only while resolving an image and applies
  `os.path.normpath()` immediately before the upstream resolver. The conversion
  `base_dir` remains relative so `input`/`include` preprocessing keeps working.
  Version or API-shape drift fails closed; the installed package is not modified.
  Remove this shim after an equivalent upstream release.

**决策**

- **Adopt**：LaTeX parser、IR、OMML、活字段、reference doc、package builder、manifest 和转换报告。
- **Wrap**：在 `Tex2WordBackend` 中调用公开 API；reference DOCX 由固定 XML 代码生成并绑定
  哈希，必须取得上游 `reference-doc/info` 成功证据，warning/静默 fallback 直接阻断。若公开
  API 发生不兼容，可切换到版本固定的 CLI `convert --report`，不侵入核心 Schema。
- **Contribute**：建议上游提供“不接受修订”的原始事件 API、评论日期/范围、manifest/source-map 扩展点和稳定 capability 信息。
- **Self-build**：不可变运行、跨后端 `SourceMap`、完整 `ChangeSet`、审批、局部补丁和综合核验。
- **替换策略**：`Backend` 输出只允许本项目的 `ReviewIR`/`SourceMap`/`ExportReport`；若 tex2word 不再兼容，切换 Pandoc 或其他后端不会改变账本、审批和补丁 Schema。

### 3.2 Pandoc 3.10

**仓库、发行与运行时**

- 官方仓库：[`jgm/pandoc`](https://github.com/jgm/pandoc)。
- 最新 Release：[`3.10`](https://github.com/jgm/pandoc/releases/tag/3.10)，发布于 2026-06-04。
- [`pandoc.cabal`](https://github.com/jgm/pandoc/blob/3.10/pandoc.cabal#L1-L50) 声明版本 3.10、许可证 GPL-2.0-or-later，并列出源码构建测试的 GHC 版本。
- 官方 [`INSTALL.md`](https://github.com/jgm/pandoc/blob/3.10/INSTALL.md) 为 Windows、macOS 和 Linux 提供 installer 或预编译包；本项目使用其可执行文件，不依赖 Python 包。

**可包装接口**

- LaTeX→DOCX 基线通过 CLI，使用 `--reference-doc` 和受控 filter 参数。
- [`--track-changes=all`](https://github.com/jgm/pandoc/blob/3.10/MANUAL.txt#L740-L755) 只作用于 DOCX reader；会把插入、删除、评论包装为具有 `insertion`、`deletion`、`comment-start`、`comment-end` 类的 spans，并包含作者和时间。
- [`--reference-doc`](https://github.com/jgm/pandoc/blob/3.10/MANUAL.txt#L1296-L1304) 可为 DOCX writer 提供样式参考。

**边界与缺口**

- Pandoc 手册没有在该接口中承诺 move pair、格式修订、评论线程、原始 OOXML 范围或所有 story part 的无损事件模型，必须以 canonical fixture 验证。
- Pandoc JSON AST 适合作为对照和中间输出，不适合作为本项目长期公共 Schema。
- **Adopt/Wrap**：外部 CLI，固定版本、超时、输入输出目录和 JSON 日志；转换与修订读取分别做能力声明。
- **替换策略**：Pandoc 后端被移除时，只失去基线/对照能力，不影响 tex2word 主后端或原始 OOXML `ChangeSet`。

### 3.3 pandoc-crossref 0.3.24 / Release v0.3.24a

**仓库、发行与运行时**

- 官方仓库：[`lierdakil/pandoc-crossref`](https://github.com/lierdakil/pandoc-crossref)。
- 最新 Release：[`v0.3.24a`](https://github.com/lierdakil/pandoc-crossref/releases/tag/v0.3.24a)，发布于 2026-05-17；tag 名含 `a`，但程序包版本是 `0.3.24`。
- [`package.yaml`](https://github.com/lierdakil/pandoc-crossref/blob/v0.3.24a/package.yaml#L1-L35) 说明它是图、公式、表和交叉引用编号 filter，依赖 Pandoc `>=3.8.2,<3.10`，许可证字段为 `GPL-2`。
- 官方 README 要求预编译 filter 与 Pandoc 版本匹配，并为 Windows、macOS、Linux 提供二进制。

**决策**

- **Adopt as optional/Wrap**：只通过 `--filter pandoc-crossref` 外部调用，版本组合在 `doctor` 和 adapter capability 中显式记录。
- **不捆绑**：不进入 Python wheel；用户独立安装，或未来由单独工具链安装器管理。
- **降级规则**：缺失或版本不兼容时，Pandoc 后端仍可运行的前提是报告稳定的“cross-reference capability unavailable”错误/降级项；不得伪装为完整转换成功。
- **替换策略**：优先由 tex2word 活字段后端承担生产转换；Pandoc adapter 可换用其他 filter，但不得改变统一 `SourceMap` 和 `ExportReport`。

### 3.4 Mingzefei/latex2word（PyPI `tex2docx`）1.3.0

**仓库、发行与运行时**

- 官方仓库：[`Mingzefei/latex2word`](https://github.com/Mingzefei/latex2word)。
- 最新 Release：[`v1.3.0`](https://github.com/Mingzefei/latex2word/releases/tag/v1.3.0)，发布于 2025-07-25。
- PyPI 发行名是 [`tex2docx 1.3.0`](https://pypi.org/project/tex2docx/1.3.0/)，Python `>=3.8`。
- [`pyproject.toml`](https://github.com/Mingzefei/latex2word/blob/v1.3.0/pyproject.toml#L8-L38) 声明 MIT classifier、依赖 `regex`、`tqdm`、`typer`，CLI 为 `tex2docx`。
- [`LICENSE`](https://github.com/Mingzefei/latex2word/blob/v1.3.0/LICENSE) 为 MIT。

**接口与风险**

- CLI：`tex2docx convert --input-texfile ... --output-docxfile ...`。
- Python：[`LatexToWordConverter`](https://github.com/Mingzefei/latex2word/blob/v1.3.0/tex2docx/tex2docx.py#L16-L95)，构造后调用 `convert()`。
- [`PandocConverter`](https://github.com/Mingzefei/latex2word/blob/v1.3.0/tex2docx/converter.py#L12-L46) 把 Pandoc 视为必需外部依赖；pandoc-crossref 缺失时只记录 warning，可能造成能力降级。
- README 明确称其为 Pandoc + pandoc-crossref 封装，并说明约 5% 内容可能需要人工修复。该声明不能代替本项目 fixture 的结构计数。
- 子图编译实际调用 XeLaTeX，且 `CompilerOptions` 开启 `-shell-escape`，是本项目必须额外收紧的安全面。

**决策**

- **Reference**：复用其预处理、Lua filter、reference.docx 和子图合成经验。
- **Optional Wrap**：若 E0 fixture 显示它显著优于直接 Pandoc adapter，可在快照内调用其 CLI；不直接继承其内部类或修改器对象。
- **不默认依赖**：其大部分核心价值与本项目必须拥有的可替换 Pandoc adapter 重叠，高风险编译选项又不符合默认安全策略。
- **替换策略**：直接 PandocBackend 能覆盖基线后即可移除此适配器；保留公开 fixture 与行为契约，不保留私有实现耦合。

### 3.5 docx-revisions 0.1.5

**仓库、发行与运行时**

- 官方仓库：[`balalofernandez/docx-revisions`](https://github.com/balalofernandez/docx-revisions)。
- 最新 Release：[`docx-revisions-v0.1.5`](https://github.com/balalofernandez/docx-revisions/releases/tag/docx-revisions-v0.1.5)，发布于 2026-04-14。
- PyPI：[`docx-revisions 0.1.5`](https://pypi.org/project/docx-revisions/0.1.5/)，Python `>=3.10`。
- [`pyproject.toml`](https://github.com/balalofernandez/docx-revisions/blob/docx-revisions-v0.1.5/pyproject.toml#L1-L12) 声明 MIT、`python-docx>=1.1.0` 和 `lxml>=4.9.0`。

**公开接口证据**

- [`__init__.py`](https://github.com/balalofernandez/docx-revisions/blob/docx-revisions-v0.1.5/docx_revisions/__init__.py) 导出 `RevisionDocument`、`RevisionParagraph`、`TrackedInsertion`、`TrackedDeletion` 等。
- [`TrackedChange`](https://github.com/balalofernandez/docx-revisions/blob/docx-revisions-v0.1.5/docx_revisions/revision.py#L26-L130) 暴露 author、date、revision_id、文本以及逐项 accept/reject。
- [`RevisionDocument`](https://github.com/balalofernandez/docx-revisions/blob/docx-revisions-v0.1.5/docx_revisions/document.py#L21-L135) 可枚举正文和表格段落中的 revisions，并接受/拒绝全部修订。

**能力缺口**

- 包描述和公开类型聚焦 `w:ins`、`w:del`；没有导出 move pair、格式修订或评论读取事件类型。
- `RevisionDocument.all_paragraphs` 明确覆盖正文和表格，但不是页眉、页脚、脚注、尾注、文本框等全部 story part。
- accept/reject 会修改文档，只能在返回 Word 的派生副本上运行，不能用于原件证据提取。
- 不产生本项目所需的稳定 `unit_id`、源 LaTeX 位置、原始 XML 证据哈希、匹配置信度和审批状态。

**决策**

- **Evaluate/Wrap**：建立只读 adapter，把它的基本插入/删除结果与 canonical OOXML fixture 对照；也可用来生成接受/拒绝派生视图。
- **Contribute**：通用 move、format、comments 和 story-part 读取能力若适合其范围，优先提交上游。
- **Self-build**：完整、只读、证据保真的 `ChangeSet` reader 仍属于本项目。
- **打包边界**：如采用，作为可替换 Python extra，由解析器协议隔离，不复制源码；移除它不得改变公共 Schema。

### 3.6 SecurityRonin/docx-mcp 0.7.4

**仓库、发行与运行时**

- 官方仓库：[`SecurityRonin/docx-mcp`](https://github.com/SecurityRonin/docx-mcp)。
- 最新 Release：[`v0.7.4`](https://github.com/SecurityRonin/docx-mcp/releases/tag/v0.7.4)，发布于 2026-05-28。
- 官方 PyPI 发行是 [`docx-mcp-server 0.7.4`](https://pypi.org/project/docx-mcp-server/0.7.4/)，Python `>=3.10`，MIT。
- [`pyproject.toml`](https://github.com/SecurityRonin/docx-mcp/blob/v0.7.4/pyproject.toml#L5-L49) 显示核心依赖包含 MCP、lxml、Mistune、Presidio 和 spaCy；这远大于本项目解析一份 DOCX 所需的运行时面。

**接口证据**

- MCP 暴露 `get_tracked_changes`、逐项/全部 accept/reject、`get_comments`、`generate_change_summary`、书签和结构审计等工具。
- [`RevisionsMixin.get_tracked_changes`](https://github.com/SecurityRonin/docx-mcp/blob/v0.7.4/docx_mcp/document/revisions.py#L12-L61) 返回 type、change_id、author、date、para_id、text。
- [`CommentsMixin.get_comments`](https://github.com/SecurityRonin/docx-mcp/blob/v0.7.4/docx_mcp/document/comments.py#L10-L25) 返回评论 id、author、date、text。

**不能直接作为 canonical 账本的原因**

- 当前 revisions 源码只遍历 `word/document.xml` 中每个 `w:p` 的**直接子元素** `w:ins`/`w:del`，接口没有 move pair、嵌套 revision、格式修订或其他 story parts 的证据。
- `get_comments` 没有返回评论范围 start/end、引用位置、thread/resolve 状态或原始 XML 证据。
- MCP 工具返回结构没有 LaTeX `SourceMap`、置信度、审批和补丁语义。
- [`cli.py`](https://github.com/SecurityRonin/docx-mcp/blob/v0.7.4/docx_mcp/cli.py#L10-L59) 会在 server 启动时静默安装/更新 `~/.claude/skills/docx-mcp`。这类跨工作区写入不符合本项目核心依赖的最小副作用要求。

**决策**

- **Reference**：借鉴其广泛 OOXML fixture、结构校验、安全 guard 和 MCP/Skill 分层。
- **Optional Integration**：只在用户已明确配置的外部 MCP 上调用；核心项目不自动安装、不自动启动、不导入其内部 mixin。
- **不进入 canonical parser**：重依赖、接口宽、修订提取范围仍不足。
- **替换策略**：该集成可完全移除且不影响 CLI 闭环；同一功能由本项目 `RevisionReader` 和可选其他 adapter 提供。

## 4. 现有上游仍缺失的审阅账本能力

| 本项目 `ChangeSet` 要求 | tex2word 1.0.5 | Pandoc 3.10 | docx-revisions 0.1.5 | docx-mcp-server 0.7.4 | v0.1 责任归属 |
|---|---|---|---|---|---|
| 插入/删除原始事件、作者、时间 | reader 会直接形成“全部接受”视图 | `--track-changes=all` 有 spans、作者、时间 | 有 `w:ins`/`w:del`、作者、时间 | main document 的直接 `w:ins`/`w:del` 有 | 本项目 raw OOXML canonical reader；其他实现作 oracle |
| moveFrom/moveTo 成对事件 | 被折叠为接受视图 | 官方接口未承诺完整 move pair | 无公开类型 | 无公开事件 | **self-build**，必要时 contribute |
| run/paragraph/table 格式修订 | 未形成账本 | 官方接口未承诺 | 无公开类型 | `get_tracked_changes` 不返回 | **self-build**，v0.1 默认 manual |
| 评论日期、精确范围、线程、解决状态 | 当前仅 author + plain text | 有 comment spans，但完整范围/线程需实测 | 无评论读取事件 | 有 id/author/date/text，无范围 | **self-build**，必要时 contribute |
| 全部 story parts 与嵌套结构 | 不构成事件流 | 需 fixture 证明 | 正文 + 表格段落 | 当前 revisions 只读 main document | **self-build** |
| 原始 XML 证据和哈希 | 无逐项事件证据 | AST 已抽象化 | 无证据哈希 | 无证据哈希 | **self-build** |
| 跨后端稳定 `unit_id` 和 LaTeX 源位置 | manifest 可借鉴但不是跨后端 SourceMap | 无项目级稳定映射 | 无 | 只有 Word para_id | **self-build**，向 tex2word contribute 扩展点 |
| 冲突、置信度、risk、审批状态 | 无 | 无 | 无 | 无 | **self-build** |
| 只应用批准 change ID 的局部 LaTeX patch | reconcile 生成合并 LaTeX，不是批准集补丁 | 无 | 无 | 无 | **self-build** |

因此 Skill 不能直接把 `tex2word to-latex`、Pandoc 的接受视图或第三方 accept/reject API包装成“一键回填”。Skill 应编排本项目 CLI：先由 tex2word/Pandoc 导出，再由本项目生成不可变 `ChangeSet`、审批文件和局部 `PatchPlan`。核心逻辑仍在独立 CLI/库中，Skill 不重复转换器，也不承担账本解析。

## 5. 打包、调用与替换策略

### Python 依赖

- `tex2word`：若作为默认主后端，按经测试的小版本范围固定；首轮用 `==1.0.5`。通过正式依赖解析安装，不 vendor 源码。
- `docx-revisions`：仅在契约测试证明有价值后进入 optional extra；首轮固定 `==0.1.5`。
- `docx-mcp-server`：不进入 core 或默认 extra；若未来提供 MCP adapter，由用户显式安装和配置。
- `tex2docx`：不进入默认依赖；E0 可在独立环境固定 `==1.3.0` 做对照。

### 外部可执行文件

- Pandoc 与 pandoc-crossref：由用户或专门工具链安装，项目仅发现、版本校验和受控调用，不进入 wheel/sdist。
- 每次 run 记录解析后的绝对可执行路径、`--version` 输出、SHA-256、命令参数和退出码；公开 manifest 中路径必须脱敏为逻辑工具 ID，避免泄漏本机路径。
- adapter 必须声明 capability；版本不匹配、缺失 filter 或使用了降级路径时返回稳定诊断，不能静默成功。

### 内部协议

```text
ConversionBackend
  -> BackendCapabilities
  -> ReviewIR (project schema)
  -> SourceMap (project schema)
  -> ExportReport (project schema)

RevisionReader
  -> raw revision events
  -> ChangeSet (project schema)
  -> evidence hashes / diagnostics
```

任何上游替换只允许发生在协议下面。`ApprovalSet`、`PatchPlan` 和 `VerificationReport` 不得依赖具体上游类型。

## 6. 尚待最小契约实验回答的问题

1. `tex2word 1.0.5` 在 Python 3.12/3.13、受支持 Windows 环境上的确定性 DOCX、manifest 和报告表现。
2. `tex2word` 公共 API 与 CLI 在同一 fixture 上是否生成等价报告；API 失败时 CLI 是否足以作为稳定 fallback。
3. Pandoc 3.9.0.2 + crossref 0.3.24a 的结构计数，以及 Pandoc 3.10 不带 crossref 的差异。
4. Pandoc `--track-changes=all` 对 move、段落级删除、范围评论、多审阅者和嵌套表格的实际 AST。
5. `docx-revisions` 对 block-level revisions、嵌套 revisions、表格和缺失 author/date 的精确行为。
6. `docx-mcp-server` 的 MCP 返回与其底层 Python 实现是否一致，以及启动副作用能否在外部集成中完全隔离。
7. 以上所有依赖在损坏 ZIP/XML、外部关系、超大部件和路径穿越 fixture 下的失败模式。

通过这些实验前，本文只支持依赖选择和接口边界，不代表 E0 上游契约实验已经完成。
