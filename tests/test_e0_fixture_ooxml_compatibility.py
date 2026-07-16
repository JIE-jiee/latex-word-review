from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree  # type: ignore[import-untyped]

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
FIXTURE = Path(__file__).parent / "fixtures/e0-minimal-paper/returned/returned-reviewed.docx"


def test_delete_and_move_source_use_word_compatible_text_elements() -> None:
    with zipfile.ZipFile(FIXTURE) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))

    deletions = root.findall(".//w:del", namespaces=NS)
    move_sources = root.findall(".//w:moveFrom", namespaces=NS)

    assert len(deletions) == 2
    assert len(move_sources) == 1
    assert all(node.findall(".//w:delText", namespaces=NS) for node in deletions)
    assert all(not node.findall(".//w:t", namespaces=NS) for node in deletions)
    assert all(node.findall(".//w:t", namespaces=NS) for node in move_sources)
    assert all(not node.findall(".//w:delText", namespaces=NS) for node in move_sources)
