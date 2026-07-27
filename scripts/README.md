# scripts

此目录只放维护、兼容性评估、fixture 生成和 Windows 发布辅助脚本。正式用户流程通过稳定
CLI 或本机网页应用提供；转换、修订提取和安全回填的唯一实现不得藏在一次性脚本中。

## 日常维护入口

```powershell
.\scripts\check.ps1 -Profile Quick
.\scripts\check.ps1 -Profile Full
```

- `Quick`：读取当前 Git 改动，检查改动过的 Python 文件并运行相关测试；不调用真实 Word、
  浏览器或安装器。
- `Full`：提交前全量质量、仓库边界、类型和 pytest 门禁。
- `Release`：已有候选构建完成后，转交给 `verify-windows-release.ps1`，不复制发布逻辑。

详细模块与测试映射见 [`docs/architecture/code-map.md`](../docs/architecture/code-map.md)。

## Windows 启动与发布

- `bootstrap-windows.ps1`：公开源码 ZIP 的 Windows 首次启动环境准备。
- `build-windows.ps1`：维护者构建冻结程序、portable ZIP 和安装器候选；内部调用权威验证器。
- `verify-windows-release.ps1`：只读核验一个已有 Windows 候选。
- `deploy-local-windows.ps1`：原子部署已经验证的本地冻结程序，并保留 `user-data`。
- `test-windows-installer.ps1`：明确 opt-in 的安装器冒烟测试。

这些脚本面向维护者，不是普通用户的并行入口。正式发布步骤与权限边界见
[`docs/release/release-process.md`](../docs/release/release-process.md)。

## 公开 E0 与 Word 合同工具

- `build_e0_public_fixture.py`：从 clean-room 生成器重建公开 fixture。
- `qa_e0_public_fixture.py`：生成公开 fixture 的结构、来源和隐私证据。
- `run_public_e0_cli_demo.py`：运行公开 E0 CLI 闭环。
- `e0_inspect_docx.py`：只读观察 DOCX 包、修订、批注、OMML、书签和关系；它是上游合同
  实验的独立预言机，不属于生产 ingest。
- `run_word_contract_qa.ps1`：明确 opt-in 的真实 Microsoft Word 合同 QA。
