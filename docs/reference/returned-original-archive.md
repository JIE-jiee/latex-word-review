# 返回 Word 原件不可变归档

`archive_returned_docx` 是修订解析之前的第一道门。它把收到的 `.docx` 原始字节复制到一个全新目录，使用固定名称 `returned-original.docx`，并写入规范化 `returned-original.manifest.json`。清单绑定 run、导出审阅 DOCX 的哈希、返回件哈希、大小和保密级别，不记录导师文件名或绝对路径。

发布使用同级临时目录与原子 rename。复制前后都会重新读取来源并逐字节比较；目标已存在时只允许完全一致的幂等复用，任何冲突或篡改均返回 `E_HASH_RETURNED_ORIGINAL_MISMATCH`，不会覆盖旧证据。归档内只允许清单和 DOCX 两个普通文件，发布前设置为只读。

```python
from pathlib import Path

from latex_word_review.ingest import archive_returned_docx, verify_returned_archive

archive = archive_returned_docx(
    Path("received/review.docx"),
    Path("run/returned"),
    run_id="run_019b0000-0000-7000-8000-000000000001",
    exported_docx_sha256="sha256:" + "a" * 64,
)
verified = verify_returned_archive(
    archive.directory,
    expected_run_id=archive.run_id,
    expected_returned_docx_sha256=archive.returned_docx_sha256,
)
```

后续 OOXML 读取器只能接收 `verified.docx_path`。接受或拒绝 Word 修订不在该步骤发生；审批只写独立的 `ApprovalSet`，回填只发生在新 LaTeX 工作副本中。
