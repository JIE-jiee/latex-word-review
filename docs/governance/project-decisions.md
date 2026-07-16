# F1 项目名称、许可证、Python 与依赖分组决策证据（历史快照）

> **状态：历史决策快照，已被当前工程配置取代。** 核验时点为
> 2026-07-16T05:18:17Z。本文保留当时的名称冲突、许可取舍和可逆性证据，不是名称
> 注册、商标审查或法律意见。文中的“当前”“待确认”“尚未创建”均只描述该快照，
> 不得用来判断现在的仓库状态。

## 0. 当前决议

| 项目 | 当前决议/状态 | 规范来源 |
|---|---|---|
| distribution、仓库 slug、CLI、Skill | `latex-word-review`；公开目标为 [`JIE-jiee/latex-word-review`](https://github.com/JIE-jiee/latex-word-review)，CLI 与薄 Skill 已实现 | `pyproject.toml`、`skills/latex-word-review/`、发布路线 |
| Python import | `latex_word_review`，已实现 | `src/latex_word_review/` |
| 许可证 | 代码、原创文档和自制 fixture 采用 Apache-2.0，根 `LICENSE` 已存在 | `LICENSE`、fixture provenance |
| Python | `>=3.12,<3.14`，首批元数据只允许 3.12/3.13 | `pyproject.toml` |
| 支持平台 | Windows-only；Linux/macOS 不进入开发、CI、发行或维护承诺 | `docs/adr/0002-windows-only-support.md`、支持矩阵 |
| 运行依赖 | `jsonschema`、`lxml`、`referencing`、`rfc8785`、`tex2word==1.0.5` | `pyproject.toml`、`uv.lock` |
| 本地仓库 | Git 与 CI 配置已初始化；本快照中“未初始化”结论已失效 | 当前工作树 |
| 远程发布 | 公开源仓库为 `JIE-jiee/latex-word-review`；远程 CI 以精确提交的 Actions 状态为准，尚无 tag 或 GitHub/PyPI prerelease | 当前发布记录 |

当前机器可执行事实以 `pyproject.toml`、`uv.lock`、打包 Schema 和测试为准。以下内容
是决策形成过程，只在明确标注的核验时点内有效。

## 1. 当时推荐默认值

| 决策项 | 推荐默认值 | 当时状态 |
|---|---|---|
| 项目展示名 | **LaTeX Word Review** | 待维护者品牌确认 |
| GitHub 仓库 slug | **`latex-word-review`** | GitHub 当前无公开 exact-name 搜索结果；尚未创建或保留 |
| PyPI distribution | **`latex-word-review`** | PyPI JSON/Simple 当前返回 404；不等于已确认可注册 |
| Python import 包 | **`latex_word_review`** | 本地命名建议，尚未创建 |
| 默认 CLI | **`latex-word-review`** | 与 distribution 同名，避免 `texreview` 的现有同领域项目混淆 |
| Codex Skill | **`latex-word-review`** | 待 CLI/Schema beta 稳定后创建 |
| 代码许可证 | **Apache-2.0** | 推荐，必须由维护者确认后再写最终许可证 |
| 原创项目文档 | **默认随项目采用 Apache-2.0** | 推荐保持单一许可；维护者可决定是否另选内容许可证 |
| 完全自制 fixture | **默认随项目采用 Apache-2.0，并逐 fixture 写明作者、生成方式、SPDX 与 SHA-256** | 仅适用于原创 fixture；第三方内容不得被项目许可证“覆盖” |
| Python 元数据范围 | **`requires-python = ">=3.12,<3.14"`** | 下限来自主后端；上限来自封版支持审计 |
| 首批宣称支持 | **Python 3.12、3.13** | 3.14 只有契约测试通过后再加入支持声明 |
| 主转换依赖 | **E0/F1 固定 `tex2word==1.0.5`** | 通过契约测试后再考虑放宽小版本范围 |
| Pandoc 工具链 | **外部工具，不进入 wheel/sdist** | 基线先固定 Pandoc 3.9.0.2 + pandoc-crossref v0.3.24a |
| 修订解析 | **项目自有只读 OOXML canonical reader** | `docx-revisions`、Pandoc、docx-mcp 只作为可替换 oracle/adapter |

推荐不使用 `texreview` 作为默认 CLI。它虽然在 PyPI 当前没有公开项目，但 GitHub 已存在同领域的 [`ispamm/TexReview`](https://github.com/ispamm/TexReview)，描述为用于 peer review 的 LaTeX class。CLI、搜索词和后续文档都会产生明显歧义。

## 2. 候选名称冲突核验

### 2.1 官方查询结果

| 候选 | PyPI `/<name>/json` | PyPI Simple | GitHub `in:name` 搜索 | 判断 |
|---|---|---|---|---|
| `latex-word-review` | 404 | 404；underscore/dot 拼写探测也为 404 | 0 个公开结果 | **推荐**：描述准确，同时包含 LaTeX、Word、Review 三个核心关键词 |
| `texreview` | 404 | 404 | 1 个 exact-name 结果：[`ispamm/TexReview`](https://github.com/ispamm/TexReview) | **不推荐**：现有项目与本项目处于相邻领域，品牌和搜索混淆明显 |
| `latex-review-bridge` | 404 | 404；underscore 拼写探测也为 404 | 0 个公开结果 | **第二选择**：冲突较少，但名称没有体现 Word，发现性弱于 `latex-word-review` |

官方证据入口：

- PyPI JSON：[`latex-word-review`](https://pypi.org/pypi/latex-word-review/json)、[`texreview`](https://pypi.org/pypi/texreview/json)、[`latex-review-bridge`](https://pypi.org/pypi/latex-review-bridge/json)。快照时均为 404。
- PyPI Simple：[`latex-word-review`](https://pypi.org/simple/latex-word-review/)、[`texreview`](https://pypi.org/simple/texreview/)、[`latex-review-bridge`](https://pypi.org/simple/latex-review-bridge/)。快照时均为 404。
- GitHub Search API：[`latex-word-review in:name`](https://api.github.com/search/repositories?q=latex-word-review+in:name)、[`texreview in:name`](https://api.github.com/search/repositories?q=texreview+in:name)、[`latex-review-bridge in:name`](https://api.github.com/search/repositories?q=latex-review-bridge+in:name)。快照时结果数分别为 0、1、0。

### 2.2 404 不是名称保留证明

PyPI 官方 [Help](https://pypi.org/help/#project-name) 明确说明：即使没有同名公开项目或 release，上传新项目仍可能因以下原因失败：与标准库模块冲突、与已有项目过度相似、被管理员禁止，或已被其他用户注册但尚无 release。因此本文只能写“当前未发现公开项目”，不能写“名称可用”或“名称已保留”。

GitHub 仓库名按 owner/organization 命名空间存在；公开搜索为 0 也不等于商标清查、组织名可用性或私有仓库不存在。正式发布前仍需：

1. 维护者确认将使用的 GitHub owner/organization；
2. 再次执行 exact-name 和相似名称搜索；
3. 在有商业化、组织背书或正式品牌计划时做独立商标/域名核查；
4. 由维护者本人创建 PyPI 项目并启用 2FA/Trusted Publishing，不由自动化任务抢注名称。

### 2.3 建议保持同名映射

```text
display name      LaTeX Word Review
GitHub repository latex-word-review
PyPI distribution latex-word-review
Python import     latex_word_review
CLI               latex-word-review
Codex Skill       latex-word-review
```

同名映射牺牲少量命令长度，但显著降低安装、文档、Skill 和错误排查中的认知成本。短 alias 可以在 beta 后单独评估；不要先发布 `texreview` 再迁移。

## 3. Apache-2.0 与 MIT 的实际取舍

官方许可文本：

- [Apache License 2.0 官方文本](https://www.apache.org/licenses/LICENSE-2.0.txt)
- [OSI 发布的 MIT License 文本](https://opensource.org/license/mit)

### 3.1 文本层面的差异

| 维度 | Apache-2.0 | MIT |
|---|---|---|
| 基本使用权 | 第 2 节明确授予永久、全球、非独占、免费且不可撤销的版权许可，可复制、修改、再许可和分发 | 允许使用、复制、修改、合并、发布、分发、再许可和销售 |
| 明示专利条款 | 第 3 节有贡献者专利授权及提起特定专利诉讼后的终止机制 | 标准 MIT 文本没有与 Apache 第 3 节对应的明示专利授权/终止条款 |
| 贡献默认条款 | 第 5 节说明，除非贡献者另有明确声明，为纳入 Work 而提交的 Contribution 默认受 Apache-2.0 约束 | 标准 MIT 文本没有独立的 contribution 条款 |
| 再分发义务 | 第 4 节要求附许可证、标注修改、保留适用 notices；如果上游 Work 有 `NOTICE`，还须保留其中适用归属 | 只要求在 copies 或 substantial portions 中包含版权和许可声明 |
| 商标 | 第 6 节明确不授予商标/商品名使用许可，合理描述来源和复制 NOTICE 除外 | 标准文本没有独立商标条款 |
| 保证与责任 | 第 7、8 节明确按原样提供并限制责任 | 有简短的按原样、无保证和责任限制条款 |
| 长度与维护成本 | 较长，需要认真维护 attribution/NOTICE 和变更说明 | 极短，贡献者和下游理解成本低 |

### 3.2 对本项目的影响

**选择 Apache-2.0 的理由**

- 项目目标是长期接受外部贡献的基础设施，不只是示例脚本；明示专利授权和 contribution 条款比 MIT 更完整。
- OOXML、格式转换、源映射和局部补丁属于可能被企业采用的技术实现，Apache-2.0 的专利与商标边界更清楚。
- 项目已经计划维护第三方依赖、SBOM 和通知，增加 Apache 的合规记录成本可以纳入既定发布流程。

**选择 MIT 的理由**

- 文本短、再分发条件简单，并与 `tex2word`、`tex2docx`、`docx-revisions`、`docx-mcp-server` 的 MIT 许可保持风格一致。
- 如果项目主要目标是教学参考、小规模维护和最大限度降低贡献门槛，MIT 更省治理成本。

**推荐默认：Apache-2.0。** 这是治理建议，不是法律结论。若维护者明确偏好极简许可和与主要 Python 上游完全一致的风格，MIT 是合理备选。

### 3.3 上游依赖不会自动决定本项目许可证

- MIT Python 上游通过正常依赖解析安装；本项目不复制它们的转换核心。依赖为 MIT 不要求本项目也必须选择 MIT。
- Pandoc 是 GPL-2.0-or-later，pandoc-crossref 上游元数据为 GPL-2。本项目只发现并调用用户独立安装的外部可执行文件，不把其源码或二进制捆入 wheel/sdist。
- 如果未来复制 MIT 上游的实质代码或资产，必须保留原版权和许可文本，不能仅用本项目 Apache-2.0 声明替代。
- 不复制 GPL 实现进入 Apache 代码库；如未来需要一并分发 GPL 工具，必须重新做许可证兼容与分发义务审核。

以上边界与 [upstream-dependency-matrix.md](../compat/upstream-dependency-matrix.md) 的 adopt/wrap 决策一致。

### 3.4 文档与自制 fixture

为避免初期出现三套许可证，推荐 F1 默认把**原创**代码、项目文档和完全自制 fixture 统一声明为 Apache-2.0，同时做以下路径级记录：

- 每个 fixture 都有 metadata，记录作者、生成方式、SPDX、SHA-256、预期结构和是否包含外部素材；
- 生成的 `.docx`、图片、Bib 文件和 LaTeX 源必须与 fixture metadata 同一许可；
- 第三方模板、论文、图像、字体、CSL 或 reference.docx 不能因为放进仓库就自动变成 Apache-2.0；许可证不明确时不提交；
- 私人论文和导师 Word 永不进入公开许可范围。

当时尚未选定第三种内容/数据许可证，也没有写最终 `LICENSE`；当前决议已经统一采用
Apache-2.0，第三方内容仍保留自身许可。

## 4. Python 支持范围

### 4.1 封版审计后的当前支持声明

```toml
[project]
requires-python = ">=3.12,<3.14"
```

首批 CI 和文档只宣称：

- Python 3.12：最低版本、必测；
- Python 3.13：必测；
- Python 3.14：当前元数据明确不接受安装；只有增加阻塞 CI、上游契约和完整闭环证据后，
  才能通过新的兼容决策移除上限。

早期快照曾建议不写 `<3.14`，该建议已被封版审计推翻：项目只在 3.12/3.13
上配置阻塞 CI，不应让更宽的安装元数据被误读成支持承诺。这个上限是本项目的发布范围，
并不声称 Python 3.14 已知不兼容。上游 `tex2word 1.0.5` 仍只声明 `>=3.12`；本机曾在
3.14 上完成的窄契约实验也不等于本项目完整闭环支持。

### 4.2 依据

- [`tex2word 1.0.5` PyPI](https://pypi.org/project/tex2word/1.0.5/) 和 [`pyproject.toml`](https://github.com/yfyang86/tex2word/blob/v1.0.5/pyproject.toml#L1-L36) 要求 Python `>=3.12`，分类器列出 3.12、3.13。
- `docx-revisions 0.1.5` 与 `docx-mcp-server 0.7.4` 要求 `>=3.10`，不会把主项目最低版本推高到 3.12 以上。
- `tex2docx 1.3.0` 要求 `>=3.8`，但它只是 E0 对照/可选 wrapper。
- Pandoc、pandoc-crossref、LaTeX 工具链是外部可执行文件，没有 Python 版本要求。

如果未来必须支持 Python 3.10/3.11，只能把 tex2word 放入独立 Python 3.12 环境并通过 CLI/进程边界调用。该方案会增加安装、诊断和跨环境路径复杂度，不推荐作为 v0.1 默认。

## 5. 当时的依赖分组建议

当前运行依赖以 `pyproject.toml` 与 `uv.lock` 为准，已经在早期转换器原型基础上增加
`jsonschema`、`referencing` 和 `rfc8785`，分别用于 v1alpha Schema 校验、内存资源注册表
与 RFC 8785 canonical JSON。

### 5.1 发布时的运行依赖

F1/E0 阶段先使用精确锁定，避免把上游波动误判为本项目问题：

```toml
[project]
dependencies = [
  "jsonschema>=4.26,<5",
  "lxml>=6.1,<7",
  "referencing>=0.37,<1",
  "rfc8785>=0.1.4,<0.2",
  "tex2word==1.0.5",
]
```

- 本项目的 raw OOXML reader 会直接使用 `lxml`，因此即使 tex2word 已经传递依赖它，也应声明为本项目的直接依赖。
- `tex2word==1.0.5` 是 E0/F1 的可复现实验锁定，不代表最终 1.0 永久精确钉死。只有多版本契约测试通过后，才考虑放宽为受控范围，例如 `>=1.0.5,<1.1`。
- 在该快照时 CLI 框架和 Schema/模型库尚未落地；后续实现已经证明并直接声明
  `jsonschema`、`referencing` 和 `rfc8785`，CLI 继续使用标准库 `argparse`。

### 5.2 可选功能 extras

建议只暴露与用户可理解功能对应的 extras，不提供把所有重依赖一次装入的 `all`：

| extra | 候选内容 | 说明 |
|---|---|---|
| `pdf-figures` | `tex2word[pdf]==1.0.5`、`pillow>=12,<13`、`pypdfium2>=5,<6` | PDF 图片栅格化能力 |
| `math-fallback` | `tex2word[mathml,mathimg]==1.0.5`、`matplotlib>=3.10,<3.12`、`kiwisolver>=1.4.8,<1.6`、`latex2mathml>=3.77,<4` | OMML 失败时的次级 MathML/图片路径；避免最低解析退回 Python 3.12 无法构建的旧 Kiwisolver/Setuptools 链 |
| `citations` | `tex2word[csl]==1.0.5`、`citeproc-py>=0.10,<0.11` | CSL citation processor |
| `docx-revisions` | `docx-revisions==0.1.5` | 只有契约测试通过后才发布；作为 oracle/派生副本辅助，不是 canonical reader |

不要创建以下默认 extras：

- `pandoc`：Pandoc 和 pandoc-crossref 是外部二进制，Python extra 不能可靠安装正确的平台/版本组合；
- `tex2docx`：只属于上游对照环境，不是生产默认路径；
- `docx-mcp`：正确 distribution 是 `docx-mcp-server`，依赖 MCP、Presidio、spaCy，且 server 启动有写入 Claude Skill 的副作用；只能用户显式管理；
- `all`：会把重型、实验和存在额外安全面的依赖混为一个未经审计的安装面。

### 5.3 不发布给用户的开发依赖组

```text
test             pytest、coverage、fixture/golden helpers
lint             ruff、类型检查
build            wheel/sdist 构建与 clean-install 验证
docs             文档构建工具
upstream-eval    tex2docx==1.3.0、docx-revisions==0.1.5
upstream-mcp     docx-mcp-server==0.7.4（单独隔离，不并入普通 dev）
```

具体开发工具和版本应在 F1 初始化时根据 CI 实测锁定；本文只冻结分组职责，不提前承诺工具。

### 5.4 外部工具 profile

外部程序不属于 Python extras。当前命令是 `latex-word-review doctor`，它默认输出
canonical JSON，不存在 `--json` 开关：

| profile | 首轮组合 | 用途 |
|---|---|---|
| `pandoc-baseline` | Pandoc 3.9.0.2 + pandoc-crossref v0.3.24a | 带 crossref 的基线导出 |
| `pandoc-reader` | Pandoc 3.10 | `--track-changes=all` 修订解析对照 |
| `latex-verify` | 受支持的 TeX、latexmk、latexdiff | 编译、引用和差异核验 |

wheel/sdist 不捆绑这些程序。每个 run 记录版本、实际可执行文件哈希和 capability，但公开报告不泄漏本机绝对路径。

## 6. 可逆与近乎不可逆影响

| 决策 | 发布前 | 首次公开发行后 | 风险级别 |
|---|---|---|---|
| 展示名/仓库 slug | 易修改 | 外链、badge、issue 引用和用户认知会迁移；GitHub redirect 也不能替代长期兼容计划 | 中 |
| PyPI distribution | 尚未注册时易修改 | 安装命令和供应链身份高度粘滞；404 不能保证以后可注册 | **高** |
| CLI 名 | 易修改 | 脚本、CI、Skill、教程和自动化都会依赖；应通过 alias + deprecation 迁移 | **高** |
| Python import 包 | 易修改 | 用户代码和插件会直接 import；迁移代价通常高于展示名 | **高** |
| Apache-2.0 / MIT | 只有单一权利人且未发布时较易调整 | 接受外部贡献后，整体重新许可需要确认相关权利；应视为近乎不可逆治理选择 | **高** |
| 文档/fixture 许可证 | 原创内容可统一调整 | 被外部复用后改许可会制造版本分裂；第三方内容本来就不能由本项目单方改许可 | **高** |
| Python 最低版本 | 易调整 | 提高下限会中断用户；降低下限会扩大维护矩阵 | 中 |
| extra 名称 | 易调整 | 出现在安装命令和锁文件中，但可通过兼容 alias/弃用周期迁移 | 中 |
| backend adapter | 协议未冻结前易调整 | 只要公共 Schema 不泄漏上游类型，仍可替换 | 低至中 |
| 复制上游源码而非 wrap | 可以避免 | 一旦发布会引入来源、许可证和长期同步责任 | **高；默认禁止** |

## 7. 当时必须由维护者确认的项目

在当时写 `pyproject.toml`、最终许可证或创建远程之前，维护者需要明确确认：

1. 是否采用 `latex-word-review` 作为仓库、PyPI、CLI 和 Skill 的统一名称；
2. 将使用哪个 GitHub owner/organization，以及是否需要商标/域名层面的进一步检查；
3. 是否接受 Apache-2.0 作为代码、原创文档和自制 fixture 的默认许可，或改选 MIT；
4. 如果文档/fixture 采用单独内容许可证，具体许可证和范围是什么；
5. 最终版权声明中的权利人名称和年份；
6. 外部贡献采用仅 Apache 第 5 节默认条款、DCO，还是另有 CLA 流程；
7. 首批是否只宣称 Python 3.12/3.13，3.14 在测试通过前保持未验证；
8. 是否接受主后端在 F1/E0 暂时精确固定 `tex2word==1.0.5`。

## 8. 当时尚未执行的动作及当前结果

当时计划在确认后执行：

1. 创建正式 `pyproject.toml`，写入统一名称和 Python 下限；**已完成**。
2. 生成最终 `LICENSE`、第三方通知和 fixture 许可元数据；**已完成**。
3. 初始化本地 Git 与 CI；**已完成并推送，精确提交的远程结果以 GitHub Actions 为准**。
4. 在维护者账户下创建 GitHub/PyPI 发布身份；**GitHub 目标身份已确认为
   `JIE-jiee/latex-word-review` 并已完成首次推送，PyPI 身份尚未创建**。
5. 从 wheel/sdist 做 clean-install 测试；**本地验证已完成，远程支持矩阵按精确提交持续核验**。

本文记录的原始决策任务没有执行这些动作，也没有替维护者保留任何名称；上面的完成
状态来自后续工程记录。GitHub 的目标 owner/slug 已由维护者确认；PyPI 名称可用性仍须在
实际包索引发布前重新核查。
