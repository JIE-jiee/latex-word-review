"""Minimal Windows-native file selection for the local product launcher."""

from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any, Final, Literal

from latex_word_review.errors import ContractError, ErrorCode

FileKind = Literal["latex", "word"]
FilePicker = Callable[[str, str, Path | None], Path | None]
SaveFilePicker = Callable[[str, str, Path | None, str], Path | None]
MessagePresenter = Callable[[str, str, int], int]

_MAX_DIALOG_PATH: Final = 32_768
_OFN_PATHMUSTEXIST: Final = 0x00000800
_OFN_FILEMUSTEXIST: Final = 0x00001000
_OFN_NOCHANGEDIR: Final = 0x00000008
_OFN_EXPLORER: Final = 0x00080000
_OFN_NODEREFERENCELINKS: Final = 0x00100000
_OFN_DONTADDTORECENT: Final = 0x02000000
_MB_OK: Final = 0x00000000
_MB_ICONERROR: Final = 0x00000010
_MB_ICONWARNING: Final = 0x00000030
_MB_SETFOREGROUND: Final = 0x00010000
_DIALOG_PROVIDER: Final = "GetOpenFileNameW"
_SAVE_DIALOG_PROVIDER: Final = "GetSaveFileNameW"
_FILE_DIALOG_LOCK: Final = threading.Lock()

_COMMON_DIALOG_ERROR_NAMES: Final[dict[int, str]] = {
    0xFFFF: "CDERR_DIALOGFAILURE",
    0x0001: "CDERR_STRUCTSIZE",
    0x0002: "CDERR_INITIALIZATION",
    0x0003: "CDERR_NOTEMPLATE",
    0x0004: "CDERR_NOHINSTANCE",
    0x0005: "CDERR_LOADSTRFAILURE",
    0x0006: "CDERR_FINDRESFAILURE",
    0x0007: "CDERR_LOADRESFAILURE",
    0x0008: "CDERR_LOCKRESFAILURE",
    0x0009: "CDERR_MEMALLOCFAILURE",
    0x000A: "CDERR_MEMLOCKFAILURE",
    0x000B: "CDERR_NOHOOK",
    0x000C: "CDERR_REGISTERMSGFAIL",
    0x3001: "FNERR_SUBCLASSFAILURE",
    0x3002: "FNERR_INVALIDFILENAME",
    0x3003: "FNERR_BUFFERTOOSMALL",
}


def _filter_spec(*parts: str) -> str:
    separator = chr(0)
    return separator.join(parts) + separator * 2


_FILTERS: Final[dict[FileKind, str]] = {
    "latex": _filter_spec(
        "\u004c\u0061\u0054\u0065\u0058 \u4e3b\u6587\u4ef6 (*.tex)",
        "*.tex",
        "\u6240\u6709\u6587\u4ef6 (*.*)",
        "*.*",
    ),
    "word": _filter_spec(
        "\u0057\u006f\u0072\u0064 \u5ba1\u9605\u7a3f (*.docx)",
        "*.docx",
        "\u6240\u6709\u6587\u4ef6 (*.*)",
        "*.*",
    ),
}
_TITLES: Final[dict[FileKind, str]] = {
    "latex": (
        "\u9009\u62e9\u8bba\u6587\u7684\u4e3b .tex "
        "\u6587\u4ef6\uff08\u6587\u4ef6\u540d\u4e0d\u9650\uff09"
    ),
    "word": "\u9009\u62e9\u5bfc\u5e08\u8fd4\u56de\u7684 Word \u5ba1\u9605\u7a3f",
}
_SUFFIXES: Final[dict[FileKind, str]] = {"latex": ".tex", "word": ".docx"}


class _OpenFileNameW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", wintypes.LPVOID),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", wintypes.LPVOID),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


def _error_hex(code: int | None) -> str | None:
    if code is None:
        return None
    normalized = code & 0xFFFFFFFF
    width = 4 if normalized <= 0xFFFF else 8
    return f"0x{normalized:0{width}X}"


def _dialog_error_details(
    *,
    stage: str,
    code: int | None,
    name: str,
) -> dict[str, Any]:
    """Build a stable diagnostic without including a user-controlled path."""

    return {
        "provider": _DIALOG_PROVIDER,
        "stage": stage,
        "code": code,
        "hex": _error_hex(code),
        "name": name,
    }


def _native_exception_error(stage: str, name: str, exc: Exception) -> ContractError:
    winerror = getattr(exc, "winerror", None)
    code = winerror if isinstance(winerror, int) and not isinstance(winerror, bool) else None
    return ContractError(
        ErrorCode.INTERNAL_INVARIANT,
        "Windows file dialog failed",
        details=_dialog_error_details(stage=stage, code=code, name=name),
    )


def _initial_directory_value(initial_dir: Path | None) -> str | None:
    """Return a usable absolute directory, otherwise let Windows choose its default."""

    if initial_dir is None:
        return None
    try:
        candidate = initial_dir.expanduser()
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return str(candidate)


def _native_present_message(title: str, message: str, flags: int) -> int:
    if os.name != "nt":  # pragma: no cover - the product and CI are Windows-only
        raise OSError("native message dialog requires Windows")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    message_box = user32.MessageBoxW
    message_box.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    message_box.restype = ctypes.c_int
    return int(message_box(None, message, title, flags))


def _present_message(
    title: str,
    message: str,
    flags: int,
    *,
    presenter: MessagePresenter | None = None,
) -> bool:
    try:
        return (presenter or _native_present_message)(title, message, flags) != 0
    except Exception:
        # This is the last-resort visibility boundary for a windowed executable.
        # A failed dialog must not replace the original startup failure.
        return False


def show_startup_failure(
    error_code: str,
    *,
    presenter: MessagePresenter | None = None,
) -> bool:
    """Show a safe, actionable error when the windowed executable cannot start."""

    return _present_message(
        "LaTeX Word Review 无法启动",
        "程序未能启动本地审阅界面。\n\n"
        "请关闭其他 LaTeX Word Review 窗口后重试；如果仍然失败，请重新安装当前版本。\n"
        "原始 LaTeX 和 Word 文件没有被修改。\n\n"
        f"错误代码：{error_code}",
        _MB_OK | _MB_ICONERROR | _MB_SETFOREGROUND,
        presenter=presenter,
    )


def show_browser_open_failure(
    url: str,
    *,
    presenter: MessagePresenter | None = None,
) -> bool:
    """Reveal the loopback URL when Windows cannot open the default browser."""

    return _present_message(
        "LaTeX Word Review 已启动",
        "程序已经在本机运行，但默认浏览器没有自动打开。\n\n"
        "请在浏览器地址栏输入：\n"
        f"{url}\n\n"
        "该地址只连接本机，不会上传论文。",
        _MB_OK | _MB_ICONWARNING | _MB_SETFOREGROUND,
        presenter=presenter,
    )


def _native_pick_file(title: str, filter_spec: str, initial_dir: Path | None) -> Path | None:
    if os.name != "nt":  # pragma: no cover - the product and CI are Windows-only
        raise ContractError(ErrorCode.TOOL_MISSING, "native file dialog requires Windows")

    initial_dir_value = _initial_directory_value(initial_dir)
    with _FILE_DIALOG_LOCK:
        try:
            comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        except Exception as exc:
            raise _native_exception_error("load_library", "NATIVE_LIBRARY_ERROR", exc) from exc

        try:
            get_open_file_name = comdlg32.GetOpenFileNameW
            get_open_file_name.argtypes = [ctypes.POINTER(_OpenFileNameW)]
            get_open_file_name.restype = wintypes.BOOL
            extended_error = comdlg32.CommDlgExtendedError
            extended_error.argtypes = []
            extended_error.restype = wintypes.DWORD
        except Exception as exc:
            raise _native_exception_error("bind_api", "NATIVE_BIND_ERROR", exc) from exc

        try:
            buffer = ctypes.create_unicode_buffer(_MAX_DIALOG_PATH)
            dialog = _OpenFileNameW()
            dialog.lStructSize = ctypes.sizeof(_OpenFileNameW)
            dialog.lpstrFilter = filter_spec
            dialog.nFilterIndex = 1
            dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
            dialog.nMaxFile = _MAX_DIALOG_PATH
            dialog.lpstrInitialDir = initial_dir_value
            dialog.lpstrTitle = title
            dialog.Flags = (
                _OFN_PATHMUSTEXIST
                | _OFN_FILEMUSTEXIST
                | _OFN_NOCHANGEDIR
                | _OFN_EXPLORER
                | _OFN_NODEREFERENCELINKS
                | _OFN_DONTADDTORECENT
            )
            dialog.lpstrDefExt = None
        except Exception as exc:
            raise _native_exception_error(
                "prepare_dialog", "NATIVE_PREPARATION_ERROR", exc
            ) from exc

        try:
            selected_ok = bool(get_open_file_name(ctypes.byref(dialog)))
        except Exception as exc:
            raise _native_exception_error("show_dialog", "NATIVE_CALL_ERROR", exc) from exc

        if selected_ok:
            selected = buffer.value
            if not selected:
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "Windows file dialog returned an empty path",
                    details=_dialog_error_details(
                        stage="read_result",
                        code=None,
                        name="EMPTY_SELECTION_RESULT",
                    ),
                )
            return Path(selected)

        try:
            code = int(extended_error())
        except Exception as exc:
            raise _native_exception_error(
                "read_extended_error", "NATIVE_DIAGNOSTIC_ERROR", exc
            ) from exc
        if code == 0:
            return None
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "Windows file dialog failed",
            details=_dialog_error_details(
                stage="show_dialog",
                code=code,
                name=_COMMON_DIALOG_ERROR_NAMES.get(code, "UNKNOWN_COMMON_DIALOG_ERROR"),
            ),
        )


def _save_dialog_error_details(
    *,
    stage: str,
    code: int | None,
    name: str,
) -> dict[str, Any]:
    return {
        "provider": _SAVE_DIALOG_PROVIDER,
        "stage": stage,
        "code": code,
        "hex": _error_hex(code),
        "name": name,
    }


def _native_save_exception_error(stage: str, name: str, exc: Exception) -> ContractError:
    winerror = getattr(exc, "winerror", None)
    code = winerror if isinstance(winerror, int) and not isinstance(winerror, bool) else None
    return ContractError(
        ErrorCode.INTERNAL_INVARIANT,
        "Windows save dialog failed",
        details=_save_dialog_error_details(stage=stage, code=code, name=name),
    )


def _native_save_file(
    title: str,
    filter_spec: str,
    initial_dir: Path | None,
    suggested_name: str,
) -> Path | None:
    if os.name != "nt":  # pragma: no cover - the product and CI are Windows-only
        raise ContractError(ErrorCode.TOOL_MISSING, "native save dialog requires Windows")
    if not suggested_name or len(suggested_name) >= _MAX_DIALOG_PATH or chr(0) in suggested_name:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "suggested file name is invalid")

    initial_dir_value = _initial_directory_value(initial_dir)
    with _FILE_DIALOG_LOCK:
        try:
            comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        except Exception as exc:
            raise _native_save_exception_error("load_library", "NATIVE_LIBRARY_ERROR", exc) from exc

        try:
            get_save_file_name = comdlg32.GetSaveFileNameW
            get_save_file_name.argtypes = [ctypes.POINTER(_OpenFileNameW)]
            get_save_file_name.restype = wintypes.BOOL
            extended_error = comdlg32.CommDlgExtendedError
            extended_error.argtypes = []
            extended_error.restype = wintypes.DWORD
        except Exception as exc:
            raise _native_save_exception_error("bind_api", "NATIVE_BIND_ERROR", exc) from exc

        try:
            buffer = ctypes.create_unicode_buffer(suggested_name, _MAX_DIALOG_PATH)
            dialog = _OpenFileNameW()
            dialog.lStructSize = ctypes.sizeof(_OpenFileNameW)
            dialog.lpstrFilter = filter_spec
            dialog.nFilterIndex = 1
            dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
            dialog.nMaxFile = _MAX_DIALOG_PATH
            dialog.lpstrInitialDir = initial_dir_value
            dialog.lpstrTitle = title
            dialog.Flags = (
                _OFN_PATHMUSTEXIST
                | _OFN_NOCHANGEDIR
                | _OFN_EXPLORER
                | _OFN_NODEREFERENCELINKS
                | _OFN_DONTADDTORECENT
            )
            dialog.lpstrDefExt = "docx"
        except Exception as exc:
            raise _native_save_exception_error(
                "prepare_dialog", "NATIVE_PREPARATION_ERROR", exc
            ) from exc

        try:
            selected_ok = bool(get_save_file_name(ctypes.byref(dialog)))
        except Exception as exc:
            raise _native_save_exception_error("show_dialog", "NATIVE_CALL_ERROR", exc) from exc

        if selected_ok:
            selected = buffer.value
            if not selected:
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "Windows save dialog returned an empty path",
                    details=_save_dialog_error_details(
                        stage="read_result",
                        code=None,
                        name="EMPTY_SELECTION_RESULT",
                    ),
                )
            return Path(selected)

        try:
            code = int(extended_error())
        except Exception as exc:
            raise _native_save_exception_error(
                "read_extended_error", "NATIVE_DIAGNOSTIC_ERROR", exc
            ) from exc
        if code == 0:
            return None
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "Windows save dialog failed",
            details=_save_dialog_error_details(
                stage="show_dialog",
                code=code,
                name=_COMMON_DIALOG_ERROR_NAMES.get(code, "UNKNOWN_COMMON_DIALOG_ERROR"),
            ),
        )


def choose_review_file(
    kind: FileKind,
    *,
    initial_dir: Path | None = None,
    picker: FilePicker | None = None,
) -> Path | None:
    """Choose and conservatively validate a LaTeX or returned Word file."""

    selected = (picker or _native_pick_file)(_TITLES[kind], _FILTERS[kind], initial_dir)
    if selected is None:
        return None
    path = selected.expanduser()
    if not path.is_absolute():
        raise ContractError(ErrorCode.PATH_ABSOLUTE, "selected file path must be absolute")
    if path.is_symlink():
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "selected file cannot be a link")
    try:
        is_file = path.is_file()
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "selected file cannot be inspected") from exc
    if not is_file:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "selected file is unavailable")
    if path.suffix.casefold() != _SUFFIXES[kind]:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            f"selected {kind} file has the wrong extension",
        )
    return path


def choose_main_tex(*, initial_dir: Path | None = None) -> Path | None:
    return choose_review_file("latex", initial_dir=initial_dir)


def choose_returned_docx(*, initial_dir: Path | None = None) -> Path | None:
    return choose_review_file("word", initial_dir=initial_dir)


def choose_review_copy_destination(
    *,
    initial_dir: Path | None = None,
    suggested_name: str = "review.docx",
    picker: SaveFilePicker | None = None,
) -> Path | None:
    """Choose a new .docx destination without accepting a browser path."""

    if (
        not suggested_name
        or Path(suggested_name).name != suggested_name
        or Path(suggested_name).suffix.casefold() != ".docx"
    ):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "suggested Word file name is invalid")
    selected = (picker or _native_save_file)(
        "另存审阅 Word 副本",
        _FILTERS["word"],
        initial_dir,
        suggested_name,
    )
    if selected is None:
        return None
    path = selected.expanduser()
    if not path.is_absolute():
        raise ContractError(ErrorCode.PATH_ABSOLUTE, "selected output path must be absolute")
    if path.suffix.casefold() != ".docx":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "selected output must have a .docx suffix")
    if path.is_symlink():
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "selected output cannot be a link")
    if path.exists():
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "selected output already exists; choose a new file name",
        )
    try:
        parent_available = path.parent.is_dir()
    except OSError as exc:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "output directory cannot be inspected",
        ) from exc
    if not parent_available:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "output directory is unavailable")
    return path


def choose_existing_changes_copy_destination(
    *,
    initial_dir: Path | None = None,
    suggested_name: str = "\u5df2\u6709\u6279\u6539\u5c55\u793a\u7a3f.docx",
    picker: SaveFilePicker | None = None,
) -> Path | None:
    """Choose a display-only copy with an unambiguous native dialog title."""

    delegate = picker or _native_save_file

    def retitled_picker(
        _title: str,
        filter_spec: str,
        initial: Path | None,
        name: str,
    ) -> Path | None:
        title = (
            "\u53e6\u5b58\u5df2\u6709 LaTeX "
            "\u6279\u6539\u5c55\u793a\u7a3f\uff08\u4ec5\u4f9b\u5bf9\u7167\uff09"
        )
        return delegate(title, filter_spec, initial, name)

    return choose_review_copy_destination(
        initial_dir=initial_dir,
        suggested_name=suggested_name,
        picker=retitled_picker,
    )


__all__ = [
    "FileKind",
    "FilePicker",
    "SaveFilePicker",
    "MessagePresenter",
    "choose_existing_changes_copy_destination",
    "choose_main_tex",
    "choose_review_copy_destination",
    "choose_returned_docx",
    "choose_review_file",
    "show_browser_open_failure",
    "show_startup_failure",
]
