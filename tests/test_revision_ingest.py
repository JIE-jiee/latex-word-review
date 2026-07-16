"""Contract, oracle, and adversarial tests for canonical revision ingestion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from latex_word_review.canonical import sha256_bytes
from latex_word_review.contracts import validate_contract
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.ids import new_run_id
from latex_word_review.revisions import (
    BookmarkBinding,
    build_changeset,
    extract_revision_events,
    normalize_revision_changes,
)
from tests._docx_factory import write_docx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "e0-minimal-paper"
RETURNED_DOCX = FIXTURE_ROOT / "returned" / "returned-reviewed.docx"
EXPECTED_CHANGESET = FIXTURE_ROOT / "expected-changeset.json"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
HASH_A = "sha256:" + "a" * 64
FIXED_RUN_ID = new_run_id(timestamp_ms=1_750_000_000_000, random_bits=1)


def expected_oracle() -> dict[str, Any]:
    if not EXPECTED_CHANGESET.is_file():
        pytest.skip("public E0 fixture is intentionally absent from this distribution")
    return cast("dict[str, Any]", json.loads(EXPECTED_CHANGESET.read_text(encoding="utf-8")))


def test_e0_raw_and_normalized_precision_recall_are_100_percent() -> None:
    expected = expected_oracle()
    extraction = extract_revision_events(RETURNED_DOCX)
    normalization = normalize_revision_changes(extraction)

    element_to_kind = {
        "w:ins": "insert",
        "w:del": "delete",
        "w:moveFrom": "move_from",
        "w:moveTo": "move_to",
        "w:rPrChange": "run_format",
        "w:comment": "comment",
    }
    expected_raw = {
        (element_to_kind[raw["element"]], raw["w_id"])
        for change in expected["changes"]
        for raw in change["raw_events"]
    }
    actual_raw = {
        (event.raw_event["kind"], event.raw_event["native_id"]) for event in extraction.events
    }
    true_positive_raw = expected_raw & actual_raw
    assert len(true_positive_raw) / len(actual_raw) == 1.0
    assert len(true_positive_raw) / len(expected_raw) == 1.0

    expected_kind = {
        "insertion": "insertion",
        "deletion": "deletion",
        "replacement": "replacement",
        "move": "move",
        "run_format": "format",
        "comment": "comment",
    }
    expected_changes = {
        (expected_kind[change["kind"]], change["unit_id"]) for change in expected["changes"]
    }
    actual_changes = {
        (change.change["kind"], change.bookmark_name) for change in normalization.changes
    }
    true_positive_changes = expected_changes & actual_changes
    assert len(true_positive_changes) / len(actual_changes) == 1.0
    assert len(true_positive_changes) / len(expected_changes) == 1.0

    raw_by_id = {event.raw_event["raw_event_id"]: event for event in extraction.events}
    change_by_bookmark = {item.bookmark_name: item.change for item in normalization.changes}
    for expected_change in expected["changes"]:
        actual = change_by_bookmark[expected_change["unit_id"]]
        assert actual["author"] == expected_change["author"]
        assert actual["timestamp"] == expected_change["timestamp"]
        native_ids = {
            raw_by_id[event_id].raw_event["native_id"] for event_id in actual["raw_event_ids"]
        }
        assert native_ids == {item["w_id"] for item in expected_change["raw_events"]}
        kind = expected_change["kind"]
        if kind in {"insertion", "deletion", "replacement", "move"}:
            assert actual["before"] == expected_change["before"]
            assert actual["after"] == expected_change["after"]
        elif kind == "comment":
            assert actual["comment"] == expected_change["comment"]


def test_e0_move_format_and_both_comment_anchor_forms_are_complete() -> None:
    extraction = extract_revision_events(RETURNED_DOCX)
    by_native_id = {event.raw_event["native_id"]: event for event in extraction.events}

    move_from = by_native_id["105"]
    move_to = by_native_id["106"]
    assert move_from.raw_event["range"] == {
        "start_native_id": "301",
        "end_native_id": "301",
        "pair_native_id": "302",
        "anchor_kind": "paired_move",
    }
    assert move_to.raw_event["range"] == {
        "start_native_id": "302",
        "end_native_id": "302",
        "pair_native_id": "301",
        "anchor_kind": "paired_move",
    }
    assert move_from.move_name == move_to.move_name == "move-001"

    run_format = by_native_id["107"].raw_event
    assert run_format["content"]["text"] == "critical observation"
    assert run_format["content"]["format_before"]["bold"] is False
    assert run_format["content"]["format_after"]["bold"] is True

    range_comment = by_native_id["201"]
    point_comment = by_native_id["202"]
    assert range_comment.raw_event["range"]["anchor_kind"] == "range"
    assert range_comment.anchor_text == (
        "Range comment anchor: verify the phrase normalized residual before approval."
    )
    assert point_comment.raw_event["range"]["anchor_kind"] == "point"
    assert point_comment.anchor_text == ""


def test_ids_fragment_hashes_and_changes_are_repeatable() -> None:
    first = extract_revision_events(RETURNED_DOCX)
    second = extract_revision_events(RETURNED_DOCX)
    first_evidence = [
        (item.raw_event["raw_event_id"], item.raw_event["evidence"]["fragment_sha256"])
        for item in first.events
    ]
    second_evidence = [
        (item.raw_event["raw_event_id"], item.raw_event["evidence"]["fragment_sha256"])
        for item in second.events
    ]
    assert first_evidence == second_evidence
    assert [item.change["change_id"] for item in normalize_revision_changes(first).changes] == [
        item.change["change_id"] for item in normalize_revision_changes(second).changes
    ]


def test_builds_a_valid_changeset_and_only_promotes_source_map_bound_text() -> None:
    location = {
        "path": "source/main.tex",
        "start_byte": 0,
        "end_byte": 0,
        "slice_sha256": sha256_bytes(b""),
        "encoding": "utf-8",
        "newline": "lf",
        "start_line": 1,
        "end_line": 1,
        "start_column": 1,
        "end_column": 1,
    }
    binding = BookmarkBinding(
        unit_id="unit_" + "1" * 32,
        source_location=location,
    )
    document = build_changeset(
        RETURNED_DOCX,
        run_id=FIXED_RUN_ID,
        source_manifest_sha256=HASH_A,
        source_map_sha256=HASH_A,
        revision_reader_capabilities_sha256=HASH_A,
        returned_artifact_path="ingest/e0-returned-original.docx",
        confidentiality="public_fixture",
        bookmark_bindings={"txr_insert_001": binding},
        generated_at="2026-07-16T12:00:00+09:00",
    )
    receipt = validate_contract(document)
    assert receipt.schema_name == "ChangeSet"
    assert document["payload"]["returned_original"]["immutable"] is True
    assert len(document["payload"]["raw_events"]) == 9
    assert len(document["payload"]["changes"]) == 7
    insertion = next(item for item in document["payload"]["changes"] if item["kind"] == "insertion")
    assert insertion["resolution"] == {
        "status": "exact",
        "method": "bookmark",
        "confidence": 1.0,
        "candidates": [],
    }
    assert insertion["safety_class"] == "plain_text_candidate"
    unbound = [item for item in document["payload"]["changes"] if item is not insertion]
    assert all(item["safety_class"] != "plain_text_candidate" for item in unbound)


def test_missing_author_and_timestamp_are_null_with_diagnostics(tmp_path: Path) -> None:
    document_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="{W_NS}"><w:body><w:p>
      <w:ins w:id="1"><w:r><w:t>new</w:t></w:r></w:ins>
    </w:p></w:body></w:document>""".encode()
    extraction = extract_revision_events(
        write_docx(tmp_path / "missing-meta.docx", document_xml=document_xml)
    )
    event = extraction.events[0].raw_event
    assert event["author"] is None
    assert event["timestamp"] is None
    assert {item["code"] for item in event["diagnostics"]} == {
        ErrorCode.REVISION_AUTHOR_MISSING.value,
        ErrorCode.REVISION_TIMESTAMP_MISSING.value,
    }


def test_missing_deleted_text_fails_closed(tmp_path: Path) -> None:
    document_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="{W_NS}"><w:body><w:p>
      <w:del w:id="2" w:author="Reviewer" w:date="2026-01-01T10:00:00+08:00">
        <w:r><w:t>not deletion evidence</w:t></w:r>
      </w:del>
    </w:p></w:body></w:document>""".encode()
    path = write_docx(tmp_path / "missing-deltext.docx", document_xml=document_xml)
    with pytest.raises(ContractError) as caught:
        extract_revision_events(path)
    assert caught.value.code is ErrorCode.REVISION_DELETE_TEXT_MISSING


def test_unknown_revision_is_preserved_and_denied(tmp_path: Path) -> None:
    document_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="{W_NS}"><w:body><w:sectPr>
      <w:sectPrChange w:id="9" w:author="Reviewer" w:date="2026-01-01T10:00:00+08:00">
        <w:sectPr/>
      </w:sectPrChange>
    </w:sectPr></w:body></w:document>""".encode()
    extraction = extract_revision_events(
        write_docx(tmp_path / "unknown.docx", document_xml=document_xml)
    )
    event = extraction.events[0].raw_event
    assert event["kind"] == "unknown"
    assert event["native_kind"] == "w:sectPrChange"
    change = normalize_revision_changes(extraction).changes[0].change
    assert change["kind"] == "unknown"
    assert change["safety_class"] == "denied_unknown"


def test_traverses_all_supported_story_parts_and_comments(tmp_path: Path) -> None:
    def insertion_root(root_name: str, native_id: str) -> bytes:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
        <w:{root_name} xmlns:w="{W_NS}"><w:p>
          <w:ins w:id="{native_id}" w:author="Reviewer"
            w:date="2026-01-01T10:00:00+08:00">
            <w:r><w:t>{root_name}</w:t></w:r>
          </w:ins>
        </w:p></w:{root_name}>""".encode()

    document_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="{W_NS}"><w:body><w:p>
      <w:ins w:id="1" w:author="Reviewer" w:date="2026-01-01T10:00:00+08:00">
        <w:r><w:t>document</w:t></w:r>
      </w:ins>
      <w:commentRangeStart w:id="9"/>
      <w:r><w:t>anchor</w:t></w:r>
      <w:commentRangeEnd w:id="9"/>
      <w:r><w:commentReference w:id="9"/></w:r>
    </w:p></w:body></w:document>""".encode()
    footnotes = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:footnotes xmlns:w="{W_NS}"><w:footnote w:id="1"><w:p>
      <w:ins w:id="4" w:author="Reviewer" w:date="2026-01-01T10:00:00+08:00">
        <w:r><w:t>footnote</w:t></w:r>
      </w:ins>
    </w:p></w:footnote></w:footnotes>""".encode()
    endnotes = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:endnotes xmlns:w="{W_NS}"><w:endnote w:id="1"><w:p>
      <w:ins w:id="5" w:author="Reviewer" w:date="2026-01-01T10:00:00+08:00">
        <w:r><w:t>endnote</w:t></w:r>
      </w:ins>
    </w:p></w:endnote></w:endnotes>""".encode()
    comments = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:comments xmlns:w="{W_NS}">
      <w:comment w:id="9" w:author="Reviewer" w:date="2026-01-01T10:00:00+08:00">
        <w:p><w:r><w:t>comment</w:t></w:r></w:p>
      </w:comment>
    </w:comments>""".encode()
    relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="header" Target="header1.xml"/>
      <Relationship Id="rId2" Type="footer" Target="footer1.xml"/>
      <Relationship Id="rId3" Type="footnotes" Target="footnotes.xml"/>
      <Relationship Id="rId4" Type="endnotes" Target="endnotes.xml"/>
      <Relationship Id="rId5" Type="comments" Target="comments.xml"/>
    </Relationships>"""
    path = write_docx(
        tmp_path / "stories.docx",
        document_xml=document_xml,
        document_rels=relationships,
        extra_parts={
            "word/header1.xml": insertion_root("hdr", "2"),
            "word/footer1.xml": insertion_root("ftr", "3"),
            "word/footnotes.xml": footnotes,
            "word/endnotes.xml": endnotes,
            "word/comments.xml": comments,
        },
    )
    extraction = extract_revision_events(path)
    assert [item.raw_event["part_uri"] for item in extraction.events] == [
        "word/document.xml",
        "word/header1.xml",
        "word/footer1.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
        "word/comments.xml",
    ]
