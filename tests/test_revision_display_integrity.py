"""Security regressions for source-bound static revision-display evidence."""

from __future__ import annotations

import shutil
import stat
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

import latex_word_review.revision_display as revision_display_module
import latex_word_review.word_fields as word_fields_module
import latex_word_review.workflow as workflow_module
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import read_stable_bytes
from latex_word_review.revision_display import (
    inspect_revision_display,
    validate_revision_display,
)
from latex_word_review.revision_macros import (
    AliasRevisionMacroCounts,
    CanonicalRevisionMacroCounts,
    RevisionDisplayExpectation,
    RevisionDisplaySegment,
    RevisionMacroInventory,
)
from latex_word_review.workflow import export_workflow, initialize_workflow
from tests.conftest import fake_invoke_word
from tests.test_revision_display_workflow import TIME

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
W = f"{{{W_NS}}}"
M = f"{{{M_NS}}}"
R = f"{{{R_NS}}}"
REL = f"{{{PKG_REL_NS}}}"


def _add_portable_optional_protected_parts(path: Path) -> None:
    """Add valid theme/font-table parts that Word would otherwise supply."""

    with zipfile.ZipFile(path, mode="r") as source:
        infos = source.infolist()
        members = {info.filename: source.read(info.filename) for info in infos}
    optional_parts = {
        "word/fontTable.xml": (
            f'<w:fonts xmlns:w="{W_NS}"><w:font w:name="LWR Baseline"/></w:fonts>'
        ).encode(),
        "word/theme/theme1.xml": (
            b'<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            b'name="LWR Baseline"><a:themeElements><a:clrScheme name="LWR">'
            b'<a:dk1><a:srgbClr val="000000"/></a:dk1></a:clrScheme>'
            b"</a:themeElements></a:theme>"
        ),
    }
    if all(name in members for name in optional_parts):
        return
    if any(name in members for name in optional_parts):
        raise AssertionError("portable protected-part fixture is incomplete")

    content_types = ET.fromstring(members["[Content_Types].xml"])
    for part_name, content_type in (
        (
            "/word/fontTable.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml",
        ),
        (
            "/word/theme/theme1.xml",
            "application/vnd.openxmlformats-officedocument.theme+xml",
        ),
    ):
        override = ET.SubElement(content_types, f"{{{CONTENT_TYPES_NS}}}Override")
        override.set("PartName", part_name)
        override.set("ContentType", content_type)
    members["[Content_Types].xml"] = ET.tostring(
        content_types,
        encoding="utf-8",
        xml_declaration=True,
    )

    relationships_name = "word/_rels/document.xml.rels"
    relationships = ET.fromstring(members[relationships_name])
    for relationship_id, relationship_type, target in (
        (
            "rIdLwrFontTable",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable",
            "fontTable.xml",
        ),
        (
            "rIdLwrTheme",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme",
            "theme/theme1.xml",
        ),
    ):
        relationship = ET.SubElement(relationships, f"{{{PKG_REL_NS}}}Relationship")
        relationship.set("Id", relationship_id)
        relationship.set("Type", relationship_type)
        relationship.set("Target", target)
    members[relationships_name] = ET.tostring(
        relationships,
        encoding="utf-8",
        xml_declaration=True,
    )
    members.update(optional_parts)

    replacement = path.with_name(f".{path.name}.portable-parts")
    original_mode = stat.S_IMODE(path.stat().st_mode)
    try:
        with zipfile.ZipFile(replacement, mode="x") as destination:
            for info in infos:
                destination.writestr(info, members[info.filename])
            for name in sorted(optional_parts):
                destination.writestr(name, members[name])
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        replacement.replace(path)
    finally:
        if path.exists():
            path.chmod(original_mode)
        if replacement.exists():
            replacement.unlink()


@pytest.fixture(scope="module")
def rich_revision_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("revision-display-integrity")
    origin = root / "origin"
    assets = origin / "assets"
    assets.mkdir(parents=True)
    fixture = Path("tests/fixtures/e0-minimal-paper/source/assets/response-curve.png")
    shutil.copyfile(fixture, assets / "first.png")
    shutil.copyfile(fixture, assets / "second.png")
    second = assets / "second.png"
    second.write_bytes(second.read_bytes() + b"LWR-DISTINCT-IMAGE")
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\usepackage{graphicx}\n"
        "\\begin{document}\n\n"
        "Stable structure paragraph.\n\n"
        "\\begin{equation} x+y=z \\label{eq:first} \\end{equation}\n\n"
        "\\begin{equation} a=b \\label{eq:second} \\end{equation}\n\n"
        "References \\ref{eq:first} and \\ref{eq:second}.\n\n"
        "\\includegraphics[width=2cm]{assets/first.png}\n\n"
        "\\includegraphics[width=2cm]{assets/second.png}\n\n"
        "\\begin{tabular}{ll}\n"
        "A & B \\\\\n"
        "C & D \\\\\n"
        "\\end{tabular}\n\n"
        "Revision marker: \\added{LWRINTEGRITYREV}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    run = root / "run"
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )
    snapshot = run / "snapshot"
    discovery = discover_project(snapshot, main_document="main.tex")
    expectation = RevisionDisplayExpectation(
        source_path="main.tex",
        source_character_offset=0,
        source_call_sha256="sha256:" + "0" * 64,
        segments=(RevisionDisplaySegment("LWRINTEGRITYREV", False),),
        macro_instances=1,
    )
    inventory = RevisionMacroInventory(
        source_tree_sha256=discovery.source_tree_sha256,
        tex_files=len(discovery.files),
        canonical_counts=CanonicalRevisionMacroCounts(added=1),
        alias_counts=AliasRevisionMacroCounts(),
        skipped_dynamic_regions=0,
        display_expectations=(expectation,),
    )
    patcher = pytest.MonkeyPatch()
    patcher.setattr(workflow_module, "scan_revision_macros", lambda *_args, **_kwargs: inventory)
    patcher.setattr(word_fields_module, "_invoke_word", fake_invoke_word)
    try:
        export_workflow(
            run,
            confidentiality="public_fixture",
            generated_at=TIME,
        )
    finally:
        patcher.undo()
    _add_portable_optional_protected_parts(run / "export/review.docx")
    _add_portable_optional_protected_parts(run / "export/existing-changes-display.docx")
    return run


def _inventory(run: Path) -> RevisionMacroInventory:
    snapshot = run / "snapshot"
    discovery = discover_project(snapshot, main_document="main.tex")
    return RevisionMacroInventory(
        source_tree_sha256=discovery.source_tree_sha256,
        tex_files=len(discovery.files),
        canonical_counts=CanonicalRevisionMacroCounts(added=1),
        alias_counts=AliasRevisionMacroCounts(),
        skipped_dynamic_regions=0,
        display_expectations=(
            RevisionDisplayExpectation(
                source_path="main.tex",
                source_character_offset=0,
                source_call_sha256="sha256:" + "0" * 64,
                segments=(RevisionDisplaySegment("LWRINTEGRITYREV", False),),
                macro_instances=1,
            ),
        ),
    )


def _mutate_docx(
    source: Path,
    destination: Path,
    mutation: Callable[[dict[str, bytes]], None],
) -> None:
    with zipfile.ZipFile(source, mode="r") as source_package:
        infos = source_package.infolist()
        members = {info.filename: source_package.read(info.filename) for info in infos}
    original_names = {info.filename for info in infos}
    mutation(members)
    with zipfile.ZipFile(destination, mode="x") as destination_package:
        for info in infos:
            if info.filename in members:
                destination_package.writestr(info, members[info.filename])
        for name in sorted(set(members) - original_names):
            destination_package.writestr(name, members[name])


def _expect_display_rejected(run: Path, mutated: Path) -> ContractError:
    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            mutated,
            inventory=_inventory(run),
            clean_reference=run / "export/review.docx",
        )
    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    return caught.value


def test_display_rejects_swapped_image_relationship_targets(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "swapped-relationship-targets.docx"

    def swap_targets(members: dict[str, bytes]) -> None:
        name = "word/_rels/document.xml.rels"
        root = ET.fromstring(members[name])
        images = [
            relationship
            for relationship in root.iter(f"{REL}Relationship")
            if (relationship.get("Type") or "").rstrip("/").endswith("/image")
        ]
        assert len(images) >= 2
        first = images[0].get("Target")
        second = images[1].get("Target")
        assert first and second and first != second
        images[0].set("Target", second)
        images[1].set("Target", first)
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        swap_targets,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] == "OPC relationships"


def test_display_rejects_swapped_drawing_embed_ids(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "swapped-drawing-embeds.docx"

    def swap_embeds(members: dict[str, bytes]) -> None:
        name = "word/document.xml"
        root = ET.fromstring(members[name])
        embedded = [node for node in root.iter() if node.get(f"{R}embed") is not None]
        assert len(embedded) >= 2
        first = embedded[0].get(f"{R}embed")
        second = embedded[1].get(f"{R}embed")
        assert first and second and first != second
        embedded[0].set(f"{R}embed", second)
        embedded[1].set(f"{R}embed", first)
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        swap_embeds,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] == "paragraph structural tokens"


def test_display_rejects_reordered_omml_equations(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "swapped-equations.docx"

    def swap_equations(members: dict[str, bytes]) -> None:
        name = "word/document.xml"
        root = ET.fromstring(members[name])
        equations = list(root.iter(f"{M}oMath"))
        assert len(equations) >= 2
        parents = {child: parent for parent in root.iter() for child in parent}
        first_parent = parents[equations[0]]
        second_parent = parents[equations[1]]
        assert first_parent is not second_parent
        first_index = list(first_parent).index(equations[0])
        second_index = list(second_parent).index(equations[1])
        first_copy = ET.fromstring(ET.tostring(equations[0]))
        second_copy = ET.fromstring(ET.tostring(equations[1]))
        first_parent.remove(equations[0])
        second_parent.remove(equations[1])
        first_parent.insert(first_index, second_copy)
        second_parent.insert(second_index, first_copy)
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        swap_equations,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] in {
        "paragraph structural tokens",
        "OMML equations",
    }


def test_display_rejects_reordered_field_instructions(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "swapped-fields.docx"

    def swap_fields(members: dict[str, bytes]) -> None:
        name = "word/document.xml"
        root = ET.fromstring(members[name])
        instructions = [
            node for node in root.iter(f"{W}instrText") if node.text and node.text.strip()
        ]
        assert len(instructions) >= 2
        first_index = next(
            index
            for index, node in enumerate(instructions)
            if any(other.text != node.text for other in instructions[index + 1 :])
        )
        second_index = next(
            index
            for index in range(first_index + 1, len(instructions))
            if instructions[index].text != instructions[first_index].text
        )
        first = instructions[first_index].text
        second = instructions[second_index].text
        assert first and second and first != second
        instructions[first_index].text = second
        instructions[second_index].text = first
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        swap_fields,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] in {
        "paragraph structural tokens",
        "field instructions",
    }


@pytest.mark.parametrize("mutation_kind", ["paragraph", "section"])
def test_display_rejects_changed_paragraph_or_section_properties(
    rich_revision_run: Path,
    tmp_path: Path,
    mutation_kind: str,
) -> None:
    mutated = tmp_path / f"changed-{mutation_kind}-properties.docx"

    def change_properties(members: dict[str, bytes]) -> None:
        name = "word/document.xml"
        root = ET.fromstring(members[name])
        if mutation_kind == "paragraph":
            paragraph = next(
                item
                for item in root.iter(f"{W}p")
                if "Stable structure" in "".join(node.text or "" for node in item.iter(f"{W}t"))
            )
            properties = paragraph.find(f"{W}pPr")
            if properties is None:
                properties = ET.Element(f"{W}pPr")
                paragraph.insert(0, properties)
            ET.SubElement(properties, f"{W}keepNext")
        else:
            section = next(root.iter(f"{W}sectPr"))
            ET.SubElement(section, f"{W}titlePg")
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        change_properties,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] in {
        "paragraph structural tokens",
        "section properties",
    }


def test_display_rejects_inherited_default_style_drift(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "inherited-default-style-drift.docx"

    def change_default_style(members: dict[str, bytes]) -> None:
        name = "word/styles.xml"
        root = ET.fromstring(members[name])
        defaults = root.find(f"{W}docDefaults")
        assert defaults is not None
        run_defaults = defaults.find(f"{W}rPrDefault")
        if run_defaults is None:
            run_defaults = ET.SubElement(defaults, f"{W}rPrDefault")
        properties = run_defaults.find(f"{W}rPr")
        if properties is None:
            properties = ET.SubElement(run_defaults, f"{W}rPr")
        assert properties.find(f"{W}smallCaps") is None
        ET.SubElement(properties, f"{W}smallCaps")
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        change_default_style,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] == "protected DOCX parts"


@pytest.mark.parametrize(
    "part_kind",
    ["content-types", "numbering", "theme", "font-table", "settings"],
)
def test_display_rejects_protected_part_semantic_drift(
    rich_revision_run: Path,
    tmp_path: Path,
    part_kind: str,
) -> None:
    mutated = tmp_path / f"protected-{part_kind}-drift.docx"

    def change_protected_part(members: dict[str, bytes]) -> None:
        if part_kind == "content-types":
            name = "[Content_Types].xml"
            root = ET.fromstring(members[name])
            default = ET.SubElement(root, f"{{{CONTENT_TYPES_NS}}}Default")
            default.set("Extension", "lwr-integrity")
            default.set("ContentType", "application/x-lwr-integrity")
        elif part_kind == "numbering":
            name = "word/numbering.xml"
            root = ET.fromstring(members[name])
            number_format = next(root.iter(f"{W}numFmt"))
            current = number_format.get(f"{W}val")
            number_format.set(f"{W}val", "lowerLetter" if current != "lowerLetter" else "decimal")
        elif part_kind == "theme":
            name = "word/theme/theme1.xml"
            root = ET.fromstring(members[name])
            color = next(node for node in root.iter() if node.tag.endswith("}srgbClr"))
            current = color.get("val")
            color.set("val", "010203" if current != "010203" else "040506")
        elif part_kind == "font-table":
            name = "word/fontTable.xml"
            root = ET.fromstring(members[name])
            font = next(root.iter(f"{W}font"))
            font.set(f"{W}name", "LWR Integrity Drift Font")
        else:
            name = "word/settings.xml"
            root = ET.fromstring(members[name])
            assert root.find(f"{W}mirrorMargins") is None
            ET.SubElement(root, f"{W}mirrorMargins")
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        change_protected_part,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] == "protected DOCX parts"


@pytest.mark.parametrize("part_drift", ["extra", "missing"])
def test_display_rejects_extra_or_missing_opc_part(
    rich_revision_run: Path,
    tmp_path: Path,
    part_drift: str,
) -> None:
    def add_unreferenced_part(members: dict[str, bytes]) -> None:
        members["word/lwr-unexpected-part.bin"] = b"unexpected"

    if part_drift == "extra":
        mutated = tmp_path / "extra-opc-part.docx"
        _mutate_docx(
            rich_revision_run / "export/existing-changes-display.docx",
            mutated,
            add_unreferenced_part,
        )
        error = _expect_display_rejected(rich_revision_run, mutated)
    else:
        clean_with_extra = tmp_path / "clean-with-extra-opc-part.docx"
        _mutate_docx(
            rich_revision_run / "export/review.docx",
            clean_with_extra,
            add_unreferenced_part,
        )
        with pytest.raises(ContractError) as caught:
            validate_revision_display(
                rich_revision_run / "export/existing-changes-display.docx",
                inventory=_inventory(rich_revision_run),
                clean_reference=clean_with_extra,
            )
        error = caught.value
        assert error.code is ErrorCode.EXPORT_SILENT_LOSS
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] == "OPC part names"


def test_display_rejects_tampered_artifact_marker_profile(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "tampered-display-marker.docx"

    def tamper_marker(members: dict[str, bytes]) -> None:
        name = "word/settings.xml"
        root = ET.fromstring(members[name])
        marker = next(
            item
            for item in root.iter(f"{W}docVar")
            if item.get(f"{W}name") == "LWR_ARTIFACT_PROFILE"
        )
        marker.set(f"{W}val", "lwr-tampered-profile")
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        tamper_marker,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.message == "LaTeX revision display settings artifact marker is invalid"


def test_display_rejects_unrelated_docvar_drift(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "unrelated-docvar-drift.docx"

    def add_docvar(members: dict[str, bytes]) -> None:
        name = "word/settings.xml"
        root = ET.fromstring(members[name])
        container = root.find(f"{W}docVars")
        assert container is not None
        variable = ET.SubElement(container, f"{W}docVar")
        variable.set(f"{W}name", "LWR_UNRELATED_USER_VALUE")
        variable.set(f"{W}val", "changed")
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        add_docvar,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] == "protected DOCX parts"


def test_display_matching_has_a_total_work_budget(
    rich_revision_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(revision_display_module, "_MAX_MATCH_WORK_UNITS", 1)

    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            rich_revision_run / "export/existing-changes-display.docx",
            inventory=_inventory(rich_revision_run),
            clean_reference=rich_revision_run / "export/review.docx",
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert "total work safety limit" in caught.value.violation.message


def test_overlapping_candidate_enumeration_is_budgeted_before_each_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    character = revision_display_module._StyledCharacter(
        value="A",
        blue=False,
        strike=False,
        double_strike=False,
        highlighted=False,
        style_full=(),
        style_without_revision=(),
    )
    paragraph = (character,) * 10_000
    monkeypatch.setattr(revision_display_module, "_MAX_MATCH_WORK_UNITS", 25_000)

    with pytest.raises(ContractError) as caught:
        revision_display_module._text_candidate_intervals(
            (paragraph,),
            "A" * 1_000,
            budget=revision_display_module._MatchBudget(),
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert "total work safety limit" in caught.value.violation.message


def test_repeated_long_text_stops_before_style_matching(
    rich_revision_run: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutated = tmp_path / "repeated-long-text.docx"

    def repeat_plain_text(members: dict[str, bytes]) -> None:
        name = "word/document.xml"
        root = ET.fromstring(members[name])
        node = next(
            item
            for item in root.iter(f"{W}t")
            if "Stable structure paragraph." in (item.text or "")
        )
        node.text = " ".join(["LWRINTEGRITYREV"] * 512)
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        repeat_plain_text,
    )
    style_checks = 0
    original_matches = revision_display_module._matches

    def count_matches(
        paragraph: tuple[revision_display_module._StyledCharacter, ...],
        start: int,
        expectation: RevisionDisplayExpectation,
    ) -> bool:
        nonlocal style_checks
        style_checks += 1
        return original_matches(paragraph, start, expectation)

    monkeypatch.setattr(revision_display_module, "_matches", count_matches)
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.message == "LaTeX revision display text location is ambiguous"
    assert style_checks == 0


@pytest.mark.parametrize("native_kind", ["tracking", "insertion"])
def test_display_rejects_native_word_revision_state(
    rich_revision_run: Path,
    tmp_path: Path,
    native_kind: str,
) -> None:
    mutated = tmp_path / f"native-{native_kind}.docx"

    def add_native_state(members: dict[str, bytes]) -> None:
        if native_kind == "tracking":
            name = "word/settings.xml"
            root = ET.fromstring(members[name])
            ET.SubElement(root, f"{W}trackRevisions")
        else:
            name = "word/document.xml"
            root = ET.fromstring(members[name])
            run = next(root.iter(f"{W}r"))
            parents = {child: parent for parent in root.iter() for child in parent}
            parent = parents[run]
            index = list(parent).index(run)
            parent.remove(run)
            insertion = ET.Element(f"{W}ins")
            insertion.set(f"{W}id", "987654")
            insertion.append(run)
            parent.insert(index, insertion)
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        add_native_state,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.message == "LaTeX changes display contains native Word revision state"


def test_binary_evidence_rejects_a_mid_inspection_digest_change(
    rich_revision_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = read_stable_bytes

    def drifted(path: Path, *, max_bytes: int) -> bytes:
        return original(path, max_bytes=max_bytes) + b"drift"

    monkeypatch.setattr(revision_display_module, "read_stable_bytes", drifted)

    with pytest.raises(ContractError) as caught:
        inspect_revision_display(
            rich_revision_run / "export/existing-changes-display.docx",
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert "changed between package and binary-part inspection" in caught.value.violation.message


def _split_added_inventory(run: Path) -> RevisionMacroInventory:
    inventory = _inventory(run)
    original = inventory.display_expectations[0]
    pieces = ("LWRINTEGRITY", "REV")
    return replace(
        inventory,
        canonical_counts=CanonicalRevisionMacroCounts(added=len(pieces)),
        display_expectations=tuple(
            replace(
                original,
                segments=(RevisionDisplaySegment(piece, False),),
                macro_instances=1,
            )
            for piece in pieces
        ),
    )


def test_display_rejects_zero_macro_inventory(rich_revision_run: Path) -> None:
    invalid = replace(
        _inventory(rich_revision_run),
        canonical_counts=CanonicalRevisionMacroCounts(),
        display_expectations=(),
    )

    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            rich_revision_run / "export/existing-changes-display.docx",
            inventory=invalid,
            clean_reference=rich_revision_run / "export/review.docx",
        )

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert caught.value.violation.message == "revision display expectations are invalid"


def test_display_rejects_inventory_not_covered_by_expectations(
    rich_revision_run: Path,
) -> None:
    invalid = replace(
        _inventory(rich_revision_run),
        canonical_counts=CanonicalRevisionMacroCounts(added=2),
    )

    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            rich_revision_run / "export/existing-changes-display.docx",
            inventory=invalid,
            clean_reference=rich_revision_run / "export/review.docx",
        )

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert (
        caught.value.violation.message
        == "revision display expectations do not cover the source inventory"
    )


def test_empty_expectation_cannot_accept_clean_review_as_display(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    inventory = _inventory(rich_revision_run)
    empty = replace(inventory.display_expectations[0], segments=())
    invalid = replace(inventory, display_expectations=(empty,))
    clean = rich_revision_run / "export/review.docx"
    candidate = tmp_path / "clean-as-display.docx"

    def disable_tracking(members: dict[str, bytes]) -> None:
        name = "word/settings.xml"
        root = ET.fromstring(members[name])
        for control in list(root.findall(f"{W}trackRevisions")):
            root.remove(control)
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(clean, candidate, disable_tracking)

    with pytest.raises(ContractError) as caught:
        validate_revision_display(candidate, inventory=invalid, clean_reference=clean)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert (
        caught.value.violation.message == "revision display expectations must contain visible text"
    )


@pytest.mark.parametrize(
    ("candidate_limit", "inventory_kind", "expected_message"),
    [
        (
            0,
            "single",
            "LaTeX revision display matching exceeded its document safety limit",
        ),
        (
            1,
            "split",
            "LaTeX revision display matching exceeded its document safety limit",
        ),
    ],
    ids=("per-text", "document-text-stage"),
)
def test_display_candidate_limits_fail_closed(
    rich_revision_run: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_limit: int,
    inventory_kind: str,
    expected_message: str,
) -> None:
    monkeypatch.setattr(
        revision_display_module,
        "_MAX_MATCH_CANDIDATES",
        candidate_limit,
    )
    inventory = (
        _inventory(rich_revision_run)
        if inventory_kind == "single"
        else _split_added_inventory(rich_revision_run)
    )

    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            rich_revision_run / "export/existing-changes-display.docx",
            inventory=inventory,
            clean_reference=rich_revision_run / "export/review.docx",
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert caught.value.violation.message == expected_message


def test_display_reuses_text_candidates_for_style_groups(
    rich_revision_run: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(revision_display_module, "_MAX_MATCH_CANDIDATES", 3)

    inspection = validate_revision_display(
        rich_revision_run / "export/existing-changes-display.docx",
        inventory=_split_added_inventory(rich_revision_run),
        clean_reference=rich_revision_run / "export/review.docx",
    )

    assert inspection.verified_expectations == 2


def test_display_rejects_added_story_part(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "added-header-story.docx"

    def add_header_story(members: dict[str, bytes]) -> None:
        content_types = ET.fromstring(members["[Content_Types].xml"])
        override = ET.SubElement(content_types, f"{{{CONTENT_TYPES_NS}}}Override")
        override.set("PartName", "/word/header-integrity.xml")
        override.set(
            "ContentType",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
        )
        members["[Content_Types].xml"] = ET.tostring(
            content_types,
            encoding="utf-8",
            xml_declaration=True,
        )

        relationships = ET.fromstring(members["word/_rels/document.xml.rels"])
        relationship = ET.SubElement(relationships, f"{REL}Relationship")
        relationship.set("Id", "rIdLwrIntegrityHeader")
        relationship.set(
            "Type",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header",
        )
        relationship.set("Target", "header-integrity.xml")
        members["word/_rels/document.xml.rels"] = ET.tostring(
            relationships,
            encoding="utf-8",
            xml_declaration=True,
        )
        members["word/header-integrity.xml"] = (
            f'<w:hdr xmlns:w="{W_NS}"><w:p><w:r><w:t>Unexpected header</w:t></w:r></w:p></w:hdr>'
        ).encode()

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        add_header_story,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] == "OPC part names"


def test_display_rejects_added_paragraph(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "added-paragraph.docx"

    def add_paragraph(members: dict[str, bytes]) -> None:
        name = "word/document.xml"
        root = ET.fromstring(members[name])
        body = root.find(f"{W}body")
        assert body is not None
        paragraph = ET.Element(f"{W}p")
        section = body.find(f"{W}sectPr")
        if section is None:
            body.append(paragraph)
        else:
            body.insert(list(body).index(section), paragraph)
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        add_paragraph,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert (
        error.violation.message
        == "LaTeX revision display paragraph layout differs from the clean review"
    )


@pytest.mark.parametrize(
    ("mutation_kind", "expected_message"),
    [
        ("append", "LaTeX revision display adds non-deletion body content"),
        ("omit", "LaTeX revision display omits clean-review body content"),
    ],
)
def test_display_rejects_non_revision_body_length_drift(
    rich_revision_run: Path,
    tmp_path: Path,
    mutation_kind: str,
    expected_message: str,
) -> None:
    mutated = tmp_path / f"body-{mutation_kind}.docx"

    def change_body_length(members: dict[str, bytes]) -> None:
        name = "word/document.xml"
        root = ET.fromstring(members[name])
        node = next(
            item
            for item in root.iter(f"{W}t")
            if "Stable structure paragraph." in (item.text or "")
        )
        assert node.text is not None
        if mutation_kind == "append":
            node.text += "X"
        else:
            assert node.text.endswith(".")
            node.text = node.text[:-1]
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        change_body_length,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.message == expected_message


def test_display_rejects_non_revision_style_inside_added_text(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "added-text-italic.docx"

    def add_italic_style(members: dict[str, bytes]) -> None:
        name = "word/document.xml"
        root = ET.fromstring(members[name])
        text = next(item for item in root.iter(f"{W}t") if (item.text or "") == "LWRINTEGRITYREV")
        parents = {child: parent for parent in root.iter() for child in parent}
        run = parents[text]
        properties = run.find(f"{W}rPr")
        if properties is None:
            properties = ET.Element(f"{W}rPr")
            run.insert(0, properties)
        ET.SubElement(properties, f"{W}i")
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        add_italic_style,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.message == "LaTeX revision display changes non-revision run properties"


def test_display_rejects_added_simple_field(
    rich_revision_run: Path,
    tmp_path: Path,
) -> None:
    mutated = tmp_path / "added-simple-field.docx"

    def add_simple_field(members: dict[str, bytes]) -> None:
        name = "word/document.xml"
        root = ET.fromstring(members[name])
        paragraph = next(
            item
            for item in root.iter(f"{W}p")
            if "Stable structure" in "".join(node.text or "" for node in item.iter(f"{W}t"))
        )
        field = ET.Element(f"{W}fldSimple")
        field.set(f"{W}instr", " REF LWR_MISSING \\h ")
        run = ET.SubElement(field, f"{W}r")
        ET.SubElement(run, f"{W}t")
        paragraph.insert(0, field)
        members[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    _mutate_docx(
        rich_revision_run / "export/existing-changes-display.docx",
        mutated,
        add_simple_field,
    )
    error = _expect_display_rejected(rich_revision_run, mutated)
    assert error.violation.details is not None
    assert error.violation.details["evidence_kind"] == "field instructions"


def test_workflow_keeps_clean_review_for_empty_revision_macro(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable eligible paragraph.\n\n"
        "Empty marker: \\added{}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    result = export_workflow(
        run,
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    assert result["phase"] == "exported"
    assert result["counts"]["revision_macro_instances"] == 1
    assert result["counts"]["revision_display_available"] == 1
    assert (run / "export/review.docx").is_file()
    assert (run / "export/existing-changes-display.docx").is_file()
