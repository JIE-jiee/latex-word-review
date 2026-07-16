# LaTeX Word Review

LaTeX Word Review 是一个可审计、逐条审批、默认拒绝危险回填的
LaTeX–Microsoft Word 审阅桥接。它让合作者在 Word 里使用“修订”和“批注”，
同时保持 LaTeX 是唯一权威源。

本项目不把 Word 整篇反向转为 LaTeX，也不覆盖原稿。它复用
[`tex2word`](https://github.com/yfyang86/tex2word) 等上游转换器，专注于不可变归档、
稳定源映射、完整修订账本、人工审批闸门、局部补丁与编译验证。

> 状态：`0.1.0b1` beta 候选，公开源仓库为
> [`JIE-jiee/latex-word-review`](https://github.com/JIE-jiee/latex-word-review)。远程验证结果
> 以 GitHub Actions 对当前提交的检查为准；尚未创建 GitHub Release。公共契约为
> `v1alpha`；适合评估、实验和审计，尚不应把任意复杂 LaTeX 文档的全自动回填视为稳定
> 承诺。
>
> 支持范围仅限 Windows 与 CPython 3.12/3.13。Linux 和 macOS 不属于本项目的开发、CI、
> 发行或维护承诺；纯 Python 发行物即使能在其他系统安装，也不代表获得支持。

## 能得到什么

```text
LaTeX 权威源
  → 只读快照 + SourceManifest
  → 带稳定 bookmark 的可编辑 Word 审阅稿
  → 返回 Word 原件只读归档
  → 包含作者、时间、插入、删除、move、格式和批注证据的 ChangeSet
  → 本机浏览器或 CLI 逐条审批
  → dry-run PatchPlan + 精确 unified diff
  → 全新的干净 LaTeX 工作副本
  → revised-clean.pdf + latexdiff.tex/.pdf + JSON/HTML 账本
  → 可离线验证的 allowlist 审计 ZIP
```

Word 修订信息不是通过 `latexdiff` 伪造的：作者、时间、before/after、批注范围和
OOXML 证据进入独立账本；`latexdiff` 只用于产生人类可视的 LaTeX/PDF 差异。

## 安全模型

- 权威 LaTeX 和收到的 Word 原件全流程保持不变，并以 SHA-256 绑定。
- 审批和应用是两道独立闸门：审批只生成 `ApprovalSet`，无权改 `.tex`。
- `plan` 默认仅 dry-run；`apply` 重新验证所有哈希和字节范围，仅写全新目录。
- v0.1 只自动应用精确 bookmark 内、置信度至少 0.99 的 UTF-8 纯正文插入/
  删除/替换，并拒绝 LaTeX 结构字符、换段、重叠或漂移。
- 公式、引用、标签、命令、环境、图表结构、move、格式修订和批注会完整入账，
  但默认转人工处理。
- 所有外部命令使用固定 argv、无 shell、小环境、硬超时和输出上限；TeX
  只在私有副本上以 `-no-shell-escape` 运行。

完整信任边界见 [Security model](docs/security/threat-model.md)。

## 安装

需要受支持的 Windows 环境和 Python 3.12 或 3.13。从公开仓库取得源码后进入项目根目录；
以下 PowerShell 流程使用 [uv](https://docs.astral.sh/uv/) 进行开发和可复现验证：

```console
git clone https://github.com/JIE-jiee/latex-word-review.git
cd latex-word-review
uv sync --frozen --group fixture --python 3.12
uv run --frozen latex-word-review --version
uv run --frozen latex-word-review doctor
```

仓库源码也可以安装到当前 Python 环境；`main` 属于开发版本，自动化或可复现环境应固定
到经过审核的 commit，而不是长期跟随分支：

```console
python -m pip install .
latex-word-review --version
```

从 wheel 安装时：

```console
python -m pip install latex_word_review-0.1.0b1-py3-none-any.whl
latex-word-review --help
```

`tex2word==1.0.5` 是默认运行时后端。Pandoc 是可选外部基线。生成 PDF 审计产物需要
Windows 上可用的 `latexmk`、对应 TeX 引擎和 `latexdiff`；缺失时会显式 `blocked`，不会
伪报成功。远程真实 TeX 门禁在 Windows runner 上使用 MiKTeX 官方 Setup Utility
`miktexsetup-5.5.0+1763023-x64.zip`，先核对固定 SHA-256，再无交互安装 basic 集合并显式
安装/验证 `xetex`、`ctex`、`fandol`、`amsmath`、`booktabs`、`graphics`、`hyperref`、
`latexmk`、`latexdiff`。公共样例固定使用可随 TeX 分发的 Fandol 字体，不依赖 Runner
的区域或 Windows 中文补充字体；是否受支持以目标提交的实际 CI 结果为准。

## 从新 clone 运行公开最小闭环

确定性模式不要求 Word、私人论文或 TeX 安装：

```console
uv run --frozen pytest -q tests/test_e2e_public_roundtrip.py -k bounded-command-double
```

已安装 `latexmk` 和 `latexdiff` 时，运行真实编译闭环：

```console
uv run --frozen pytest -q tests/test_e2e_public_roundtrip.py -k installed-latexmk-latexdiff
```

两者都使用完全自制、Apache-2.0、中英双语 E0 fixture，实际跑过导出、
Word 修订、归档、审批、回填、核验、账本和审计包。

若使用完整 GitHub 源码 checkout，希望看到每一步真实 CLI receipt 和可检查产物，可运行
`scripts/run_public_e0_cli_demo.py`；完整说明见
[`docs/tutorial-public-e0.md`](docs/tutorial-public-e0.md)。该演示从本次导出的 DOCX 动态
生成一条合成修订，因此 bookmark 与 SourceMap 保持同一运行绑定。wheel/sdist 不分发
测试 fixture 和演示脚本，避免把测试文档二进制混入安装包。

## 实际工作流

```console
latex-word-review new-run
latex-word-review snapshot --help
latex-word-review export --help
latex-word-review archive --help
latex-word-review ingest --help
latex-word-review approve serve --help
latex-word-review plan --help
latex-word-review apply --help
latex-word-review verify --help
latex-word-review ledger --help
latex-word-review bundle --help
```

命令故意使用显式输入/输出和 sealed JSON 对象，不猜测用户的项目目录。建议
运行目录、每一步的完整命令面、退出码和两道闸门见
[CLI 与运行目录契约](docs/reference/cli.md)。逐条浏览器审批见
[本地审批服务](docs/reference/review-server.md)。

## Codex Skill

仓库内的 [`skills/latex-word-review/`](skills/latex-word-review/) 是同一 CLI 的薄编排层。
它负责建立只读边界、按顺序调用命令、在逐条审批和应用前停在正确闸门，并汇报可恢复
状态；它不包含转换、OOXML 解析、审批或补丁业务逻辑。将该目录作为 Skill 安装后可用
`$latex-word-review` 触发。核心 Python 包仍须单独安装并以 CLI 输出为权威。

Skill 不会把“处理返回稿”解释成默认接受全部修订。用户须逐项决定，或明确委托一条
精确审批策略；即使 ApprovalSet 已 final，执行 `apply` 仍需要第二个独立指令。两种
授权情形的独立验证见 [Skill forward-test record](docs/reviews/codex-skill-forward-test.md)。

## 项目结构

- `src/latex_word_review/`：独立 Python 库、CLI、Schema 和后端适配器。
- `tests/fixtures/e0-minimal-paper/`：可重建的公开 DOCX/LaTeX 契约语料。
- `tests/`：单元、安全、契约、CLI 集成和公开端到端测试。
- `docs/adr/`：上游 adopt/wrap/contribute/self-build 决策。
- `docs/reference/`：公开契约与安全不变量。
- `skills/latex-word-review/`：只编排 CLI 的薄 Codex Skill。
- `samples/private/`：仅本地压力测试，Git 默认忽略。

## 支持与已知限制

Windows 支持矩阵见 [platform-support.md](docs/compat/platform-support.md)，功能边界见
[v0.1-scope.md](docs/compat/v0.1-scope.md)，上游选型和许可证证据见
[upstream-dependency-matrix.md](docs/compat/upstream-dependency-matrix.md)，冻结依赖的漏洞、
许可证与供应链审计见
[dependency-supply-chain-audit.md](docs/reviews/dependency-supply-chain-audit.md)。

当前最重要的限制是：自动应用只覆盖精确定位的纯正文；不承诺任意宏、
自定义类、复杂表格或特定期刊模板能无损导出；Word 审阅稿是派生副本，不是
可反向覆盖的第二权威源。Linux 和 macOS 上的问题不会作为本项目发行阻断项，维护者也
不承诺为这些系统提供安装、兼容或故障排查支持。

## 开发与贡献

```console
uv sync --frozen --group fixture --python 3.12
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest --cov=latex_word_review --cov-report=term-missing
python scripts/qa_e0_public_fixture.py --output-dir build/fixture-qa
```

提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 和
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。可复现的非敏感缺陷与兼容性问题请提交
到 [GitHub Issues](https://github.com/JIE-jiee/latex-word-review/issues)；安全问题不要公开
披露，应使用 [GitHub 私密漏洞报告](https://github.com/JIE-jiee/latex-word-review/security/advisories/new)。
代码、原创文档和公开 fixture 在 [Apache License 2.0](LICENSE) 下发布。
