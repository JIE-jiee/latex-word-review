"""End-to-end contracts for clean review and static LaTeX-change display DOCX files."""

from __future__ import annotations

import copy
import shutil
import stat
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import latex_word_review.workflow as workflow_module
from latex_word_review.backends.base import BackendCapabilities, BackendRequest, BackendResult
from latex_word_review.backends.tex2word import Tex2WordBackend
from latex_word_review.canonical import canonical_json, compute_payload_sha256, seal_envelope
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_models import ExportFinding, export_report_commitment
from latex_word_review.hashing import digest_file
from latex_word_review.jsonio import read_contract_file
from latex_word_review.revision_display import inspect_revision_display, validate_revision_display
from latex_word_review.revision_macros import scan_revision_macros
from latex_word_review.workflow import (
    export_workflow,
    initialize_workflow,
    receive_workflow,
    workflow_status,
)
from latex_word_review.workflow_objects import (
    EXPORT_REPORT_INTERFACE_VERSION,
    PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
)
from tests.e2e_helpers import create_whole_bookmark_replacement

TIME = "2026-07-25T00:00:00Z"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W = f"{{{W_NS}}}"
M = f"{{{M_NS}}}"


class _DisplayResultMutator:
    def __init__(
        self,
        mutation: Callable[[BackendRequest, BackendResult], BackendResult],
    ) -> None:
        self._backend = Tex2WordBackend()
        self._mutation = mutation

    def capabilities(self) -> BackendCapabilities:
        return self._backend.capabilities()

    def export(self, request: BackendRequest) -> BackendResult:
        result = self._backend.export(request)
        if request.revision_view == "display" and result.succeeded:
            return self._mutation(request, result)
        return result


def _write_revision_project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\usepackage{trackchanges}\n"
        "\\newcommand{\\delete}[2][]{\\deleted[#1]{#2}}\n"
        "\\begin{document}\n\n"
        "Stable preface.\n\n"
        "Added marker: \\added{LWRADDED}.\n\n"
        "Deleted marker: \\deleted{LWRDELETED}.\n\n"
        "Replaced marker: \\replaced{LWRCURRENT}{LWRFORMER}.\n\n"
        "Alias addition: \\add{LWRALIASADD}.\n\n"
        "Alias deletion: \\delete{LWRALIASDELETE}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_plain_project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n\n"
        "Plain review text without revision commands.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_additions_only_project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\usepackage{trackchanges}\n"
        "\\begin{document}\n\n"
        "Stable editable sentence.\n\n"
        "Added marker: \\added{LWRADDITIONONLY}.\n\n"
        "Alias addition: \\add{LWRALIASADDITIONONLY}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_structured_revision_project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "\\section{Scope}\\label{sec:scope}\n\n"
        "Stable editable sentence.\n\n"
        "Plain addition: \\added{LWRPLAINSTRUCTURED}.\n\n"
        "Formula addition: \\added{energy \\(E=mc^2\\)}.\n\n"
        "Reference replacement: "
        "\\replaced{see Section~\\ref{sec:scope}}{LWRFORMERREFERENCE}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_tex_dash_revision_project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable editable sentence.\n\n"
        "Dash addition: \\added{LWR--DASH}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )


def _initialize_and_export(origin: Path, run: Path) -> dict[str, Any]:
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )
    return export_workflow(
        run,
        confidentiality="public_fixture",
        generated_at=TIME,
    )


def _write_resealed_contract(path: Path, document: dict[str, Any]) -> None:
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    path.write_bytes(canonical_json(seal_envelope(document)) + b"\n")
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def _strip_current_revision_display_evidence(payload: dict[str, Any]) -> None:
    payload.pop("existing_changes_display_docx", None)
    metrics = cast("dict[str, Any]", payload["metrics"])
    for group_name in ("source", "output"):
        group = cast("dict[str, int]", metrics[group_name])
        for name in tuple(group):
            if name.startswith(("revision_macro_", "revision_macros_", "revision_display_")):
                group.pop(name)


@pytest.fixture(scope="module")
def revision_run_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("revision-display-workflow")
    origin = root / "origin"
    run = root / "run"
    _write_revision_project(origin)
    _initialize_and_export(origin, run)
    return run


@pytest.fixture(scope="module")
def additions_only_run_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("additions-only-display-workflow")
    origin = root / "origin"
    run = root / "run"
    _write_additions_only_project(origin)
    _initialize_and_export(origin, run)
    return run


@pytest.fixture(scope="module")
def positional_deletion_run_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("position-bound-deletion-display")
    origin = root / "origin"
    run = root / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable eligible paragraph.\n\n"
        "BoundaryA\\deleted{LWRPOSITIONOLD}BoundaryB.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    _initialize_and_export(origin, run)
    return run


def _copy_run(template: Path, tmp_path: Path) -> Path:
    destination = tmp_path / "run"
    shutil.copytree(template, destination)
    return destination


def _artifact_marker_values(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as package:
        root = ET.fromstring(package.read("word/settings.xml"))
    values: dict[str, str] = {}
    for variable in root.findall(f".//{W}docVar"):
        name = variable.get(f"{W}name")
        value = variable.get(f"{W}val")
        if name is not None and name.startswith("LWR_") and value is not None:
            values[name] = value
    return values


def _repackage_docx(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source, mode="r") as source_package:
        infos = source_package.infolist()
        members = {info.filename: source_package.read(info.filename) for info in infos}
    with zipfile.ZipFile(destination, mode="x") as destination_package:
        for info in reversed(infos):
            destination_package.writestr(info, members[info.filename])


def _mutate_display_run(
    source: Path,
    destination: Path,
    *,
    token: str,
    mutation: str,
    occurrence: int = 0,
) -> None:
    with zipfile.ZipFile(source, mode="r") as source_package:
        infos = source_package.infolist()
        members = {info.filename: source_package.read(info.filename) for info in infos}
    root = ET.fromstring(members["word/document.xml"])
    runs = [
        run
        for run in root.iter(f"{W}r")
        if token in "".join(node.text or "" for node in run.iter(f"{W}t"))
    ]
    assert 0 <= occurrence < len(runs)
    run = runs[occurrence]
    properties = run.find(f"{W}rPr")
    if properties is None:
        properties = ET.Element(f"{W}rPr")
        run.insert(0, properties)

    if mutation in {"highlight", "dstrike"}:
        property_node = ET.SubElement(properties, f"{W}{mutation}")
        property_node.set(f"{W}val", "true")
    elif mutation == "red":
        color_node = properties.find(f"{W}color")
        if color_node is None:
            color_node = ET.SubElement(properties, f"{W}color")
        color_node.set(f"{W}val", "FF0000")
    elif mutation in {"tab", "last_rendered_page_break"}:
        text = next(node for node in run.findall(f"{W}t") if token in (node.text or ""))
        value = text.text or ""
        split = value.index(token) + len(token) // 2
        before, after = value[:split], value[split:]
        text.text = before
        position = list(run).index(text)
        marker = "tab" if mutation == "tab" else "lastRenderedPageBreak"
        run.insert(position + 1, ET.Element(f"{W}{marker}"))
        trailing = ET.Element(f"{W}t")
        trailing.text = after
        run.insert(position + 2, trailing)
    else:
        raise AssertionError(f"unknown test mutation: {mutation}")

    members["word/document.xml"] = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )
    with zipfile.ZipFile(destination, mode="x") as destination_package:
        for info in infos:
            destination_package.writestr(info, members[info.filename])


def _remove_first_table(path: Path) -> None:
    with zipfile.ZipFile(path, mode="r") as source_package:
        infos = source_package.infolist()
        members = {info.filename: source_package.read(info.filename) for info in infos}
    root = ET.fromstring(members["word/document.xml"])
    removed = False
    for parent in root.iter():
        for child in list(parent):
            if child.tag != f"{W}tbl":
                continue
            parent.remove(child)
            removed = True
            break
        if removed:
            break
    assert removed
    members["word/document.xml"] = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )
    replacement = path.with_name("display-without-table.docx")
    with zipfile.ZipFile(replacement, mode="x") as destination_package:
        for info in infos:
            destination_package.writestr(info, members[info.filename])
    replacement.replace(path)


def _mutate_backend_docx(
    request: BackendRequest,
    result: BackendResult,
    mutation: Callable[[dict[str, bytes]], None],
) -> BackendResult:
    with zipfile.ZipFile(request.output_path, mode="r") as source_package:
        infos = source_package.infolist()
        members = {info.filename: source_package.read(info.filename) for info in infos}
    mutation(members)
    replacement = request.output_path.with_name("mutated-display.docx")
    with zipfile.ZipFile(replacement, mode="x") as destination_package:
        for info in infos:
            destination_package.writestr(info, members[info.filename])
    replacement.replace(request.output_path)
    digest = digest_file(request.output_path, max_bytes=128 * 1024 * 1024)
    return replace(
        result,
        artifact_sha256=digest.sha256,
        artifact_size_bytes=digest.size_bytes,
    )


def _set_direct_run_boolean_property(
    root: ET.Element,
    token: str,
    property_name: str,
    enabled: bool,
) -> None:
    runs = [
        run
        for run in root.iter(f"{W}r")
        if token in "".join(node.text or "" for node in run.iter(f"{W}t"))
    ]
    assert len(runs) == 1
    run = runs[0]
    properties = run.find(f"{W}rPr")
    if properties is None:
        properties = ET.Element(f"{W}rPr")
        run.insert(0, properties)
    for node in list(properties.findall(f"{W}{property_name}")):
        properties.remove(node)
    if enabled:
        node = ET.SubElement(properties, f"{W}{property_name}")
        node.set(f"{W}val", "true")


def _set_direct_run_color_property(
    root: ET.Element,
    token: str,
    color_value: str | None,
) -> None:
    runs = [
        run
        for run in root.iter(f"{W}r")
        if token in "".join(node.text or "" for node in run.iter(f"{W}t"))
    ]
    assert len(runs) == 1
    run = runs[0]
    properties = run.find(f"{W}rPr")
    if properties is None:
        properties = ET.Element(f"{W}rPr")
        run.insert(0, properties)
    for node in list(properties.findall(f"{W}color")):
        properties.remove(node)
    if color_value is not None:
        node = ET.SubElement(properties, f"{W}color")
        node.set(f"{W}val", color_value)


def _mutate_docx_copy(
    source: Path,
    destination: Path,
    mutation: Callable[[ET.Element], None],
) -> None:
    with zipfile.ZipFile(source, mode="r") as source_package:
        infos = source_package.infolist()
        members = {info.filename: source_package.read(info.filename) for info in infos}
    root = ET.fromstring(members["word/document.xml"])
    mutation(root)
    members["word/document.xml"] = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )
    with zipfile.ZipFile(destination, mode="x") as destination_package:
        for info in infos:
            destination_package.writestr(info, members[info.filename])


def _move_display_token_within_paragraph(
    source: Path,
    destination: Path,
    *,
    token: str,
    location: str,
) -> None:
    def mutate(root: ET.Element) -> None:
        parents = {child: parent for parent in root.iter() for child in parent}
        token_run = next(
            run
            for run in root.iter(f"{W}r")
            if token in "".join(node.text or "" for node in run.iter(f"{W}t"))
        )
        paragraph = token_run
        while paragraph.tag != f"{W}p":
            paragraph = parents[paragraph]
        assert parents[token_run] is paragraph
        paragraph.remove(token_run)

        if location == "start":
            properties = paragraph.find(f"{W}pPr")
            insertion_index = 1 if properties is not None else 0
            paragraph.insert(insertion_index, token_run)
            return
        if location == "end":
            paragraph.append(token_run)
            return
        assert location == "wrong"
        plain_runs = [
            run
            for run in paragraph.findall(f"{W}r")
            if "".join(node.text or "" for node in run.iter(f"{W}t")).strip()
        ]
        anchor_run = max(
            plain_runs,
            key=lambda run: len("".join(node.text or "" for node in run.iter(f"{W}t"))),
        )
        anchor_texts = list(anchor_run.iter(f"{W}t"))
        assert len(anchor_texts) == 1
        value = anchor_texts[0].text or ""
        split = max(1, len(value) // 2)
        assert split < len(value)
        trailing_run = copy.deepcopy(anchor_run)
        trailing_texts = list(trailing_run.iter(f"{W}t"))
        assert len(trailing_texts) == 1
        anchor_texts[0].text = value[:split]
        trailing_texts[0].text = value[split:]
        insertion_index = list(paragraph).index(anchor_run) + 1
        paragraph.insert(insertion_index, token_run)
        paragraph.insert(insertion_index + 1, trailing_run)

    _mutate_docx_copy(source, destination, mutate)


def _make_blue_returned_review(run: Path, destination: Path) -> None:
    source_map = read_contract_file(
        run / "export/objects/source-map.json",
        expected_schema="SourceMap",
    )
    payload = cast("dict[str, Any]", source_map["payload"])
    mapping = next(
        item
        for item in cast("list[dict[str, Any]]", payload["mappings"])
        if item["status"] == "exact"
    )
    location = cast("dict[str, Any]", mapping["source_location"])
    source = (run / "snapshot" / cast("str", location["path"])).read_bytes()
    start = cast("int", location["start_byte"])
    end = cast("int", location["end_byte"])
    before = source[start:end].decode("utf-8")
    anchor = cast("dict[str, Any]", mapping["docx_anchor"])
    unstyled = destination.with_name("unstyled-returned.docx")
    create_whole_bookmark_replacement(
        run / "export/review.docx",
        unstyled,
        bookmark_name=cast("str", anchor["name"]),
        before=before,
        after=f"{before} BLUE-REVIEW-EDIT",
    )
    with zipfile.ZipFile(unstyled, mode="r") as source_package:
        infos = source_package.infolist()
        members = {info.filename: source_package.read(info.filename) for info in infos}
    root = ET.fromstring(members["word/document.xml"])
    inserted_run = root.find(f".//{W}ins/{W}r")
    assert inserted_run is not None
    properties = inserted_run.find(f"{W}rPr")
    if properties is None:
        properties = ET.Element(f"{W}rPr")
        inserted_run.insert(0, properties)
    color = ET.SubElement(properties, f"{W}color")
    color.set(f"{W}val", "0000FF")
    members["word/document.xml"] = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )
    with zipfile.ZipFile(destination, mode="x") as destination_package:
        for info in infos:
            destination_package.writestr(info, members[info.filename])
    unstyled.unlink()


def _document_root(path: Path) -> ET.Element:
    with zipfile.ZipFile(path) as package:
        return ET.fromstring(package.read("word/document.xml"))


def _visible_text(root: ET.Element) -> str:
    return "".join(node.text or "" for node in root.iter(f"{W}t"))


def _enabled(raw: str | None) -> bool:
    return (raw or "true").casefold() not in {"0", "false", "off", "no", "none"}


def _direct_run_style(root: ET.Element, token: str) -> tuple[str | None, bool, bool, bool]:
    matches: list[tuple[str | None, bool, bool, bool]] = []
    for run in root.iter(f"{W}r"):
        text = "".join(node.text or "" for node in run.iter(f"{W}t"))
        if token not in text:
            continue
        properties = run.find(f"{W}rPr")
        color = None if properties is None else properties.find(f"{W}color")
        strike = None if properties is None else properties.find(f"{W}strike")
        double_strike = None if properties is None else properties.find(f"{W}dstrike")
        highlight = None if properties is None else properties.find(f"{W}highlight")
        matches.append(
            (
                None if color is None else color.get(f"{W}val"),
                strike is not None and _enabled(strike.get(f"{W}val")),
                double_strike is not None and _enabled(double_strike.get(f"{W}val")),
                highlight is not None and _enabled(highlight.get(f"{W}val")),
            )
        )
    assert len(matches) == 1, f"expected one run containing {token!r}, got {len(matches)}"
    return matches[0]


def test_revision_workflow_generates_clean_baseline_and_static_blue_display(
    revision_run_template: Path,
) -> None:
    run = revision_run_template
    review = run / "export/review.docx"
    display = run / "export/existing-changes-display.docx"

    assert review.is_file()
    assert display.is_file()
    assert not review.stat().st_mode & stat.S_IWUSR
    assert not display.stat().st_mode & stat.S_IWUSR

    review_root = _document_root(review)
    review_text = _visible_text(review_root)
    assert "LWRADDED" in review_text
    assert "LWRCURRENT" in review_text
    assert "LWRALIASADD" in review_text
    assert "LWRDELETED" not in review_text
    assert "LWRFORMER" not in review_text
    assert "LWRALIASDELETE" not in review_text

    display_root = _document_root(display)
    display_text = _visible_text(display_root)
    for token in (
        "LWRADDED",
        "LWRDELETED",
        "LWRCURRENT",
        "LWRFORMER",
        "LWRALIASADD",
        "LWRALIASDELETE",
    ):
        assert token in display_text
    assert display_text.index("LWRFORMER") < display_text.index("LWRCURRENT")
    assert _direct_run_style(display_root, "LWRADDED") == ("0000FF", False, False, False)
    assert _direct_run_style(display_root, "LWRDELETED") == ("0000FF", True, False, False)
    assert _direct_run_style(display_root, "LWRFORMER") == ("0000FF", True, False, False)
    assert _direct_run_style(display_root, "LWRCURRENT") == ("0000FF", False, False, False)
    assert _direct_run_style(display_root, "LWRALIASADD") == ("0000FF", False, False, False)
    assert _direct_run_style(display_root, "LWRALIASDELETE") == (
        "0000FF",
        True,
        False,
        False,
    )
    assert display_root.find(f".//{W}highlight") is None

    inspection = inspect_revision_display(display)
    assert inspection.blue_text_characters > 0
    assert inspection.strike_text_characters > 0
    assert inspection.highlighted_text_characters == 0
    assert inspection.native_revision_elements == 0
    assert inspection.track_revisions_enabled is False

    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = cast("dict[str, Any]", report["payload"])
    artifact = cast("dict[str, Any]", payload["existing_changes_display_docx"])
    assert _artifact_marker_values(review) == {}
    assert _artifact_marker_values(display) == {
        "LWR_ARTIFACT_ROLE": "latex_changes_display_docx",
        "LWR_ARTIFACT_PROFILE": "lwr-existing-changes-display-v1",
        "LWR_RUN_ID": report["run_id"],
    }
    metrics = cast("dict[str, Any]", payload["metrics"])
    source_metrics = cast("dict[str, int]", metrics["source"])
    output_metrics = cast("dict[str, int]", metrics["output"])
    assert artifact["path"] == "export/existing-changes-display.docx"
    assert artifact["role"] == "latex_changes_display_docx"
    assert artifact["immutable"] is True
    assert source_metrics["revision_macros_total"] == 5
    assert source_metrics["revision_macros_added"] == 1
    assert source_metrics["revision_macros_deleted"] == 1
    assert source_metrics["revision_macros_replaced"] == 1
    assert source_metrics["revision_macros_alias_add"] == 1
    assert source_metrics["revision_macros_alias_delete"] == 1
    assert source_metrics["revision_display_expected_expectations"] == 5
    assert source_metrics["revision_display_expected_blue_text_characters"] == 62
    assert source_metrics["revision_display_expected_strike_text_characters"] == 33
    assert output_metrics["revision_display_available"] == 1
    assert output_metrics["revision_display_profile_version"] == 2
    assert output_metrics["revision_display_highlighted_text_characters"] == 0
    assert output_metrics["revision_display_native_revision_elements"] == 0
    assert output_metrics["revision_display_track_revisions_enabled"] == 0
    assert output_metrics["revision_display_verified_expectations"] == 5
    assert output_metrics["revision_display_verified_blue_text_characters"] == 62
    assert output_metrics["revision_display_verified_strike_text_characters"] == 33

    status = workflow_status(run)
    assert status["phase"] == "exported"
    assert status["integrity"] == "workflow_bindings_verified"
    assert status["counts"]["revision_macro_instances"] == 5
    assert status["counts"]["revision_display_available"] == 1


def test_structured_revision_macros_degrade_per_item_without_failing_export(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    _write_structured_revision_project(origin)

    result = _initialize_and_export(origin, run)

    review = run / "export/review.docx"
    display = run / "export/existing-changes-display.docx"
    assert result["counts"]["revision_macro_instances"] == 3
    assert result["counts"]["revision_display_available"] == 1
    assert review.is_file()
    assert display.is_file()
    assert not review.stat().st_mode & stat.S_IWUSR
    assert not display.stat().st_mode & stat.S_IWUSR

    review_root = _document_root(review)
    review_text = _visible_text(review_root)
    assert "LWRPLAINSTRUCTURED" in review_text
    assert "LWRFORMERREFERENCE" not in review_text
    assert review_root.find(f".//{M}oMath") is not None

    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = cast("dict[str, Any]", report["payload"])
    assert payload["status"] == "partial"
    findings = cast("list[dict[str, Any]]", payload["findings"])
    structured_findings = [
        item for item in findings if item["code"] == ErrorCode.REVISION_STRUCTURED_TEXT.value
    ]
    assert len(structured_findings) == 2
    for finding in structured_findings:
        assert finding["severity"] == "warning"
        assert finding["recoverable"] is True
        location = cast("dict[str, Any]", finding["source_location"])
        assert location["path"] == "main.tex"
        assert location["start_byte"] < location["end_byte"]
        assert location["slice_sha256"].startswith("sha256:")

    metrics = cast("dict[str, Any]", payload["metrics"])
    source_metrics = cast("dict[str, int]", metrics["source"])
    output_metrics = cast("dict[str, int]", metrics["output"])
    assert source_metrics["revision_macros_total"] == 3
    assert source_metrics["revision_display_exact_macro_instances"] == 1
    assert source_metrics["revision_display_degraded_macro_instances"] == 2
    assert source_metrics["revision_display_degradation_calls"] == 2
    assert output_metrics["revision_display_available"] == 1
    assert (
        cast("dict[str, Any]", payload["existing_changes_display_docx"])["path"]
        == "export/existing-changes-display.docx"
    )


def test_tex_dash_sequence_uses_the_visible_unicode_projection(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    _write_tex_dash_revision_project(origin)

    result = _initialize_and_export(origin, run)

    assert result["counts"]["revision_display_available"] == 1
    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    metrics = cast("dict[str, Any]", cast("dict[str, Any]", report["payload"])["metrics"])
    source_metrics = cast("dict[str, int]", metrics["source"])
    output_metrics = cast("dict[str, int]", metrics["output"])
    assert source_metrics["revision_display_expected_blue_text_characters"] == len("LWR--DASH")
    assert output_metrics["revision_display_verified_blue_text_characters"] == len("LWR–DASH")
    assert output_metrics["revision_display_verified_expectations"] == 1


def test_structured_display_failure_keeps_clean_review_with_sealed_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    _write_structured_revision_project(origin)
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    def fail_optional_display(*_args: object, **_kwargs: object) -> object:
        raise ContractError(ErrorCode.EXPORT_SILENT_LOSS, "synthetic display failure")

    monkeypatch.setattr(
        workflow_module,
        "_write_existing_changes_display",
        fail_optional_display,
    )
    result = export_workflow(
        run,
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    assert result["counts"]["revision_macro_instances"] == 3
    assert result["counts"]["revision_display_available"] == 0
    assert (run / "export/review.docx").is_file()
    assert not (run / "export/existing-changes-display.docx").exists()
    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = cast("dict[str, Any]", report["payload"])
    assert payload["status"] == "partial"
    assert payload["existing_changes_display_docx"] is None
    findings = cast("list[dict[str, Any]]", payload["findings"])
    display_warnings = [
        item for item in findings if item["code"] == ErrorCode.EXPORT_DEGRADED.value
    ]
    assert len(display_warnings) == 1
    assert display_warnings[0]["severity"] == "warning"
    assert display_warnings[0]["recoverable"] is True
    status = workflow_status(run)
    assert status["counts"]["revision_display_available"] == 0

    core = workflow_module._load_core(run)
    payload_without_warning = copy.deepcopy(payload)
    payload_without_warning["findings"] = [
        item for item in findings if item["code"] != ErrorCode.EXPORT_DEGRADED.value
    ]
    with pytest.raises(ContractError) as raised:
        workflow_module._validate_existing_changes_display(
            core,
            run / "export",
            payload_without_warning,
            producer_interface_version=EXPORT_REPORT_INTERFACE_VERSION,
        )
    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_exact_only_display_failure_remains_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    _write_revision_project(origin)
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    def fail_required_display(*_args: object, **_kwargs: object) -> object:
        raise ContractError(ErrorCode.EXPORT_SILENT_LOSS, "synthetic display failure")

    monkeypatch.setattr(
        workflow_module,
        "_write_existing_changes_display",
        fail_required_display,
    )
    with pytest.raises(ContractError) as raised:
        export_workflow(
            run,
            confidentiality="public_fixture",
            generated_at=TIME,
        )

    assert raised.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert not (run / "export").exists()


@pytest.mark.parametrize(
    ("mutation", "token"),
    [
        ("highlight", "LWRCURRENT"),
        ("dstrike", "LWRDELETED"),
        ("tab", "LWRADDED"),
    ],
)
def test_display_validator_rejects_unexpected_style_or_visible_barrier(
    revision_run_template: Path,
    tmp_path: Path,
    mutation: str,
    token: str,
) -> None:
    snapshot = revision_run_template / "snapshot"
    discovery = discover_project(snapshot, main_document="main.tex")
    inventory = scan_revision_macros(snapshot, discovery)
    mutated = tmp_path / f"{mutation}.docx"
    _mutate_display_run(
        revision_run_template / "export/existing-changes-display.docx",
        mutated,
        token=token,
        mutation=mutation,
    )

    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            mutated,
            inventory=inventory,
            clean_reference=revision_run_template / "export/review.docx",
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS


def test_display_validator_ignores_zero_width_last_rendered_page_break(
    revision_run_template: Path,
    tmp_path: Path,
) -> None:
    snapshot = revision_run_template / "snapshot"
    discovery = discover_project(snapshot, main_document="main.tex")
    inventory = scan_revision_macros(snapshot, discovery)
    mutated = tmp_path / "last-rendered-page-break.docx"
    _mutate_display_run(
        revision_run_template / "export/existing-changes-display.docx",
        mutated,
        token="LWRADDED",
        mutation="last_rendered_page_break",
    )

    inspection = validate_revision_display(
        mutated,
        inventory=inventory,
        clean_reference=revision_run_template / "export/review.docx",
    )

    assert inspection.verified_expectations == inventory.expected_display_expectations


@pytest.mark.parametrize("location", ["start", "end", "wrong"])
def test_display_rejects_deleted_text_moved_from_its_source_boundary(
    positional_deletion_run_template: Path,
    tmp_path: Path,
    location: str,
) -> None:
    snapshot = positional_deletion_run_template / "snapshot"
    discovery = discover_project(snapshot, main_document="main.tex")
    inventory = scan_revision_macros(snapshot, discovery)
    mutated = tmp_path / f"deleted-moved-{location}.docx"
    _move_display_token_within_paragraph(
        positional_deletion_run_template / "export/existing-changes-display.docx",
        mutated,
        token="LWRPOSITIONOLD",
        location=location,
    )

    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            mutated,
            inventory=inventory,
            clean_reference=positional_deletion_run_template / "export/review.docx",
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert (
        caught.value.violation.message
        == "LaTeX revision deletion is not at its source-bound clean-review position"
    )


def test_deletion_context_folds_spaces_without_losing_its_boundary(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable eligible paragraph.\n\n"
        "Context A \\deleted{LWRSPACEOLD} B tail.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    _initialize_and_export(origin, run)
    snapshot = run / "snapshot"
    discovery = discover_project(snapshot, main_document="main.tex")
    inventory = scan_revision_macros(snapshot, discovery)
    deletion = next(
        expectation
        for expectation in inventory.display_expectations
        if expectation.strike_text_characters
    )
    assert deletion.left_context.endswith(" ")
    assert deletion.right_context.startswith(" ")
    inspection = validate_revision_display(
        run / "export/existing-changes-display.docx",
        inventory=inventory,
        clean_reference=run / "export/review.docx",
    )
    assert inspection.verified_strike_text_characters == len("LWRSPACEOLD")


def test_display_rejects_deletion_without_safe_source_context(
    revision_run_template: Path,
) -> None:
    snapshot = revision_run_template / "snapshot"
    discovery = discover_project(snapshot, main_document="main.tex")
    inventory = scan_revision_macros(snapshot, discovery)
    contextless = replace(
        inventory,
        display_expectations=tuple(
            replace(expectation, left_context="", right_context="")
            if expectation.strike_text_characters
            else expectation
            for expectation in inventory.display_expectations
        ),
    )

    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            revision_run_template / "export/existing-changes-display.docx",
            inventory=contextless,
            clean_reference=revision_run_template / "export/review.docx",
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert (
        caught.value.violation.message
        == "LaTeX revision deletion has no safe same-paragraph source context"
    )


def test_display_rejects_nonunique_clean_deletion_boundary(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable eligible paragraph.\n\n"
        "Repeated anchor A\\deleted{LWRCONTEXTOLD}B.\n\n"
        "Repeated anchor AB.\n\n"
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

    with pytest.raises(ContractError) as caught:
        export_workflow(
            run,
            confidentiality="public_fixture",
            generated_at=TIME,
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert (
        caught.value.violation.message
        == "LaTeX revision deletion context does not identify one clean-review boundary"
    )
    assert not (run / "export").exists()


def test_display_validator_requires_every_repeated_instance(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable anchor paragraph.\n\n"
        "First: \\added{LWRDUPLICATE}.\n\n"
        "Second: \\added{LWRDUPLICATE}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    _initialize_and_export(origin, run)
    snapshot = run / "snapshot"
    discovery = discover_project(snapshot, main_document="main.tex")
    inventory = scan_revision_macros(snapshot, discovery)
    assert len(inventory.display_expectations) == 2

    mutated = tmp_path / "one-of-two-red.docx"
    _mutate_display_run(
        run / "export/existing-changes-display.docx",
        mutated,
        token="LWRDUPLICATE",
        mutation="red",
        occurrence=0,
    )

    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            mutated,
            inventory=inventory,
            clean_reference=run / "export/review.docx",
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS


def test_same_text_substitution_is_rejected_as_ambiguous(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{xcolor}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable anchor paragraph.\n\n"
        "Revision: \\added{LWRCOLLISION}.\n\n"
        "Unrelated same text: LWRCOLLISION.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractError) as caught:
        _initialize_and_export(origin, run)

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert caught.value.violation.message == "LaTeX revision display text location is ambiguous"
    details = caught.value.violation.details
    assert details is not None
    assert details["expected_text_instances"] == 1
    assert details["observed_text_instances"] == 2
    assert not (run / "export").exists()


def test_display_backend_findings_must_match_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    _write_revision_project(origin)

    def add_warning(
        _request: BackendRequest,
        result: BackendResult,
    ) -> BackendResult:
        finding = ExportFinding(
            code=ErrorCode.EXPORT_DEGRADED,
            severity="warning",
            phase="export",
            message="display-only synthetic warning",
            recoverable=True,
            remediation="regenerate the display",
        )
        return replace(
            result,
            findings=(*result.findings, finding),
        )

    backend = _DisplayResultMutator(add_warning)
    monkeypatch.setattr(
        workflow_module,
        "Tex2WordBackend",
        lambda: backend,
    )
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    with pytest.raises(ContractError) as caught:
        export_workflow(
            run,
            confidentiality="public_fixture",
            generated_at=TIME,
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert not (run / "export").exists()


def test_display_structure_must_match_clean_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable anchor paragraph.\n\n"
        "\\begin{tabular}{ll}\n"
        "A & B \\\\\n"
        "C & D \\\\\n"
        "\\end{tabular}\n\n"
        "Changed: \\added{LWRSTRUCTURE}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    def remove_table(
        request: BackendRequest,
        result: BackendResult,
    ) -> BackendResult:
        _remove_first_table(request.output_path)
        digest = digest_file(request.output_path, max_bytes=128 * 1024 * 1024)
        return replace(
            result,
            artifact_sha256=digest.sha256,
            artifact_size_bytes=digest.size_bytes,
        )

    backend = _DisplayResultMutator(remove_table)
    monkeypatch.setattr(
        workflow_module,
        "Tex2WordBackend",
        lambda: backend,
    )
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    with pytest.raises(ContractError) as caught:
        export_workflow(
            run,
            confidentiality="public_fixture",
            generated_at=TIME,
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    details = caught.value.violation.details
    assert details is not None
    assert (
        details.get("metric") in {"paragraphs", "tables"}
        or details.get("evidence_kind") == "table structure"
        or set(details) == {"clean_paragraphs", "display_paragraphs"}
        or set(details) == {"part_uri", "clean_tables", "display_tables"}
    )
    assert not (run / "export").exists()


@pytest.mark.parametrize("mutation_kind", ["body", "formula", "table-cell"])
def test_display_rejects_non_revision_content_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_kind: str,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable body token LWRBODYSAFE.\n\n"
        "Formula $x+y=z$.\n\n"
        "\\begin{tabular}{ll}\n"
        "A & B \\\\\n"
        "C & D \\\\\n"
        "\\end{tabular}\n\n"
        "Changed: \\added{LWRSTRUCTUREDREV}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    def mutate_content(
        request: BackendRequest,
        result: BackendResult,
    ) -> BackendResult:
        def mutate(members: dict[str, bytes]) -> None:
            root = ET.fromstring(members["word/document.xml"])
            if mutation_kind == "body":
                node = next(
                    item for item in root.iter(f"{W}t") if "LWRBODYSAFE" in (item.text or "")
                )
                node.text = (node.text or "").replace("LWRBODYSAFE", "LWRBODYXAFE")
            elif mutation_kind == "formula":
                node = next(item for item in root.iter(f"{M}t") if item.text)
                node.text = f"{node.text}X"
            else:
                cell_text = next(
                    item
                    for cell in root.iter(f"{W}tc")
                    for item in cell.iter(f"{W}t")
                    if (item.text or "") == "A"
                )
                cell_text.text = "Z"
            members["word/document.xml"] = ET.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
            )

        return _mutate_backend_docx(request, result, mutate)

    backend = _DisplayResultMutator(mutate_content)
    monkeypatch.setattr(workflow_module, "Tex2WordBackend", lambda: backend)
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    with pytest.raises(ContractError) as caught:
        export_workflow(
            run,
            confidentiality="public_fixture",
            generated_at=TIME,
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert not (run / "export").exists()


def test_display_rejects_non_revision_style_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Existing bold: \\textbf{LWRSTYLEA}.\n\n"
        "Existing plain: LWRSTYLEB.\n\n"
        "Revision: \\added{LWRSTYLEREV}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    def move_style(
        request: BackendRequest,
        result: BackendResult,
    ) -> BackendResult:
        def mutate(members: dict[str, bytes]) -> None:
            root = ET.fromstring(members["word/document.xml"])
            _set_direct_run_boolean_property(root, "LWRSTYLEA", "b", False)
            _set_direct_run_boolean_property(root, "LWRSTYLEB", "b", True)
            members["word/document.xml"] = ET.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
            )

        return _mutate_backend_docx(request, result, mutate)

    backend = _DisplayResultMutator(move_style)
    monkeypatch.setattr(workflow_module, "Tex2WordBackend", lambda: backend)
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    with pytest.raises(ContractError) as caught:
        export_workflow(
            run,
            confidentiality="public_fixture",
            generated_at=TIME,
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert "styling outside a revision macro" in caught.value.violation.message
    assert not (run / "export").exists()


@pytest.mark.parametrize("style_kind", ["blue", "highlight"])
def test_display_rejects_existing_blue_or_highlight_migration(
    tmp_path: Path,
    style_kind: str,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    origin.mkdir()
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Existing A: LWRSTYLEA.\n\n"
        "Existing B: LWRSTYLEB.\n\n"
        "Revision: \\added{LWRSTYLEPOSITION}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    _initialize_and_export(origin, run)
    clean = tmp_path / f"clean-{style_kind}.docx"
    display = tmp_path / f"display-{style_kind}.docx"

    def style_a(root: ET.Element) -> None:
        if style_kind == "blue":
            _set_direct_run_color_property(root, "LWRSTYLEA", "0000FF")
        else:
            _set_direct_run_boolean_property(root, "LWRSTYLEA", "highlight", True)

    def style_b(root: ET.Element) -> None:
        if style_kind == "blue":
            _set_direct_run_color_property(root, "LWRSTYLEB", "0000FF")
        else:
            _set_direct_run_boolean_property(root, "LWRSTYLEB", "highlight", True)

    _mutate_docx_copy(run / "export/review.docx", clean, style_a)
    _mutate_docx_copy(run / "export/existing-changes-display.docx", display, style_b)
    discovery = discover_project(run / "snapshot", main_document="main.tex")
    inventory = scan_revision_macros(run / "snapshot", discovery)

    with pytest.raises(ContractError) as caught:
        validate_revision_display(
            display,
            inventory=inventory,
            clean_reference=clean,
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert "styling outside a revision macro" in caught.value.violation.message


def test_display_rejects_same_path_image_byte_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    assets = origin / "assets"
    assets.mkdir(parents=True)
    shutil.copyfile(
        Path("tests/fixtures/e0-minimal-paper/source/assets/response-curve.png"),
        assets / "response-curve.png",
    )
    (origin / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage{changes}\n"
        "\\begin{document}\n\n"
        "Stable image.\n\n"
        "\\includegraphics[width=2cm]{assets/response-curve.png}\n\n"
        "Revision: \\added{LWRIMAGEREV}.\n\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    def change_media_bytes(
        request: BackendRequest,
        result: BackendResult,
    ) -> BackendResult:
        def mutate(members: dict[str, bytes]) -> None:
            media_name = next(name for name in sorted(members) if name.startswith("word/media/"))
            members[media_name] = members[media_name] + b"LWR"

        return _mutate_backend_docx(request, result, mutate)

    backend = _DisplayResultMutator(change_media_bytes)
    monkeypatch.setattr(workflow_module, "Tex2WordBackend", lambda: backend)
    initialize_workflow(
        origin,
        run,
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    with pytest.raises(ContractError) as caught:
        export_workflow(
            run,
            confidentiality="public_fixture",
            generated_at=TIME,
        )

    assert caught.value.code is ErrorCode.EXPORT_SILENT_LOSS
    details = caught.value.violation.details
    assert details is not None
    assert details.get("evidence_kind") == "media and embeddings"
    assert not (run / "export").exists()


def test_workflow_without_revision_macros_does_not_emit_display(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    _write_plain_project(origin)

    result = _initialize_and_export(origin, run)

    assert result["counts"]["revision_macro_instances"] == 0
    assert result["counts"]["revision_display_available"] == 0
    assert not (run / "export/existing-changes-display.docx").exists()
    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = cast("dict[str, Any]", report["payload"])
    metrics = cast("dict[str, Any]", payload["metrics"])
    output_metrics = cast("dict[str, int]", metrics["output"])
    assert payload["existing_changes_display_docx"] is None
    assert output_metrics["revision_display_available"] == 0
    status = workflow_status(run)
    assert status["counts"]["revision_macro_instances"] == 0
    assert status["counts"]["revision_display_available"] == 0


@pytest.mark.parametrize(
    "producer_interface_version",
    ["export-report-builder-v1", PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION],
)
def test_previous_no_macro_report_may_omit_display_field(
    tmp_path: Path,
    producer_interface_version: str,
) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    _write_plain_project(origin)
    _initialize_and_export(origin, run)
    core = workflow_module._load_core(run)
    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = copy.deepcopy(cast("dict[str, Any]", report["payload"]))
    _strip_current_revision_display_evidence(payload)

    inventory, metrics, digest = workflow_module._validate_existing_changes_display(
        core,
        run / "export",
        payload,
        producer_interface_version=producer_interface_version,
    )

    assert inventory is None
    assert metrics == {"revision_display_available": 0}
    assert digest is None


@pytest.mark.parametrize(
    "producer_interface_version",
    ["export-report-builder-v1", PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION],
)
def test_previous_report_with_revision_macros_remains_readable_without_v3_evidence(
    revision_run_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    producer_interface_version: str,
) -> None:
    run = _copy_run(revision_run_template, tmp_path)
    display = run / "export/existing-changes-display.docx"
    display.chmod(stat.S_IRUSR | stat.S_IWUSR)
    display.unlink()
    core = workflow_module._load_core(run)
    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = copy.deepcopy(cast("dict[str, Any]", report["payload"]))
    _strip_current_revision_display_evidence(payload)

    def unexpected_scan(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy reports must not be reinterpreted by the v3 macro scanner")

    monkeypatch.setattr(workflow_module, "scan_revision_macros", unexpected_scan)
    inventory, metrics, digest = workflow_module._validate_existing_changes_display(
        core,
        run / "export",
        payload,
        producer_interface_version=producer_interface_version,
    )

    assert inventory is None
    assert metrics == {"revision_display_available": 0}
    assert digest is None


def test_previous_report_rejects_v3_revision_metrics(
    revision_run_template: Path,
    tmp_path: Path,
) -> None:
    run = _copy_run(revision_run_template, tmp_path)
    display = run / "export/existing-changes-display.docx"
    display.chmod(stat.S_IRUSR | stat.S_IWUSR)
    display.unlink()
    core = workflow_module._load_core(run)
    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = copy.deepcopy(cast("dict[str, Any]", report["payload"]))
    payload.pop("existing_changes_display_docx")

    with pytest.raises(ContractError) as raised:
        workflow_module._validate_existing_changes_display(
            core,
            run / "export",
            payload,
            producer_interface_version=PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
        )

    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH


@pytest.mark.parametrize(
    "producer_interface_version",
    ["export-report-builder-v1", PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION],
)
def test_previous_report_schema_rejects_v3_display_field(
    revision_run_template: Path,
    tmp_path: Path,
    producer_interface_version: str,
) -> None:
    run = _copy_run(revision_run_template, tmp_path)
    report_path = run / "export/objects/export-report.json"
    report = read_contract_file(report_path, expected_schema="ExportReport")
    producer = cast("dict[str, Any]", report["producer"])
    producer["interface_version"] = producer_interface_version
    _write_resealed_contract(report_path, report)

    with pytest.raises(ContractError) as raised:
        workflow_status(run)

    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_resealed_display_artifact_id_must_be_digest_derived(
    revision_run_template: Path,
    tmp_path: Path,
) -> None:
    run = _copy_run(revision_run_template, tmp_path)
    report_path = run / "export/objects/export-report.json"
    source_map_path = run / "export/objects/source-map.json"
    report = read_contract_file(report_path, expected_schema="ExportReport")
    report_payload = cast("dict[str, Any]", report["payload"])
    artifact = cast("dict[str, Any]", report_payload["existing_changes_display_docx"])
    artifact["artifact_id"] = "art_" + "z" * 64
    source_map = read_contract_file(source_map_path, expected_schema="SourceMap")
    source_map_payload = cast("dict[str, Any]", source_map["payload"])
    source_map_payload["export_report_commitment"] = export_report_commitment(report_payload)
    resealed_source_map = seal_envelope(source_map)
    report_payload["source_map_sha256"] = compute_payload_sha256(resealed_source_map)
    _write_resealed_contract(source_map_path, source_map)
    _write_resealed_contract(report_path, report)

    with pytest.raises(ContractError) as raised:
        workflow_status(run)

    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_profile_v2_exact_only_revision_task_remains_fully_readable(
    revision_run_template: Path,
    tmp_path: Path,
) -> None:
    run = _copy_run(revision_run_template, tmp_path)
    report_path = run / "export/objects/export-report.json"
    source_map_path = run / "export/objects/source-map.json"
    report = read_contract_file(report_path, expected_schema="ExportReport")
    report_payload = cast("dict[str, Any]", report["payload"])
    metrics = cast("dict[str, Any]", report_payload["metrics"])
    source_metrics = cast("dict[str, int]", metrics["source"])
    source_metrics["revision_macro_inventory_version"] = 2
    for name in (
        "revision_display_exact_macro_instances",
        "revision_display_degraded_macro_instances",
        "revision_display_degradation_calls",
    ):
        source_metrics.pop(name)

    source_map = read_contract_file(source_map_path, expected_schema="SourceMap")
    source_map_payload = cast("dict[str, Any]", source_map["payload"])
    source_map_payload["export_report_commitment"] = export_report_commitment(report_payload)
    resealed_source_map = seal_envelope(source_map)
    report_payload["source_map_sha256"] = compute_payload_sha256(resealed_source_map)
    _write_resealed_contract(source_map_path, source_map)
    _write_resealed_contract(report_path, report)

    status = workflow_status(run)

    assert status["phase"] == "exported"
    assert status["counts"]["revision_macro_instances"] == 5
    assert status["counts"]["revision_display_available"] == 1


def test_profile_v2_revision_evidence_rejects_current_only_metrics(
    revision_run_template: Path,
    tmp_path: Path,
) -> None:
    run = _copy_run(revision_run_template, tmp_path)
    core = workflow_module._load_core(run)
    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = copy.deepcopy(cast("dict[str, Any]", report["payload"]))
    metrics = cast("dict[str, Any]", payload["metrics"])
    source_metrics = cast("dict[str, int]", metrics["source"])
    source_metrics["revision_macro_inventory_version"] = 2

    with pytest.raises(ContractError) as raised:
        workflow_module._validate_existing_changes_display(
            core,
            run / "export",
            payload,
            producer_interface_version=EXPORT_REPORT_INTERFACE_VERSION,
        )

    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_current_no_macro_report_cannot_omit_display_field(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    run = tmp_path / "run"
    _write_plain_project(origin)
    _initialize_and_export(origin, run)
    core = workflow_module._load_core(run)
    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = copy.deepcopy(cast("dict[str, Any]", report["payload"]))
    payload.pop("existing_changes_display_docx")

    with pytest.raises(ContractError) as raised:
        workflow_module._validate_existing_changes_display(
            core,
            run / "export",
            payload,
            producer_interface_version=EXPORT_REPORT_INTERFACE_VERSION,
        )

    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_current_resealed_report_cannot_strip_revision_display(
    revision_run_template: Path,
    tmp_path: Path,
) -> None:
    run = _copy_run(revision_run_template, tmp_path)
    display = run / "export/existing-changes-display.docx"
    display.chmod(stat.S_IRUSR | stat.S_IWUSR)
    display.unlink()
    report_path = run / "export/objects/export-report.json"
    source_map_path = run / "export/objects/source-map.json"
    report = read_contract_file(report_path, expected_schema="ExportReport")
    report_payload = cast("dict[str, Any]", report["payload"])
    report_payload.pop("existing_changes_display_docx")
    source_map = read_contract_file(source_map_path, expected_schema="SourceMap")
    source_map_payload = cast("dict[str, Any]", source_map["payload"])
    source_map_payload["export_report_commitment"] = export_report_commitment(report_payload)
    resealed_source_map = seal_envelope(source_map)
    report_payload["source_map_sha256"] = compute_payload_sha256(resealed_source_map)
    _write_resealed_contract(source_map_path, source_map)
    _write_resealed_contract(report_path, report)

    with pytest.raises(ContractError):
        workflow_status(run)


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_workflow_status_fails_closed_when_revision_display_is_missing_or_tampered(
    revision_run_template: Path,
    tmp_path: Path,
    mutation: str,
) -> None:
    run = _copy_run(revision_run_template, tmp_path)
    display = run / "export/existing-changes-display.docx"
    display.chmod(stat.S_IRUSR | stat.S_IWUSR)
    if mutation == "missing":
        display.unlink()
    else:
        display.write_bytes(display.read_bytes() + b"tampered")
        display.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    with pytest.raises(ContractError) as raised:
        workflow_status(run)

    assert raised.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_revision_display_cannot_be_used_as_returned_review_baseline(
    revision_run_template: Path,
    tmp_path: Path,
) -> None:
    run = _copy_run(revision_run_template, tmp_path)
    display = run / "export/existing-changes-display.docx"

    with pytest.raises(ContractError) as raised:
        receive_workflow(
            run,
            display,
            confidentiality="public_fixture",
            generated_at=TIME,
        )

    assert raised.value.code is ErrorCode.REVISION_BASELINE_DRIFT
    assert not (run / "receive").exists()
    assert not list((run / ".lwr-staging").glob(".lwr-stage-receive-*"))


@pytest.mark.parametrize(
    "copy_mode",
    ["sealed-original", "program-copy", "word-save-as-repackaged"],
)
def test_additions_only_display_is_rejected_by_sealed_role_after_save_as(
    additions_only_run_template: Path,
    tmp_path: Path,
    copy_mode: str,
) -> None:
    run = _copy_run(additions_only_run_template, tmp_path)
    review = run / "export/review.docx"
    display = run / "export/existing-changes-display.docx"
    assert _visible_text(_document_root(review)) == _visible_text(_document_root(display))

    report = read_contract_file(
        run / "export/objects/export-report.json",
        expected_schema="ExportReport",
    )
    payload = cast("dict[str, Any]", report["payload"])
    artifact = cast("dict[str, Any]", payload["existing_changes_display_docx"])
    assert artifact["role"] == "latex_changes_display_docx"
    assert digest_file(display, max_bytes=128 * 1024 * 1024).sha256 == artifact["sha256"]

    returned = display
    if copy_mode == "program-copy":
        returned = tmp_path / "program-copy-existing-changes-display.docx"
        shutil.copyfile(display, returned)
        assert digest_file(returned, max_bytes=128 * 1024 * 1024) == digest_file(
            display,
            max_bytes=128 * 1024 * 1024,
        )
    elif copy_mode == "word-save-as-repackaged":
        returned = tmp_path / "saved-as-existing-changes-display.docx"
        _repackage_docx(display, returned)
        assert digest_file(returned, max_bytes=128 * 1024 * 1024) != digest_file(
            display,
            max_bytes=128 * 1024 * 1024,
        )
    assert _artifact_marker_values(returned) == {
        "LWR_ARTIFACT_ROLE": "latex_changes_display_docx",
        "LWR_ARTIFACT_PROFILE": "lwr-existing-changes-display-v1",
        "LWR_RUN_ID": report["run_id"],
    }

    with pytest.raises(ContractError) as raised:
        receive_workflow(
            run,
            returned,
            confidentiality="public_fixture",
            generated_at=TIME,
        )

    assert raised.value.code is ErrorCode.REVISION_BASELINE_DRIFT
    assert not (run / "receive").exists()
    assert not list((run / ".lwr-staging").glob(".lwr-stage-receive-*"))


def test_blue_text_in_editable_review_copy_is_not_an_artifact_role(
    additions_only_run_template: Path,
    tmp_path: Path,
) -> None:
    run = _copy_run(additions_only_run_template, tmp_path)
    review = run / "export/review.docx"
    returned = tmp_path / "blue-edited-review.docx"
    assert _artifact_marker_values(review) == {}
    _make_blue_returned_review(run, returned)
    assert _artifact_marker_values(returned) == {}
    returned_root = _document_root(returned)
    blue_insert = returned_root.find(f".//{W}ins/{W}r/{W}rPr/{W}color")
    assert blue_insert is not None
    assert blue_insert.get(f"{W}val") == "0000FF"

    result = receive_workflow(
        run,
        returned,
        confidentiality="public_fixture",
        generated_at=TIME,
    )

    assert result["phase"] == "ingested"
    assert result["counts"]["changes"] == 1
