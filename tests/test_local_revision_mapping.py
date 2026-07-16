"""End-to-end contracts for bookmark-local Word edits and UTF-8 source spans."""

from __future__ import annotations

from pathlib import Path

import pytest

from latex_word_review.canonical import sha256_bytes
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_file
from latex_word_review.ids import new_run_id
from latex_word_review.revisions import BookmarkBinding, build_changeset
from latex_word_review.source_units import build_text_provenance
from tests._docx_factory import write_docx

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
BOOKMARK = "lwr_local_mapping_contract"
RUN_ID = new_run_id(timestamp_ms=1_750_000_000_000, random_bits=19)
HASH = "sha256:" + "a" * 64


def _document(inner: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="{W_NS}"><w:body><w:p>
      <w:bookmarkStart w:id="7" w:name="{BOOKMARK}"/>
      {inner}
      <w:bookmarkEnd w:id="7"/>
    </w:p></w:body></w:document>""".encode()


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _deleted(native_id: int, text: str) -> str:
    return (
        f'<w:del w:id="{native_id}" w:author="Synthetic Reviewer" '
        f'w:date="2026-07-16T06:30:00Z"><w:r><w:delText xml:space="preserve">'
        f"{text}</w:delText></w:r></w:del>"
    )


def _inserted(native_id: int, text: str) -> str:
    return (
        f'<w:ins w:id="{native_id}" w:author="Synthetic Reviewer" '
        f'w:date="2026-07-16T06:30:00Z"><w:r><w:t xml:space="preserve">'
        f"{text}</w:t></w:r></w:ins>"
    )


def _field_marker(marker: str) -> str:
    return f'<w:r><w:fldChar w:fldCharType="{marker}"/></w:r>'


def _complex_field(result: str, *, instruction: str = " REF target ") -> str:
    return (
        _field_marker("begin")
        + f"<w:r><w:instrText>{instruction}</w:instrText></w:r>"
        + _field_marker("separate")
        + result
        + _field_marker("end")
    )


def _binding(raw_source: bytes, *, source_start: int = 100) -> BookmarkBinding:
    review_text, provenance = build_text_provenance(raw_source)
    return BookmarkBinding(
        unit_id="unit_" + "1" * 32,
        source_location={
            "path": "source/main.tex",
            "start_byte": source_start,
            "end_byte": source_start + len(raw_source),
            "slice_sha256": sha256_bytes(raw_source),
            "encoding": "utf-8",
            "newline": "lf",
            "start_line": 1,
            "end_line": 1,
            "start_column": 1,
            "end_column": len(review_text) + 1,
        },
        normalized_text_sha256=sha256_bytes(review_text.encode("utf-8")),
        review_length=len(review_text),
        text_provenance=provenance,
    )


def _changeset(
    tmp_path: Path,
    *,
    baseline_text: str,
    returned_inner: str,
    source_bytes: bytes | None = None,
    baseline_inner: str | None = None,
) -> dict[str, object]:
    baseline = write_docx(
        tmp_path / "baseline.docx",
        document_xml=_document(_run(baseline_text) if baseline_inner is None else baseline_inner),
    )
    returned = write_docx(
        tmp_path / "returned.docx",
        document_xml=_document(returned_inner),
    )
    return build_changeset(
        returned,
        export_baseline_path=baseline,
        export_baseline_sha256=digest_file(baseline, max_bytes=128 * 1024 * 1024).sha256,
        run_id=RUN_ID,
        source_manifest_sha256=HASH,
        source_map_sha256=HASH,
        revision_reader_capabilities_sha256=HASH,
        bookmark_bindings={
            BOOKMARK: _binding(
                baseline_text.encode("utf-8") if source_bytes is None else source_bytes
            )
        },
        generated_at="2026-07-16T12:00:00+09:00",
    )


def test_repeated_word_replacement_uses_the_second_occurrence(tmp_path: Path) -> None:
    document = _changeset(
        tmp_path,
        baseline_text="same same same",
        returned_inner=(
            _run("same ") + _deleted(1, "same") + _inserted(2, "changed") + _run(" same")
        ),
    )

    change = document["payload"]["changes"][0]  # type: ignore[index]
    assert (change["kind"], change["before"], change["after"]) == (
        "replacement",
        "same",
        "changed",
    )
    assert change["resolution"]["status"] == "exact"
    assert change["source_location"]["start_byte"] == 105
    assert change["source_location"]["end_byte"] == 109
    assert change["source_location"]["slice_sha256"] == sha256_bytes(b"same")


def test_multiple_cjk_and_emoji_changes_map_to_independent_byte_spans(tmp_path: Path) -> None:
    document = _changeset(
        tmp_path,
        baseline_text="甲乙🙂丙丁",
        returned_inner=(
            _run("甲乙") + _inserted(1, "新增") + _run("🙂") + _deleted(2, "丙") + _run("丁")
        ),
    )

    changes = document["payload"]["changes"]  # type: ignore[index]
    insertion = next(change for change in changes if change["kind"] == "insertion")
    deletion = next(change for change in changes if change["kind"] == "deletion")
    assert (
        insertion["source_location"]["start_byte"],
        insertion["source_location"]["end_byte"],
    ) == (106, 106)
    assert (
        deletion["source_location"]["start_byte"],
        deletion["source_location"]["end_byte"],
    ) == (110, 113)
    assert all(change["safety_class"] == "plain_text_candidate" for change in changes)


def test_collapsed_source_whitespace_is_reported_but_never_auto_patched(
    tmp_path: Path,
) -> None:
    document = _changeset(
        tmp_path,
        baseline_text="alpha beta",
        returned_inner=_run("alpha") + _deleted(1, " ") + _run("beta"),
        source_bytes=b"alpha\r\n  beta",
    )

    change = document["payload"]["changes"][0]  # type: ignore[index]
    assert change["resolution"] == {
        "status": "unsupported",
        "method": "manual",
        "confidence": 0.0,
        "candidates": [],
    }
    assert change["safety_class"] == "manual_high_risk"
    diagnostics = document["payload"]["diagnostics"]  # type: ignore[index]
    assert any(item["code"] == ErrorCode.MAP_CONFIDENCE_LOW.value for item in diagnostics)


def test_revision_may_not_split_a_combining_character_sequence(tmp_path: Path) -> None:
    document = _changeset(
        tmp_path,
        baseline_text="éclair",
        returned_inner=_deleted(1, "e") + _run("́clair"),
    )

    change = document["payload"]["changes"][0]  # type: ignore[index]
    assert change["resolution"]["status"] == "unsupported"
    assert change["safety_class"] == "manual_high_risk"


@pytest.mark.parametrize(
    ("returned_inner", "expected_context"),
    [
        (
            _run("A")
            + (
                '<w:ins w:id="11" w:author="Synthetic Reviewer" '
                'w:date="2026-07-16T06:30:00Z">'
                '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                "<w:r><w:instrText> REF target </w:instrText></w:r>"
                "<w:r><w:t>field result</w:t></w:r>"
                '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:ins>'
            )
            + _run("B"),
            "p",
        ),
        (
            _run("A")
            + "<w:hyperlink>"
            + _inserted(12, "linked label")
            + "</w:hyperlink>"
            + _run("B"),
            "hyperlink",
        ),
        (
            _run("A")
            + (
                '<w:ins w:id="13" w:author="Synthetic Reviewer" '
                'w:date="2026-07-16T06:30:00Z">'
                "<w:r><w:drawing><w:t>text box</w:t></w:drawing></w:r></w:ins>"
            )
            + _run("B"),
            "p",
        ),
    ],
)
def test_structured_word_insertions_are_never_plain_text_candidates(
    tmp_path: Path,
    returned_inner: str,
    expected_context: str,
) -> None:
    document = _changeset(
        tmp_path,
        baseline_text="AB",
        returned_inner=returned_inner,
    )

    change = document["payload"]["changes"][0]  # type: ignore[index]
    raw_event = document["payload"]["raw_events"][0]  # type: ignore[index]
    assert change["resolution"]["status"] == "unsupported"
    assert change["safety_class"] == "manual_high_risk"
    assert raw_event["evidence"]["content_class"] == "structured"
    assert expected_context in raw_event["evidence"]["structural_context"]
    diagnostics = document["payload"]["diagnostics"]  # type: ignore[index]
    assert any(item["code"] == ErrorCode.REVISION_STRUCTURED_TEXT.value for item in diagnostics)


def test_revision_inside_complex_field_sibling_range_is_manual(tmp_path: Path) -> None:
    baseline_inner = _run("A") + _complex_field(_run("B"))
    returned_inner = _run("A") + _complex_field(_inserted(21, "X") + _run("B"))

    document = _changeset(
        tmp_path,
        baseline_text="AB",
        baseline_inner=baseline_inner,
        returned_inner=returned_inner,
    )

    change = document["payload"]["changes"][0]  # type: ignore[index]
    raw_event = document["payload"]["raw_events"][0]  # type: ignore[index]
    assert change["resolution"]["status"] == "unsupported"
    assert change["safety_class"] == "manual_high_risk"
    assert raw_event["evidence"]["content_class"] == "structured"
    assert "complexField" in raw_event["evidence"]["structural_context"]


def test_nested_complex_field_revision_is_manual(tmp_path: Path) -> None:
    baseline_inner = _complex_field(
        _complex_field(_run("AB"), instruction=" REF inner "),
        instruction=" REF outer ",
    )
    returned_inner = _complex_field(
        _complex_field(_run("A") + _inserted(22, "X") + _run("B"), instruction=" REF inner "),
        instruction=" REF outer ",
    )

    document = _changeset(
        tmp_path,
        baseline_text="AB",
        baseline_inner=baseline_inner,
        returned_inner=returned_inner,
    )

    raw_event = document["payload"]["raw_events"][0]  # type: ignore[index]
    change = document["payload"]["changes"][0]  # type: ignore[index]
    assert change["safety_class"] == "manual_high_risk"
    assert "complexField" in raw_event["evidence"]["structural_context"]


def test_malformed_complex_field_makes_paragraph_revision_manual(tmp_path: Path) -> None:
    baseline_inner = _field_marker("begin") + _run("AB")
    returned_inner = _field_marker("begin") + _run("A") + _inserted(23, "X") + _run("B")

    document = _changeset(
        tmp_path,
        baseline_text="AB",
        baseline_inner=baseline_inner,
        returned_inner=returned_inner,
    )

    raw_event = document["payload"]["raw_events"][0]  # type: ignore[index]
    change = document["payload"]["changes"][0]  # type: ignore[index]
    assert change["safety_class"] == "manual_high_risk"
    assert "complexFieldMalformed" in raw_event["evidence"]["structural_context"]


def test_untracked_complex_field_injection_fails_baseline_gate(tmp_path: Path) -> None:
    returned_inner = _run("A") + _complex_field(_inserted(24, "X") + _run("B"))

    with pytest.raises(ContractError) as raised:
        _changeset(
            tmp_path,
            baseline_text="AB",
            returned_inner=returned_inner,
        )

    assert raised.value.code is ErrorCode.REVISION_BASELINE_DRIFT
