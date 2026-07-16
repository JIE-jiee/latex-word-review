"""Clean-room helpers that simulate a public Word tracked-change round trip."""

from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree  # type: ignore[import-untyped]

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DOCUMENT_PART = "word/document.xml"


def create_whole_bookmark_replacement(
    base_docx: Path,
    returned_docx: Path,
    *,
    bookmark_name: str,
    before: str,
    after: str,
    author: str = "Synthetic Roundtrip Reviewer",
    timestamp: str = "2026-07-16T06:30:00Z",
) -> None:
    """Replace one complete exported review unit with paired w:del/w:ins."""

    with zipfile.ZipFile(base_docx, "r") as source:
        infos = source.infolist()
        members = {info.filename: source.read(info) for info in infos}
    root = etree.fromstring(members[DOCUMENT_PART])
    starts = root.xpath(
        ".//w:bookmarkStart[@w:name=$name]",
        namespaces={"w": W_NS},
        name=bookmark_name,
    )
    if len(starts) != 1:
        raise ValueError("synthetic replacement requires exactly one bookmark")
    start = starts[0]
    native_id = start.get(f"{{{W_NS}}}id")
    parent = start.getparent()
    if parent is None or native_id is None:
        raise ValueError("synthetic bookmark is incomplete")
    ends = parent.xpath(
        "./w:bookmarkEnd[@w:id=$native_id]",
        namespaces={"w": W_NS},
        native_id=native_id,
    )
    if len(ends) != 1:
        raise ValueError("synthetic bookmark end is incomplete")
    end = ends[0]
    start_index = parent.index(start)
    end_index = parent.index(end)
    if end_index <= start_index:
        raise ValueError("synthetic bookmark range is invalid")
    for child in list(parent)[start_index + 1 : end_index]:
        parent.remove(child)

    deleted = etree.Element(f"{{{W_NS}}}del")
    deleted.set(f"{{{W_NS}}}id", "9101")
    deleted.set(f"{{{W_NS}}}author", author)
    deleted.set(f"{{{W_NS}}}date", timestamp)
    deleted_run = etree.SubElement(deleted, f"{{{W_NS}}}r")
    deleted_text = etree.SubElement(deleted_run, f"{{{W_NS}}}delText")
    deleted_text.set(f"{{{XML_NS}}}space", "preserve")
    deleted_text.text = before

    inserted = etree.Element(f"{{{W_NS}}}ins")
    inserted.set(f"{{{W_NS}}}id", "9102")
    inserted.set(f"{{{W_NS}}}author", author)
    inserted.set(f"{{{W_NS}}}date", timestamp)
    inserted_run = etree.SubElement(inserted, f"{{{W_NS}}}r")
    inserted_text = etree.SubElement(inserted_run, f"{{{W_NS}}}t")
    inserted_text.set(f"{{{XML_NS}}}space", "preserve")
    inserted_text.text = after
    parent.insert(start_index + 1, deleted)
    parent.insert(start_index + 2, inserted)

    members[DOCUMENT_PART] = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    returned_docx.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(returned_docx, "x") as target:
        for info in infos:
            target.writestr(info, members[info.filename])
