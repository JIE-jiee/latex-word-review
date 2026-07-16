# 领域契约语义说明（v1alpha 已实现）

## 1. 文档状态与边界

状态：`implemented-v1alpha`，当前精确版本为 `1.0.0-alpha.1`。

包内 `src/latex_word_review/schemas/v1alpha/*.schema.json` 是字段名、类型、必填项、
枚举和未知字段策略的规范来源；`contracts.py` 的校验器是跨对象哈希绑定及语义不变量
的可执行来源。本文件解释领域语义和设计理由，不另行定义一套平行 Schema。若本文示例
与打包 Schema 或校验器不一致，以打包 Schema 和校验器为准，并应在同一变更中修正文档
与契约测试。

ADR-0001、上游契约实验、命名和许可证门槛均已完成，本文描述的 12 个对象已经进入生产
库与 CLI。`v1alpha` 表示可执行的预发布契约，并不等于 1.0 最终稳定；后续调整仍不得
削弱不可变来源、哈希绑定、逐条审批、默认 dry-run 和 fail-closed 原则。

本契约只描述本项目拥有的稳定审阅语义，不暴露或复制 `tex2word`、Pandoc 或其他上游的 AST、内部节点类和序列化格式。后端适配器必须把上游输出规范化为本文对象；上游原生调试结果只能作为带哈希的非公共证据产物保存。

领域对象按以下顺序形成：

```text
SourceManifest
  ├─> ReviewIR ─> SourceMap ─> ExportReport
  └────────────────────────────────────────┐
                                            ↓
returned-original.docx ─> RawRevisionEvent ─> ChangeSet
                                                ↓
                                           ApprovalSet
                                                ↓
                                            PatchPlan
                                                ↓
                                      VerificationReport
                                                ↓
                                           AuditBundle

RunManifest 对所有阶段、工具、产物和哈希绑定建立总索引。
BackendCapabilities 是后端探测快照，被 ExportReport/ChangeSet 引用。
```

## 2. 通用封装与基础类型

### 2.1 对象封装

所有版本化对象使用同一种顶层封装：

| 字段 | 类型 | 约束 |
|---|---|---|
| `schema_name` | string | 固定对象名，例如 `ChangeSet`；区分大小写 |
| `schema_version` | SemVer string | v1alpha 从 `1.0.0-alpha.1` 开始 |
| `object_id` | string | 带类型前缀的稳定 ID；规则见第 4 节 |
| `run_id` | string/null | 所属运行；全局能力快照可为 `null` |
| `generated_at` | ISO 8601 string | 必须含时区；只表示生成时间，不参与语义 ID |
| `producer` | `ToolIdentity` | 生成该封装的工具和版本 |
| `payload` | object | 领域语义；对象间绑定只绑定其语义哈希 |
| `integrity.payload_sha256` | `Sha256` | RFC 8785/JCS 规范化 `payload` 后的 SHA-256 |
| `integrity.document_sha256` | `Sha256` | 规范化整个对象、排除本字段自身后的 SHA-256 |
| `extensions` | object | 反向域名命名的非安全关键扩展；默认 `{}` |

`payload_sha256` 是审批和补丁的授权绑定；`document_sha256` 用于证明封装元数据也未变化。重复处理相同输入时，`generated_at` 可以不同，但语义 payload、稳定 ID 和 `payload_sha256` 必须相同，除非算法、配置或输入确实改变。

### 2.2 `Sha256`

- 文本形式固定为 `sha256:` 加 64 个小写十六进制字符。
- 文件哈希针对原始字节，不做换行、编码、ZIP 时间戳或 XML 规范化。
- 规范化 OOXML 的比较哈希必须另用明确字段，例如 `normalized_ooxml_sha256`，不得冒充原文件哈希。
- 空字节串使用标准 SHA-256，不使用 `null` 或自定义哨兵。

### 2.3 JSON 规范化

- 语义哈希使用 RFC 8785 JSON Canonicalization Scheme。
- 禁止 `NaN`、`Infinity`、重复键和依赖对象键顺序的语义。
- 有序事件、审阅单元和补丁操作必须使用数组，数组顺序是语义的一部分。
- 集合在写入 payload 前按契约指定键排序；例如文件按规范化相对路径排序。
- 字符串不做隐式 Unicode 归一化；若需要规范化文本，必须同时保留原始文本哈希并记录明确的 `normalization_profile`。

### 2.4 路径、时间与缺失值

- 所有路径均使用 `/` 分隔的项目快照或 run 根目录相对路径。
- 禁止绝对路径、盘符、UNC、`..`、空段、NUL 和越界符号链接。
- 源位置的行列号只供显示；自动补丁以字节范围和源切片哈希为准。
- 时间必须是含 UTC 偏移或 `Z` 的 ISO 8601。上游未提供作者或时间时必须写 `null` 并产生诊断项，不得推测。
- `null` 表示“已知缺失”；字段不存在表示当前 Schema 不定义该概念。两者不得混用。

### 2.5 `ToolIdentity`

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 稳定工具名 |
| `version` | string | 精确版本；未知时为 `null` 并报告 |
| `interface_version` | string/null | 适配器或插件接口版本 |
| `distribution` | string/null | 包或可执行来源，不记录个人安装路径 |
| `executable_sha256` | `Sha256`/null | 可取得时记录 |
| `configuration_sha256` | `Sha256`/null | 影响语义结果的有效配置哈希 |

### 2.6 `ArtifactRef`

| 字段 | 类型 | 说明 |
|---|---|---|
| `artifact_id` | string | `art_` 加内容派生 ID |
| `path` | relative path | 相对于 `run_root` 或 `snapshot_root` |
| `path_base` | enum | `run_root`、`snapshot_root`、`bundle_root` |
| `role` | enum/string | 如 `returned_original`、`review_docx`、`unified_diff` |
| `media_type` | string | IANA 类型或项目约定类型 |
| `size_bytes` | integer | 非负 |
| `sha256` | `Sha256` | 原始文件字节哈希 |
| `immutable` | boolean | 原件、快照和证据必须为 `true` |
| `confidentiality` | enum | `public_fixture`、`local_private`、`derived_private` |

### 2.7 `SourceLocation`

| 字段 | 类型 | 说明 |
|---|---|---|
| `path` | relative path | 相对于快照根目录 |
| `start_byte` / `end_byte` | integer | UTF-8 文件原始字节的半开区间 `[start,end)` |
| `slice_sha256` | `Sha256` | 区间原始字节哈希 |
| `encoding` | string | v0.1 通常为 `utf-8`；必须显式记录 |
| `newline` | enum | `lf`、`crlf`、`mixed`、`none` |
| `start_line` / `end_line` | integer/null | 1-based，仅供显示 |
| `start_column` / `end_column` | integer/null | 1-based，仅供显示 |

### 2.8 `Diagnostic`

| 字段 | 类型 | 说明 |
|---|---|---|
| `diagnostic_id` | string | 内容派生，重复运行稳定 |
| `code` | string | 稳定机器码，见第 10 节 |
| `severity` | enum | `info`、`warning`、`error`、`fatal` |
| `phase` | enum | `doctor` 至 `bundle` |
| `message` | string | 面向人的说明，不作为兼容接口 |
| `recoverable` | boolean | 是否可在不放宽安全规则下重试 |
| `unit_id` | string/null | 相关审阅单元 |
| `change_id` | string/null | 相关变更 |
| `source_location` | `SourceLocation`/null | 可定位时填写 |
| `evidence` | `ArtifactRef`/null | 可审计证据 |
| `remediation` | string/null | 建议动作，不得自动执行外部写操作 |

## 3. 哈希层级与绑定链

### 3.1 源树哈希

`source_tree_sha256` 的输入为按 `path` 排序的下列记录数组：

```json
[{"path":"sections/method.tex","role":"tex","size_bytes":42,"sha256":"sha256:..."}]
```

它不包含本机源目录、文件修改时间和 inode。文件的原始字节哈希变化必然改变源树哈希。

### 3.2 强制绑定

| 下游对象 | 必须绑定的上游语义或字节哈希 |
|---|---|
| `ReviewIR` | `SourceManifest.payload_sha256`、`source_tree_sha256`、规范化策略哈希 |
| `SourceMap` | SourceManifest、ReviewIR、导出 DOCX 字节哈希、锚点策略哈希 |
| `ExportReport` | SourceManifest、BackendCapabilities、ReviewIR、SourceMap、review DOCX |
| `RawRevisionEvent` | returned-original DOCX 字节哈希、OOXML part 字节哈希、证据片段哈希 |
| `ChangeSet` | returned-original DOCX、SourceManifest、SourceMap、解析器能力/配置、全部 raw event |
| `ApprovalSet` | `ChangeSet.payload_sha256`；同时记录每个变更指纹 |
| `PatchPlan` | SourceManifest/source tree、ChangeSet、ApprovalSet、补丁策略、计划时源文件哈希 |
| `VerificationReport` | PatchPlan、应用后源树、实际 diff、原始来源与 returned-original 的复核哈希 |
| `AuditBundle` | RunManifest、VerificationReport、每个收录 ArtifactRef 的原始字节哈希 |

任何绑定不一致都产生 `fatal` 诊断并阻止后续有写入能力的动作。不得通过“重新计算并覆盖旧哈希”修复漂移；必须建立新的快照、ChangeSet、ApprovalSet 或 PatchPlan。

## 4. 稳定 ID 规则

所有 ID 使用小写前缀和 Base32/十六进制摘要，至少保留 128 bit；碰撞时扩展摘要，不追加随机序号。

| ID | 规则 |
|---|---|
| `run_id` | `run_` + UUIDv7；表示一次用户运行，不要求跨运行稳定 |
| `source_manifest_id` | `src_` + `source_tree_sha256` 派生摘要 |
| `unit_id` | `unit_` + hash(`source_tree_sha256`, path, byte range, unit kind, slice hash, unit-id-profile version) |
| `raw_event_id` | `rev_` + hash(returned DOCX hash, part URI,事件种类, native id,文档顺序,证据片段哈希) |
| `change_id` | `chg_` + hash(按文档顺序排列的 raw event IDs,组合规则版本) |
| `approval_set_id` | `apr_` + hash(`changeset_sha256`, revision,前序审批哈希,逐项决定与未决定项,审批审计)；计算时排除本 ID |
| `patch_plan_id` | `plan_` + hash(全部上游绑定,策略与 planner 配置,operations/blocked/excluded/preconditions)；计算时排除本 ID |
| `operation_id` | `op_` + hash(patch bindings, change_id, target range, replacement bytes hash) |
| `diagnostic_id` | `diag_` + hash(code, phase, location/evidence fingerprint) |
| `artifact_id` | `art_` + 文件 SHA-256 摘要 |

稳定 ID 不等于授权。即使 `change_id` 相同，只要 ChangeSet、ApprovalSet、源树、策略或计划哈希不同，也不能复用旧 PatchPlan。

## 5. 领域对象

### 5.1 `RunManifest`

用途：作为一次审阅运行的总索引和阶段审计链，不承载后端私有对象。

`payload` 字段：

| 字段 | 类型 | 约束 |
|---|---|---|
| `manifest_revision` | integer | 从 1 单调增加 |
| `previous_manifest_payload_sha256` | `Sha256`/null | revision 1 为 `null`；形成哈希链 |
| `status` | enum | `active`、`blocked`、`failed`、`completed`、`aborted` |
| `current_phase` | enum | `initialized`、`snapshotted`、`exported`、`ingested`、`reviewed`、`planned`、`applied`、`verified`、`bundled` |
| `policy_profile` | object | 名称、版本、payload 哈希 |
| `source_manifest` | hash reference/null | snapshot 后必须存在 |
| `selected_backends` | array | 后端身份、能力 payload 哈希和用途 |
| `phase_events` | array | 有序事件；含开始/完成时间、状态、输入/输出哈希和诊断 IDs |
| `artifacts` | `ArtifactRef[]` | 本 run 已登记的产物 |
| `object_bindings` | array | 对象名、ID、payload/document hash |
| `source_origin_pre_sha256` | `Sha256`/null | 来源树运行前指纹 |
| `source_origin_post_sha256` | `Sha256`/null | verify 后复核；必须相等 |
| `diagnostics` | `Diagnostic[]` | run 级发现项 |

每次阶段变化产生新的 RunManifest revision；旧 revision 不修改，审计包保存完整哈希链。`blocked` 可以在输入或决定改变后由新 revision 恢复；禁止删除先前阻塞证据。

### 5.2 `SourceManifest`

用途：定义不可变 LaTeX 快照和文件图。

| 字段 | 类型 | 约束 |
|---|---|---|
| `source_manifest_id` | string | 由源树哈希派生 |
| `main_document` | relative path | 必须位于快照内 |
| `snapshot_artifact` | `ArtifactRef` | `immutable=true` |
| `source_tree_sha256` | `Sha256` | 第 3.1 节算法 |
| `files` | array | path、role、media type、size、原始 SHA-256、encoding/newline |
| `dependency_edges` | array | `from`、`to`、`kind`；路径均在快照内 |
| `engine_hints` | array | 源文件声明提示，仅记录不盲从 |
| `external_references` | array | 被拒绝或显式供应的外部依赖及诊断 |
| `discovery_profile` | object | 发现算法版本和配置哈希 |
| `immutability` | object | 快照只读状态和来源前置哈希 |

符号链接、硬链接逃逸、工作区外 `\input` 和路径穿越不能被悄悄跟随；它们进入 `external_references` 并默认阻止快照完成。

### 5.3 `BackendCapabilities`

用途：记录某一精确后端版本实际探测到的能力，而不是复制 README 声明。

| 字段 | 类型 | 说明 |
|---|---|---|
| `backend_id` | string | 稳定适配器标识 |
| `backend_role` | enum | `export`、`revision_reader`、`validator`、`baseline` |
| `tool` | `ToolIdentity` | 精确版本和适配器接口 |
| `probe_platform` | object | OS、架构、Python/外部工具版本，不含个人路径 |
| `operations` | array | `export_docx`、`read_revisions` 等 |
| `features` | object | 标准 feature key 到能力记录 |
| `determinism` | enum | `verified`、`claimed`、`not_verified`、`failed` |
| `tested_contracts` | array | fixture、测试结果、证据哈希 |
| `limitations` | array | 稳定代码、结构类型、降级与替代路径 |
| `security_requirements` | array | 外部进程、网络、宏执行等要求 |
| `probe_evidence` | `ArtifactRef[]` | 命令与结果证据 |

每个 feature 记录：

```json
{
  "support": "full|partial|none|unknown",
  "representations": ["omml"],
  "preserves_metadata": ["author", "timestamp"],
  "diagnostic_codes": [],
  "evidence_sha256": "sha256:..."
}
```

标准 feature keys 至少包括 `body_text`、`cjk`、`inline_math`、`display_math`、`omml`、`images`、`tables`、`bookmarks`、`live_fields`、`labels`、`references`、`citations`、`raw_insert`、`raw_delete`、`move_pair`、`format_revision`、`comments`、`comment_range` 和 `source_spans`。

### 5.4 `ReviewIR`

用途：后端无关的审阅语义，不是完整 LaTeX AST，也不保证无损重建 LaTeX。

| 字段 | 类型 | 约束 |
|---|---|---|
| `source_manifest_sha256` | `Sha256` | 强绑定 SourceManifest payload |
| `source_tree_sha256` | `Sha256` | 强绑定原始字节树 |
| `normalization_profile` | object | 名称、版本、配置哈希 |
| `document_language_hints` | array | BCP 47；仅提示 |
| `units` | `ReviewUnit[]` | 按文档顺序排列 |
| `relations` | array | 父子、引用、图题/对象、标签关系 |
| `diagnostics` | `Diagnostic[]` | 未识别结构必须出现 |

`ReviewUnit` 至少包含：

- `unit_id`、`kind`、`ordinal`、`parent_unit_id`；
- `source_location` 和原始切片哈希；
- `normalized_text`、`normalized_text_sha256`；
- `semantic_role`，例如标题级别、caption 类型或 label 名；
- `risk_class`：`plain_text_low`、`formula_high`、`reference_high`、`table_high`、`resource_high`、`structural_high`、`unknown_denied`；
- `neighbor_unit_ids`；
- `diagnostic_ids`。

允许的 `kind` 包括 `document`、`title`、`heading`、`paragraph`、`list_item`、`caption`、`table`、`table_cell`、`equation`、`figure`、`citation`、`reference`、`footnote`、`code`、`unknown`。遇到未知后端节点必须规范化为 `unknown` 并保留证据诊断，不得塞入公共对象的任意 AST blob。

### 5.5 `SourceMap`

用途：把稳定审阅单元映射到 LaTeX 原始字节和导出 DOCX 锚点。

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_manifest_sha256` | `Sha256` | 源快照绑定 |
| `review_ir_sha256` | `Sha256` | ReviewIR payload 绑定 |
| `review_docx_sha256` | `Sha256` | 导出 DOCX 原始字节 |
| `anchor_profile` | object | 书签/自定义锚点策略和版本哈希 |
| `mappings` | array | 每个 `unit_id` 一项或显式 unmapped |
| `coverage` | object | 总单元、已锚定、未锚定和按风险分类计数 |
| `diagnostics` | `Diagnostic[]` | 锚点冲突、重名、降级 |

每个 mapping 至少包含：

- `unit_id` 和 `source_location`；
- `docx_anchor`：`part_uri`、`kind`、`name`、可选 paragraph/object id；
- `source_fingerprint`：切片、规范化文本和邻接单元哈希；
- `mapping_method`：导出阶段通常为 `exact_source_span`；
- `confidence`：0 至 1；
- `status`：`exact`、`degraded`、`unmapped`、`conflict`。

行号、paragraph id 和 XPath 不能单独作为 PatchPlan 权威依据。自动回填必须同时有源字节范围、切片哈希和稳定 unit ID。

### 5.6 `ExportReport`

用途：证明导出成功范围、降级和结构计数，不以“生成了 DOCX”代替验收。

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | enum | `success`、`partial`、`failed` |
| `source_manifest_sha256` | `Sha256` | 输入 |
| `backend_capabilities_sha256` | `Sha256` | 精确能力快照 |
| `review_ir_sha256` | `Sha256`/null | 可用时绑定 |
| `source_map_sha256` | `Sha256`/null | 可用时绑定 |
| `review_docx` | `ArtifactRef`/null | 失败时可为 null |
| `metrics.source` | object | 标题、正文、公式、图、表、引用等计数 |
| `metrics.output` | object | OMML、图片、表格、字段、书签等计数 |
| `feature_results` | array | 每项 `preserved/degraded/unsupported/failed` |
| `findings` | `Diagnostic[]` | 必须含源位置或单元证据 |
| `validation` | object | OOXML、打开性、关系和结构检查结果 |

`partial` 不是成功别名：CLI 必须返回文档化状态，且所有差额都有 finding。支持范围内出现无诊断内容丢失时状态强制为 `failed`。

### 5.7 `RawRevisionEvent`

用途：对 returned-original DOCX 中一个原始修订/批注事实的不可变描述。它是 `ChangeSet` 的子对象或独立 sidecar，不是可执行补丁。

| 字段 | 类型 | 说明 |
|---|---|---|
| `raw_event_id` | string | 第 4 节算法 |
| `returned_docx_sha256` | `Sha256` | 只读原件字节哈希 |
| `part_uri` | string | 例如 `word/document.xml` |
| `part_sha256` | `Sha256` | 原 OOXML part 字节哈希 |
| `kind` | enum | `insert`、`delete`、`move_from`、`move_to`、`run_format`、`paragraph_format`、`table_format`、`comment`、`unknown` |
| `native_id` | string/null | OOXML `w:id` 等，仅作证据，不作为全局 ID |
| `author` | string/null | 不推测 |
| `timestamp` | ISO 8601/null | 不推测 |
| `document_order` | integer | story parts 合并后的稳定顺序 |
| `content` | object | text、deleted text、comment text、format before/after 等 |
| `range` | object/null | comment/move 范围、pair native id、anchor kind |
| `evidence` | object | node ordinal、fragment hash、可选证据 ArtifactRef |
| `diagnostics` | `Diagnostic[]` | 缺失元数据、未知标签、非法嵌套 |

删除必须读取 `w:delText`；解析器不得先接受修订再生成 raw event。相邻删除和插入可以在 ChangeSet 组合为替换，但两个 raw events 必须原样保留。

### 5.8 `ChangeSet`

用途：把原始 Word 证据规范化、组合并定位到源单元，形成不可变审阅账本。

| 字段 | 类型 | 说明 |
|---|---|---|
| `returned_original` | `ArtifactRef` | `immutable=true`，原始 DOCX SHA |
| `source_manifest_sha256` | `Sha256` | 审阅基线 |
| `source_map_sha256` | `Sha256` | 锚点和源位置 |
| `revision_reader_capabilities_sha256` | `Sha256` | 解析路径证据 |
| `ingest_profile` | object | 组合、规范化和匹配算法版本/哈希 |
| `raw_events` | `RawRevisionEvent[]` | 原始顺序和证据完整保留 |
| `changes` | `Change[]` | 面向审批的组合视图 |
| `counts` | object | 原始、组合、未匹配、冲突和类型计数 |
| `diagnostics` | `Diagnostic[]` | 全局发现项 |

每个 `Change` 至少包含：

- `change_id`、`kind` 和有序 `raw_event_ids`；
- `author`、`timestamp`；组合事件不一致时写明 `authors/timestamps` 并标冲突；
- `before`、`after`、`comment` 或格式差异；
- `unit_id` 和 `source_location`，不可定位时为 `null`；
- `resolution.status`：`exact`、`fallback`、`unmatched`、`conflict`、`unsupported`；
- `resolution.method`：`bookmark`、`source_fingerprint`、`manual` 等；
- `resolution.confidence` 和候选证据；
- `safety_class`：`plain_text_candidate`、`ledger_only`、`manual_high_risk`、`denied_unknown`；
- `initial_decision`：通常为 `pending`；无法安全定位时为 `manual` 或 `conflict`；
- `change_fingerprint`：关键字段的语义哈希。

ChangeSet 不在审批时原地修改。用户决定只写入新的 ApprovalSet。

### 5.9 `ApprovalSet`

用途：第一道安全闸门，只记录逐条决定，不修改 `.tex`，不生成 `revised-clean/`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `approval_set_id` | string | 由第 4 节身份前像派生；哈希输入排除本字段，避免自引用 |
| `revision` | integer | 决定集合版本 |
| `supersedes_payload_sha256` | `Sha256`/null | 前一版审批 |
| `changeset_sha256` | `Sha256` | 必须绑定 ChangeSet payload |
| `source_manifest_sha256` | `Sha256` | 防止跨基线复用 |
| `status` | enum | `draft`、`final` |
| `decided_by` | object | 本地用户标签；不要求邮箱 |
| `decisions` | array | 每个 `change_id` 最多一项 |
| `undecided_change_ids` | array | 显式列出，不暗示接受 |
| `decision_summary` | object | 各状态计数 |
| `audit` | object | 创建方式、批量操作记录和前序哈希 |

Decision 字段：

- `change_id` 和 `change_fingerprint`；
- `decision`：`accepted`、`accepted_with_edit`、`rejected`、`manual`、`conflict`；
- `final_text`：仅 `accepted_with_edit` 必填；其他状态必须为 `null`；
- `reason`、`decided_at`、可选 `risk_acknowledgement`；
- `decision_source`：`cli`、`local_ui`、`codex_skill`、`imported`。

`accepted` 只表示用户认可修改意图，不授予绕过安全矩阵的权限。高风险变更即使 accepted，也不能自动应用；用户应将其转为 `manual` 或在未来受支持的专用流程中处理。

### 5.10 `PatchPlan`

用途：第二道安全闸门的 dry-run 产物。它描述可以应用的局部操作，但 `plan` 本身不得创建或修改任何 `.tex` 工作副本。

| 字段 | 类型 | 说明 |
|---|---|---|
| `patch_plan_id` | string | 由第 4 节身份前像派生；哈希输入排除本字段，避免自引用 |
| `status` | enum | `ready`、`blocked`、`noop` |
| `mode` | enum | v0.1 固定 `dry_run` |
| `source_manifest_sha256` | `Sha256` | 基线对象 |
| `source_tree_sha256` | `Sha256` | 精确源字节树 |
| `changeset_sha256` | `Sha256` | 账本 payload |
| `approval_set_sha256` | `Sha256` | final 审批 payload |
| `policy_sha256` | `Sha256` | 自动应用安全矩阵和阈值 |
| `planner` | `ToolIdentity` | 精确算法和配置 |
| `operations` | `PatchOperation[]` | 仅获批且合格项 |
| `accepted_but_blocked` | array | 获批但因风险/漂移/歧义被阻止的 change IDs 和代码 |
| `excluded_changes` | array | rejected/manual/pending/conflict，含原因 |
| `unified_diff` | `ArtifactRef`/null | 预览 diff；hash 必须与 operations 对账 |
| `summary` | object | approved、planned、blocked、excluded、overlap 计数 |
| `preconditions` | array | apply 前必须重新计算的哈希条件 |

`PatchOperation` 至少包含：

- `operation_id`、`change_id`、`kind`：`insert`、`delete`、`replace`；
- `target`：SourceLocation 与目标文件当前 SHA-256；
- `expected_bytes_sha256` 和可读 `before_text`；
- `replacement_text`、`encoding`、`newline_policy`；
- `unit_id`、源切片哈希、context fingerprint；
- `confidence`、`safety_class` 和逐项 `safety_checks`；
- `diff_hunk_sha256`。

默认 `allow_partial_plan=false`：只要 final ApprovalSet 中存在 accepted/accepted_with_edit 但无法安全计划的项，整个计划为 `blocked`，避免静默忽略已接受意图。用户将其改为 `manual` 后可生成仅含安全项的新计划。

### 5.11 `VerificationReport`

用途：证明应用结果、原件不可变、编译和 `latexdiff` 状态，并对批准项与实际 diff 对账。

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | enum | `pass`、`fail`、`blocked` |
| `patch_plan_sha256` | `Sha256` | 实际使用的 ready plan |
| `source_manifest_sha256` | `Sha256` | 原始基线 |
| `revised_source_manifest_sha256` | `Sha256`/null | 应用后的新工作副本 |
| `original_source_pre/post_sha256` | `Sha256` | 必须相等 |
| `returned_original_pre/post_sha256` | `Sha256` | 必须相等 |
| `actual_diff` | `ArtifactRef`/null | 实际源码 diff |
| `patch_reconciliation` | object | planned/applied/missing/extra/重复计数 |
| `compile` | object | 工具、受限命令、退出码、超时、日志证据 |
| `references` | object | 未定义引用、重复标签、文献和资源检查 |
| `structure` | object | 公式、图、表、章节等异常差额 |
| `latexdiff` | object | tex/pdf ArtifactRefs 和状态 |
| `deliverables` | array | clean、diff、ledger 等 ArtifactRefs |
| `diagnostics` | `Diagnostic[]` | 所有失败可定位 |

`patch_reconciliation.unapproved_modification_count` 必须为 0；否则报告强制 `fail`。编译成功不能掩盖多改、漏改或原件漂移。

### 5.12 `AuditBundle`

用途：以可验证清单打包一次运行的必要审计证据和交付物。

| 字段 | 类型 | 说明 |
|---|---|---|
| `bundle_format_version` | SemVer | 独立于领域 Schema |
| `run_manifest_sha256` | `Sha256` | 最终 RunManifest payload |
| `verification_report_sha256` | `Sha256` | VerificationReport payload |
| `content_classification` | enum | `public_fixture`、`local_private` |
| `entries` | array | 规范化相对路径、角色、大小、字节 SHA、来源对象和 ArtifactRef selector |
| `excluded_entries` | array | 原始 Word、私有中间文件等未包含原因 |
| `privacy_scan` | object | 状态、规则版本、报告 ArtifactRef |
| `manifest_sha256` | `Sha256` | entries 规范化清单哈希 |
| `reproducibility` | object | ZIP 顺序、时间戳策略和工具版本 |

普通 entry 的 selector 必须解析到显式收录、且获 RunManifest 授权的 sealed source contract
payload 内 immutable `ArtifactRef`；extensions 不提供 artifact authority。`$document` 只绑定合同自身的 canonical sealed bytes。Bundle
必须显式收录其准确 RunManifest 与 VerificationReport，离线验证据此重建完整来源链。
归档文件自身 SHA-256 由外层 RunManifest 的 ArtifactRef 或 `.sha256` sidecar 记录，避免“归档内部包含自身哈希”的循环。Bundle 默认不为公开发布脱敏；含论文正文时必须标为 `local_private`。

## 6. 审阅和运行状态机

### 6.1 运行阶段

```text
initialized
  → snapshotted
  → exported
  → ingested
  → reviewed
  → planned
  → applied
  → verified
  → bundled
```

- 每个箭头都需要前置对象哈希有效，并生成新 RunManifest revision。
- 任一阶段可生成 `blocked` 或 `failed` revision；恢复时产生后续 revision，不删除失败证据。
- 禁止从 `reviewed` 直接进入 `applied`；必须存在 `PatchPlan.status=ready`。
- `plan` 不得隐式 apply；`review` 不得隐式 plan。

### 6.2 定位状态与审批状态分离

定位状态：

```text
exact | fallback | unmatched | conflict | unsupported
```

用户决定状态：

```text
pending
  ├─> accepted
  ├─> accepted_with_edit
  ├─> rejected
  ├─> manual
  └─> conflict
```

状态修改通过新 ApprovalSet revision 表达，不原地覆盖。`unmatched`、`conflict`、`unsupported` 不能进入自动补丁；批量接受也不能改变定位或风险分类。

### 6.3 PatchPlan 状态

```text
noop      没有获批且合格的操作
ready     所有计划前置条件在 plan 时满足
blocked   存在漂移、重叠、歧义、未知类型或获批但不安全的项
```

PatchPlan 是不可变计划，不在 apply 后改写状态。apply/verify 的结果由新的 RunManifest revision 和 VerificationReport 记录。apply 前重新核验后发现漂移时，旧 plan 仍保留但不得执行，必须重新生成。

## 7. 版本兼容与未知字段

### 7.1 Schema 兼容规则

- 主版本不同：拒绝用于任何业务动作，代码 `E_SCHEMA_MAJOR_UNSUPPORTED`。
- 同一主版本、更高 prerelease/minor：报告类对象可只读展示，但写入性动作必须满足 `compatibility.min_reader` 和 `min_writer`；否则拒绝。
- PatchPlan、ApprovalSet 和 ChangeSet 进入 plan/apply 时必须由明确支持的精确 Schema 范围验证，不能 best effort。
- patch 版本只允许修复不改变语义的校验或文档问题；新增字段需 minor/prerelease 递增。
- Schema 迁移必须生成新对象、保留原对象哈希和迁移工具身份，禁止静默原地升级。

### 7.2 未知字段与枚举

- 核心对象默认 `additionalProperties=false`。
- 扩展只能放入顶层 `extensions`，key 使用反向域名，如 `org.example.feature`。
- 未知扩展必须在读写往返时保留，但永远不能赋予自动应用权限、降低风险或绕过哈希检查。
- 未知 revision/unit/change kind 规范化为核心值 `unknown`，同时保留 `native_kind` 和证据；安全分类固定 `denied_unknown`。
- 未知审批 decision、PatchOperation kind 或安全枚举是 fatal Schema 错误，不做字符串猜测或默认接受。

### 7.3 Capability 演进

新增后端能力只改变 BackendCapabilities 和显式 feature 结果。公共对象不得依赖后端模块名、AST 节点名或未版本化的字典键。后端删除能力时必须产生新的能力 payload 哈希，使旧 ExportReport/PatchPlan 绑定失效。

## 8. 最小端到端 insertion JSON 示例

下面是一个说明字段关系的单文件组合示例；哈希为格式合法的示意值，不代表 E0 fixture 的真实计算结果。实际文件分别保存并各自带第 2.1 节封装。

```json
{
  "example_note": "illustrative hashes only",
  "source_manifest": {
    "schema_name": "SourceManifest",
    "schema_version": "1.0.0-alpha.1",
    "object_id": "src_aaaaaaaaaaaaaaaaaaaaaaaaaa",
    "run_id": "run_019b0000-0000-7000-8000-000000000001",
    "generated_at": "2026-01-15T08:00:00+08:00",
    "producer": {"name": "latex-word-review", "version": "0.1.0b2", "interface_version": null, "distribution": "wheel", "executable_sha256": null, "configuration_sha256": "sha256:1111111111111111111111111111111111111111111111111111111111111111"},
    "payload": {
      "source_manifest_id": "src_aaaaaaaaaaaaaaaaaaaaaaaaaa",
      "main_document": "main.tex",
      "source_tree_sha256": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "files": [
        {"path": "sections/introduction.tex", "role": "tex", "media_type": "text/x-tex", "size_bytes": 96, "sha256": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "encoding": "utf-8", "newline": "lf"}
      ],
      "dependency_edges": [],
      "external_references": []
    },
    "integrity": {"payload_sha256": "sha256:1212121212121212121212121212121212121212121212121212121212121212", "document_sha256": "sha256:1313131313131313131313131313131313131313131313131313131313131313"},
    "extensions": {}
  },
  "review_ir": {
    "schema_name": "ReviewIR",
    "schema_version": "1.0.0-alpha.1",
    "object_id": "ir_14141414141414141414141414",
    "run_id": "run_019b0000-0000-7000-8000-000000000001",
    "generated_at": "2026-01-15T08:01:00+08:00",
    "producer": {"name": "latex-word-review", "version": "0.1.0b2", "interface_version": "backend-v1alpha1", "distribution": "wheel", "executable_sha256": null, "configuration_sha256": "sha256:1515151515151515151515151515151515151515151515151515151515151515"},
    "payload": {
      "source_manifest_sha256": "sha256:1212121212121212121212121212121212121212121212121212121212121212",
      "source_tree_sha256": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "normalization_profile": {"name": "review-text-v1", "version": "1", "configuration_sha256": "sha256:1616161616161616161616161616161616161616161616161616161616161616"},
      "units": [
        {
          "unit_id": "unit_intro_7b6d9f93a6f2d180",
          "kind": "paragraph",
          "ordinal": 3,
          "parent_unit_id": null,
          "source_location": {"path": "sections/introduction.tex", "start_byte": 0, "end_byte": 73, "slice_sha256": "sha256:1717171717171717171717171717171717171717171717171717171717171717", "encoding": "utf-8", "newline": "lf", "start_line": 1, "end_line": 1, "start_column": 1, "end_column": 74},
          "normalized_text": "The baseline describes a repeatable cyclic response.",
          "normalized_text_sha256": "sha256:1818181818181818181818181818181818181818181818181818181818181818",
          "semantic_role": "body",
          "risk_class": "plain_text_low",
          "neighbor_unit_ids": [],
          "diagnostic_ids": []
        }
      ],
      "relations": [],
      "diagnostics": []
    },
    "integrity": {"payload_sha256": "sha256:1919191919191919191919191919191919191919191919191919191919191919", "document_sha256": "sha256:2020202020202020202020202020202020202020202020202020202020202020"},
    "extensions": {}
  },
  "source_map": {
    "schema_name": "SourceMap",
    "schema_version": "1.0.0-alpha.1",
    "object_id": "map_21212121212121212121212121",
    "run_id": "run_019b0000-0000-7000-8000-000000000001",
    "generated_at": "2026-01-15T08:02:00+08:00",
    "producer": {"name": "latex-word-review", "version": "0.1.0b2", "interface_version": "backend-v1alpha1", "distribution": "wheel", "executable_sha256": null, "configuration_sha256": "sha256:2222222222222222222222222222222222222222222222222222222222222222"},
    "payload": {
      "source_manifest_sha256": "sha256:1212121212121212121212121212121212121212121212121212121212121212",
      "review_ir_sha256": "sha256:1919191919191919191919191919191919191919191919191919191919191919",
      "review_docx_sha256": "sha256:2323232323232323232323232323232323232323232323232323232323232323",
      "anchor_profile": {"name": "bookmark-v1", "version": "1", "configuration_sha256": "sha256:2424242424242424242424242424242424242424242424242424242424242424"},
      "mappings": [
        {
          "unit_id": "unit_intro_7b6d9f93a6f2d180",
          "source_location": {"path": "sections/introduction.tex", "start_byte": 0, "end_byte": 73, "slice_sha256": "sha256:1717171717171717171717171717171717171717171717171717171717171717", "encoding": "utf-8", "newline": "lf", "start_line": 1, "end_line": 1, "start_column": 1, "end_column": 74},
          "docx_anchor": {"part_uri": "word/document.xml", "kind": "bookmark", "name": "txr_unit_intro_7b6d9f93a6f2d180", "paragraph_id": null},
          "source_fingerprint": {"slice_sha256": "sha256:1717171717171717171717171717171717171717171717171717171717171717", "normalized_text_sha256": "sha256:1818181818181818181818181818181818181818181818181818181818181818", "neighbor_sha256": "sha256:2525252525252525252525252525252525252525252525252525252525252525"},
          "mapping_method": "exact_source_span",
          "confidence": 1.0,
          "status": "exact"
        }
      ],
      "coverage": {"total": 1, "exact": 1, "degraded": 0, "unmapped": 0, "conflict": 0},
      "diagnostics": []
    },
    "integrity": {"payload_sha256": "sha256:2626262626262626262626262626262626262626262626262626262626262626", "document_sha256": "sha256:2727272727272727272727272727272727272727272727272727272727272727"},
    "extensions": {}
  },
  "export_report": {
    "schema_name": "ExportReport",
    "schema_version": "1.0.0-alpha.1",
    "object_id": "export_282828282828282828282828",
    "run_id": "run_019b0000-0000-7000-8000-000000000001",
    "generated_at": "2026-01-15T08:03:00+08:00",
    "producer": {"name": "latex-word-review", "version": "0.1.0b2", "interface_version": "backend-v1alpha1", "distribution": "wheel", "executable_sha256": null, "configuration_sha256": "sha256:2929292929292929292929292929292929292929292929292929292929292929"},
    "payload": {
      "status": "success",
      "source_manifest_sha256": "sha256:1212121212121212121212121212121212121212121212121212121212121212",
      "backend_capabilities_sha256": "sha256:3030303030303030303030303030303030303030303030303030303030303030",
      "review_ir_sha256": "sha256:1919191919191919191919191919191919191919191919191919191919191919",
      "source_map_sha256": "sha256:2626262626262626262626262626262626262626262626262626262626262626",
      "review_docx": {"artifact_id": "art_23232323232323232323232323", "path": "export/review.docx", "path_base": "run_root", "role": "review_docx", "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "size_bytes": 40000, "sha256": "sha256:2323232323232323232323232323232323232323232323232323232323232323", "immutable": true, "confidentiality": "derived_private"},
      "metrics": {"source": {"paragraphs": 1}, "output": {"paragraphs": 1, "bookmarks": 1}},
      "feature_results": [{"feature": "body_text", "status": "preserved", "source_count": 1, "output_count": 1}],
      "findings": [],
      "validation": {"ooxml": "pass", "openability": "pass"}
    },
    "integrity": {"payload_sha256": "sha256:3131313131313131313131313131313131313131313131313131313131313131", "document_sha256": "sha256:3232323232323232323232323232323232323232323232323232323232323232"},
    "extensions": {}
  },
  "change_set": {
    "schema_name": "ChangeSet",
    "schema_version": "1.0.0-alpha.1",
    "object_id": "changes_333333333333333333333333",
    "run_id": "run_019b0000-0000-7000-8000-000000000001",
    "generated_at": "2026-01-16T09:10:00+08:00",
    "producer": {"name": "latex-word-review", "version": "0.1.0b2", "interface_version": "revision-reader-v1alpha1", "distribution": "wheel", "executable_sha256": null, "configuration_sha256": "sha256:3434343434343434343434343434343434343434343434343434343434343434"},
    "payload": {
      "returned_original": {"artifact_id": "art_35353535353535353535353535", "path": "ingest/returned-original.docx", "path_base": "run_root", "role": "returned_original", "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "size_bytes": 41000, "sha256": "sha256:3535353535353535353535353535353535353535353535353535353535353535", "immutable": true, "confidentiality": "local_private"},
      "source_manifest_sha256": "sha256:1212121212121212121212121212121212121212121212121212121212121212",
      "source_map_sha256": "sha256:2626262626262626262626262626262626262626262626262626262626262626",
      "revision_reader_capabilities_sha256": "sha256:3636363636363636363636363636363636363636363636363636363636363636",
      "ingest_profile": {"name": "word-review-v1", "version": "1", "configuration_sha256": "sha256:3737373737373737373737373737373737373737373737373737373737373737"},
      "raw_events": [
        {
          "raw_event_id": "rev_8b27ea0f43a267e1",
          "returned_docx_sha256": "sha256:3535353535353535353535353535353535353535353535353535353535353535",
          "part_uri": "word/document.xml",
          "part_sha256": "sha256:3838383838383838383838383838383838383838383838383838383838383838",
          "kind": "insert",
          "native_id": "101",
          "author": "Reviewer Alpha",
          "timestamp": "2026-01-16T09:00:00+08:00",
          "document_order": 7,
          "content": {"text": "carefully ", "deleted_text": null, "comment_text": null, "format_before": null, "format_after": null},
          "range": null,
          "evidence": {"node_ordinal": 7, "fragment_sha256": "sha256:3939393939393939393939393939393939393939393939393939393939393939", "artifact": null},
          "diagnostics": []
        }
      ],
      "changes": [
        {
          "change_id": "chg_52a7f2bdca9940a1",
          "kind": "insertion",
          "raw_event_ids": ["rev_8b27ea0f43a267e1"],
          "author": "Reviewer Alpha",
          "timestamp": "2026-01-16T09:00:00+08:00",
          "before": "",
          "after": "carefully ",
          "comment": null,
          "unit_id": "unit_intro_7b6d9f93a6f2d180",
          "source_location": {"path": "sections/introduction.tex", "start_byte": 13, "end_byte": 13, "slice_sha256": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "encoding": "utf-8", "newline": "lf", "start_line": 1, "end_line": 1, "start_column": 14, "end_column": 14},
          "resolution": {"status": "exact", "method": "bookmark", "confidence": 1.0, "candidates": []},
          "safety_class": "plain_text_candidate",
          "initial_decision": "pending",
          "change_fingerprint": "sha256:4040404040404040404040404040404040404040404040404040404040404040"
        }
      ],
      "counts": {"raw_events": 1, "changes": 1, "unmatched": 0, "conflicts": 0},
      "diagnostics": []
    },
    "integrity": {"payload_sha256": "sha256:4141414141414141414141414141414141414141414141414141414141414141", "document_sha256": "sha256:4242424242424242424242424242424242424242424242424242424242424242"},
    "extensions": {}
  },
  "approval_set": {
    "schema_name": "ApprovalSet",
    "schema_version": "1.0.0-alpha.1",
    "object_id": "apr_43434343434343434343434343",
    "run_id": "run_019b0000-0000-7000-8000-000000000001",
    "generated_at": "2026-01-16T09:30:00+08:00",
    "producer": {"name": "latex-word-review", "version": "0.1.0b2", "interface_version": null, "distribution": "wheel", "executable_sha256": null, "configuration_sha256": "sha256:4444444444444444444444444444444444444444444444444444444444444444"},
    "payload": {
      "approval_set_id": "apr_43434343434343434343434343",
      "revision": 1,
      "supersedes_payload_sha256": null,
      "changeset_sha256": "sha256:4141414141414141414141414141414141414141414141414141414141414141",
      "source_manifest_sha256": "sha256:1212121212121212121212121212121212121212121212121212121212121212",
      "status": "final",
      "decided_by": {"id": "paper-author", "display_name": "Paper Author"},
      "decisions": [
        {"change_id": "chg_52a7f2bdca9940a1", "change_fingerprint": "sha256:4040404040404040404040404040404040404040404040404040404040404040", "decision": "accepted", "final_text": null, "reason": "Wording accepted", "decided_at": "2026-01-16T09:29:00+08:00", "risk_acknowledgement": null, "decision_source": "local_ui"}
      ],
      "undecided_change_ids": [],
      "decision_summary": {"accepted": 1, "accepted_with_edit": 0, "rejected": 0, "manual": 0, "conflict": 0, "pending": 0},
      "audit": {"bulk_operations": [], "previous_payload_sha256": null}
    },
    "integrity": {"payload_sha256": "sha256:4545454545454545454545454545454545454545454545454545454545454545", "document_sha256": "sha256:4646464646464646464646464646464646464646464646464646464646464646"},
    "extensions": {}
  },
  "patch_plan": {
    "schema_name": "PatchPlan",
    "schema_version": "1.0.0-alpha.1",
    "object_id": "plan_4747474747474747474747474",
    "run_id": "run_019b0000-0000-7000-8000-000000000001",
    "generated_at": "2026-01-16T09:31:00+08:00",
    "producer": {"name": "latex-word-review", "version": "0.1.0b2", "interface_version": null, "distribution": "wheel", "executable_sha256": null, "configuration_sha256": "sha256:4848484848484848484848484848484848484848484848484848484848484848"},
    "payload": {
      "patch_plan_id": "plan_4747474747474747474747474",
      "status": "ready",
      "mode": "dry_run",
      "source_manifest_sha256": "sha256:1212121212121212121212121212121212121212121212121212121212121212",
      "source_tree_sha256": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "changeset_sha256": "sha256:4141414141414141414141414141414141414141414141414141414141414141",
      "approval_set_sha256": "sha256:4545454545454545454545454545454545454545454545454545454545454545",
      "policy_sha256": "sha256:4949494949494949494949494949494949494949494949494949494949494949",
      "planner": {"name": "latex-word-review", "version": "0.1.0b2", "interface_version": "planner-v1alpha1", "distribution": "wheel", "executable_sha256": null, "configuration_sha256": "sha256:4848484848484848484848484848484848484848484848484848484848484848"},
      "operations": [
        {
          "operation_id": "op_50c07e7e5af38d4c",
          "change_id": "chg_52a7f2bdca9940a1",
          "kind": "insert",
          "target": {"path": "sections/introduction.tex", "start_byte": 13, "end_byte": 13, "slice_sha256": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "encoding": "utf-8", "newline": "lf", "start_line": 1, "end_line": 1, "start_column": 14, "end_column": 14},
          "target_file_sha256": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "expected_bytes_sha256": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          "before_text": "",
          "replacement_text": "carefully ",
          "encoding": "utf-8",
          "newline_policy": "preserve",
          "unit_id": "unit_intro_7b6d9f93a6f2d180",
          "unit_source_slice_sha256": "sha256:1717171717171717171717171717171717171717171717171717171717171717",
          "context_fingerprint": "sha256:2525252525252525252525252525252525252525252525252525252525252525",
          "confidence": 1.0,
          "safety_class": "plain_text_candidate",
          "safety_checks": {"approved": true, "exact_unit": true, "slice_hash_match": true, "plain_text_only": true, "no_overlap": true, "no_structural_boundary": true},
          "diff_hunk_sha256": "sha256:5050505050505050505050505050505050505050505050505050505050505050"
        }
      ],
      "accepted_but_blocked": [],
      "excluded_changes": [],
      "unified_diff": {"artifact_id": "art_51515151515151515151515151", "path": "apply/changes.patch", "path_base": "run_root", "role": "unified_diff", "media_type": "text/x-diff", "size_bytes": 180, "sha256": "sha256:5151515151515151515151515151515151515151515151515151515151515151", "immutable": true, "confidentiality": "derived_private"},
      "summary": {"approved": 1, "planned": 1, "blocked": 0, "excluded": 0, "overlaps": 0},
      "preconditions": [
        {"kind": "source_tree_sha256_equals", "expected": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"kind": "changeset_payload_sha256_equals", "expected": "sha256:4141414141414141414141414141414141414141414141414141414141414141"},
        {"kind": "approval_payload_sha256_equals", "expected": "sha256:4545454545454545454545454545454545454545454545454545454545454545"}
      ]
    },
    "integrity": {"payload_sha256": "sha256:5252525252525252525252525252525252525252525252525252525252525252", "document_sha256": "sha256:5353535353535353535353535353535353535353535353535353535353535353"},
    "extensions": {}
  }
}
```

该示例的授权链是：SourceManifest payload `12…` → SourceMap `26…` → ChangeSet `41…` → ApprovalSet `45…` → PatchPlan `52…`。任一环节重算不同，旧 plan 必须拒绝 apply。

## 9. Fail-closed 不变量

以下规则在 CLI、Python API、本地 UI 和未来 Skill 中完全相同，不能由调用入口绕过：

1. 原始 LaTeX 来源目录和 returned-original DOCX 永不作为写入目标。
2. 没有有效 SourceManifest 和不可变快照时禁止 export 以后的状态推进。
3. 未知 Schema 主版本、未知审批状态、未知补丁操作或未知风险类型禁止写入性动作。
4. 未支持结构必须有机器可读诊断；支持范围内无诊断内容丢失视为失败。
5. RawRevisionEvent 缺失删除文本时不得构造可自动应用的 deletion/replacement。
6. 作者、时间和证据缺失写 `null` 并报告，不得从邻近修订猜测。
7. ChangeSet 不可在审批时修改；ApprovalSet 必须绑定其 payload 哈希和逐项变更指纹。
8. `review` 只能写审批对象；`plan` 只能写预览；只有显式 `apply` 可以写新工作副本。
9. PatchPlan 必须同时绑定源树、ChangeSet、ApprovalSet、策略和目标文件/切片哈希。
10. 只有普通正文 insert/delete/replace、精确锚点、唯一候选、置信度达标且无结构边界时可成为 v0.1 operation。
11. 公式、引用、标签、编号、表格、资源路径、格式修订、move 和 unknown 永不进入 v0.1 自动补丁。
12. 获批但不安全的变更默认阻止整个 plan，不得静默跳过。
13. 源文件漂移、哈希不匹配、重叠 hunk、重复候选或编码/换行不明时禁止 apply。
14. apply 只写新目录，并在失败时保证没有部分成功状态；再次应用同一 plan 不得重复插入。
15. 实际源码 diff 必须逐项等于计划 operations；`unapproved_modification_count != 0` 强制 verify 失败。
16. 原来源树和 returned-original 的 verify 前后哈希必须相等。
17. 编译成功不能覆盖审批、哈希、diff 对账或原件不可变失败。
18. `extensions`、后端 capability 声明和 UI 批量操作均不能提升安全权限。
19. 日志、诊断和 bundle 中的路径必须相对化；任何私人内容默认 `local_private`。
20. Skill 只能调用同一 CLI/Schema，不能直接解析 OOXML、改 `.tex` 或伪造审批/计划对象。

## 10. 错误码与 CLI 退出码

### 10.1 错误记录原则

- 错误码和退出码是公共契约；人类消息不是。
- 同一根因在 JSON、CLI、HTML 和 Skill 中使用同一 code。
- 多个诊断同时存在时，CLI 返回最高严重度对应退出码；完整列表写入报告。
- warning 不允许掩盖 fail-closed 条件；这类条件必须使用 error/fatal。

### 10.2 稳定错误码族

| 代码 | 含义 |
|---|---|
| `E_SCHEMA_INVALID` | 对象不符合声明 Schema |
| `E_SCHEMA_MAJOR_UNSUPPORTED` | 不支持的主版本 |
| `E_SCHEMA_UNKNOWN_SECURITY_FIELD` | 安全关键对象出现未知字段/枚举 |
| `E_PATH_ABSOLUTE` / `E_PATH_TRAVERSAL` / `E_PATH_LINK_ESCAPE` | 路径越界 |
| `E_HASH_INTEGRITY_MISMATCH` | envelope 的 payload/document 完整性哈希不匹配 |
| `E_HASH_SOURCE_MISMATCH` | 源树或文件漂移 |
| `E_HASH_CHANGESET_MISMATCH` | 账本绑定失效 |
| `E_HASH_APPROVAL_MISMATCH` | 审批绑定失效 |
| `E_HASH_PATCHPLAN_MISMATCH` | 计划或 diff 被篡改 |
| `E_HASH_RETURNED_ORIGINAL_MISMATCH` | 返回 Word 原件变化 |
| `E_TOOL_MISSING` / `E_TOOL_VERSION_UNSUPPORTED` | 必要工具不可用 |
| `E_BACKEND_CAPABILITY_MISSING` | 后端不具备请求能力 |
| `E_BACKEND_FAILED` | 后端运行失败或无有效产物 |
| `E_EXPORT_SILENT_LOSS` | 支持范围内容丢失但无降级证据 |
| `W_EXPORT_DEGRADED` | 已报告的安全降级 |
| `E_DOCX_INVALID_PACKAGE` / `E_DOCX_UNSAFE_RELATIONSHIP` | DOCX 损坏或危险 |
| `E_REVISION_DELETE_TEXT_MISSING` | 删除证据缺失 |
| `W_REVISION_AUTHOR_MISSING` / `W_REVISION_TIMESTAMP_MISSING` | 元数据明确缺失 |
| `E_MAP_UNMATCHED` / `E_MAP_AMBIGUOUS` / `E_MAP_CONFIDENCE_LOW` | 定位失败 |
| `E_APPROVAL_NOT_FINAL` | draft 审批不能 plan |
| `E_APPROVAL_CHANGE_UNKNOWN` | 决定引用不存在的 change |
| `E_APPROVAL_FINAL_TEXT_REQUIRED` | accepted_with_edit 无最终文本 |
| `E_PATCH_UNSAFE_KIND` | 结构类型不允许自动回填 |
| `E_PATCH_SOURCE_DRIFT` | 计划后源文件改变 |
| `E_PATCH_OVERLAP` | 补丁范围重叠 |
| `E_PATCH_ACCEPTED_BUT_BLOCKED` | 获批项无法安全计划 |
| `E_PATCH_UNAPPROVED_CHANGE` | 补丁含未批准修改 |
| `E_APPLY_PARTIAL_WRITE` | 应用未保持原子性 |
| `E_VERIFY_COMPILE_FAILED` | 修订副本编译失败 |
| `E_VERIFY_DIFF_MISMATCH` | 实际 diff 与计划不符 |
| `E_VERIFY_ORIGINAL_MUTATED` | 权威源或 Word 原件被改动 |
| `E_BUNDLE_HASH_MISMATCH` / `E_BUNDLE_PRIVATE_RELEASE` | 审计包完整性/发布策略失败 |
| `E_INTERNAL_INVARIANT` | 程序缺陷；不得自动重试写入 |

### 10.3 CLI 退出码

| 退出码 | 类别 |
|---|---|
| `0` | 成功；报告中无阻塞条件 |
| `2` | CLI 用法、配置或 Schema 输入错误 |
| `3` | 工具/环境缺失或版本不支持 |
| `4` | 不可信输入、安全或路径拒绝 |
| `5` | 后端/export/inspect 失败 |
| `6` | ingest/ChangeSet 失败 |
| `7` | approval/plan 被安全策略阻止 |
| `8` | apply 失败或漂移 |
| `9` | verify/bundle 失败 |
| `10` | 内部不变量错误 |

## 11. 实现状态与变更纪律

原 C2 实施门槛已经完成：ADR-0001 已确定主/基线后端与 canonical reader；E0 fixture
已提供独立 oracle；项目名称、Apache-2.0 许可、Python 范围和 12 个 JSON Schema 已
落地；golden、未知版本/字段、路径、哈希篡改与上下游替换测试均在自动化套件中执行。

后续契约变更必须遵守：

1. 先修改规范 Schema，并同步 `contracts.py` 的语义校验；
2. 同一变更更新 golden/负向测试、本语义说明与参考文档；
3. 不兼容字段或枚举变更使用新的 Schema 版本，不静默放宽旧版本；
4. 保持 `additionalProperties=false + extensions`、哈希绑定和 fail-closed 规则；
5. v0.1 自动应用范围继续限定为已批准、精确定位的纯正文安全子集。
