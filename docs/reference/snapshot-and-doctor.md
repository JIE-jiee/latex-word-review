# 项目发现、不可变快照与环境诊断

状态：`v0.1` 已实现的生产库与 CLI。Python API、CLI、测试和未来 Codex Skill
共享同一组路径、哈希、发现、快照和诊断边界。

## 设计目标

这一层只做四件事：验证可移植相对路径、确定性发现 LaTeX 项目、从只读来源发布
不可变快照，以及只读探测本机工具。它不安装依赖、不运行转换、不修改源项目，也
不把本机绝对路径写入 manifest 或 doctor 报告。

## CLI 入口

当前安装包已接入真实命令，而不只是保留接口：

```console
latex-word-review doctor [--cwd PROJECT_COPY]
latex-word-review snapshot SOURCE DESTINATION --run-id RUN_ID --manifest-out SOURCE_MANIFEST.json [--main main.tex]
```

`doctor` 默认把 canonical JSON 报告写到 stdout，不需要 `--json` 开关；必需工具
阻断时返回退出码 `3`。`snapshot` 发布只读快照，并把通过 v1alpha Schema 的
`SourceManifest` 写入一个调用方指定、尚不存在的 JSON 文件。两条命令都只检测或
复制，不安装工具，也不修改来源。

公共领域契约仍以
[`docs/architecture/domain-contracts.md`](../architecture/domain-contracts.md) 为准。
本页的 `snapshot-manifest.json` 是快照内部清单；它不是伪装成完整
`SourceManifest` envelope 的替代物。后续编排层必须显式构造并校验领域对象。

## 路径边界

`validate_relative_path()` 接受以 `/` 分隔的非空项目相对路径。下列输入会按稳定
错误码 fail-closed：

- POSIX 绝对路径、Windows 盘符路径、盘符相对路径和 UNC 路径；
- `.`、`..`、空路径段、反斜杠、NUL 和控制字符；
- Windows 保留设备名、冒号，以及以空格或句点结尾的路径段。

`resolve_within()` 在验证字符串后解析真实路径；符号链接或 junction 一旦把目标带出
允许根目录，就返回 `E_PATH_LINK_ESCAPE`。`ensure_disjoint_roots()` 还拒绝快照目标与
来源相等、互相包含，或目标自身及其已有祖先是符号链接/junction。

调用 API 时传入的 `Path` 根目录是进程本地控制参数，可以是绝对路径；任何持久化或
可序列化的文件引用都必须是经验证的相对路径。这一区分避免把个人目录误写入审计
产物。

## 确定性项目发现

入口：

```python
from pathlib import Path

from latex_word_review.discovery import discover_project

project = discover_project(Path("project-copy"), main_document="main.tex")
print(project.source_tree_sha256)
```

主文档选择顺序为：调用方明确指定；否则使用根目录的 `main.tex`；否则仅当根目录
恰有一个同时包含 `\documentclass` 和 `\begin{document}` 的顶层 `.tex` 文件时选中。
缺失或歧义都会阻止继续。

发现器递归处理以下静态、显式引用：

- `\input{...}` 和 `\include{...}`；
- `\bibliography{...}` 和 `\addbibresource{...}`；
- `\includegraphics{...}` 与静态 `\graphicspath{{...}}`；
- 前 20 行中的 `% !TeX program = ...` 引擎提示。

未带后缀的 TeX、BibTeX 和图片引用使用固定候选后缀。图片引用只有在末尾是
已知图形后缀（`.pdf`/`.png`/`.jpg`/`.jpeg`/`.eps`/`.svg`）时才视为显式后缀；
文件基名中的句点不会阻止后缀搜索。对未知的表面“后缀”会同时尝试原名和追加
固定图形后缀；若多个真实文件同时命中，仍以 `ambiguous` fail-closed。普通 TeX 引用与
`\graphicspath` 均相对于主进程工作目录（项目源根）解析；这与导出适配器固定的
`cwd` 以及标准 TeX 的文件查找语义一致，不会在处理 `\input` 时暗中切换到被包含
文件的目录。多个真实候选会标记为
`ambiguous`，缺失候选会标记为 `missing`。绝对、穿越、动态宏、越界链接和不安全
`\graphicspath` 不会被猜测；引用只以相对值或 `unsafe-path:<fingerprint>` 出现在
`external_references`，并阻止快照。

当前是有意保守的静态发现器，不展开宏、条件分支、`TEXINPUTS`/kpathsea、shell
escape、运行时生成文件、glob、`\import`/`subfiles`，也不枚举本地 `.sty`、`.cls`
或未被上述命令直接引用的资产。需要这些能力的项目必须由后续版本扩展发现 profile，
不能靠宽松 fallback 静默放行。

默认资源上限为：

| 限制 | 默认值 |
| --- | ---: |
| 发现文件数 | 2,048 |
| 单文件字节数 | 16 MiB |
| 全部发现文件字节数 | 256 MiB |
| 每个 TeX 文件的依赖引用数 | 256 |

根目录 fallback 枚举也受文件数上限约束。TeX 与 BibTeX 文本必须是严格 UTF-8；文件
在读取前后用设备、inode、大小和纳秒 mtime 指纹检查漂移。

## 文件与树哈希

`digest_file()` 对原始字节计算带 `sha256:` 前缀的 SHA-256，并同时绑定字节数。
换行符不会被转换。`source_tree_sha256()` 先按规范化相对路径排序，每项只纳入：

```text
path, role, size_bytes, sha256
```

随后使用项目公共 `rfc8785` 实现生成 RFC 8785 canonical JSON，再计算 SHA-256。
mtime、inode、本机根目录和遍历顺序都不进入树哈希；重复路径会被拒绝。

## 不可变快照

入口：

```python
from pathlib import Path

from latex_word_review.snapshot import snapshot_project

result = snapshot_project(
    Path("readonly-source-copy"),
    Path("runs/run-001/snapshot"),
)
```

发布流程如下：

```mermaid
flowchart LR
    A["发现并哈希只读来源"] --> B["取得目标同级排他锁"]
    B --> C["再次发现，拒绝漂移"]
    C --> D["复制到同文件系统临时目录"]
    D --> E["逐文件复核原始字节哈希"]
    E --> F["写入 canonical manifest"]
    F --> G["再次复核来源并设为只读"]
    G --> H["原子 rename 发布"]
    H --> I["复核来源、manifest 与快照文件集"]
```

临时目录与目标同级，因此正常发布的 rename 不跨文件系统。任何复制、来源漂移、哈希
或发布失败都会清理本次调用拥有的临时树；不会删除预先存在的陌生目标。目标已存在时
只允许两种结果：manifest、文件集、逐文件哈希和只读位完全匹配则幂等复用；否则返回
冲突，绝不覆盖。快照目录、文件和 manifest 的 owner 写位都会移除，输出树不包含
符号链接或 junction。

`snapshot-manifest.json` 包含：格式版本、内容派生的 `source_manifest_id`、主文档、
树哈希、相对文件清单、依赖边、引擎提示、外部引用、发现 profile 哈希和来源前后哈希。
manifest 使用 canonical JSON 加单个结尾换行；不记录来源根或目标根。

只读权限是防误写层，不是操作系统级不可撤销存储。具备目录管理权限的调用者仍可修改
权限。排他锁阻止遵守本协议的并发发布；当前不把一个主动恶意且能同时改写目标父目录的
本地进程视为受支持威胁模型。rename 提供原子可见性，但本阶段未承诺断电后的目录项
持久化（没有跨平台目录 `fsync` 保证）。

## 只读环境诊断

`diagnose_environment()` 只查找、哈希并以 `--version` 探测工具，不执行安装、升级、
配置写入或网络访问：

```python
from pathlib import Path

from latex_word_review.doctor import diagnose_environment

report = diagnose_environment(cwd=Path.cwd())
serializable = report.as_dict()
```

默认必需项是 `tex2word` 可执行文件、`tex2word` Python 包和 `lxml` Python 包。
`tex2word 1.0.5` 没有 `--version` 选项，因此其可执行入口用无副作用的 `--help`
探活，精确版本由同一环境的 distribution metadata 提供。Pandoc、
`pandoc-crossref`、`latexmk`、`latexdiff`、TeX 引擎、Biber、Tectonic 和 LibreOffice
作为可选能力报告。必需项缺失时状态为 `blocked`；仅可选项缺失时为 `degraded`；全部
可用时为 `pass`。

子进程永不经过 shell，stdin 关闭，普通探测默认超时 5 秒，允许范围为 `(0, 60]`
秒；Biber 的独立发行版冷启动需要解包 PAR runtime，因此使用显式 60 秒预算。stdout
和 stderr 各自默认最多读取 1 MiB、最多允许 16 MiB。输出按 UTF-8 解码，非法序列用
替换字符表示，NUL 也被替换。墙钟预算从启动运行器开始计算；Windows launcher 的冷启动
和 Job 纳管时间也包含在内。

每个工具都在 doctor 自己创建并清理的临时工作目录中运行；`TEMP`、`TMP` 与
`TMPDIR` 也只指向该目录，绝不回落到 `--cwd` 指定的项目目录。这样 Biber/MiKTeX
的 `par-*` 缓存或 `mik*.tmp` 只会成为本次探测的临时数据。Windows 先启动一个在
内部 gate 上等待的受控 Python launcher；只有 launcher 成功纳入启用 kill-on-close
的 Job Object 后，运行器才放行它启动真实工具。这消除“工具先启动孙进程并退出、
随后才 assign Job”的竞态；Job 创建或分配失败时会在 gate 放行前 fail closed，不会
降级成裸跑。`taskkill /T` 与直系进程终止仍作为已受控 launcher 的清理回退。
POSIX 使用独立 session/process group。超时、输出超限，或直系进程已经退出但孙进程
仍持有输出管道时，运行器都会终止这个隔离树。

命令只有在直系进程结束且两个输出 reader 都已收束后才算完成。reader 使用无缓冲原始
管道，清理路径不会尝试关闭另一个线程正在持锁读取的 `BufferedReader`。因此继承输出
管道的孙进程不能绕过墙钟超时或把诊断卡成读取线程内部错误。此类结果正常记录为
`timed_out`，或作为输出不完整的 `E_TOOL_VERSION_UNSUPPORTED`，而不是
`E_INTERNAL_INVARIANT`。Job Object、process group 和 `taskkill` 都是同权限下的进程
收束机制，不替代针对恶意程序的 OS/container sandbox。

探测环境只透传启动所需的少量系统变量，并强制 UTF-8。`run_command()` 的调用方如未
显式传入环境，也会获得一个独立临时根，而不会把临时变量默认指向执行 `cwd`。

doctor 序列化结果只保存稳定工具名、必需性、状态、提取出的版本、可执行文件哈希、
探测输出哈希和错误码。它不保存可执行文件路径、工作目录或原始 stdout/stderr，因此
不会因常见版本输出而把个人安装目录带入报告。底层 `run_command()` 返回的原始输出供
调用方即时处理，调用方不得不加审查地把它写入公共日志。

## 失败语义与测试证据

这一层复用公共 `ContractError`/`ErrorCode`：格式或资源问题使用
`E_SCHEMA_INVALID`，路径问题使用 `E_PATH_ABSOLUTE`、`E_PATH_TRAVERSAL` 或
`E_PATH_LINK_ESCAPE`，来源漂移/快照冲突使用 `E_HASH_SOURCE_MISMATCH`，工具缺失或
不支持使用 `E_TOOL_MISSING`/`E_TOOL_VERSION_UNSUPPORTED`。

合成测试覆盖 POSIX/Windows/UNC 路径、穿越和 NUL、真实或模拟的 symlink/junction
逃逸、Unicode、树哈希顺序、资源上限、依赖发现、不安全引用脱敏、非 UTF-8 BibTeX、
来源漂移、幂等复用、目标冲突、只读位、缺失工具、UTF-8 输出、Biber 冷启动预算、
doctor 工作目录不变，以及超时/输出超限时终止持有管道的合成孙进程。另有回归用例
覆盖“直系父进程立即成功退出、孙进程继续持有管道”：运行器须及时返回，并验证孙进程
不再存活。测试不读取私人论文或导师返回文件。
