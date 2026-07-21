"""Security-focused branch tests for Word ownership cleanup and review layout."""

from __future__ import annotations

import ctypes
import json
import zipfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from lxml import etree  # type: ignore[import-untyped]

import latex_word_review.word_fields as word_fields_module
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.review_layout import validate_review_layout
from tests._docx_factory import write_docx

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS = {"w": W_NS, "wp": WP_NS, "m": M_NS}
STARTED_FILETIME = 0x01DC_AAAA_BBBB_CCCC


def _q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _xml_bytes(root: etree._Element) -> bytes:
    return cast(
        bytes,
        etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True),
    )


class _FakeFunction:
    """Callable accepting the ctypes metadata assigned by the production code."""

    def __init__(self, callback: Callable[..., object]) -> None:
        self.callback = callback
        self.argtypes: object | None = None
        self.restype: object | None = None

    def __call__(self, *args: object) -> object:
        return self.callback(*args)


class _FakeKernel32:
    """In-memory kernel32 double; no method can reach an operating-system API."""

    def __init__(
        self,
        *,
        open_handle: int = 701,
        times_ok: bool = True,
        started_filetime: int = STARTED_FILETIME,
        query_ok: bool = True,
        image_name: str = "WINWORD.EXE",
        terminate_ok: bool = True,
    ) -> None:
        self.open_handle = open_handle
        self.times_ok = times_ok
        self.started_filetime = started_filetime
        self.query_ok = query_ok
        self.image_name = image_name
        self.terminate_ok = terminate_ok
        self.calls: list[str] = []
        self.OpenProcess = _FakeFunction(self._open_process)
        self.GetProcessTimes = _FakeFunction(self._get_process_times)
        self.QueryFullProcessImageNameW = _FakeFunction(self._query_image)
        self.TerminateProcess = _FakeFunction(self._terminate)
        self.WaitForSingleObject = _FakeFunction(self._wait)
        self.CloseHandle = _FakeFunction(self._close)

    def _open_process(self, _access: object, _inherit: object, _pid: object) -> int:
        self.calls.append("OpenProcess")
        return self.open_handle

    def _get_process_times(
        self,
        _handle: object,
        creation: Any,
        _exit_time: object,
        _kernel_time: object,
        _user_time: object,
    ) -> int:
        self.calls.append("GetProcessTimes")
        if self.times_ok:
            creation._obj.low = self.started_filetime & 0xFFFFFFFF
            creation._obj.high = self.started_filetime >> 32
        return int(self.times_ok)

    def _query_image(
        self,
        _handle: object,
        _flags: object,
        image_buffer: Any,
        image_size: Any,
    ) -> int:
        self.calls.append("QueryFullProcessImageNameW")
        if self.query_ok:
            image_buffer.value = self.image_name
            image_size._obj.value = len(self.image_name)
        return int(self.query_ok)

    def _terminate(self, _handle: object, _exit_code: object) -> int:
        self.calls.append("TerminateProcess")
        return int(self.terminate_ok)

    def _wait(self, _handle: object, _timeout: object) -> int:
        self.calls.append("WaitForSingleObject")
        return 0

    def _close(self, _handle: object) -> int:
        self.calls.append("CloseHandle")
        return 1


def _write_pid_state(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _valid_pid_state() -> dict[str, object]:
    return {
        "schema_version": "word-field-process/v1",
        "owned": True,
        "pid": 4242,
        "started_filetime_utc": STARTED_FILETIME,
    }


def _install_fake_kernel(
    monkeypatch: pytest.MonkeyPatch,
    kernel: _FakeKernel32,
) -> None:
    monkeypatch.setattr(word_fields_module, "os", SimpleNamespace(name="nt"))

    def load_kernel(name: str, *, use_last_error: bool) -> _FakeKernel32:
        assert name == "kernel32"
        assert use_last_error is True
        return kernel

    monkeypatch.setattr(ctypes, "WinDLL", load_kernel, raising=False)


def test_cleanup_owned_word_process_rejects_state_before_loading_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_state = tmp_path / "word-process.json"
    kernel_loads: list[str] = []

    def forbidden_loader(name: str, *, use_last_error: bool) -> object:
        del use_last_error
        kernel_loads.append(name)
        raise AssertionError("untrusted state reached kernel32")

    monkeypatch.setattr(ctypes, "WinDLL", forbidden_loader, raising=False)
    monkeypatch.setattr(word_fields_module, "os", SimpleNamespace(name="posix"))
    _write_pid_state(pid_state, _valid_pid_state())
    word_fields_module._cleanup_owned_word_process(pid_state, cwd=tmp_path)

    monkeypatch.setattr(word_fields_module, "os", SimpleNamespace(name="nt"))
    invalid_states: tuple[object | None, ...] = (
        None,
        {"schema_version": "word-field-process/v1"},
        {**_valid_pid_state(), "schema_version": "word-field-process/v2"},
        {**_valid_pid_state(), "owned": False},
        {**_valid_pid_state(), "pid": True},
        {**_valid_pid_state(), "pid": 0},
        {**_valid_pid_state(), "started_filetime_utc": "not-an-integer"},
    )
    for state in invalid_states:
        pid_state.unlink(missing_ok=True)
        if state is not None:
            _write_pid_state(pid_state, state)
        word_fields_module._cleanup_owned_word_process(pid_state, cwd=tmp_path)

    pid_state.write_bytes(b"not-json")
    word_fields_module._cleanup_owned_word_process(pid_state, cwd=tmp_path)
    assert kernel_loads == []


@pytest.mark.parametrize(
    ("kernel", "expected_calls"),
    (
        (_FakeKernel32(open_handle=0), ["OpenProcess"]),
        (
            _FakeKernel32(times_ok=False),
            ["OpenProcess", "GetProcessTimes", "CloseHandle"],
        ),
        (
            _FakeKernel32(started_filetime=STARTED_FILETIME + 1),
            ["OpenProcess", "GetProcessTimes", "CloseHandle"],
        ),
        (
            _FakeKernel32(query_ok=False),
            [
                "OpenProcess",
                "GetProcessTimes",
                "QueryFullProcessImageNameW",
                "CloseHandle",
            ],
        ),
        (
            _FakeKernel32(image_name="notepad.exe"),
            [
                "OpenProcess",
                "GetProcessTimes",
                "QueryFullProcessImageNameW",
                "CloseHandle",
            ],
        ),
        (
            _FakeKernel32(terminate_ok=False),
            [
                "OpenProcess",
                "GetProcessTimes",
                "QueryFullProcessImageNameW",
                "TerminateProcess",
                "CloseHandle",
            ],
        ),
    ),
    ids=(
        "cannot-open",
        "cannot-read-times",
        "pid-reused",
        "cannot-read-image",
        "wrong-executable",
        "terminate-refused",
    ),
)
def test_cleanup_owned_word_process_fails_closed_on_identity_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kernel: _FakeKernel32,
    expected_calls: list[str],
) -> None:
    pid_state = tmp_path / "word-process.json"
    _write_pid_state(pid_state, _valid_pid_state())
    _install_fake_kernel(monkeypatch, kernel)

    word_fields_module._cleanup_owned_word_process(pid_state, cwd=tmp_path)

    assert kernel.calls == expected_calls
    assert "WaitForSingleObject" not in kernel.calls


def test_cleanup_owned_word_process_terminates_only_exact_owned_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_state = tmp_path / "word-process.json"
    _write_pid_state(pid_state, _valid_pid_state())
    kernel = _FakeKernel32()
    _install_fake_kernel(monkeypatch, kernel)

    word_fields_module._cleanup_owned_word_process(pid_state, cwd=tmp_path)

    assert kernel.calls == [
        "OpenProcess",
        "GetProcessTimes",
        "QueryFullProcessImageNameW",
        "TerminateProcess",
        "WaitForSingleObject",
        "CloseHandle",
    ]


def _paragraph(text: str) -> etree._Element:
    paragraph = etree.Element(_q(W_NS, "p"))
    properties = etree.SubElement(paragraph, _q(W_NS, "pPr"))
    spacing = etree.SubElement(properties, _q(W_NS, "spacing"))
    spacing.set(_q(W_NS, "before"), "0")
    spacing.set(_q(W_NS, "after"), "0")
    run = etree.SubElement(paragraph, _q(W_NS, "r"))
    etree.SubElement(run, _q(W_NS, "t")).text = text
    return paragraph


def _cell(text: str) -> etree._Element:
    cell = etree.Element(_q(W_NS, "tc"))
    properties = etree.SubElement(cell, _q(W_NS, "tcPr"))
    width = etree.SubElement(properties, _q(W_NS, "tcW"))
    width.set(_q(W_NS, "type"), "dxa")
    width.set(_q(W_NS, "w"), "4680")
    cell.append(_paragraph(text))
    return cell


def _valid_review_document(*, frozen: bool) -> bytes:
    root = etree.Element(
        _q(W_NS, "document"),
        nsmap={"w": W_NS, "wp": WP_NS, "m": M_NS},
    )
    body = etree.SubElement(root, _q(W_NS, "body"))
    table = etree.SubElement(body, _q(W_NS, "tbl"))
    table_properties = etree.SubElement(table, _q(W_NS, "tblPr"))
    table_width = etree.SubElement(table_properties, _q(W_NS, "tblW"))
    table_width.set(_q(W_NS, "type"), "dxa")
    table_width.set(_q(W_NS, "w"), "9360")
    table_layout = etree.SubElement(table_properties, _q(W_NS, "tblLayout"))
    table_layout.set(_q(W_NS, "type"), "fixed")
    grid = etree.SubElement(table, _q(W_NS, "tblGrid"))
    for _ in range(2):
        etree.SubElement(grid, _q(W_NS, "gridCol")).set(_q(W_NS, "w"), "4680")
    for row_index, labels in enumerate((("Heading A", "Heading B"), ("A", "B"))):
        row = etree.SubElement(table, _q(W_NS, "tr"))
        row_properties = etree.SubElement(row, _q(W_NS, "trPr"))
        etree.SubElement(row_properties, _q(W_NS, "cantSplit"))
        if row_index == 0:
            etree.SubElement(row_properties, _q(W_NS, "tblHeader"))
        for label in labels:
            row.append(_cell(label))

    image_paragraph = etree.SubElement(body, _q(W_NS, "p"))
    image_run = etree.SubElement(image_paragraph, _q(W_NS, "r"))
    drawing = etree.SubElement(image_run, _q(W_NS, "drawing"))
    inline = etree.SubElement(drawing, _q(WP_NS, "inline"))
    extent = etree.SubElement(inline, _q(WP_NS, "extent"))
    extent.set("cx", "1000000")
    extent.set("cy", "500000")

    equation = etree.SubElement(body, _q(W_NS, "p"))
    equation_properties = etree.SubElement(equation, _q(W_NS, "pPr"))
    tabs = etree.SubElement(equation_properties, _q(W_NS, "tabs"))
    for value, position in (("center", "4680"), ("right", "9360")):
        tab = etree.SubElement(tabs, _q(W_NS, "tab"))
        tab.set(_q(W_NS, "val"), value)
        tab.set(_q(W_NS, "pos"), position)
    begin_run = etree.SubElement(equation, _q(W_NS, "r"))
    begin = etree.SubElement(begin_run, _q(W_NS, "fldChar"))
    begin.set(_q(W_NS, "fldCharType"), "begin")
    begin.set(_q(W_NS, "dirty"), "false" if frozen else "true")
    instruction_run = etree.SubElement(equation, _q(W_NS, "r"))
    etree.SubElement(instruction_run, _q(W_NS, "instrText")).text = "SEQ Equation \\* ARABIC"
    math = etree.SubElement(equation, _q(M_NS, "oMath"))
    etree.SubElement(math, _q(M_NS, "r"))

    simple_paragraph = etree.SubElement(body, _q(W_NS, "p"))
    simple = etree.SubElement(simple_paragraph, _q(W_NS, "fldSimple"))
    simple.set(_q(W_NS, "instr"), "REF bookmark_1 \\h")
    simple.set(_q(W_NS, "dirty"), "false" if frozen else "true")
    simple.append(_paragraph("1"))

    section = etree.SubElement(body, _q(W_NS, "sectPr"))
    page_size = etree.SubElement(section, _q(W_NS, "pgSz"))
    page_size.set(_q(W_NS, "w"), "12240")
    margins = etree.SubElement(section, _q(W_NS, "pgMar"))
    margins.set(_q(W_NS, "left"), "1440")
    margins.set(_q(W_NS, "right"), "1440")
    columns = etree.SubElement(section, _q(W_NS, "cols"))
    columns.set(_q(W_NS, "num"), "1")
    return _xml_bytes(root)


def _settings(*, frozen: bool) -> bytes:
    root = etree.Element(_q(W_NS, "settings"), nsmap={"w": W_NS})
    update = etree.SubElement(root, _q(W_NS, "updateFields"))
    update.set(_q(W_NS, "val"), "false" if frozen else "true")
    return _xml_bytes(root)


def _write_valid_review(path: Path, *, frozen: bool) -> Path:
    return write_docx(
        path,
        document_xml=_valid_review_document(frozen=frozen),
        extra_parts={"word/settings.xml": _settings(frozen=frozen)},
    )


def _one(root: etree._Element, xpath: str) -> etree._Element:
    matches = cast(list[etree._Element], root.xpath(xpath, namespaces=NS))
    assert len(matches) == 1
    return matches[0]


def _mutate_document(root: etree._Element, case: str) -> None:
    if case == "missing-body":
        body = _one(root, "/w:document/w:body")
        root.remove(body)
    elif case == "extra-section":
        _one(root, "/w:document/w:body").append(etree.Element(_q(W_NS, "sectPr")))
    elif case == "nested-section":
        section = _one(root, "/w:document/w:body/w:sectPr")
        parent = section.getparent()
        assert parent is not None
        parent.remove(section)
        _one(root, "(//w:p/w:pPr)[1]").append(section)
    elif case == "two-columns":
        _one(root, "//w:sectPr/w:cols").set(_q(W_NS, "num"), "2")
    elif case == "table-width":
        _one(root, "//w:tbl/w:tblPr/w:tblW").set(_q(W_NS, "w"), "9359")
    elif case == "table-layout":
        layout = _one(root, "//w:tbl/w:tblPr/w:tblLayout")
        parent = layout.getparent()
        assert parent is not None
        parent.remove(layout)
    elif case == "table-grid":
        _one(root, "//w:tblGrid/w:gridCol[1]").set(_q(W_NS, "w"), "0")
    elif case == "row-split":
        marker = _one(root, "//w:tbl/w:tr[1]/w:trPr/w:cantSplit")
        parent = marker.getparent()
        assert parent is not None
        parent.remove(marker)
    elif case == "cell-width":
        _one(root, "//w:tbl/w:tr[1]/w:tc[1]/w:tcPr/w:tcW").set(_q(W_NS, "type"), "auto")
    elif case == "cell-spacing":
        _one(root, "//w:tbl/w:tr[1]/w:tc[1]//w:pPr/w:spacing").set(_q(W_NS, "before"), "12")
    elif case == "header-repeat":
        marker = _one(root, "//w:tbl/w:tr[1]/w:trPr/w:tblHeader")
        parent = marker.getparent()
        assert parent is not None
        parent.remove(marker)
    elif case == "placeholder":
        _one(root, "//w:tbl/w:tr[1]/w:tc[1]//w:t").text = "[sub-figure omitted] (a)"
    elif case == "image-width":
        _one(root, "//wp:inline/wp:extent").set("cx", "999999999")
    elif case == "equation-tabs":
        _one(root, "//w:p[.//w:instrText]/w:pPr/w:tabs/w:tab[1]").set(_q(W_NS, "pos"), "1")
    elif case == "complex-dirty":
        _one(root, "//w:fldChar[@w:fldCharType='begin']").attrib.pop(_q(W_NS, "dirty"))
    elif case == "simple-dirty":
        _one(root, "//w:fldSimple").attrib.pop(_q(W_NS, "dirty"))
    elif case == "complex-refresh":
        _one(root, "//w:fldChar[@w:fldCharType='begin']").set(_q(W_NS, "dirty"), "true")
    elif case == "simple-refresh":
        _one(root, "//w:fldSimple").set(_q(W_NS, "dirty"), "true")
    else:
        raise AssertionError(f"unknown document mutation: {case}")


def _mutate_settings(root: etree._Element, case: str) -> None:
    update = _one(root, "/w:settings/w:updateFields")
    if case == "duplicate":
        root.append(deepcopy(update))
    elif case == "disable":
        update.set(_q(W_NS, "val"), "false")
    elif case == "enable":
        update.set(_q(W_NS, "val"), "true")
    else:
        raise AssertionError(f"unknown settings mutation: {case}")


def _rewrite_package(
    source: Path,
    destination: Path,
    *,
    part: str,
    mutate: Callable[[etree._Element], None] | None = None,
    drop: bool = False,
) -> Path:
    with zipfile.ZipFile(source) as package:
        members = [(info, package.read(info.filename)) for info in package.infolist()]
    with zipfile.ZipFile(destination, "w") as output:
        for info, data in members:
            if info.filename == part and drop:
                continue
            if info.filename == part and mutate is not None:
                root = etree.fromstring(data)
                mutate(root)
                data = _xml_bytes(root)
            output.writestr(info, data)
    return destination


@pytest.fixture
def pending_review(tmp_path: Path) -> Path:
    path = _write_valid_review(tmp_path / "pending.docx", frozen=False)
    validate_review_layout(path, field_refresh="pending")
    return path


@pytest.fixture
def frozen_review(tmp_path: Path) -> Path:
    path = _write_valid_review(tmp_path / "frozen.docx", frozen=True)
    validate_review_layout(path, field_refresh="frozen")
    return path


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("missing-body", "missing document body"),
        ("extra-section", "one final body section"),
        ("nested-section", "one final body section"),
        ("two-columns", "not single-column"),
        ("table-width", "table geometry"),
        ("table-layout", "table geometry"),
        ("table-grid", "table geometry"),
        ("row-split", "table row can split"),
        ("cell-width", "table cell width"),
        ("cell-spacing", "paragraph spacing"),
        ("header-repeat", "header does not repeat"),
        ("placeholder", "placeholder remains visible"),
        ("image-width", "image geometry"),
        ("equation-tabs", "equation tabs"),
        ("complex-dirty", "complex field is not marked dirty"),
        ("simple-dirty", "simple field is not marked dirty"),
    ),
)
def test_validate_review_layout_rejects_document_invariant_breaks(
    tmp_path: Path,
    pending_review: Path,
    case: str,
    message: str,
) -> None:
    broken = _rewrite_package(
        pending_review,
        tmp_path / f"broken-{case}.docx",
        part="word/document.xml",
        mutate=lambda root: _mutate_document(root, case),
    )

    with pytest.raises(ContractError) as raised:
        validate_review_layout(broken, field_refresh="pending")

    assert raised.value.code is ErrorCode.INTERNAL_INVARIANT
    assert message in str(raised.value)


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("complex-refresh", "complex field still requests"),
        ("simple-refresh", "simple field still requests"),
    ),
)
def test_validate_review_layout_rejects_dirty_fields_after_freeze(
    tmp_path: Path,
    frozen_review: Path,
    case: str,
    message: str,
) -> None:
    broken = _rewrite_package(
        frozen_review,
        tmp_path / f"broken-{case}.docx",
        part="word/document.xml",
        mutate=lambda root: _mutate_document(root, case),
    )

    with pytest.raises(ContractError) as raised:
        validate_review_layout(broken, field_refresh="frozen")

    assert raised.value.code is ErrorCode.INTERNAL_INVARIANT
    assert message in str(raised.value)


@pytest.mark.parametrize(
    ("refresh", "case", "message"),
    (
        ("pending", "missing", "settings part is missing"),
        ("pending", "duplicate", "control is not unique"),
        ("pending", "disable", "refresh is not enabled"),
        ("frozen", "enable", "refresh is still enabled"),
    ),
)
def test_validate_review_layout_rejects_unsafe_settings(
    tmp_path: Path,
    refresh: Literal["pending", "frozen"],
    case: str,
    message: str,
) -> None:
    source = _write_valid_review(tmp_path / f"valid-{refresh}.docx", frozen=refresh == "frozen")
    broken = _rewrite_package(
        source,
        tmp_path / f"broken-settings-{case}.docx",
        part="word/settings.xml",
        drop=case == "missing",
        mutate=None if case == "missing" else lambda root: _mutate_settings(root, case),
    )

    with pytest.raises(ContractError) as raised:
        validate_review_layout(broken, field_refresh=refresh)

    assert raised.value.code is ErrorCode.INTERNAL_INVARIANT
    assert message in str(raised.value)


def _story_with_fields() -> etree._Element:
    root = etree.Element(_q(W_NS, "document"), nsmap={"w": W_NS, "m": M_NS})
    body = etree.SubElement(root, _q(W_NS, "body"))
    paragraph = etree.SubElement(body, _q(W_NS, "p"))
    run = etree.SubElement(paragraph, _q(W_NS, "r"))
    etree.SubElement(run, _q(W_NS, "t")).text = "Reviewer text"
    etree.SubElement(run, _q(W_NS, "tab"))
    symbol = etree.SubElement(run, _q(W_NS, "sym"))
    symbol.set(_q(W_NS, "font"), "Symbol")
    symbol.set(_q(W_NS, "char"), "F061")
    begin = etree.SubElement(run, _q(W_NS, "fldChar"))
    begin.set(_q(W_NS, "fldCharType"), "begin")
    etree.SubElement(run, _q(W_NS, "instrText")).text = "SEQ Figure \\* ARABIC"
    separate = etree.SubElement(run, _q(W_NS, "fldChar"))
    separate.set(_q(W_NS, "fldCharType"), "separate")
    etree.SubElement(run, _q(W_NS, "t")).text = "1"
    end = etree.SubElement(run, _q(W_NS, "fldChar"))
    end.set(_q(W_NS, "fldCharType"), "end")
    simple = etree.SubElement(paragraph, _q(W_NS, "fldSimple"))
    simple.set(_q(W_NS, "instr"), "REF bookmark_1")
    etree.SubElement(simple, _q(W_NS, "t")).text = "cached"
    math = etree.SubElement(paragraph, _q(M_NS, "oMath"))
    etree.SubElement(math, _q(M_NS, "t")).text = "x"
    return root


def test_protected_story_digest_ignores_only_mutable_field_results_and_math() -> None:
    original = _story_with_fields()
    baseline = word_fields_module._protected_story_digest(original)

    mutable_copy = deepcopy(original)
    texts = cast(list[etree._Element], mutable_copy.xpath("//w:t", namespaces=NS))
    texts[-1].text = "updated cached result"
    _one(mutable_copy, "//w:fldSimple/w:t").text = "updated simple result"
    _one(mutable_copy, "//m:oMath/m:t").text = "y"
    assert word_fields_module._protected_story_digest(mutable_copy) == baseline

    reviewer_edit = deepcopy(original)
    _one(reviewer_edit, "(//w:t)[1]").text = "Reviewer text changed"
    assert word_fields_module._protected_story_digest(reviewer_edit) != baseline

    symbol_edit = deepcopy(original)
    _one(symbol_edit, "//w:sym").set(_q(W_NS, "char"), "F062")
    assert word_fields_module._protected_story_digest(symbol_edit) != baseline


@pytest.mark.parametrize(
    "instruction",
    (
        "REF bookmark \\h \\h",
        "PAGEREF bookmark \\r",
        "REF bookmark \\x",
        "HYPERLINK https://example.invalid",
    ),
)
def test_parse_field_code_rejects_unsafe_or_ambiguous_instructions(instruction: str) -> None:
    with pytest.raises(ContractError) as raised:
        word_fields_module._parse_field_code(instruction)

    assert raised.value.code is ErrorCode.BACKEND_CAPABILITY_MISSING
    assert raised.value.violation.details == {
        "stage": "field_finalize",
        "failure_kind": "unsupported_field",
    }


def test_parse_field_code_normalizes_supported_fields() -> None:
    sequence = word_fields_module._parse_field_code("  seq   Equation  \\*  arabic ")
    reference = word_fields_module._parse_field_code("REF bookmark_1 \\r \\h")
    page_reference = word_fields_module._parse_field_code("pageref bookmark_1 \\h")

    assert (sequence.kind, sequence.target, sequence.normalized_code) == (
        "SEQ",
        "Equation",
        "seq Equation \\* arabic",
    )
    assert (reference.kind, reference.target) == ("REF", "bookmark_1")
    assert (page_reference.kind, page_reference.target) == ("PAGEREF", "bookmark_1")


def _default_note_root(local: str) -> etree._Element:
    root = etree.Element(_q(W_NS, f"{local}s"), nsmap={"w": W_NS, "m": M_NS})
    note = etree.SubElement(root, _q(W_NS, local))
    note.set(_q(W_NS, "id"), "-1")
    etree.SubElement(note, _q(W_NS, "p"))
    return root


def test_default_note_story_detection_is_conservative() -> None:
    footnotes = _default_note_root("footnote")
    endnotes = _default_note_root("endnote")
    assert word_fields_module._is_default_note_story("word/footnotes.xml", footnotes)
    assert word_fields_module._is_default_note_story("word/endnotes.xml", endnotes)
    assert not word_fields_module._is_default_note_story("word/comments.xml", footnotes)

    for mutation in ("positive-id", "bad-id", "text", "math", "protected-table"):
        changed = deepcopy(footnotes)
        note = _one(changed, "//w:footnote")
        if mutation == "positive-id":
            note.set(_q(W_NS, "id"), "1")
        elif mutation == "bad-id":
            note.set(_q(W_NS, "id"), "not-an-integer")
        elif mutation == "text":
            etree.SubElement(note, _q(W_NS, "t")).text = "content"
        elif mutation == "math":
            etree.SubElement(note, _q(M_NS, "oMath"))
        else:
            etree.SubElement(note, _q(W_NS, "tbl"))
        assert not word_fields_module._is_default_note_story("word/footnotes.xml", changed)
