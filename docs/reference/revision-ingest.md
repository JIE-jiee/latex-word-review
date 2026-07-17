# DOCX 修订证据读取与 ChangeSet 构造

本模块是 v1alpha 的生产库边界：它把导师返回的 DOCX 作为不可变证据读取，规范化为可验证的 `ChangeSet`。它不接受/拒绝修订，不保存 DOCX，不解压到磁盘，不运行 Word、LibreOffice、宏、嵌入对象或关系目标。

## 返回原件的只读归档门

修订解析前必须先调用 `archive_returned_docx()`。它把收到的 `.docx` 原始字节复制到全新
目录，固定命名为 `returned-original.docx`，并写入规范化
`returned-original.manifest.json`。清单绑定 run、导出审阅稿哈希、返回件哈希、大小和保密
级别，但不记录导师文件名或绝对路径。

归档通过同级临时目录和原子 rename 发布。复制前后重新读取来源并逐字节比较；目标已存在时
只允许完全一致的幂等复用，任何冲突或篡改均返回
`E_HASH_RETURNED_ORIGINAL_MISMATCH`，不会覆盖旧证据。目录内只允许清单和 DOCX 两个普通
文件，发布前设为只读。后续 OOXML 读取器只能接收 `verify_returned_archive()` 返回的已验证
路径：

```python
from pathlib import Path

from latex_word_review.ingest import archive_returned_docx, verify_returned_archive

archive = archive_returned_docx(
    Path('received/review.docx'),
    Path('run/returned'),
    run_id='run_019b0000-0000-7000-8000-000000000001',
    exported_docx_sha256='sha256:' + 'a' * 64,
)
verified = verify_returned_archive(
    archive.directory,
    expected_run_id=archive.run_id,
    expected_returned_docx_sha256=archive.returned_docx_sha256,
)
```

归档不会接受或拒绝 Word 修订。审批只写独立 `ApprovalSet`，回填只发生在新的 LaTeX 工作
副本中。

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
    export_baseline_path="export/review.docx",
    export_baseline_sha256="sha256:" + "4" * 64,
    run_id="run_019b0000-0000-7000-8000-000000000001",
    source_manifest_sha256="sha256:" + "0" * 64,
    source_map_sha256="sha256:" + "1" * 64,
    revision_reader_capabilities_sha256="sha256:" + "2" * 64,
    bookmark_bindings=bindings,
)
```

调用方必须传入本次 run 的真实对象哈希和原始导出 DOCX；示例值只用于展示字段形状。
生产调用应通过 `bookmark_bindings_from_source_map()` 取得包含 review 长度和 text provenance
的完整绑定，不应手工缩减 SourceMap 字段。

## 不可变导出基线

`build_changeset()` 在提取返回修订前，先把导出 baseline 和返回稿分别投影为 Word 的
reject-changes 语义视图，并要求其**纯文本回填所需范围**一致。它验证 baseline 文件字节
哈希、可见文本、段落/表格结构、复杂域结构与指令、insert/delete/move 修订语义、bookmark 集合和 bookmark
原文。关闭 Track Changes 的编辑、Accept All、锚点删除/重复/漂移，或当前读取器无法安全
解释的结构修订都会以稳定错误码 fail closed；调用方不得改用模糊文本 diff 绕过。

这不是对整篇 Word 的完整视觉或语义等价证明。段落标记修订、格式、OMML 公式、图片、
超链接与 relationship 目标、content control/custom XML、嵌入对象与 AlternateContent 仍需
人工完整性复核，且永远不进入自动补丁范围。验证结果写入
`ChangeSet.payload.baseline_verification`：`status=verified_for_text_patch`、
`automatic_patch_scope=plain_text_only`，并分别列出 `verified_scope` 和
`unverified_scope`；`manual_integrity_review_required` 固定为 `true`。

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
- 原生 bookmark 只是证据，不是 LaTeX 位置。只有调用方用 `BookmarkBinding` 传入已校验
  `SourceMap` 的 `unit_id`、`source_location`、review 长度和 text provenance 后，纯文本
  变更才可标记为 `plain_text_candidate`；否则为 `unmatched/manual`。
- insertion/deletion/replacement 使用 OOXML 节点序号与 bookmark reject-view 相对范围，
  映射为原始 LaTeX 的精确 UTF-8 half-open byte span。重复词不会靠字符串搜索猜位置。
- 折叠空白或 CRLF、combining sequence、variation selector、emoji modifier、ZWJ sequence
  或 regional-indicator 边界不能被证明安全时，变更保留证据但转为 manual。

raw event 的 `rev_...`、change 的 `chg_...`、fragment/change 哈希和 `changes_...` envelope ID 均由规范内容派生。同一 DOCX、限制、绑定和上游哈希会生成相同的证据顺序与 ID；`generated_at` 是 envelope 运行时元数据，需要完全重现 envelope 时应显式传入。

## 验证与局限

E0 公开合成 returned DOCX 的 oracle 测试达到 raw event 与 normalized change 的 precision/recall 各 100%：9 个 raw event 规范化为 7 个 change。定向测试同时覆盖重复/别名 ZIP member、CRC、加密与压缩方法、zip bomb 限制、深位置 DTD/entity、外部关系、读取中变更、缺元数据/删除文本、unknown kind 与所有支持的 story part。

当前边界不推断 SourceMap，不解析 Word 现代 comments extensions 的额外语义，也不将格式/
move/批注自动回填 LaTeX。这些信息会保留为审计证据，由后续审批与补丁规划阶段决定。
