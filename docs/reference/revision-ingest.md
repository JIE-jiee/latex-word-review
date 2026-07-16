# DOCX 修订证据读取与 ChangeSet 构造

本模块是 v1alpha 的生产库边界：它把导师返回的 DOCX 作为不可变证据读取，规范化为可验证的 `ChangeSet`。它不接受/拒绝修订，不保存 DOCX，不解压到磁盘，不运行 Word、LibreOffice、宏、嵌入对象或关系目标。

## 处理链

```text
read_docx_package
  -> extract_revision_events
  -> normalize_revision_changes
  -> build_changeset
```

- `read_docx_package(path, limits=...)` 完成 OPC/ZIP/XML 安全验证，返回内存中的 `DocxPackage`。
- `extract_revision_events(path, limits=...)` 按确定顺序保留原始修订和批注证据。
- `normalize_revision_changes(extraction, bookmark_bindings=...)` 组合替换和移动，但不丢失任何 raw event。
- `build_changeset(...)` 构造并立即校验 C2 `ChangeSet` envelope。

例如：

```python
from latex_word_review.revisions import BookmarkBinding, build_changeset

bindings = {
    "exported-bookmark": BookmarkBinding(
        unit_id="unit_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        source_location={
            "path": "sections/method.tex",
            "start_byte": 128,
            "end_byte": 160,
            "slice_sha256": "sha256:" + "3" * 64,
            "encoding": "utf-8",
            "newline": "lf",
            "start_line": 12,
            "end_line": 12,
            "start_column": 1,
            "end_column": 32,
        },
    )
}

changeset = build_changeset(
    "returned-original.docx",
    run_id="run_019b0000-0000-7000-8000-000000000001",
    source_manifest_sha256="sha256:" + "0" * 64,
    source_map_sha256="sha256:" + "1" * 64,
    revision_reader_capabilities_sha256="sha256:" + "2" * 64,
    bookmark_bindings=bindings,
)
```

调用方必须传入本次 run 的真实对象哈希；示例值只用于展示字段形状。

## 安全边界

`DocxReadLimits` 默认限制单文件 128 MiB、4096 个 ZIP member、单 member 64 MiB、总解压量 256 MiB、单 XML 16 MiB、100 万个 XML 节点及 1000:1 压缩比。限制可以收紧，不应为了“读成功”无上限放大。

读取器 fail closed 拒绝：

- 重复或经 Unicode/大小写/百分号解码后重名的 member，非规范名、穿越、盘符、NUL 与反斜杠；
- 加密 member、非 stored/deflated 压缩、CRC 失败、资源超限或 zip bomb；
- DTD/entity、非法 XML、缺少必要 OPC part；
- 所有 external relationship，包括外部超链。内部关系必须留在 package 中并指向已存在的 member。

读取前后都对原路径执行稳定读取和 SHA-256 比对。读取中发生变化或文件变得不可读时，返回 `E_HASH_RETURNED_ORIGINAL_MISMATCH`；不安全容器和关系分别返回 `E_DOCX_INVALID_PACKAGE` 或 `E_DOCX_UNSAFE_RELATIONSHIP`。

## 证据模型

故事 part 按 `document -> headers -> footers -> footnotes -> endnotes -> comments` 合并。每个 raw event 包含 part URI、part 原始字节哈希、全局顺序、原生 ID/类型、作者、时间、文本或格式前后态、范围证据、规范 XML fragment 哈希及诊断。支持：

- `w:ins`、`w:del`、`w:moveFrom`、`w:moveTo`；
- `w:rPrChange`、`w:pPrChange`、`w:tblPrChange`；
- 经 start/end/reference plumbing 对齐的经典 `word/comments.xml` 点批注与范围批注；
- 已识别但未支持的修订节点，保留为 `unknown` + `native_kind`，安全类固定为 `denied_unknown`。

缺失/无效/无时区时间不会被猜测：值为 `null` 并生成 `W_REVISION_TIMESTAMP_MISSING`。缺作者同理生成 `W_REVISION_AUTHOR_MISSING`。真正的 `w:del` 缺少 `w:delText`/`w:delInstrText` 时以 `E_REVISION_DELETE_TEXT_MISSING` 终止；`w:moveFrom` 的前态则必须来自普通 `w:t`，因为移动语义已经由外层 move 容器表达，使用 `w:delText` 会被 Microsoft Word 视为损坏文档。

## 规范化规则

- 只有同 part、同父节点、相邻、作者/时间/bookmark 一致的 `del + ins` 才组合为 replacement。
- move 依原生 move range name/ID 配对；不完整或元数据冲突的 move 保留为 conflict/manual。
- 批注是 `ledger_only`，move 和格式变更是 `manual_high_risk`。
- 原生 bookmark 只是证据，不是 LaTeX 位置。只有调用方用 `BookmarkBinding` 传入已校验 `SourceMap` 的 `unit_id` 和 `source_location` 后，纯文本变更才可标记为 `plain_text_candidate`；否则为 `unmatched/manual`。

raw event 的 `rev_...`、change 的 `chg_...`、fragment/change 哈希和 `changes_...` envelope ID 均由规范内容派生。同一 DOCX、限制、绑定和上游哈希会生成相同的证据顺序与 ID；`generated_at` 是 envelope 运行时元数据，需要完全重现 envelope 时应显式传入。

## 验证与局限

E0 公开合成 returned DOCX 的 oracle 测试达到 raw event 与 normalized change 的 precision/recall 各 100%：9 个 raw event 规范化为 7 个 change。定向测试同时覆盖重复/别名 ZIP member、CRC、加密与压缩方法、zip bomb 限制、深位置 DTD/entity、外部关系、读取中变更、缺元数据/删除文本、unknown kind 与所有支持的 story part。

当前边界不推断 SourceMap，不解析 Word 现代 comments extensions 的额外语义，也不将格式/move/批注自动回填 LaTeX。这些信息会保留为审计证据，由后续审批与补丁规划阶段决定。
