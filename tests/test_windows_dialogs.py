"""Tests for the Windows-native product file-selection boundary."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import latex_word_review.windows_dialogs as windows_dialogs
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.windows_dialogs import (
    choose_review_copy_destination,
    choose_review_file,
    show_browser_open_failure,
    show_startup_failure,
)


class _FakeNativeFunction:
    def __init__(self, result: int, callback: Callable[..., int] | None = None) -> None:
        self.result = result
        self.callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> int:
        if self.callback is not None:
            return self.callback(*args)
        return self.result


class _FakeCommonDialog:
    def __init__(
        self,
        *,
        selected: int,
        extended_error: int,
        selected_path: Path | None = None,
    ) -> None:
        self.initial_dir: str | None = None
        self.flags: int | None = None

        def get_open_file_name(dialog_pointer: Any) -> int:
            dialog = ctypes.cast(
                dialog_pointer,
                ctypes.POINTER(windows_dialogs._OpenFileNameW),
            ).contents
            self.initial_dir = dialog.lpstrInitialDir
            self.flags = int(dialog.Flags)
            if selected and selected_path is not None:
                pointer_field = (
                    ctypes.addressof(dialog) + windows_dialogs._OpenFileNameW.lpstrFile.offset
                )
                destination = ctypes.c_void_p.from_address(pointer_field).value
                assert destination is not None
                value = ctypes.create_unicode_buffer(str(selected_path))
                ctypes.memmove(destination, value, ctypes.sizeof(value))
            return selected

        self.GetOpenFileNameW = _FakeNativeFunction(selected, get_open_file_name)
        self.CommDlgExtendedError = _FakeNativeFunction(extended_error)


class _FakeSaveCommonDialog:
    def __init__(
        self,
        *,
        selected: int,
        extended_error: int,
        selected_path: Path | None = None,
    ) -> None:
        self.initial_dir: str | None = None
        self.initial_file: str | None = None
        self.default_extension: str | None = None
        self.flags: int | None = None

        def get_save_file_name(dialog_pointer: Any) -> int:
            dialog = ctypes.cast(
                dialog_pointer,
                ctypes.POINTER(windows_dialogs._OpenFileNameW),
            ).contents
            self.initial_dir = dialog.lpstrInitialDir
            self.default_extension = dialog.lpstrDefExt
            self.flags = int(dialog.Flags)
            self.initial_file = ctypes.wstring_at(dialog.lpstrFile)
            if selected and selected_path is not None:
                pointer_field = (
                    ctypes.addressof(dialog) + windows_dialogs._OpenFileNameW.lpstrFile.offset
                )
                destination = ctypes.c_void_p.from_address(pointer_field).value
                assert destination is not None
                value = ctypes.create_unicode_buffer(str(selected_path))
                ctypes.memmove(destination, value, ctypes.sizeof(value))
            return selected

        self.GetSaveFileNameW = _FakeNativeFunction(selected, get_save_file_name)
        self.CommDlgExtendedError = _FakeNativeFunction(extended_error)


@pytest.mark.skipif(os.name != "nt", reason="the product supports Windows only")
@pytest.mark.parametrize(
    ("selected", "extended_error", "expected_code"),
    [
        (0, 0, None),
        (0, 0xFFFF, ErrorCode.INTERNAL_INVARIANT),
        (1, 0, ErrorCode.INTERNAL_INVARIANT),
    ],
)
def test_native_dialog_distinguishes_cancel_error_and_empty_success(
    monkeypatch: pytest.MonkeyPatch,
    selected: int,
    extended_error: int,
    expected_code: ErrorCode | None,
) -> None:
    fake = _FakeCommonDialog(selected=selected, extended_error=extended_error)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake)

    if expected_code is None:
        assert windows_dialogs._native_pick_file("Choose", "All\0*.*\0\0", None) is None
        return

    with pytest.raises(ContractError) as captured:
        windows_dialogs._native_pick_file("Choose", "All\0*.*\0\0", None)

    assert captured.value.code is expected_code
    if extended_error:
        assert captured.value.as_dict()["details"] == {
            "provider": "GetOpenFileNameW",
            "stage": "show_dialog",
            "code": extended_error,
            "hex": "0xFFFF",
            "name": "CDERR_DIALOGFAILURE",
        }
    else:
        assert captured.value.as_dict()["details"] == {
            "provider": "GetOpenFileNameW",
            "stage": "read_result",
            "code": None,
            "hex": None,
            "name": "EMPTY_SELECTION_RESULT",
        }


@pytest.mark.skipif(os.name != "nt", reason="the product supports Windows only")
@pytest.mark.parametrize(
    ("extended_error", "expected_name"),
    [
        (0x0001, "CDERR_STRUCTSIZE"),
        (0x0002, "CDERR_INITIALIZATION"),
        (0x0003, "CDERR_NOTEMPLATE"),
        (0x0004, "CDERR_NOHINSTANCE"),
        (0x0005, "CDERR_LOADSTRFAILURE"),
        (0x0006, "CDERR_FINDRESFAILURE"),
        (0x0007, "CDERR_LOADRESFAILURE"),
        (0x0008, "CDERR_LOCKRESFAILURE"),
        (0x0009, "CDERR_MEMALLOCFAILURE"),
        (0x000A, "CDERR_MEMLOCKFAILURE"),
        (0x000B, "CDERR_NOHOOK"),
        (0x000C, "CDERR_REGISTERMSGFAIL"),
        (0x3001, "FNERR_SUBCLASSFAILURE"),
        (0x3002, "FNERR_INVALIDFILENAME"),
        (0x3003, "FNERR_BUFFERTOOSMALL"),
        (0xDEAD, "UNKNOWN_COMMON_DIALOG_ERROR"),
    ],
)
def test_native_dialog_reports_allowlisted_extended_error(
    monkeypatch: pytest.MonkeyPatch,
    extended_error: int,
    expected_name: str,
) -> None:
    fake = _FakeCommonDialog(selected=0, extended_error=extended_error)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake)

    with pytest.raises(ContractError) as captured:
        windows_dialogs._native_pick_file("Choose", "All\0*.*\0\0", None)

    details = captured.value.as_dict()["details"]
    assert details == {
        "provider": "GetOpenFileNameW",
        "stage": "show_dialog",
        "code": extended_error,
        "hex": f"0x{extended_error:04X}",
        "name": expected_name,
    }


@pytest.mark.skipif(os.name != "nt", reason="the product supports Windows only")
def test_native_dialog_returns_nonempty_selected_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    selected = (tmp_path / "main.tex").resolve()
    fake = _FakeCommonDialog(selected=1, extended_error=0, selected_path=selected)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake)

    result = windows_dialogs._native_pick_file("Choose", "LaTeX\0*.tex\0\0", tmp_path)

    assert result == selected
    assert fake.initial_dir == str(tmp_path)
    assert fake.flags is not None
    assert fake.flags & windows_dialogs._OFN_EXPLORER
    assert fake.flags & windows_dialogs._OFN_NODEREFERENCELINKS


@pytest.mark.skipif(os.name != "nt", reason="the product supports Windows only")
def test_native_save_dialog_uses_new_docx_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    selected = (tmp_path / "review-for-advisor.docx").resolve()
    fake = _FakeSaveCommonDialog(
        selected=1,
        extended_error=0,
        selected_path=selected,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake)

    result = windows_dialogs._native_save_file(
        "Save",
        "Word\0*.docx\0\0",
        tmp_path,
        "review.docx",
    )

    assert result == selected
    assert fake.initial_dir == str(tmp_path)
    assert fake.initial_file == "review.docx"
    assert fake.default_extension == "docx"
    assert fake.flags is not None
    assert not fake.flags & windows_dialogs._OFN_FILEMUSTEXIST


@pytest.mark.skipif(os.name != "nt", reason="the product supports Windows only")
def test_native_dialog_ignores_invalid_initial_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeCommonDialog(selected=0, extended_error=0)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake)

    result = windows_dialogs._native_pick_file(
        "Choose",
        "LaTeX\0*.tex\0\0",
        tmp_path / "directory-that-no-longer-exists",
    )

    assert result is None
    assert fake.initial_dir is None


@pytest.mark.skipif(os.name != "nt", reason="the product supports Windows only")
def test_native_dialog_wraps_library_load_failure_without_leaking_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = r"C:\synthetic-private-fixture\unpublished\main.tex"
    failure = OSError(secret)
    failure.winerror = 126

    def fail_load(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(ctypes, "WinDLL", fail_load)

    with pytest.raises(ContractError) as captured:
        windows_dialogs._native_pick_file("Choose", "All\0*.*\0\0", None)

    serialized = captured.value.as_dict()
    assert serialized["details"] == {
        "provider": "GetOpenFileNameW",
        "stage": "load_library",
        "code": 126,
        "hex": "0x007E",
        "name": "NATIVE_LIBRARY_ERROR",
    }
    assert secret not in repr(serialized)


@pytest.mark.skipif(os.name != "nt", reason="the product supports Windows only")
def test_native_dialog_wraps_call_failure_without_leaking_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = r"C:\synthetic-private-fixture\unpublished\main.tex"
    failure = OSError(secret)
    failure.winerror = 5
    fake = _FakeCommonDialog(selected=0, extended_error=0)

    def fail_call(*_args: object) -> int:
        raise failure

    fake.GetOpenFileNameW = _FakeNativeFunction(0, fail_call)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake)

    with pytest.raises(ContractError) as captured:
        windows_dialogs._native_pick_file("Choose", "All\0*.*\0\0", None)

    serialized = captured.value.as_dict()
    assert serialized["details"] == {
        "provider": "GetOpenFileNameW",
        "stage": "show_dialog",
        "code": 5,
        "hex": "0x0005",
        "name": "NATIVE_CALL_ERROR",
    }
    assert secret not in repr(serialized)


@pytest.mark.skipif(os.name != "nt", reason="the product supports Windows only")
def test_native_dialog_serializes_concurrent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def delayed_cancel(*_args: object) -> int:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return 0

    fake = _FakeCommonDialog(selected=0, extended_error=0)
    fake.GetOpenFileNameW = _FakeNativeFunction(0, delayed_cancel)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                windows_dialogs._native_pick_file,
                "Choose",
                "All\0*.*\0\0",
                None,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=2) for future in futures]

    assert results == [None, None]
    assert maximum_active == 1


def test_dialog_cancel_is_not_an_error(tmp_path: Path) -> None:
    def cancel(_: str, __: str, initial: Path | None) -> Path | None:
        assert initial == tmp_path
        return None

    assert choose_review_file("latex", initial_dir=tmp_path, picker=cancel) is None


def test_save_destination_cancel_is_a_safe_noop(tmp_path: Path) -> None:
    calls: list[tuple[str, Path | None, str]] = []

    def cancel(
        title: str,
        _filter_spec: str,
        initial: Path | None,
        suggested_name: str,
    ) -> Path | None:
        calls.append((title, initial, suggested_name))
        return None

    result = choose_review_copy_destination(
        initial_dir=tmp_path,
        suggested_name="paper-review.docx",
        picker=cancel,
    )

    assert result is None
    assert calls == [("另存审阅 Word 副本", tmp_path, "paper-review.docx")]


def test_save_destination_accepts_only_a_new_docx(tmp_path: Path) -> None:
    selected = tmp_path / "advisor-copy.DOCX"

    result = choose_review_copy_destination(picker=lambda *_args: selected)

    assert result == selected
    assert not selected.exists()


def test_save_destination_rejects_existing_target(tmp_path: Path) -> None:
    selected = tmp_path / "existing.docx"
    selected.write_bytes(b"keep-me")

    with pytest.raises(ContractError) as captured:
        choose_review_copy_destination(picker=lambda *_args: selected)

    assert captured.value.code is ErrorCode.SCHEMA_INVALID
    assert selected.read_bytes() == b"keep-me"


@pytest.mark.parametrize(
    "selected",
    [
        Path("relative.docx"),
        Path("relative.doc"),
    ],
)
def test_save_destination_rejects_relative_or_wrong_suffix(selected: Path) -> None:
    with pytest.raises(ContractError) as captured:
        choose_review_copy_destination(picker=lambda *_args: selected)

    assert captured.value.code in {ErrorCode.PATH_ABSOLUTE, ErrorCode.SCHEMA_INVALID}


def test_save_destination_requires_existing_parent(tmp_path: Path) -> None:
    selected = tmp_path / "missing" / "review.docx"

    with pytest.raises(ContractError) as captured:
        choose_review_copy_destination(picker=lambda *_args: selected)

    assert captured.value.code is ErrorCode.SCHEMA_INVALID
    assert not selected.parent.exists()


def test_save_destination_rejects_unsafe_suggested_name(tmp_path: Path) -> None:
    with pytest.raises(ContractError) as captured:
        choose_review_copy_destination(
            suggested_name="../review.docx",
            picker=lambda *_args: tmp_path / "unused.docx",
        )

    assert captured.value.code is ErrorCode.SCHEMA_INVALID


@pytest.mark.parametrize(
    ("kind", "filename"),
    [("latex", "main.tex"), ("word", "returned.DOCX")],
)
def test_dialog_returns_existing_expected_file(
    tmp_path: Path,
    kind: str,
    filename: str,
) -> None:
    selected = tmp_path / filename
    selected.write_bytes(b"fixture")

    result = choose_review_file(
        kind,  # type: ignore[arg-type]
        picker=lambda *_: selected,
    )

    assert result == selected


def test_wrong_extension_is_rejected(tmp_path: Path) -> None:
    selected = tmp_path / "returned.doc"
    selected.write_bytes(b"legacy")

    with pytest.raises(ContractError) as captured:
        choose_review_file("word", picker=lambda *_: selected)

    assert captured.value.code is ErrorCode.SCHEMA_INVALID


def test_relative_result_is_rejected() -> None:
    with pytest.raises(ContractError) as captured:
        choose_review_file("latex", picker=lambda *_: Path("main.tex"))

    assert captured.value.code is ErrorCode.PATH_ABSOLUTE


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ContractError) as captured:
        choose_review_file("latex", picker=lambda *_: tmp_path / "missing.tex")

    assert captured.value.code is ErrorCode.SCHEMA_INVALID


def test_symlink_selection_is_rejected_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "main.tex"
    target.write_text("content", encoding="utf-8")
    link = tmp_path / "linked.tex"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("real Windows symlink creation is unavailable")

    with pytest.raises(ContractError) as captured:
        choose_review_file("latex", picker=lambda *_: link)

    assert captured.value.code is ErrorCode.PATH_LINK_ESCAPE


def test_startup_failure_dialog_is_actionable_without_exposing_exception() -> None:
    calls: list[tuple[str, str, int]] = []

    def present(title: str, message: str, flags: int) -> int:
        calls.append((title, message, flags))
        return 1

    shown = show_startup_failure(
        ErrorCode.TOOL_MISSING.value,
        presenter=present,
    )

    assert shown is True
    assert len(calls) == 1
    assert "无法启动" in calls[0][0]
    assert ErrorCode.TOOL_MISSING.value in calls[0][1]
    assert "原始 LaTeX 和 Word 文件没有被修改" in calls[0][1]


def test_browser_failure_dialog_reveals_loopback_address() -> None:
    calls: list[tuple[str, str, int]] = []
    url = "http://127.0.0.1:43129/"

    def present(title: str, message: str, flags: int) -> int:
        calls.append((title, message, flags))
        return 1

    shown = show_browser_open_failure(
        url,
        presenter=present,
    )

    assert shown is True
    assert len(calls) == 1
    assert url in calls[0][1]
    assert "不会上传论文" in calls[0][1]


def test_main_tex_picker_title_explains_that_the_filename_is_not_fixed(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "manuscript.tex"
    selected.write_text("synthetic", encoding="utf-8")
    observed: list[tuple[str, Path | None]] = []

    def pick(title: str, _filter_spec: str, initial: Path | None) -> Path:
        observed.append((title, initial))
        return selected

    result = windows_dialogs.choose_review_file(
        "latex",
        initial_dir=tmp_path,
        picker=pick,
    )

    assert result == selected
    assert observed == [
        (
            "\u9009\u62e9\u8bba\u6587\u7684\u4e3b .tex "
            "\u6587\u4ef6\uff08\u6587\u4ef6\u540d\u4e0d\u9650\uff09",
            tmp_path,
        )
    ]
