# Python API 与稳定性边界

状态：包版本 `0.1.0b1`（beta candidate，尚未公开发布）；领域 Schema 版本
`1.0.0-alpha.1`（`v1alpha`）。Python API 已可运行并有测试覆盖，但尚未承诺 1.0
级别的签名稳定性。完整工作流的首选自动化入口仍是 CLI；Python API 适合需要在同一
进程内组合、验证或测试底层能力的调用方。

## 兼容性层级

本项目把“机器契约稳定”与“Python 函数签名稳定”分开：

| 层级 | 保证 | 变更规则 |
|---|---|---|
| 契约稳定（v1alpha） | 12 个 Schema 的对象名/版本、稳定错误码、退出码类别、哈希算法与 fail-closed 含义 | 不兼容变更必须使用新 Schema 版本；不得静默放宽旧版本 |
| 支持的 alpha API | 根包 `latex_word_review.__all__` 中的名称 | pre-1.0 期间可演进；有意的不兼容变更必须写入 changelog 并同步文档/测试 |
| 实验工作流 API | 下表标记为 experimental 的模块级 `__all__` | 可在 alpha 版本间调整参数或返回类型；应固定包版本并先跑契约测试 |
| 私有实现 | 以下划线开头的名称/模块，以及未列入任何 `__all__` 的对象 | 无兼容保证，不得由 Skill 或第三方集成直接依赖 |

JSON 字段、类型、枚举、必填项和未知字段策略的规范来源是打包的
`schemas/v1alpha/*.schema.json`，应通过 `load_schema()` 读取；不要复制文档中的示例
来另造 Schema。跨对象哈希与语义不变量由 `validate_contract()` 执行。详见
[`contracts.md`](contracts.md) 与
[`domain-contracts.md`](../architecture/domain-contracts.md)。

## 模块边界

| 模块 | 稳定性 | 面向调用方的范围 |
|---|---|---|
| `latex_word_review` | supported alpha façade | 只使用根包 `__all__`；包含契约、错误、ID、路径、哈希、发现、doctor、runtime 和 snapshot 的常用类型/函数 |
| `canonical`, `contracts`, `errors` | contract-stable semantics；supported alpha signatures | canonical JSON、envelope 密封/核验、Schema 加载/校验、稳定错误和退出码 |
| `ids`, `paths`, `hashing` | supported alpha | 稳定内容 ID、可移植路径、文件/树哈希；优先从根包导入 |
| `discovery`, `doctor`, `runtime`, `snapshot` | supported alpha | 项目发现、只读诊断、受限子进程与不可变快照；优先从根包导入 |
| `backends`, `export`, `export_models`, `inspection`, `source_units` | experimental | 转换适配器、锚点、DOCX 结构验收和正文单元 |
| `docx_reader`, `revisions`, `ingest` | experimental | 只读 OOXML 证据读取、修订规范化和返回 Word 归档 |
| `approval`, `review_server`, `planner`, `applier` | experimental | 逐条审批、仅本机 UI、dry-run 计划和新工作副本应用 |
| `latex_verify`, `ledger`, `bundle`, `workflow_objects`, `jsonio` | experimental | 编译/差异核验、账本、审计包、领域对象构造和 no-clobber JSON I/O |
| `cli` | CLI surface supported；Python embedding experimental | 支持安装后的 `latex-word-review` 命令；`build_parser()`/`main()` 不作为长期嵌入协议 |
| `schemas`, `__about__`, `__main__` | internal package layout | 通过公开加载器、`__version__` 或 console script 使用，不依赖文件布局 |
| `backends._tex2word_worker` 及任意 `_name` | private | 子进程隔离和实现细节，无兼容保证 |

模块标为 experimental 不表示不安全或未经测试，而是表示 Python 签名尚未冻结。它们
仍执行与 CLI 相同的 Schema、哈希、路径和 fail-closed 检查；实验调用方不得绕开
这些校验，也不得把私有 helper 当作授权接口。

## 推荐导入方式

契约与基础设施优先从根包导入：

```python
from latex_word_review import (
    ContractError,
    ErrorCode,
    discover_project,
    load_contract_json,
    snapshot_project,
    validate_contract,
)

document = load_contract_json(raw_json)
receipt = validate_contract(document)
print(receipt.schema_name, receipt.payload_sha256)
```

需要实验工作流函数时，从所属模块显式导入并固定 beta 候选包版本：

```python
from latex_word_review.approval import create_approval_set, record_decision
from latex_word_review.planner import plan_patch
```

不要导入私有名称、直接打开包内 Schema 路径、调用
`latex_word_review.backends._tex2word_worker`，或依赖异常消息文本。集成应判断
`ContractError.code`/`exit_code`，持久化对象必须先通过 `validate_contract()`。

## 发布前的 API 检查

修改公共 Python 表面时，维护者至少应：

1. 比较根包与相关模块的 `__all__`，确认新增/移除是有意的；
2. 对 Schema 或错误码变更执行契约兼容评审，而不只运行类型检查；
3. 同步本页、对应 reference 文档、测试和 changelog；
4. 从构建出的 wheel/sdist clean install 后运行导入 smoke test；
5. 在 1.0 前明确冻结哪些 experimental 模块升级为稳定公共 API。
