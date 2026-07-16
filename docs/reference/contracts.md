# 公共契约

状态：`v1alpha`，当前精确版本 `1.0.0-alpha.1`。

本页说明已实现的可执行契约层。字段名、类型、必填项、枚举和未知字段策略以包内
`latex_word_review.schemas.v1alpha` 的 JSON Schema 为规范来源；跨对象哈希与语义
不变量由 `contracts.py` 的校验器执行。领域含义、状态机和安全理由见
[`docs/architecture/domain-contracts.md`](../architecture/domain-contracts.md)，它是
语义说明而不是另一套 Schema。Python 导入稳定性见
[`docs/reference/python-api.md`](python-api.md)。

## 实现边界

- JSON Schema 使用 Draft 2020-12，并由 `jsonschema>=4.26,<5` 验证。
- 授权哈希使用 RFC 8785 JSON Canonicalization Scheme，并由
  `rfc8785>=0.1.4,<0.2` 生成；没有自研或宽松 fallback。
- 所有核心 envelope 和安全关键 payload 都设为
  `additionalProperties: false`。非安全关键扩展只能放入顶层
  `extensions`，key 必须采用反向域名形式。
- 默认只接受精确版本 `1.0.0-alpha.1`。未知主版本返回
  `E_SCHEMA_MAJOR_UNSUPPORTED`；同主版本但未实现的版本也拒绝进入业务对象。
- Schema 通过不等于可写：`validate_contract()` 还会检查不可变原件、范围、
  ID 唯一性、计数、审批完备性、PatchPlan 绑定、verify 对账和 bundle 清单等
  语义不变量，最后复核 payload/document 两层哈希。

## 版本化对象

包内 `latex_word_review.schemas.v1alpha` 提供 12 个入口 Schema：

1. `RunManifest`
2. `SourceManifest`
3. `BackendCapabilities`
4. `ReviewIR`
5. `SourceMap`
6. `ExportReport`
7. `RawRevisionEvent`
8. `ChangeSet`
9. `ApprovalSet`
10. `PatchPlan`
11. `VerificationReport`
12. `AuditBundle`

它们共享严格的 `ToolIdentity`、`ArtifactRef`、`SourceLocation`、`Diagnostic`
等定义。`RawRevisionEvent` 的独立 envelope 与 `ChangeSet.raw_events` 使用同一
`RawRevisionEventData` 定义，避免 sidecar 和内嵌表示漂移。

## Envelope 与哈希

每个对象固定包含：

```text
schema_name, schema_version, object_id, run_id, generated_at,
producer, payload, integrity, extensions
```

- `integrity.payload_sha256` 是 JCS(`payload`) 的 SHA-256，供下游授权绑定。
- `integrity.document_sha256` 是 JCS(整个 envelope，但移除该字段自身) 的
  SHA-256，覆盖生成时间、producer、extensions 等封装元数据。
- 哈希字符串固定为 `sha256:` 加 64 个小写十六进制字符。
- `seal_envelope()` 总是返回深拷贝并重新计算两层哈希；它不能让不安全对象
  合法化，调用方随后仍须执行 `validate_contract()`。

`SourceManifest.source_tree_sha256` 使用按相对路径排序的记录数组计算，每项只含
`path`、`role`、`size_bytes` 和原文件字节 SHA-256，不混入本机绝对路径、mtime
或 inode。

## 最小 Python 用法

```python
from latex_word_review import load_contract_json, validate_contract

document = load_contract_json(contract_bytes)
receipt = validate_contract(document)
print(receipt.schema_name, receipt.payload_sha256)
```

创建对象时使用 `make_envelope()`；它会构造、密封并立即执行同一套完整验证：

```python
from latex_word_review import make_envelope

document = make_envelope(
    schema_name="ApprovalSet",
    object_id=approval_set_id,
    run_id=run_id,
    generated_at="2026-01-16T09:30:00+08:00",
    producer=tool_identity,
    payload=approval_payload,
)
```

读取 Schema：

```python
from latex_word_review import available_schema_names, load_schema

for name in available_schema_names():
    schema = load_schema(name)
```

核验下游字段是否真正绑定上游 payload，而非仅检查字符串格式：

```python
from latex_word_review import verify_payload_binding

verify_payload_binding(source_map, "source_manifest_sha256", source_manifest)
```

## 稳定 ID

`stable_id(prefix, semantic_input)` 对 JCS 语义输入取 SHA-256，默认保留前
128 bit。内容 ID 不附加随机序号；发生碰撞时应使用更长摘要。包同时提供
`derive_source_manifest_id()`、`derive_unit_id()`、`derive_raw_event_id()`、
`derive_change_id()`、`derive_diagnostic_id()` 和 `derive_artifact_id()`。
`new_run_id()` 生成 UUIDv7，只用于一次运行身份，不承诺跨运行稳定。

## 解析与错误处理

`load_contract_json()` 只接收 UTF-8 JSON，拒绝重复 key、`NaN` 和 Infinity。
`ContractError` 提供稳定的 `code`、`path`、`exit_code` 与 `as_dict()`；人类消息
不是兼容接口。未知 payload 字段、未知安全枚举和未知 PatchOperation kind 使用
`E_SCHEMA_UNKNOWN_SECURITY_FIELD`，不会猜测或默认接受。

两层 envelope 完整性与声明值不一致时使用 `E_HASH_INTEGRITY_MISMATCH`；它与
`E_HASH_SOURCE_MISMATCH`、`E_HASH_CHANGESET_MISMATCH` 等特定上游绑定错误不同，
统一映射到不可信输入退出类别 `4`。

常用退出类别：Schema/输入 `2`、环境 `3`、不可信输入/哈希 `4`、导出 `5`、
ingest `6`、审批/计划 `7`、apply `8`、verify/bundle `9`、内部不变量 `10`。

## 测试证据

`tests/test_contracts.py` 覆盖：

- 12 种最小 golden contract；
- RFC 8785 canonical/hash golden vector；
- 缺字段、未知安全字段、绝对路径与路径穿越；
- payload 和封装元数据篡改；
- 未知主版本、未实现的同主版本；
- 未知 Word revision kind 与未知 PatchOperation kind；
- SourceManifest 文件替换后重新密封仍因 source-tree 绑定失败；
- 上下游 payload 绑定替换、final 审批隐藏未决定项；
- JSON 重复 key 与非有限数值。

这些测试使用完全合成的数据，不读取私人论文或返回 Word 原件。
