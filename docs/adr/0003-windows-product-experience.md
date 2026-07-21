# ADR-0003：Windows 本机产品体验与分发

- 状态：Accepted
- 日期：2026-07-17
- 范围：Windows 普通用户入口、产品编排、本机界面、冻结运行时和安装资产

## 背景

latex-word-review 已有可审计的底层闭环，但 0.1 Beta 把运行目录、ApprovalSet、
PatchPlan 和多条 CLI 命令直接暴露给用户。该接口适合开发者与自动化，却不满足“安装后选择
main.tex，经四步完成审阅”的普通用户目标。

本轮不重写转换、修订提取、审批、补丁或核验核心。产品层必须继续服从以下不变量：

1. 原始 LaTeX 与返回 Word 原件保持不变。
2. 审批和应用是两道独立的人类闸门。
3. 只有密封对象、精确来源位置和安全策略共同允许的正文修改才可自动应用。
4. Windows 是唯一支持和发布平台。
5. 核心库/CLI 是权威实现；GUI 和 Codex Skill 只编排同一套 API。

## 上游复用核查

核查日期为 2026-07-17。下表中的版本只描述核查证据；实际构建仍须在锁文件和发行记录中
固定精确版本与哈希。

| 上游 | 许可与维护证据 | 决策 | 本项目仍需实现的差距 |
|---|---|---|---|
| [OpenRefine](https://github.com/OpenRefine/OpenRefine) | BSD-3-Clause；2026 年仍有 Windows installer/ZIP 和活跃发布 | adopt 其“本机服务 + 默认浏览器 + 项目主页”交互模式，不复制代码 | LaTeX/Word 领域状态、安全闸门、文件白名单和会话恢复 |
| [PyInstaller](https://pyinstaller.org/en/stable/) | GPL-2.0 加 Bootloader Exception，官方明确允许分发生成的独立程序 | adopt 为 Windows build-only 工具；先 onedir，后续不把 onefile 作为默认 | 冻结 worker、自启协议、包数据、外部 DLL 环境和真实 bundle 测试 |
| [Inno Setup](https://github.com/jrsoftware/issrc) | Inno Setup License；允许使用、修改和再分发，维护中的 Windows 安装器项目 | adopt 为当前用户安装器生成工具 | 固定 AppId、升级/卸载合同、安装资产清单、签名与 CI |
| [Microsoft Playwright](https://github.com/microsoft/playwright) | Apache-2.0；活跃发布并支持 Windows 浏览器自动化 | adopt 为测试专用 E2E 工具，不进入运行安装包 | 四步用户旅程、可访问性、恢复与安全负例 |
| tex2word、PDFium、Pillow | 已在 ADR-0001 和成熟化复用审查中评估 | 继续 wrap/adopt | 冻结包数据、内部 worker smoke probe 和安装态 doctor |

## 决策

### 1. 普通用户入口

提供一个 Windows 启动器。无参数启动时打开本机中文控制台；高级用户仍可调用现有 CLI。
控制台由只绑定 127.0.0.1 随机端口的本机 HTTP 服务提供，并在默认浏览器打开。

用户主流程固定为：

1. 选择 main.tex 并完成环境预检。
2. 生成并交付审阅 Word。
3. 导入返回 Word，在中文卡片中审批。
4. 查看 PatchPlan 摘要，第二次确认后生成结果。

界面默认不显示对象哈希、JSON 路径、byte span 或修订文件名；这些证据只进入“技术详情”。

### 2. 产品编排层

新增 ApplicationSession 产品门面。GUI 直接调用 Python API，不执行 CLI 子进程，也不
自行解析或修改安全 JSON。

产品层负责：

- 自动创建运行根和无覆盖的审批/计划/结果版本路径；
- 从已有密封对象重建完整生命周期状态；
- 后台执行耗时任务，并保证同一 run 同时至多一个写操作；
- 把稳定错误码映射为中文原因、仍然安全的内容和下一步操作；
- 允许关闭后继续、定向重试核验和清理工具拥有的临时产物。

最近任务索引和界面偏好不是权威证据。索引缺失或被修改时，状态必须从密封对象重新验证。

### 3. 两道闸门

第一道闸门是最终化 ApprovalSet。界面可调用既有 accept_all_safe，但只能批量采用 exact
plain_text_candidate；公式、引用、结构、move、冲突和低置信度项不得进入批量采用。

第二道闸门先展示人类可读的 PatchPlan 和逐文件差异，再要求用户明确确认。确认之前不得
调用 applier；“一键生成结果”只描述闸门之后的 apply/verify/ledger/bundle 编排。

### 4. 本机界面

使用服务端生成的语义 HTML、仓库内固定 CSS 和极少量无框架 JavaScript。禁止 CDN、
React、Electron 和运行时网络依赖。动态内容必须转义；所有写请求仍须通过精确 Host、
实例 Cookie、独立 CSRF、请求大小和并发陈旧提交保护。Origin 默认必须与本机服务完全一致；
唯一兼容例外是字面量 `Origin: null` 同时携带单一且精确的
`Sec-Fetch-Site: same-origin`，用于处理实测会把本机表单序列化为 opaque origin 的内嵌
Chromium。该例外不能绕过 Cookie 或 CSRF。Cookie 名包含当前监听端口以避免并行实例覆盖，
但这只是名称隔离；HTTP Cookie 本身不提供端口保密或端口作用域。

文件选择使用 Windows 原生对话框或启动器传入的规范化路径。浏览器端不得暴露任意路径读取
API，也不得通过“上传整个论文目录”绕开来源发现和快照合同。

### 5. Windows 分发

公开 Beta 同时生成：

- PyInstaller onedir 目录的 portable ZIP；
- 由相同 onedir 字节构建的 Inno Setup 当前用户安装器。

默认安装到当前用户目录，不请求管理员权限。安装目录只放程序；论文、运行记录和偏好进入
用户数据目录或用户明确选择的工作区。卸载不得删除用户论文或审阅记录。

安装包包含 Python 运行时、本项目、tex2word、Pillow、PDFium 和必要 Schema/模板。
Microsoft Word、MiKTeX、Pandoc、Playwright 浏览器和开发工具不打包，也不静默安装。

Beta 不实现静默自动更新。相同 AppId 的新安装器承担显式升级。没有可信 Authenticode
证书时发布页必须提供 SHA-256、SBOM/内容清单并说明 SmartScreen 边界；不得以自签名冒充
公共信任。

### 6. 冻结 worker 协议

冻结后 sys.executable 指向应用 EXE，不是可执行 -m 或 -c 的 Python。所有 tex2word、
PDF 图像和 Windows gated launcher 必须经固定 allowlist 的内部 self-spawn 子命令分派。
未知 worker、额外参数或环境漂移一律 fail closed。

开发态继续支持隔离 Python worker；冻结态使用同一 EXE 的内部入口。两种模式都必须保留
Windows Job Object、固定 argv、最小环境、超时、输出上限和父进程后验验证。

安装态 doctor 以嵌入 package/version 和内部 worker smoke probe 判断 tex2word 能力，
不得因为 onedir 没有虚拟环境 Scripts/tex2word.exe 而误报阻断。

### 7. 测试与发布

保留所有核心契约、真实 Word COM 和 MiKTeX/latexdiff 门禁，新增：

- ApplicationSession 状态/恢复/互斥/幂等单测；
- 本机 HTTP 安全、中文错误和静态资源测试；
- Playwright 四步浏览器 E2E；
- 从无 Python/Git 的 Windows 环境启动 onedir 和安装器；
- portable/install 字节与行为等价、升级/卸载、空格/CJK/长路径；
- 有/无 MiKTeX 的完整与部分完成路径；
- 冻结 worker、PDF 第二页、Accept All、Track Changes off 和 bookmark 损坏负例。

只有安装后的最终字节通过同等闭环后，才可上传 GitHub prerelease 资产。

## 未采用的方案

- **继续以 CLI 作为默认入口**：安全但不满足普通用户目标。
- **Electron 或完整原生 GUI 重写**：运行体积、供应链和维护面显著扩大，且重复现有本机页面。
- **PyInstaller onefile 作为默认**：每次解包、启动更慢，且本项目大量 self-spawn/外部工具，
  审计和故障定位成本更高。
- **MSIX/WiX 作为首个 Beta**：在签名、Store/企业部署需求出现前收益不足；保留为 1.0 后评估。
- **捆绑或静默安装 MiKTeX/Word/Pandoc**：体积、许可、网络和系统状态变化超出核心安装范围。
- **自动接受所有修改**：破坏审批责任和安全回填边界。

## 影响

底层对象和现有 CLI 保持兼容，普通用户不再手工管理它们。项目会增加产品会话、HTML/CSS、
Windows 构建脚本和测试专用浏览器依赖，但不引入运行时云服务或大型前端框架。安装资产的
许可、哈希、SBOM、签名状态和已知限制必须进入发行证据。
