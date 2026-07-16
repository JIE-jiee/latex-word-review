# ADR-0002：Windows-only 支持范围

- 状态：已接受
- 日期：2026-07-16
- 决策者：项目维护者
- 取代：ADR-0001 中的跨平台支持驱动因素；历史上游实验事实不变
- 关联：`../compat/platform-support.md`、`../release/release-process.md`

## 背景

项目的实际审阅环境、Microsoft Word 视觉验收、MiKTeX 工具链和维护资源均集中在
Windows。继续把 Ubuntu 和 macOS 列为发布阻断平台，会要求维护与核心用户场景无关的
安装器、runner 差异和外部 TeX 组合，并容易把“代码偶然可运行”误写成长期支持承诺。

维护者因此决定把公开项目的开发、CI、发行和用户支持范围收敛到 Windows。这是支持与
资源分配决策，不是删除输入安全防御，也不改写过去已经完成的上游实验和漏洞证据。

## 决策

1. 项目只维护 Windows，首批 Python 范围保持 CPython 3.12 和 3.13。
2. Linux 和 macOS 不设 CI、clean-install、兼容性、问题排查或发行阻断承诺。纯 Python
   wheel 即使能安装，或某条命令偶然可运行，也不构成支持。
3. GitHub Actions 的阻塞测试、fixture、打包和 clean-install 证据只来自 Windows runner。
4. 真实 TeX 门禁使用 Windows runner，下载 MiKTeX 官方 Setup Utility
   `miktexsetup-5.5.0+1763023-x64.zip` 并核对固定官方 SHA-256；随后无交互安装 basic
   集合，并按版本库清单显式安装/验证包含传递依赖的 29 包 E0 闭包；每次隔离 MiKTeX
   的 TeX 引擎调用显式禁用按需安装，真实测试前后还必须证明完整已安装包清单未变化。
   公开样例固定使用 `fontset=fandol`，不依赖 Runner 的可选中文系统字体。v3 证据必须
   记录安装器文件名和 SHA-256、包清单摘要、包 digest、完整清单计数/摘要、CTeX/Fandol
   资源解析结果与工具版本，并完成恰好一项真实 E2E 测试且零跳过。
5. 固定版本和工作流定义不是成功证据。只有 GitHub Actions 对精确源提交的实际通过结果
   才能满足远程门禁；在此之前不得宣称 Windows CI 已绿或创建正式 prerelease。
6. Microsoft Word for Windows 是主要审阅与人工视觉验收环境。LibreOffice for Windows
   可以作为可选交叉检查，但不构成维护依赖。
7. 项目继续拒绝 POSIX/UNC/盘符穿越、symlink、case alias、非规范归档名和其他危险输入。
   这些规则保护 Windows 用户处理不可信跨来源文件，不代表支持相应操作系统。
8. 上游文档中关于 Linux/macOS 可执行文件、历史 macOS 漏洞或既有 Ubuntu 实验的陈述可
   作为客观证据保留，但必须与当前支持声明明确区分。

## 后果

### 正面影响

- CI、文档、外部工具链和真实用户环境使用同一平台边界，支持承诺更可信。
- 维护资源集中到 Word for Windows、MiKTeX、PowerShell 和 Windows 文件系统语义。
- Linux/macOS runner 或包管理器波动不再阻断与目标用户无关的发行。

### 代价与限制

- 非 Windows 用户可能仍能运行部分纯 Python 功能，但维护者不承诺复现或修复其平台问题。
- 原先的 Ubuntu real-TeX 和 macOS Tier 2 证据不再是当前发行依据。
- Windows real-TeX runner 的 MiKTeX Setup Utility、MiKTeX 包仓库和工具版本不受
  `uv.lock` 覆盖，必须在每次门禁中分别做摘要绑定或记录并按失败关闭。

## 将来扩展平台

重新支持 Linux 或 macOS 需要新的维护者决策，以及对应的阻塞 CI、clean-install、真实
外部工具证据、支持政策、故障排查文档和发行观察。不得仅凭外部贡献、宽松依赖元数据或
一次成功安装自动扩大支持范围。
