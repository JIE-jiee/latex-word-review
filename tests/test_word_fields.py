"""Focused contracts for safe Word field refresh and baseline sealing."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from lxml import etree  # type: ignore[import-untyped]

import latex_word_review.word_fields as word_fields_module
from latex_word_review.docx_reader import read_docx_package
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.inspection import DocxInspection
from latex_word_review.review_layout import apply_review_layout, validate_review_layout
from latex_word_review.runtime import CommandResult
from latex_word_review.word_fields import finalize_word_fields, validate_frozen_field_state
from tests._docx_factory import write_docx

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS = {"w": W_NS, "m": M_NS}
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
FIELD_SCRIPT = (
    Path(word_fields_module.__file__).resolve().parent
    / "assets"
    / word_fields_module.WORD_FIELD_SCRIPT
)


def _q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _document_xml(
    *,
    field_instruction: str | None = None,
    include_math: bool = False,
) -> bytes:
    root = etree.Element(_q(W_NS, "document"), nsmap=NS)
    body = etree.SubElement(root, _q(W_NS, "body"))
    paragraph = etree.SubElement(body, _q(W_NS, "p"))
    run = etree.SubElement(paragraph, _q(W_NS, "r"))
    text = etree.SubElement(run, _q(W_NS, "t"))
    text.text = "Synthetic review paragraph."

    if field_instruction is not None:
        field_paragraph = etree.SubElement(body, _q(W_NS, "p"))
        begin_run = etree.SubElement(field_paragraph, _q(W_NS, "r"))
        begin = etree.SubElement(begin_run, _q(W_NS, "fldChar"))
        begin.set(_q(W_NS, "fldCharType"), "begin")
        begin.set(_q(W_NS, "dirty"), "false")
        instruction_run = etree.SubElement(field_paragraph, _q(W_NS, "r"))
        instruction = etree.SubElement(instruction_run, _q(W_NS, "instrText"))
        instruction.text = field_instruction
        separate_run = etree.SubElement(field_paragraph, _q(W_NS, "r"))
        separate = etree.SubElement(separate_run, _q(W_NS, "fldChar"))
        separate.set(_q(W_NS, "fldCharType"), "separate")
        result_run = etree.SubElement(field_paragraph, _q(W_NS, "r"))
        result = etree.SubElement(result_run, _q(W_NS, "t"))
        result.text = "1"
        end_run = etree.SubElement(field_paragraph, _q(W_NS, "r"))
        end = etree.SubElement(end_run, _q(W_NS, "fldChar"))
        end.set(_q(W_NS, "fldCharType"), "end")

    if include_math:
        math_paragraph = etree.SubElement(body, _q(W_NS, "p"))
        math = etree.SubElement(math_paragraph, _q(M_NS, "oMath"))
        math_run = etree.SubElement(math, _q(M_NS, "r"))
        math_text = etree.SubElement(math_run, _q(M_NS, "t"))
        math_text.text = "x"

    section = etree.SubElement(body, _q(W_NS, "sectPr"))
    page_size = etree.SubElement(section, _q(W_NS, "pgSz"))
    page_size.set(_q(W_NS, "w"), "12240")
    page_size.set(_q(W_NS, "h"), "15840")
    margins = etree.SubElement(section, _q(W_NS, "pgMar"))
    margins.set(_q(W_NS, "left"), "1440")
    margins.set(_q(W_NS, "right"), "1440")
    columns = etree.SubElement(section, _q(W_NS, "cols"))
    columns.set(_q(W_NS, "num"), "2")
    return cast(
        bytes,
        etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True),
    )


def _pending_docx(
    root: Path,
    *,
    field_instruction: str | None = None,
    include_math: bool = False,
) -> Path:
    raw = root / "raw.docx"
    pending = root / "pending.docx"
    write_docx(
        raw,
        document_xml=_document_xml(
            field_instruction=field_instruction,
            include_math=include_math,
        ),
    )
    apply_review_layout(raw, pending)
    validate_review_layout(pending, field_refresh="pending")
    return pending


def _pending_table_docx(root: Path, instruction: str) -> Path:
    document = etree.fromstring(_document_xml(field_instruction=instruction))
    body = document.find(_q(W_NS, "body"))
    assert body is not None
    section = body.find(_q(W_NS, "sectPr"))
    assert section is not None
    table = etree.Element(_q(W_NS, "tbl"))
    etree.SubElement(table, _q(W_NS, "tblPr"))
    row = etree.SubElement(table, _q(W_NS, "tr"))
    cell = etree.SubElement(row, _q(W_NS, "tc"))
    paragraph = etree.SubElement(cell, _q(W_NS, "p"))
    run = etree.SubElement(paragraph, _q(W_NS, "r"))
    etree.SubElement(run, _q(W_NS, "t")).text = "Compact cell"
    body.insert(body.index(section), table)
    raw = root / "raw-table.docx"
    pending = root / "pending-table.docx"
    write_docx(
        raw,
        document_xml=cast(
            bytes,
            etree.tostring(
                document,
                encoding="UTF-8",
                xml_declaration=True,
                standalone=True,
            ),
        ),
    )
    apply_review_layout(raw, pending)
    validate_review_layout(pending, field_refresh="pending")
    return pending


def _append_complex_field(parent: etree._Element, instruction: str) -> None:
    begin_run = etree.SubElement(parent, _q(W_NS, "r"))
    begin = etree.SubElement(begin_run, _q(W_NS, "fldChar"))
    begin.set(_q(W_NS, "fldCharType"), "begin")
    begin.set(_q(W_NS, "dirty"), "false")
    instruction_run = etree.SubElement(parent, _q(W_NS, "r"))
    instruction_element = etree.SubElement(instruction_run, _q(W_NS, "instrText"))
    instruction_element.text = instruction
    separate_run = etree.SubElement(parent, _q(W_NS, "r"))
    separate = etree.SubElement(separate_run, _q(W_NS, "fldChar"))
    separate.set(_q(W_NS, "fldCharType"), "separate")
    result_run = etree.SubElement(parent, _q(W_NS, "r"))
    result = etree.SubElement(result_run, _q(W_NS, "t"))
    result.text = "1"
    end_run = etree.SubElement(parent, _q(W_NS, "r"))
    end = etree.SubElement(end_run, _q(W_NS, "fldChar"))
    end.set(_q(W_NS, "fldCharType"), "end")


def _pending_textbox_docx(root: Path, instruction: str) -> Path:
    document = etree.fromstring(_document_xml())
    body = document.find(_q(W_NS, "body"))
    assert body is not None
    section = body.find(_q(W_NS, "sectPr"))
    assert section is not None
    drawing_paragraph = etree.Element(_q(W_NS, "p"))
    drawing_run = etree.SubElement(drawing_paragraph, _q(W_NS, "r"))
    textbox = etree.SubElement(drawing_run, _q(W_NS, "txbxContent"))
    textbox_paragraph = etree.SubElement(textbox, _q(W_NS, "p"))
    _append_complex_field(textbox_paragraph, instruction)
    body.insert(body.index(section), drawing_paragraph)

    raw = root / "raw.docx"
    pending = root / "pending.docx"
    write_docx(
        raw,
        document_xml=cast(
            bytes,
            etree.tostring(
                document,
                encoding="UTF-8",
                xml_declaration=True,
                standalone=True,
            ),
        ),
    )
    apply_review_layout(raw, pending)
    validate_review_layout(pending, field_refresh="pending")
    return pending


def _pending_image_docx(root: Path, instruction: str) -> Path:
    document = etree.fromstring(_document_xml(field_instruction=instruction))
    body = document.find(_q(W_NS, "body"))
    assert body is not None
    section = body.find(_q(W_NS, "sectPr"))
    assert section is not None
    image_paragraph = etree.Element(_q(W_NS, "p"))
    image_run = etree.SubElement(image_paragraph, _q(W_NS, "r"))
    drawing = etree.SubElement(image_run, _q(W_NS, "drawing"))
    blip = etree.SubElement(drawing, _q(A_NS, "blip"))
    blip.set(_q(R_NS, "embed"), "rIdImage")
    body.insert(body.index(section), image_paragraph)

    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        '<Relationship Id="rIdImage" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/image" Target="media/image1.png"/>'
        "</Relationships>"
    ).encode()
    raw = root / "raw.docx"
    pending = root / "pending.docx"
    write_docx(
        raw,
        document_xml=cast(
            bytes,
            etree.tostring(
                document,
                encoding="UTF-8",
                xml_declaration=True,
                standalone=True,
            ),
        ),
        document_rels=relationships,
        extra_parts={
            "word/media/image1.png": b"synthetic-image-one",
            "word/media/image2.png": b"synthetic-image-two",
        },
    )
    apply_review_layout(raw, pending)
    validate_review_layout(pending, field_refresh="pending")
    return pending


def _sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _argument(arguments: Sequence[str], flag: str) -> str:
    index = tuple(arguments).index(flag)
    return arguments[index + 1]


def _install_success_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutate_output: Callable[[Path], None] | None = None,
    report_raw: bytes | None = None,
    report_mutator: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        word_fields_module,
        "_powershell_executable",
        lambda: Path("powershell.exe"),
    )

    def fake_run_command(
        executable: str | Path,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float = 5.0,
        max_output_bytes: int = 1024 * 1024,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        source = Path(_argument(arguments, "-SourceDocx"))
        destination = Path(_argument(arguments, "-DestinationDocx"))
        report_path = Path(_argument(arguments, "-ReportPath"))
        expected_fields = int(_argument(arguments, "-ExpectedFieldCount"))
        expected_unresolved = int(_argument(arguments, "-ExpectedUnresolvedFields"))
        assert environment is not None
        assert Path(environment["TEMP"]).resolve() == destination.parent.resolve()
        assert Path(environment["TMP"]).resolve() == destination.parent.resolve()
        shutil.copyfile(source, destination)
        if mutate_output is not None:
            mutate_output(destination)
        report_path.write_text(
            json.dumps(
                {
                    "schema_version": "word-field-refresh/v1",
                    "status": "passed",
                    "word_version": "16.0-test",
                    "field_count": expected_fields,
                    "updated_fields": expected_fields - expected_unresolved,
                    "unresolved_fields": expected_unresolved,
                    "revision_count": 0,
                    "bookmark_count": 0,
                    "source_sha256": _sha256_hex(source),
                    "output_sha256": _sha256_hex(destination),
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        if report_mutator is not None:
            payload = cast(
                "dict[str, object]",
                json.loads(report_path.read_text(encoding="utf-8")),
            )
            report_mutator(payload)
            report_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
                newline="\n",
            )
        if report_raw is not None:
            report_path.write_bytes(report_raw)
        observed.update(
            {
                "executable": executable,
                "arguments": tuple(arguments),
                "cwd": cwd,
                "timeout_s": timeout_s,
                "max_output_bytes": max_output_bytes,
            }
        )
        return CommandResult(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            output_truncated=False,
            duration_ms=37,
            output_sha256="sha256:" + "0" * 64,
        )

    monkeypatch.setattr(word_fields_module, "run_command", fake_run_command)
    return observed


def _install_result_runner(
    monkeypatch: pytest.MonkeyPatch,
    result: CommandResult,
) -> None:
    monkeypatch.setattr(
        word_fields_module,
        "_powershell_executable",
        lambda: Path("powershell.exe"),
    )

    def fake_run_command(
        _executable: str | Path,
        _arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float = 5.0,
        max_output_bytes: int = 1024 * 1024,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del cwd, timeout_s, max_output_bytes, environment
        return result

    monkeypatch.setattr(word_fields_module, "run_command", fake_run_command)


def _assert_frozen_trackless(path: Path) -> None:
    package = read_docx_package(path)
    validate_frozen_field_state(package)
    validate_review_layout(path, field_refresh="frozen")
    settings = package.xml_root("word/settings.xml")
    updates = settings.findall(_q(W_NS, "updateFields"))
    assert len(updates) == 1
    assert updates[0].get(_q(W_NS, "val")) == "false"
    assert settings.find(_q(W_NS, "trackRevisions")) is None
    document = package.xml_root("word/document.xml")
    assert all(_q(W_NS, "dirty") not in field.attrib for field in document.iter())


def _drop_math(path: Path) -> None:
    raw = path.read_bytes()
    replacement = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive,
        zipfile.ZipFile(replacement, mode="w") as output,
    ):
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == "word/document.xml":
                root = etree.fromstring(data)
                for math in tuple(root.iter(_q(M_NS, "oMath"))):
                    parent = math.getparent()
                    assert parent is not None
                    parent.remove(math)
                data = cast(
                    bytes,
                    etree.tostring(
                        root,
                        encoding="UTF-8",
                        xml_declaration=True,
                        standalone=True,
                    ),
                )
            output.writestr(info, data)
    path.write_bytes(replacement.getvalue())


def _rewrite_zip(path: Path, mutate_member: Callable[[str, bytes], bytes]) -> None:
    raw = path.read_bytes()
    replacement = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive,
        zipfile.ZipFile(replacement, mode="w") as output,
    ):
        for info in archive.infolist():
            output.writestr(info, mutate_member(info.filename, archive.read(info.filename)))
    path.write_bytes(replacement.getvalue())


def _change_body_text(path: Path) -> None:
    def mutate_member(name: str, data: bytes) -> bytes:
        if name != "word/document.xml":
            return data
        root = etree.fromstring(data)
        text = next(
            element
            for element in root.iter(_q(W_NS, "t"))
            if element.text == "Synthetic review paragraph."
        )
        text.text = "Tampered review paragraph."
        return cast(
            bytes,
            etree.tostring(
                root,
                encoding="UTF-8",
                xml_declaration=True,
                standalone=True,
            ),
        )

    _rewrite_zip(path, mutate_member)


def _change_media_bytes(path: Path) -> None:
    def mutate_member(name: str, data: bytes) -> bytes:
        if name == "word/media/image1.png":
            return b"word-output-replaced-image"
        return data

    _rewrite_zip(path, mutate_member)


def _change_relationship_target(path: Path) -> None:
    def mutate_member(name: str, data: bytes) -> bytes:
        if name != "word/_rels/document.xml.rels":
            return data
        root = etree.fromstring(data)
        relationship = next(
            element
            for element in root.iter(_q(PKG_REL_NS, "Relationship"))
            if element.get("Id") == "rIdImage"
        )
        relationship.set("Target", "media/image2.png")
        return cast(
            bytes,
            etree.tostring(
                root,
                encoding="UTF-8",
                xml_declaration=True,
                standalone=True,
            ),
        )

    _rewrite_zip(path, mutate_member)


def test_field_finalizer_script_matches_python_security_contract() -> None:
    text = FIELD_SCRIPT.read_text(encoding="utf-8")

    save_as_call = (
        "$document.SaveAs2(\n"
        "        $DestinationFullPath,\n"
        "        $WdFormatXmlDocument,\n"
        "        [System.Type]::Missing,\n"
        "        [System.Type]::Missing,\n"
        "        $false\n"
        "    )"
    )
    reference_pattern = (
        '"^(?<kind>REF|PAGEREF)\\s+'
        "(?<target>[A-Za-z_][A-Za-z0-9_]{0,254})"
        '(?<switches>(?:\\s+\\\\[hr])*)$"'
    )

    assert save_as_call in text
    assert reference_pattern in text
    assert text.count("$bookmarks.ShowHidden = $true") == 2
    assert "function Get-WinWordProcessSnapshot" in text
    assert "function Get-WinWordIdentityKey" in text
    assert "$beforeWordIdentities = @(Get-WinWordProcessSnapshot)" in text
    assert "started_filetime_utc" in text
    assert "StartTime.ToUniversalTime().ToFileTimeUtc()" in text
    assert "$beforePids" not in text
    assert "ShowRevisions" not in text
    assert "PrintRevisions" not in text
    assert ("[string] $secondPass.results_sha256 -cne [string] $thirdPass.results_sha256") in text


def test_zero_fields_are_sealed_without_invoking_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_docx(tmp_path)
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()

    def forbidden_word(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("zero-field documents must not invoke Word")

    monkeypatch.setattr(word_fields_module, "_invoke_word", forbidden_word)
    report = finalize_word_fields(source, destination)

    assert source.read_bytes() == source_before
    assert report.source_sha256 == "sha256:" + hashlib.sha256(source_before).hexdigest()
    assert report.word_automation_used is False
    assert report.word_version is None
    assert report.word_output_sha256 is None
    assert (report.field_count, report.updated_fields, report.unresolved_fields) == (0, 0, 0)
    assert report.duration_ms == 0
    _assert_frozen_trackless(destination)
    assert not list(tmp_path.glob(".review.docx.*"))


def test_supported_field_uses_bounded_runner_and_seals_final_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_docx(tmp_path, field_instruction=r" SEQ Figure \* ARABIC ")
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()
    observed = _install_success_runner(monkeypatch)

    report = finalize_word_fields(source, destination)

    assert source.read_bytes() == source_before
    assert report.word_automation_used is True
    assert report.word_version == "16.0-test"
    assert report.word_output_sha256 is not None
    assert (report.field_count, report.updated_fields, report.unresolved_fields) == (1, 1, 0)
    assert report.duration_ms == 37
    arguments = cast("tuple[str, ...]", observed["arguments"])
    assert arguments[:8] == (
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Sta",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        arguments[7],
    )
    observed_cwd = cast("Path", observed["cwd"])
    assert observed_cwd.parent == tmp_path
    assert observed_cwd.name.startswith(".review.docx.word-run-")
    assert not observed_cwd.exists()
    assert observed["timeout_s"] == 60
    assert observed["max_output_bytes"] == 64 * 1024
    _assert_frozen_trackless(destination)
    assert not list(tmp_path.glob(".review.docx.*"))


def test_word_field_refresh_reapplies_review_layout_before_sealing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_table_docx(tmp_path, r" SEQ Table \* ARABIC ")
    destination = tmp_path / "review.docx"

    def remove_explicit_spacing(path: Path) -> None:
        def mutate(name: str, data: bytes) -> bytes:
            if name != "word/document.xml":
                return data
            root = etree.fromstring(data)
            spacing = root.find(".//w:tbl//w:p/w:pPr/w:spacing", NS)
            assert spacing is not None
            spacing.getparent().remove(spacing)
            return cast(
                bytes,
                etree.tostring(
                    root,
                    encoding="UTF-8",
                    xml_declaration=True,
                    standalone=True,
                ),
            )

        _rewrite_zip(path, mutate)

    _install_success_runner(monkeypatch, mutate_output=remove_explicit_spacing)
    report = finalize_word_fields(source, destination)

    assert report.word_automation_used is True
    _assert_frozen_trackless(destination)
    document = read_docx_package(destination).xml_root("word/document.xml")
    spacing = document.find(".//w:tbl//w:p/w:pPr/w:spacing", NS)
    assert spacing is not None
    assert spacing.get(_q(W_NS, "before")) == "0"
    assert spacing.get(_q(W_NS, "after")) == "0"


def test_style_property_restore_keeps_word_ids_and_restores_trusted_formatting() -> None:
    reference = etree.fromstring(
        (
            f'<w:styles xmlns:w="{W_NS}">'
            "<w:docDefaults><w:rPrDefault><w:rPr>"
            '<w:rFonts w:ascii="Times New Roman" w:eastAsia="SimSun"/>'
            "</w:rPr></w:rPrDefault></w:docDefaults>"
            '<w:style w:type="paragraph" w:styleId="Normal">'
            '<w:name w:val="Normal"/><w:pPr><w:jc w:val="both"/></w:pPr>'
            "</w:style></w:styles>"
        ).encode()
    )
    current = etree.fromstring(
        (
            f'<w:styles xmlns:w="{W_NS}">'
            "<w:docDefaults><w:rPrDefault><w:rPr>"
            '<w:rFonts w:ascii="Times New Roman" w:eastAsia="wrong-font"/>'
            "</w:rPr></w:rPrDefault></w:docDefaults>"
            '<w:style w:type="paragraph" w:styleId="a">'
            '<w:name w:val="Normal"/><w:basedOn w:val="a"/>'
            "</w:style></w:styles>"
        ).encode()
    )

    restored = etree.fromstring(
        word_fields_module._restore_review_style_properties(
            cast(bytes, etree.tostring(current)),
            cast(bytes, etree.tostring(reference)),
        )
    )

    style = restored.find("w:style", NS)
    assert style is not None
    assert style.get(_q(W_NS, "styleId")) == "a"
    assert style.find("w:basedOn", NS).get(_q(W_NS, "val")) == "a"
    assert style.find("w:pPr/w:jc", NS).get(_q(W_NS, "val")) == "both"
    assert (
        restored.find("w:docDefaults/w:rPrDefault/w:rPr/w:rFonts", NS).get(_q(W_NS, "eastAsia"))
        == "SimSun"
    )


@pytest.mark.parametrize(
    "instruction",
    (
        r"REF missing_bookmark \r",
        r"REF missing_bookmark \h \r",
        r"REF missing_bookmark \r \h",
    ),
    ids=("ref-r", "ref-h-r", "ref-r-h"),
)
def test_supported_missing_reference_switches_are_counted_as_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    instruction: str,
) -> None:
    source = _pending_docx(tmp_path, field_instruction=instruction)
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()
    observed = _install_success_runner(monkeypatch)

    report = finalize_word_fields(source, destination)

    assert source.read_bytes() == source_before
    assert (report.field_count, report.updated_fields, report.unresolved_fields) == (1, 0, 1)
    arguments = cast("tuple[str, ...]", observed["arguments"])
    assert _argument(arguments, "-ExpectedUnresolvedFields") == "1"
    _assert_frozen_trackless(destination)


@pytest.mark.parametrize(
    "instruction",
    (
        r"PAGEREF missing_bookmark \r",
        r"REF missing_bookmark \r \r",
        r"REF missing_bookmark \h \h",
    ),
    ids=("pageref-r", "duplicate-r", "duplicate-h"),
)
def test_unsafe_reference_switches_are_rejected_before_word_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    instruction: str,
) -> None:
    source = _pending_docx(tmp_path, field_instruction=instruction)
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()

    def forbidden_word(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unsafe reference fields must be rejected before Word")

    monkeypatch.setattr(word_fields_module, "_invoke_word", forbidden_word)
    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is ErrorCode.BACKEND_CAPABILITY_MISSING
    assert source.read_bytes() == source_before
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended paths are Windows-only")
def test_extended_paths_are_stripped_only_at_the_word_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_docx(
        tmp_path,
        field_instruction=r"SEQ Figure \* ARABIC",
    )
    destination = tmp_path / "review.docx"
    extended_destination = Path("\\\\?\\" + os.fspath(destination.resolve()))
    observed = _install_success_runner(monkeypatch)

    report = finalize_word_fields(source, extended_destination)

    assert report.field_count == 1
    assert destination.is_file()
    arguments = cast("tuple[str, ...]", observed["arguments"])
    for flag in (
        "-SourceDocx",
        "-WorkingDocx",
        "-DestinationDocx",
        "-ReportPath",
        "-PidStatePath",
    ):
        value = _argument(arguments, flag)
        assert Path(value).is_absolute()
        assert not value.startswith("\\\\?\\")
    observed_cwd = cast("Path", observed["cwd"])
    assert observed_cwd.is_absolute()
    assert not os.fspath(observed_cwd).startswith("\\\\?\\")


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        (
            CommandResult(9, "", "", False, False, 4, "sha256:" + "1" * 64),
            ErrorCode.BACKEND_FAILED,
        ),
        (
            CommandResult(-1, "", "", True, False, 60_000, "sha256:" + "2" * 64),
            ErrorCode.BACKEND_FAILED,
        ),
        (
            CommandResult(0, "", "", False, False, 5, "sha256:" + "3" * 64),
            ErrorCode.BACKEND_FAILED,
        ),
    ],
    ids=("nonzero", "timeout", "missing-artifacts"),
)
def test_runner_failures_do_not_publish_or_leave_owned_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: CommandResult,
    expected_code: ErrorCode,
) -> None:
    source = _pending_docx(tmp_path, field_instruction=r"SEQ Figure \* ARABIC")
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()
    _install_result_runner(monkeypatch, result)

    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is expected_code
    assert source.read_bytes() == source_before
    assert not destination.exists()
    assert not list(tmp_path.glob(".review.docx.*"))


def test_unsupported_field_is_rejected_before_word_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_docx(tmp_path, field_instruction='DDEAUTO "calc.exe"')
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()

    def forbidden_word(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unsupported fields must be rejected before Word")

    monkeypatch.setattr(word_fields_module, "_invoke_word", forbidden_word)
    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is ErrorCode.BACKEND_CAPABILITY_MISSING
    assert source.read_bytes() == source_before
    assert not destination.exists()


def test_existing_destination_and_source_alias_are_never_overwritten(tmp_path: Path) -> None:
    source = _pending_docx(tmp_path, field_instruction=r"SEQ Figure \* ARABIC")
    source_before = source.read_bytes()
    destination = tmp_path / "review.docx"
    destination.write_bytes(b"keep-existing")

    with pytest.raises(ContractError) as occupied:
        finalize_word_fields(source, destination)
    with pytest.raises(ContractError) as alias:
        finalize_word_fields(source, source)

    assert occupied.value.code is ErrorCode.BACKEND_FAILED
    assert alias.value.code is ErrorCode.BACKEND_FAILED
    assert destination.read_bytes() == b"keep-existing"
    assert source.read_bytes() == source_before


def test_word_structure_loss_fails_closed_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_docx(
        tmp_path,
        field_instruction=r"SEQ Figure \* ARABIC",
        include_math=True,
    )
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()
    _install_success_runner(monkeypatch, mutate_output=_drop_math)

    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert source.read_bytes() == source_before
    assert not destination.exists()
    assert not list(tmp_path.glob(".review.docx.*"))


def test_malformed_word_report_is_mapped_to_backend_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_docx(tmp_path, field_instruction=r"SEQ Figure \* ARABIC")
    destination = tmp_path / "review.docx"
    _install_success_runner(monkeypatch, report_raw=b"{not-json")

    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is ErrorCode.BACKEND_FAILED
    assert not destination.exists()
    assert not list(tmp_path.glob(".review.docx.*"))


def test_boolean_word_report_counter_is_rejected_as_backend_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_docx(tmp_path, field_instruction=r"SEQ Figure \* ARABIC")
    destination = tmp_path / "review.docx"

    def replace_counter_with_boolean(payload: dict[str, object]) -> None:
        payload["field_count"] = True

    _install_success_runner(
        monkeypatch,
        report_mutator=replace_counter_with_boolean,
    )

    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is ErrorCode.BACKEND_FAILED
    assert not destination.exists()
    assert not list(tmp_path.glob(".review.docx.*"))


def test_hardlink_publish_race_preserves_concurrent_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_docx(tmp_path)
    destination = tmp_path / "review.docx"
    concurrent_bytes = b"concurrent-writer-owned-file"
    real_link = os.link

    def racing_link(
        source_path: Path,
        destination_path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del follow_symlinks
        if destination_path == destination:
            destination_path.write_bytes(concurrent_bytes)
            raise FileExistsError("synthetic hardlink publication race")
        real_link(source_path, destination_path)

    monkeypatch.setattr(os, "link", racing_link)

    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is ErrorCode.BACKEND_FAILED
    assert destination.exists()
    assert destination.read_bytes() == concurrent_bytes
    assert not list(tmp_path.glob(".review.docx.*"))


def test_final_validation_failure_after_seal_removes_owned_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_docx(tmp_path, field_instruction=r"SEQ Figure \* ARABIC")
    destination = tmp_path / "review.docx"
    _install_success_runner(monkeypatch)
    compare_structure = word_fields_module._compare_structure
    compare_calls = 0

    def fail_second_comparison(before: DocxInspection, after: DocxInspection) -> None:
        nonlocal compare_calls
        compare_calls += 1
        if compare_calls == 2:
            raise ContractError(
                ErrorCode.EXPORT_SILENT_LOSS,
                "synthetic final validation failure",
            )
        compare_structure(before, after)

    monkeypatch.setattr(
        word_fields_module,
        "_compare_structure",
        fail_second_comparison,
    )

    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert compare_calls == 2
    assert not destination.exists()
    assert not list(tmp_path.glob(".review.docx.*"))


def test_main_document_textbox_field_is_rejected_before_word_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pending_textbox_docx(tmp_path, r"SEQ Figure \* ARABIC")
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()

    def forbidden_word(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("textbox fields must be rejected before Word")

    monkeypatch.setattr(word_fields_module, "_invoke_word", forbidden_word)
    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is ErrorCode.BACKEND_CAPABILITY_MISSING
    assert source.read_bytes() == source_before
    assert not destination.exists()


@pytest.mark.parametrize(
    ("mutate_output", "include_image"),
    [
        (_change_body_text, False),
        (_change_media_bytes, True),
        (_change_relationship_target, True),
    ],
    ids=("ordinary-body-text", "media-bytes", "relationship-target"),
)
def test_word_output_unrelated_mutation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate_output: Callable[[Path], None],
    include_image: bool,
) -> None:
    if include_image:
        source = _pending_image_docx(tmp_path, r"SEQ Figure \* ARABIC")
    else:
        source = _pending_docx(
            tmp_path,
            field_instruction=r"SEQ Figure \* ARABIC",
        )
    destination = tmp_path / "review.docx"
    source_before = source.read_bytes()
    _install_success_runner(monkeypatch, mutate_output=mutate_output)

    with pytest.raises(ContractError) as raised:
        finalize_word_fields(source, destination)

    assert raised.value.code is ErrorCode.EXPORT_SILENT_LOSS
    assert source.read_bytes() == source_before
    assert not destination.exists()
    assert not list(tmp_path.glob(".review.docx.*"))
