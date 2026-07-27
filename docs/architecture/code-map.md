# 代码地图与维护边界

这份地图回答两个问题：一个功能应该改哪里，以及改完后先跑哪一级检查。项目仍采用一套
Python 核心；CLI、本机网页应用、冻结 Windows 程序和 Codex Skill 都编排同一套业务实现，
不是四套互相复制的产品。

## 依赖方向

代码应大致保持下面的单向依赖：

```text
入口与界面
  -> 应用编排
    -> 工作流与领域能力
      -> 领域与安全原语
        -> Schema / 纯数据契约
```

- 上层可以组合下层；下层不得为了页面按钮或 CLI 输出反向导入上层。
- 本机网页层应调用 `ApplicationSession`，不要绕过它直接拼运行目录或修改密封对象。
- `workflow.py` 负责阶段编排，不应重新实现 DOCX、LaTeX 或图片解析算法。
- 后端只通过 `backends/base.py` 的能力接口接入；先评估上游，再新增自研转换逻辑。
- 安全常量、Schema 和错误码只能有一个权威定义，调用方不得各自复制。
- Skill 和 Plugin 只编排 CLI，不承载唯一业务实现。

## 模块职责

| 区域 | 主要文件 | 职责 |
| --- | --- | --- |
| 入口与界面 | `cli.py`、`__main__.py`、`app_server.py`、`app_views.py`、`app_presenter.py` | 参数、HTTP/CSRF、页面渲染和用户可见信息 |
| 应用编排 | `application.py`、`app_jobs.py`、`worker_dispatch.py`、`session_status.py` | 四步产品旅程、后台任务、恢复和操作门面 |
| 工作流编排 | `workflow.py`、`workflow_objects.py` | snapshot、export、receive、approve、plan、apply、verify、bundle 的阶段连接 |
| 项目与运行环境 | `discovery.py`、`snapshot.py`、`doctor.py`、`runtime.py`、`frozen_runtime.py` | 主文件发现、只读快照、工具探测和冻结运行时 |
| 导出与后端 | `export.py`、`export_models.py`、`export_limits.py`、`backends/`、`tex2word_compat.py` | LaTeX→Word 后端合同、超时和结果规范化 |
| LaTeX 源证据 | `source_units.py`、`source_features.py`、`revision_macros.py` | 正文单元、复杂结构盘点和已有批改宏受限扫描 |
| Word 展示与结构 | `revision_display.py`、`review_layout.py`、`review_reference.py`、`docx_anchor.py`、`word_fields.py`、`word_semantics.py` | 蓝色批改展示、参考样式、锚点、域和 Word 语义 |
| 图片链路 | `graphic_targets.py`、`image_materializer.py`、`image_overlay.py`、`_image_worker.py` | 图片定位、格式物化、PDF overlay 和隔离 worker |
| 返回稿读取 | `ingest.py`、`revisions.py`、`docx_reader.py`、`offset_mapping.py` | 原件归档、修订/批注提取和精确源位置映射 |
| 审批与回填 | `approval.py`、`review_server.py`、`planner.py`、`applier.py` | 决策记录、补丁计划、第二道闸门和局部原子应用 |
| 核验与交付 | `latex_verify.py`、`ledger.py`、`bundle.py`、`support_bundle.py` | 编译/引用核验、账本、交付包和支持信息 |
| 共享领域值与运行布局 | `domain_values.py`、`run_layout.py` | 跨界面共享的值对象，以及运行目录中产物路径的唯一权威定义 |
| 领域与安全原语 | `contracts.py`、`schema_catalog.py`、`canonical.py`、`jsonio.py`、`hashing.py`、`paths.py`、`ids.py`、`errors.py` | 不变量、规范化序列化、哈希、路径边界、标识与稳定错误码 |
| 公共合同资产 | `schemas/`、`assets/` | 版本化 JSON Schema 和随包哈希绑定的静态资产 |

## 常见改动从哪里开始

| 想改的功能 | 先看 | 第一轮测试 |
| --- | --- | --- |
| 页面、按钮、任务恢复 | `application.py`、`app_server.py`、`app_views.py` | `test_application.py`、`test_app_server.py`、`test_app_views.py` |
| LaTeX 转 Word、超时、后端 | `export.py`、`export_limits.py`、`backends/` | `test_export_backends.py`、`test_export_timeout_contract.py` |
| 已有 `\added`/`\deleted`/`\replaced` 展示 | `revision_macros.py`、`revision_display.py` | `test_revision_macros.py`、`test_revision_display_workflow.py` |
| 图片、PDF 截图或 overlay | `image_materializer.py`、`image_overlay.py` | `test_image_materializer.py`、`test_image_overlay.py` |
| 返回 Word 无法识别 | `ingest.py`、`revisions.py`、`docx_reader.py` | `test_revision_ingest.py`、`test_ingest_archive.py` |
| 自动回填范围 | `offset_mapping.py`、`planner.py`、`applier.py` | `test_offset_mapping.py`、`test_plan_apply.py` |
| 项目发现或“越界引用” | `discovery.py`、`paths.py`、`snapshot.py` | `test_discovery.py`、`test_paths_hashing.py` |
| Word 排版 | `review_reference.py`、`review_layout.py`、`word_fields.py` | `test_review_reference.py`、`test_review_layout.py`、`test_word_fields.py` |
| 错误文案 | `errors.py`、`user_messages.py`、`app_presenter.py` | `test_user_messages.py`、`test_app_presenter.py` |
| 打包或双击启动 | `scripts/bootstrap-windows.ps1`、`packaging/`、`installer/` | `test_windows_bootstrap.py`、`test_windows_packaging.py` |

## 三层检查

日常小改先运行：

```powershell
.\scripts\check.ps1 -Profile Quick
```

`Quick` 从 Git 的未暂存、已暂存和未跟踪文件中选择相关测试，只对本轮 Python 文件做
Ruff、格式和 mypy 检查。它不会启动真实 Microsoft Word、系统浏览器或安装器。若需要复现
映射而不执行命令，可使用 `-ListOnly -ChangedPath <path>`。

准备提交或合并前运行：

```powershell
.\scripts\check.ps1 -Profile Full
```

`Full` 对齐本地全量质量门：离线锁文件、仓库边界、Ruff、mypy、发布工具自测和带分支覆盖率
的完整 pytest。真实 Word、Edge、MiKTeX 和安装器证据仍由明确的 opt-in QA 或 GitHub Actions
提供，不能把环境 skip 描述为通过。

已有 Windows 候选构建完成后，只调用权威验证器：

```powershell
.\scripts\check.ps1 -Profile Release -ReleaseRoot build\windows-release
```

`Release` 不复制发布逻辑，也不负责构建或安装；它直接转交给
`scripts/verify-windows-release.ps1`。正式候选的构建、许可证和远程晋级仍以
`docs/release/release-process.md` 为准。

## 测试层次

1. **单元/合同测试**：一个模块或一个不变量，默认快速、无外部程序。
2. **工作流测试**：使用自制 fixture 和 fake tools 串联阶段，仍不得触碰私人论文。
3. **公开 E0 闭环**：验证可再分发样本及安装后核心流程。
4. **环境 QA**：真实 Word COM、系统 Edge、MiKTeX 和安装器，必须显式 opt-in。
5. **发行门禁**：冻结候选、隐私/许可证、确定性与 GitHub Actions 证据。

修复缺陷时先在最小层增加能失败的测试，再向上补一项跨层回归；不要用一次完整真实论文
转换代替可重复的最小测试。
