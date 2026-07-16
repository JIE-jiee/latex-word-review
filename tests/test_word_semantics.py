from __future__ import annotations

from pathlib import Path

import pytest

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.word_semantics import (
    CAPABILITIES,
    WordSemanticLimits,
    compare_export_baseline,
    project_word_semantics,
)

from ._docx_factory import write_docx

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _document(body: str, *, extra_namespaces: str = "") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}" {extra_namespaces}>
  <w:body>{body}</w:body>
</w:document>""".encode()


def _baseline_xml(text: str = "A old C") -> bytes:
    return _document(
        f"""<w:p>
  <w:bookmarkStart w:id="1" w:name="unit_alpha"/>
  <w:r><w:t>{text}</w:t></w:r>
  <w:bookmarkEnd w:id="1"/>
</w:p>"""
    )


def test_save_equivalent_noise_and_run_splitting_are_ignored(tmp_path: Path) -> None:
    baseline = write_docx(
        tmp_path / "baseline.docx",
        document_xml=_document(
            """<w:p w:rsidR="00000001">
  <w:bookmarkStart w:id="1" w:name="unit_alpha"/>
  <w:r><w:t xml:space="preserve">Hello </w:t></w:r><w:r><w:t>world</w:t></w:r>
  <w:bookmarkEnd w:id="1"/>
</w:p>"""
        ),
    )
    returned = write_docx(
        tmp_path / "returned.docx",
        document_xml=_document(
            """<w:p w:rsidR="FFFFFFFF" w:rsidRDefault="ABCDEF12">
  <w:bookmarkStart w:id="99" w:name="unit_alpha"/>
  <w:proofErr w:type="spellStart"/>
  <w:r><w:t>Hello</w:t></w:r><w:r><w:t xml:space="preserve"> </w:t></w:r>
  <w:proofErr w:type="spellEnd"/><w:r><w:t>world</w:t></w:r>
  <w:bookmarkEnd w:id="99"/>
</w:p>"""
        ),
        extra_parts={"docProps/noise.xml": b"<noise value='zip serialization'/>"},
    )

    comparison = compare_export_baseline(baseline, returned, ["unit_alpha"])

    assert comparison.drift_status == "clean"
    assert comparison.bookmark_issues == ()
    assert comparison.safe_for_automatic_patch
    assert comparison.baseline.file_sha256 != comparison.returned.file_sha256


def test_reject_and_accept_views_preserve_tokens_and_revision_coordinates(
    tmp_path: Path,
) -> None:
    returned = write_docx(
        tmp_path / "tracked.docx",
        document_xml=_document(
            """<w:p>
  <w:bookmarkStart w:id="1" w:name="unit_alpha"/>
  <w:r><w:t>A </w:t></w:r>
  <w:del w:id="2"><w:r><w:delText>old</w:delText></w:r></w:del>
  <w:ins w:id="3"><w:r><w:t>new</w:t></w:r></w:ins>
  <w:r><w:tab/><w:t>C</w:t><w:br/><w:t>D</w:t><w:cr/>
    <w:noBreakHyphen/><w:softHyphen/><w:t>E</w:t></w:r>
  <w:bookmarkEnd w:id="1"/>
</w:p>"""
        ),
    )

    projection = project_word_semantics(returned)
    story = projection.story("word/document.xml")

    assert story is not None
    assert story.original.text == "A old\tC\nD\n‑­E"
    assert story.final.text == "A new\tC\nD\n‑­E"
    assert [atom.kind for atom in story.original.atoms].count("line_break") == 2
    deletion, insertion = story.revision_spans
    assert (deletion.kind, deletion.base_start, deletion.base_end) == ("delete", 2, 5)
    assert (insertion.kind, insertion.base_start, insertion.base_end) == ("insert", 5, 5)
    assert story.revision_span(deletion.node_ordinal) == deletion

    bookmark = story.bookmarks[0]
    assert bookmark.original is not None
    assert bookmark.original.start == 0
    assert bookmark.original.text == story.original.text
    assert [
        (span.node_ordinal, span.relative_start, span.relative_end)
        for span in bookmark.revision_spans
    ] == [
        (deletion.node_ordinal, 2, 5),
        (insertion.node_ordinal, 5, 5),
    ]


def test_tracked_insert_delete_and_move_leave_reject_baseline_invariant(
    tmp_path: Path,
) -> None:
    baseline = write_docx(
        tmp_path / "baseline.docx",
        document_xml=_baseline_xml("A old moved B C"),
    )
    returned = write_docx(
        tmp_path / "returned.docx",
        document_xml=_document(
            """<w:p>
  <w:bookmarkStart w:id="1" w:name="unit_alpha"/>
  <w:r><w:t>A </w:t></w:r>
  <w:del w:id="2"><w:r><w:delText>old</w:delText></w:r></w:del>
  <w:ins w:id="3"><w:r><w:t>new</w:t></w:r></w:ins>
  <w:r><w:t xml:space="preserve"> </w:t></w:r>
  <w:moveFromRangeStart w:id="10" w:name="move-1"/>
  <w:moveFrom w:id="11"><w:r><w:t xml:space="preserve">moved </w:t></w:r></w:moveFrom>
  <w:moveFromRangeEnd w:id="10"/>
  <w:r><w:t>B C</w:t></w:r>
  <w:moveToRangeStart w:id="20" w:name="move-1"/>
  <w:moveTo w:id="21"><w:r><w:t xml:space="preserve"> moved</w:t></w:r></w:moveTo>
  <w:moveToRangeEnd w:id="20"/>
  <w:bookmarkEnd w:id="1"/>
</w:p>"""
        ),
    )

    comparison = compare_export_baseline(baseline, returned, ["unit_alpha"])
    story = comparison.returned.story("word/document.xml")

    assert story is not None
    assert story.original.text == "A old moved B C"
    assert story.final.text == "A new B C moved"
    assert comparison.drift_status == "clean"
    assert comparison.bookmark_issues == ()
    move_from = next(span for span in story.revision_spans if span.kind == "move_from")
    move_to = next(span for span in story.revision_spans if span.kind == "move_to")
    assert move_from.base_end > move_from.base_start
    assert move_to.base_start == move_to.base_end


@pytest.mark.parametrize("returned_text", ["A new C", "A silently changed C"])
def test_accept_all_or_untracked_edit_is_detected(
    tmp_path: Path,
    returned_text: str,
) -> None:
    baseline = write_docx(tmp_path / "baseline.docx", document_xml=_baseline_xml())
    returned = write_docx(
        tmp_path / "returned.docx",
        document_xml=_baseline_xml(returned_text),
    )

    comparison = compare_export_baseline(baseline, returned, ["unit_alpha"])

    assert comparison.accepted_or_untracked_drift
    assert comparison.drift_status == "accepted_or_untracked_drift"
    assert not comparison.safe_for_automatic_patch
    assert comparison.story_differences[0].text_changed


def test_missing_and_duplicate_bookmarks_are_reported_separately(tmp_path: Path) -> None:
    baseline = write_docx(tmp_path / "baseline.docx", document_xml=_baseline_xml("same"))
    missing = write_docx(
        tmp_path / "missing.docx",
        document_xml=_document("<w:p><w:r><w:t>same</w:t></w:r></w:p>"),
    )
    duplicate = write_docx(
        tmp_path / "duplicate.docx",
        document_xml=_document(
            """<w:p>
  <w:bookmarkStart w:id="1" w:name="unit_alpha"/>
  <w:bookmarkStart w:id="2" w:name="unit_alpha"/>
  <w:r><w:t>same</w:t></w:r>
  <w:bookmarkEnd w:id="2"/><w:bookmarkEnd w:id="1"/>
</w:p>"""
        ),
    )

    missing_result = compare_export_baseline(baseline, missing, ["unit_alpha"])
    duplicate_result = compare_export_baseline(baseline, duplicate, ["unit_alpha"])

    assert missing_result.drift_status == "clean"
    assert missing_result.missing_bookmarks == ("unit_alpha",)
    assert duplicate_result.drift_status == "clean"
    assert duplicate_result.duplicate_bookmarks == ("unit_alpha",)
    assert not missing_result.safe_for_automatic_patch
    assert not duplicate_result.safe_for_automatic_patch


def test_bookmark_removed_only_in_accept_view_is_not_considered_safe(tmp_path: Path) -> None:
    baseline = write_docx(tmp_path / "baseline.docx", document_xml=_baseline_xml("same"))
    returned = write_docx(
        tmp_path / "returned.docx",
        document_xml=_document(
            """<w:p><w:del w:id="5">
  <w:bookmarkStart w:id="1" w:name="unit_alpha"/>
  <w:r><w:delText>same</w:delText></w:r>
  <w:bookmarkEnd w:id="1"/>
</w:del></w:p>"""
        ),
    )

    result = compare_export_baseline(baseline, returned, ["unit_alpha"])

    assert result.drift_status == "clean"
    assert result.missing_bookmarks == ("unit_alpha",)


def test_unknown_revision_markup_fails_closed(tmp_path: Path) -> None:
    path = write_docx(
        tmp_path / "unknown.docx",
        document_xml=_document(
            """<w:p><w:futureRevision w:id="1">
  <w:r><w:t>x</w:t></w:r>
</w:futureRevision></w:p>"""
        ),
    )

    with pytest.raises(ContractError) as exc_info:
        project_word_semantics(path)

    assert exc_info.value.code is ErrorCode.DOCX_INVALID_PACKAGE


def test_known_format_revision_is_ignored_with_explicitly_unsupported_capability(
    tmp_path: Path,
) -> None:
    path = write_docx(
        tmp_path / "format.docx",
        document_xml=_document(
            """<w:p><w:r><w:rPr><w:b/>
  <w:rPrChange w:id="1"><w:rPr/></w:rPrChange>
</w:rPr><w:t>x</w:t></w:r></w:p>"""
        ),
    )

    projection = project_word_semantics(path)

    assert projection.stories[0].original.text == "x"
    assert not projection.capabilities.formatting


def test_complex_field_markers_and_instruction_are_part_of_baseline_structure(
    tmp_path: Path,
) -> None:
    def field(instruction: str) -> bytes:
        return _document(
            f"""<w:p><w:r><w:t>A</w:t></w:r>
  <w:r><w:fldChar w:fldCharType="begin"/></w:r>
  <w:r><w:instrText>{instruction}</w:instrText></w:r>
  <w:r><w:fldChar w:fldCharType="separate"/></w:r>
  <w:r><w:t>B</w:t></w:r>
  <w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>"""
        )

    baseline = write_docx(tmp_path / "baseline-field.docx", document_xml=field(" REF alpha "))
    changed_instruction = write_docx(
        tmp_path / "returned-field.docx",
        document_xml=field(" REF beta "),
    )
    plain_text_only = write_docx(
        tmp_path / "returned-plain.docx",
        document_xml=_document("<w:p><w:r><w:t>AB</w:t></w:r></w:p>"),
    )

    instruction_result = compare_export_baseline(baseline, changed_instruction, [])
    marker_result = compare_export_baseline(baseline, plain_text_only, [])

    assert instruction_result.story_differences[0].structure_changed
    assert marker_result.story_differences[0].structure_changed
    assert not instruction_result.story_differences[0].text_changed
    assert not marker_result.story_differences[0].text_changed


def test_committed_review_fixture_is_reject_equivalent_to_its_export_baseline() -> None:
    fixture_root = Path(__file__).parent / "fixtures" / "e0-minimal-paper"
    baseline = fixture_root / "base" / "review-base.docx"
    returned = fixture_root / "returned" / "returned-reviewed.docx"
    baseline_projection = project_word_semantics(baseline)
    expected_bookmarks = [
        bookmark.name
        for story in baseline_projection.stories
        for bookmark in story.bookmarks
        if not bookmark.name.startswith("_")
    ]

    comparison = compare_export_baseline(
        baseline_projection,
        returned,
        expected_bookmarks,
    )
    returned_story = comparison.returned.story("word/document.xml")

    assert comparison.drift_status == "clean"
    assert comparison.bookmark_issues == ()
    assert returned_story is not None
    assert returned_story.original.text != returned_story.final.text
    assert len(returned_story.revision_spans) == 6


def test_capability_boundary_and_projection_resource_limit_are_explicit(tmp_path: Path) -> None:
    path = write_docx(
        tmp_path / "limited.docx",
        document_xml=_document("<w:p><w:r><w:t>x</w:t></w:r></w:p>"),
    )

    assert CAPABILITIES.visible_text
    assert CAPABILITIES.paragraph_table_structure
    assert CAPABILITIES.field_structure
    assert not CAPABILITIES.formatting
    assert not CAPABILITIES.omml
    assert not CAPABILITIES.images
    assert "OMML equation semantics" in CAPABILITIES.unsupported
    with pytest.raises(ContractError) as exc_info:
        project_word_semantics(path, semantic_limits=WordSemanticLimits(max_atoms_per_view=1))
    assert exc_info.value.code is ErrorCode.DOCX_INVALID_PACKAGE
