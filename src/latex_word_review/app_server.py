"""Loopback-only Windows product server.

The browser is only a local view. Durable truth is reconstructed by the
ApplicationSession facade from sealed run artifacts for every operation.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.resources
import os
import re
import secrets
import socket
import stat
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import parse_qsl, urlsplit

from latex_word_review.__about__ import __version__
from latex_word_review.app_jobs import JobManager, JobSnapshot, ProgressReporter
from latex_word_review.app_presenter import (
    build_error_recovery_control,
    render_session_view,
)
from latex_word_review.app_views import APP_STYLESHEET_PATH, render_app_page
from latex_word_review.application import ApplicationSession
from latex_word_review.discovery import ProjectDiscovery, discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.jsonio import read_contract_file
from latex_word_review.paths import resolve_within, validate_relative_path
from latex_word_review.support_bundle import (
    SUPPORT_BUNDLE_FILENAME,
    SupportBundleRequest,
    build_support_bundle_bytes,
    verify_support_bundle_bytes,
)
from latex_word_review.user_messages import (
    format_safe_technical_details,
    user_message_for_error,
)
from latex_word_review.windows_dialogs import (
    choose_main_tex,
    choose_returned_docx,
    choose_review_copy_destination,
    show_browser_open_failure,
)

LOOPBACK_HOST: Final = "127.0.0.1"
SESSION_COOKIE: Final = "lwr_app_session"
MAX_FORM_BYTES: Final = 64 * 1024
MAX_ARTIFACT_BYTES: Final = 512 * 1024 * 1024
_MAX_FIELDS: Final = 10
_MAX_RECENT_SESSIONS: Final = 20
_MAX_SCANNED_SESSIONS: Final = 1_000
_MAX_HOME_SUMMARY_BYTES: Final = 4 * 1024 * 1024
_MAX_DELETE_TREE_ENTRIES: Final = 250_000
_MAX_PENDING_SUPPORT_DOWNLOADS: Final = 32
_SELECTION_TTL_SECONDS: Final = 60 * 60
_EXPORT_TIMEOUT_SECONDS: Final = 60.0
_SESSION_KEY_RE: Final = re.compile(r"session_[0-9a-f]{32}")
_SELECTION_KEY_RE: Final = re.compile(r"selection_[0-9a-f]{32}")
_JOB_KEY_RE: Final = re.compile(r"job_[0-9a-f]{32}")
_SUPPORT_KEY_RE: Final = re.compile(r"support_[0-9a-f]{32}")
_SUPPORT_STAGE_RE: Final = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_SUPPORT_TOKEN_RE: Final = re.compile(
    r"support1\.(E_[A-Z0-9_]{1,94})\.([a-z][a-z0-9_-]{0,63})\.([0-9a-f]{64})"
)
_ARTIFACT_KEY_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FILTERS: Final = frozenset({"all", "pending", "safe", "manual", "conflict"})
_DECISIONS: Final = frozenset({"accepted", "accepted_with_edit", "rejected", "manual", "conflict"})
_HEX: Final = frozenset("0123456789abcdefABCDEF")
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x0400
_GRACEFUL_DRAIN_BYTES: Final = MAX_FORM_BYTES + 1
_GRACEFUL_DRAIN_CHUNK_BYTES: Final = 8 * 1024
_GRACEFUL_DRAIN_TIMEOUT_SECONDS: Final = 0.25
_SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'",
    ),
    ("Cache-Control", "no-store, max-age=0"),
    ("Pragma", "no-cache"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)
_PHASE_LABELS: Final[dict[str, tuple[str, str]]] = {
    "ready_to_export": ("等待生成 Word", "info"),
    "waiting_for_return": ("等待返回 Word", "warning"),
    "approval_required": ("等待审批", "warning"),
    "approval_in_progress": ("审批中", "warning"),
    "approval_ready_to_finalize": ("审批待确认", "warning"),
    "ready_to_plan": ("等待补丁预览", "warning"),
    "plan_blocked": ("需要人工处理", "danger"),
    "awaiting_apply_confirmation": ("等待最终确认", "warning"),
    "applied": ("正在核验", "info"),
    "verified": ("正在整理结果", "info"),
    "partially_completed": ("部分完成", "warning"),
    "ready_to_bundle": ("正在生成审计包", "info"),
    "completed": ("已完成", "success"),
}
_DOWNLOADABLE_ARTIFACTS: Final = frozenset(
    {
        "audit_bundle",
        "actual_diff",
        "compile_log",
        "ledger_html",
        "ledger_json",
        "latexdiff_pdf",
        "latexdiff_tex",
        "revised_clean_pdf",
        "run_manifest",
        "verification_report",
    }
)
_SUPPORT_BUILD_IDENTIFIER: Final = f"windows-local-{__version__}"

PathPicker = Callable[[Path | None], Path | None]
PathOpener = Callable[[Path], None]
BrowserOpener = Callable[[str], bool]
BrowserFailureNotifier = Callable[[str], None]


def _default_browser_failure_notifier(url: str) -> None:
    show_browser_open_failure(url)


def _open_browser_or_notify(
    url: str,
    opener: BrowserOpener,
    failure_notifier: BrowserFailureNotifier,
) -> None:
    try:
        opened = opener(url)
    except Exception:
        opened = False
    if not opened:
        with suppress(Exception):
            failure_notifier(url)


def _is_link_or_junction(path: Path) -> bool:
    junction_probe = getattr(path, "is_junction", None)
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return (
            path.is_symlink()
            or bool(junction_probe is not None and junction_probe())
            or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
        )
    except OSError as exc:
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "path link status is unavailable") from exc


def _validated_delete_tree(root: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Inventory one owned tree without following links or reparse points.

    The complete validation pass happens before the first unlink.  This keeps a
    corrupt task removable while refusing any tree whose filesystem topology is
    not locally provable.
    """

    if _is_link_or_junction(root):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "review session cannot be a link")
    directories: list[Path] = []
    files: list[Path] = []
    pending = [root]
    entry_count = 0
    while pending:
        directory = pending.pop()
        if _is_link_or_junction(directory):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "task tree contains a reparse point")
        directories.append(directory)
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entry_count += 1
                    if entry_count > _MAX_DELETE_TREE_ENTRIES:
                        raise ContractError(
                            ErrorCode.SCHEMA_INVALID,
                            "task tree exceeds the deletion safety limit",
                        )
                    child = Path(entry.path)
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise ContractError(
                            ErrorCode.INTERNAL_INVARIANT,
                            "task tree entry cannot be inspected for deletion",
                            details={"stage": "delete_session_scan"},
                        ) from exc
                    if entry.is_symlink() or bool(
                        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        raise ContractError(
                            ErrorCode.PATH_LINK_ESCAPE,
                            "task tree contains a reparse point",
                        )
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(child)
                    elif stat.S_ISREG(metadata.st_mode):
                        files.append(child)
                    else:
                        raise ContractError(
                            ErrorCode.PATH_LINK_ESCAPE,
                            "task tree contains an unsupported filesystem entry",
                        )
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "task tree cannot be inspected for deletion",
                details={"stage": "delete_session_scan"},
            ) from exc
    return tuple(files), tuple(directories)


def _make_owned_entry_writable(path: Path) -> None:
    """Clear only the Windows read-only bit on a previously validated entry."""

    if _is_link_or_junction(path):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "task tree changed before deletion")
    try:
        path.chmod(stat.S_IREAD | stat.S_IWRITE, follow_symlinks=False)
    except (NotImplementedError, TypeError):  # pragma: no cover - legacy Windows fallback
        if _is_link_or_junction(path):
            raise ContractError(
                ErrorCode.PATH_LINK_ESCAPE,
                "task tree changed before deletion",
            ) from None
        path.chmod(stat.S_IREAD | stat.S_IWRITE)


def _remove_validated_delete_tree(
    files: tuple[Path, ...],
    directories: tuple[Path, ...],
) -> None:
    for path in files:
        if _is_link_or_junction(path):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "task tree changed before deletion")
        try:
            path.unlink()
        except PermissionError:
            try:
                _make_owned_entry_writable(path)
                path.unlink()
            except OSError as exc:
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "read-only task file could not be deleted",
                    details={"stage": "delete_session_remove"},
                ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "task file could not be deleted",
                details={"stage": "delete_session_remove"},
            ) from exc
    for path in reversed(directories):
        if _is_link_or_junction(path):
            raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "task tree changed before deletion")
        try:
            path.rmdir()
        except PermissionError:
            try:
                _make_owned_entry_writable(path)
                path.rmdir()
            except OSError as exc:
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "read-only task directory could not be deleted",
                    details={"stage": "delete_session_remove"},
                ) from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "task directory could not be deleted",
                details={"stage": "delete_session_remove"},
            ) from exc


def _require_real_directory(path: Path, label: str) -> Path:
    if _is_link_or_junction(path):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, f"{label} cannot be a link")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} is unavailable") from exc
    if not resolved.is_dir() or _is_link_or_junction(resolved):
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} must be a directory")
    return resolved


def _ensure_real_directory(path: Path, label: str) -> Path:
    if path.exists() or path.is_symlink():
        return _require_real_directory(path, label)
    try:
        path.mkdir(parents=True)
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, f"{label} could not be created") from exc
    return _require_real_directory(path, label)


def default_app_data_root(*, environ: Mapping[str, str] | None = None) -> Path:
    """Return the per-user Windows application-data root without creating it."""

    values = os.environ if environ is None else environ
    raw = values.get("LOCALAPPDATA")
    if not raw:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "LOCALAPPDATA is unavailable")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ContractError(ErrorCode.PATH_ABSOLUTE, "LOCALAPPDATA must be absolute")
    return root / "LatexWordReview"


def _default_main_picker(initial: Path | None) -> Path | None:
    return choose_main_tex(initial_dir=initial)


def _default_word_picker(initial: Path | None) -> Path | None:
    return choose_returned_docx(initial_dir=initial)


def _default_review_copy_picker(initial: Path | None) -> Path | None:
    return choose_review_copy_destination(initial_dir=initial)


def _default_path_opener(path: Path) -> None:
    if os.name != "nt":  # pragma: no cover - this distribution is Windows-only
        raise ContractError(ErrorCode.TOOL_MISSING, "opening files requires Windows")
    os.startfile(str(path))


@dataclass(frozen=True, slots=True)
class SelectedProject:
    token: str
    main_file: Path
    discovery: ProjectDiscovery
    created_at: float


@dataclass(frozen=True, slots=True)
class _JobRoute:
    success_href: str
    retry_href: str
    back_href: str
    step: int


@dataclass(frozen=True, slots=True)
class _PendingSupportDownload:
    """Creation-time identity for one pending, one-shot support download."""

    data: bytes
    sha256: str
    size_bytes: int


class AppState:
    """Thread-safe non-authoritative application plumbing."""

    def __init__(
        self,
        data_root: Path,
        *,
        main_picker: PathPicker = _default_main_picker,
        word_picker: PathPicker = _default_word_picker,
        review_copy_picker: PathPicker = _default_review_copy_picker,
        path_opener: PathOpener = _default_path_opener,
        monotonic: Callable[[], float] = time.monotonic,
        max_workers: int = 2,
    ) -> None:
        if not data_root.is_absolute():
            raise ContractError(ErrorCode.PATH_ABSOLUTE, "application data root must be absolute")
        self.data_root = _ensure_real_directory(data_root, "application data root")
        self.runs_root = _ensure_real_directory(self.data_root / "runs", "runs root")
        self.session_token = secrets.token_urlsafe(48)
        self.csrf_token = secrets.token_urlsafe(48)
        self._support_hmac_key = secrets.token_bytes(32)
        self._main_picker = main_picker
        self._word_picker = word_picker
        self._review_copy_picker = review_copy_picker
        self._path_opener = path_opener
        self._monotonic = monotonic
        self._last_source_directory: Path | None = None
        self._last_return_directory: Path | None = None
        self._last_output_directory: Path | None = None
        self._lock = threading.RLock()
        self._support_condition = threading.Condition(self._lock)
        self._support_closing = False
        self._support_inflight = 0
        self._selected: dict[str, SelectedProject] = {}
        self._job_routes: dict[str, _JobRoute] = {}
        self._review_copy_notices: dict[str, str] = {}
        self._support_downloads: dict[str, _PendingSupportDownload] = {}
        self.jobs = JobManager(max_workers=max_workers)

    def issue_support_token(
        self,
        session_key: str,
        error_code: ErrorCode | str,
        stage: str,
    ) -> str:
        """Return a tamper-evident token bound to one task and safe error context."""

        if _SESSION_KEY_RE.fullmatch(session_key) is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid support session key")
        try:
            code = error_code if isinstance(error_code, ErrorCode) else ErrorCode(error_code)
        except ValueError as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "support error code is not stable",
            ) from exc
        if _SUPPORT_STAGE_RE.fullmatch(stage) is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "support stage is not stable")
        payload = f"{session_key}\0{code.value}\0{stage}".encode("ascii")
        signature = hmac.new(
            self._support_hmac_key,
            payload,
            hashlib.sha256,
        ).hexdigest()
        return f"support1.{code.value}.{stage}.{signature}"

    def _support_request_from_token(
        self,
        session_key: str,
        support_token: str,
    ) -> SupportBundleRequest:
        match = _SUPPORT_TOKEN_RE.fullmatch(support_token)
        if match is None or _SESSION_KEY_RE.fullmatch(session_key) is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid support request token")
        code_value, stage, signature = match.groups()
        expected = self.issue_support_token(session_key, code_value, stage)
        if not secrets.compare_digest(expected, support_token):
            raise ContractError(ErrorCode.SCHEMA_INVALID, "support request token was modified")
        return SupportBundleRequest(
            error_code=code_value,
            stage=stage,
            build_identifier=_SUPPORT_BUILD_IDENTIFIER,
            task_identifier=session_key,
        )

    def bind_support_action(
        self,
        view: dict[str, object],
        *,
        session_key: str,
        stage: str,
    ) -> dict[str, object]:
        """Attach a signed support POST only to errors that promise that action."""

        if view.get("page") != "error":
            return view
        error = view.get("error")
        if not isinstance(error, dict):
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "error view is missing its strict card",
            )
        code = error.get("code")
        if not isinstance(code, str):
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "error view is missing its stable code",
            )
        message = user_message_for_error(code)
        if message.action != "create_support_bundle":
            return view
        support_token = self.issue_support_token(session_key, message.code, stage)
        control = build_error_recovery_control(
            message.action,
            message.action_label,
            csrf_token=self.csrf_token,
            session_key=session_key,
            support_token=support_token,
        )
        if control is None:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "support recovery action could not be bound",
            )
        error["primary_action"] = control
        return view

    def create_support_download(self, session_key: str, support_token: str) -> str:
        """Create one bounded redacted ZIP held only in memory until claimed."""

        self._session_root(session_key, must_exist=True)
        request = self._support_request_from_token(session_key, support_token)
        with self._support_condition:
            if self._support_closing:
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "application is closing and cannot create support downloads",
                )
            if (
                len(self._support_downloads) + self._support_inflight
                >= _MAX_PENDING_SUPPORT_DOWNLOADS
            ):
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "too many support downloads are waiting",
                )
            self._support_inflight += 1
        try:
            data = build_support_bundle_bytes(request)
            verification = verify_support_bundle_bytes(data)
            with self._support_condition:
                if self._support_closing:
                    raise ContractError(
                        ErrorCode.INTERNAL_INVARIANT,
                        "application closed during support bundle creation",
                    )
                for _attempt in range(8):
                    download_key = f"support_{secrets.token_hex(16)}"
                    if download_key in self._support_downloads:
                        continue
                    self._support_downloads[download_key] = _PendingSupportDownload(
                        data=data,
                        sha256=verification.sha256,
                        size_bytes=verification.size_bytes,
                    )
                    return download_key
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "support download identifier could not be allocated",
                )
        finally:
            with self._support_condition:
                self._support_inflight -= 1
                if self._support_inflight < 0:
                    self._support_inflight = 0
                    raise ContractError(
                        ErrorCode.INTERNAL_INVARIANT,
                        "support creation reservation underflow",
                    )
                self._support_condition.notify_all()

    def claim_support_download(self, download_key: str) -> bytes:
        """Claim and remove one bounded, verified in-memory support archive."""

        if _SUPPORT_KEY_RE.fullmatch(download_key) is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid support download key")
        with self._support_condition:
            if self._support_closing:
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "application is closing and cannot claim support downloads",
                )
            pending = self._support_downloads.pop(download_key, None)
            if pending is None:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "support download is unavailable",
                )
            self._support_inflight += 1
        try:
            verification = verify_support_bundle_bytes(pending.data)
            if verification.size_bytes != pending.size_bytes or not secrets.compare_digest(
                verification.sha256, pending.sha256
            ):
                raise ContractError(
                    ErrorCode.BUNDLE_HASH_MISMATCH,
                    "support download changed after creation",
                )
            return pending.data
        finally:
            with self._support_condition:
                self._support_inflight -= 1
                if self._support_inflight < 0:
                    self._support_inflight = 0
                    raise ContractError(
                        ErrorCode.INTERNAL_INVARIANT,
                        "support claim reservation underflow",
                    )
                self._support_condition.notify_all()

    def close(self) -> None:
        with self._support_condition:
            self._support_closing = True
            self._support_condition.notify_all()
        self.jobs.close()
        with self._support_condition:
            while self._support_inflight:
                self._support_condition.wait()
            self._support_downloads.clear()

    def home_view(self) -> dict[str, object]:
        recent: list[tuple[float, dict[str, object]]] = []
        try:
            # Filesystem times rank navigation cards only. The lightweight
            # summary validates only the bounded SourceManifest; opening a
            # session or performing any action rebuilds all workflow evidence.
            candidates = sorted(
                (
                    (path.stat().st_mtime, path)
                    for path in islice(self.runs_root.iterdir(), _MAX_SCANNED_SESSIONS)
                    if _SESSION_KEY_RE.fullmatch(path.name) is not None
                    and not _is_link_or_junction(path)
                    and path.is_dir()
                ),
                key=lambda item: (item[0], item[1].name),
                reverse=True,
            )[:_MAX_RECENT_SESSIONS]
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "runs root cannot be scanned",
            ) from exc
        for modified, path in candidates:
            try:
                objects = path / "objects"
                source_manifest_path = objects / "source-manifest.json"
                if _is_link_or_junction(objects) or not objects.is_dir():
                    raise ContractError(
                        ErrorCode.SCHEMA_INVALID,
                        "home summary objects directory is unavailable",
                    )
                if _is_link_or_junction(source_manifest_path) or not source_manifest_path.is_file():
                    raise ContractError(
                        ErrorCode.SCHEMA_INVALID,
                        "home summary SourceManifest is unavailable",
                    )
                source_manifest = read_contract_file(
                    source_manifest_path,
                    expected_schema="SourceManifest",
                    max_bytes=_MAX_HOME_SUMMARY_BYTES,
                )
                payload = source_manifest.get("payload")
                if not isinstance(payload, Mapping):
                    raise ContractError(
                        ErrorCode.SCHEMA_INVALID, "SourceManifest payload is invalid"
                    )
                main_value = payload.get("main_document")
                if not isinstance(main_value, str):
                    raise ContractError(
                        ErrorCode.SCHEMA_INVALID, "SourceManifest main document is invalid"
                    )
                main_document = validate_relative_path(main_value)
                name = Path(main_document).stem or "未命名论文"
                label, tone = "打开时核验", "neutral"
                action_label = "打开并核验"
                next_step = "打开任务后会完整核验密封证据，并显示准确状态与下一步。"
            except (ContractError, OSError):
                label, tone = "需要检查", "danger"
                name = "无法读取的任务"
                action_label = "打开并检查"
                next_step = "打开后查看安全诊断；任何操作前仍会完整核验。"
            recent.append(
                (
                    modified,
                    {
                        "name": name,
                        "status": label,
                        "updated_at": (
                            datetime.fromtimestamp(modified).astimezone().strftime("%Y-%m-%d %H:%M")
                            if modified
                            else "未知"
                        ),
                        "href": f"/session/{path.name}",
                        "delete_href": f"/session/{path.name}/delete",
                        "tone": tone,
                        "action_label": action_label,
                        "next_step": next_step,
                    },
                )
            )
        recent.sort(key=lambda item: item[0], reverse=True)
        return {
            "page": "home",
            "form_fields": {"csrf": self.csrf_token},
            "exit_form_fields": {"csrf": self.csrf_token},
            "new_action": "/new",
            "exit_action": "/app/exit",
            "recent_sessions": [item[1] for item in recent[:_MAX_RECENT_SESSIONS]],
        }

    def select_project(self) -> SelectedProject | None:
        selected = self._main_picker(self._last_source_directory)
        if selected is None:
            return None
        try:
            main_file = selected.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "selected main file is no longer available",
            ) from exc
        if (
            _is_link_or_junction(selected)
            or not main_file.is_file()
            or main_file.suffix.casefold() != ".tex"
        ):
            raise ContractError(ErrorCode.SCHEMA_INVALID, "selected main file is invalid")
        self._last_source_directory = main_file.parent
        discovery = discover_project(main_file.parent, main_document=main_file.name)
        token = f"selection_{secrets.token_hex(16)}"
        record = SelectedProject(token, main_file, discovery, self._monotonic())
        with self._lock:
            self._purge_selections()
            self._selected[token] = record
        return record

    def _purge_selections(self) -> None:
        cutoff = self._monotonic() - _SELECTION_TTL_SECONDS
        expired = [key for key, value in self._selected.items() if value.created_at < cutoff]
        for key in expired:
            del self._selected[key]

    def selected_project(self, token: str) -> SelectedProject:
        if _SELECTION_KEY_RE.fullmatch(token) is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid selection token")
        with self._lock:
            self._purge_selections()
            selected = self._selected.get(token)
        if selected is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "project selection has expired")
        return selected

    def preflight_view(self, token: str) -> dict[str, object]:
        selected = self.selected_project(token)
        discovery = selected.discovery
        total_bytes = sum(item.size_bytes for item in discovery.files)
        checks: list[dict[str, str]] = [
            {
                "status": "ok",
                "label": "主文件",
                "message": f"已识别 {discovery.main_document}，将以只读方式创建快照。",
            },
            {
                "status": "error" if discovery.blocked else "ok",
                "label": "项目依赖",
                "message": (
                    f"发现 {len(discovery.external_references)} 个越界或不安全引用，必须先处理。"
                    if discovery.blocked
                    else f"已核对 {len(discovery.files)} 个文件，共 {total_bytes / 1024:.1f} KiB。"
                ),
            },
            {
                "status": "info",
                "label": "Word 与 PDF",
                "message": "生成审阅稿不修改原稿；PDF 将按本机已安装工具尽力生成。",
            },
        ]
        return {
            "page": "preflight",
            "project_name": selected.main_file.stem,
            "main_file": discovery.main_document,
            "source_location": str(selected.main_file.parent),
            "checks": checks,
            "can_continue": not discovery.blocked,
            "form_fields": {"csrf": self.csrf_token, "selection": token},
            "choose_form_fields": {"csrf": self.csrf_token},
            "choose_action": "/new",
            "continue_action": "/session/export",
        }

    def _session_root(self, session_key: str, *, must_exist: bool) -> Path:
        if _SESSION_KEY_RE.fullmatch(session_key) is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid session key")
        candidate = self.runs_root / session_key
        if must_exist:
            resolved = _require_real_directory(candidate, "review session")
            if resolved.parent != self.runs_root:
                raise ContractError(ErrorCode.PATH_TRAVERSAL, "review session escaped runs root")
            return resolved
        if candidate.exists() or candidate.is_symlink():
            raise ContractError(ErrorCode.SCHEMA_INVALID, "review session already exists")
        return candidate

    def load_session(self, session_key: str) -> ApplicationSession:
        return ApplicationSession.load(self._session_root(session_key, must_exist=True))

    def _action_session(self, session_key: str) -> ApplicationSession:
        """Create an action facade without duplicating its mandatory inspection.

        ``ApplicationSession`` mutation methods all begin with an independent
        ``_inspect()``.  Calling ``load()`` here would perform the same complete
        evidence reconstruction synchronously before the job can even be
        queued.  The session-root check remains eager so invalid or escaping
        keys never reach a worker; durable workflow evidence is then validated
        by the action itself inside the worker.
        """

        return ApplicationSession(self._session_root(session_key, must_exist=True))

    def session_view(
        self,
        session_key: str,
        *,
        selected_filter: str = "all",
    ) -> dict[str, object]:
        if selected_filter not in _FILTERS:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "unknown approval filter")
        view = render_session_view(
            self.load_session(session_key),
            session_key=session_key,
            csrf_token=self.csrf_token,
            selected_filter=selected_filter,
        )
        self.bind_support_action(
            view,
            session_key=session_key,
            stage="session_view",
        )
        with self._lock:
            saved_name = self._review_copy_notices.pop(session_key, None)
        if saved_name is not None:
            display_name = saved_name if len(saved_name) <= 120 else saved_name[:117] + "..."
            raw_notices = view.get("notices")
            notices = (
                list(cast("list[dict[str, str]]", raw_notices))
                if isinstance(raw_notices, list)
                else []
            )
            notices.insert(
                0,
                {
                    "tone": "success",
                    "title": "审阅 Word 副本已保存",
                    "message": (
                        f"已另存副本：{display_name}。"
                        "内部密封审阅稿保持不变，可继续导入修改后的 Word。"
                    ),
                },
            )
            view["notices"] = notices
        return view

    def delete_confirmation_view(self, session_key: str) -> dict[str, object]:
        """Describe an owned task without requiring its sealed evidence to load."""

        session_root = self._session_root(session_key, must_exist=True)
        try:
            modified = session_root.stat().st_mtime
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "review session cannot be inspected",
                details={"stage": "delete_session_confirm"},
            ) from exc
        try:
            status = ApplicationSession.load(session_root).status()
            main_document = cast("str", status["main_document"])
            phase = cast("str", status["phase"])
            name = Path(main_document).stem or "未命名论文"
            phase_label = _PHASE_LABELS.get(phase, ("需要检查", "danger"))[0]
        except (ContractError, OSError, KeyError, TypeError):
            name = f"无法读取的任务（…{session_key[-8:]}）"
            phase_label = "任务证据无法读取"
        active = self.jobs.active_for(session_key)
        return {
            "page": "delete_confirm",
            "step": 1,
            "project_name": name,
            "session_name": name,
            "status": phase_label,
            "updated_at": datetime.fromtimestamp(modified).astimezone().strftime("%Y-%m-%d %H:%M"),
            "can_delete": active is None,
            "delete_action": "/session/delete",
            "cancel_href": "/",
            "form_fields": {"csrf": self.csrf_token, "session": session_key},
        }

    def delete_session(self, session_key: str) -> None:
        """Delete exactly one app-owned task after a complete topology check."""

        with self._lock:
            session_root = self._session_root(session_key, must_exist=True)
            if self.jobs.active_for(session_key) is not None:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "an active review session cannot be deleted",
                )
            files, directories = _validated_delete_tree(session_root)
            if self.jobs.active_for(session_key) is not None:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "an active review session cannot be deleted",
                )
            _remove_validated_delete_tree(files, directories)

    def choose_returned_word(self, session_key: str) -> Path | None:
        # Validate only the local session boundary before opening the native
        # dialog.  The receive action independently reconstructs all sealed
        # evidence after the user has selected a file.
        self._session_root(session_key, must_exist=True)
        initial_directory = (
            self._last_return_directory
            or self._last_output_directory
            or self._last_source_directory
        )
        selected = self._word_picker(initial_directory)
        if selected is not None:
            self._last_return_directory = selected.parent
        return selected

    def save_review_copy(self, session_key: str) -> bool:
        """Choose and publish an editable copy without accepting a browser path."""

        self._session_root(session_key, must_exist=True)
        selected = self._review_copy_picker(self._last_output_directory)
        if selected is None:
            return False
        with self._lock:
            if self.jobs.active_for(session_key) is not None:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "an active review session cannot save a Word copy",
                )
            saved = self._action_session(session_key).save_review_copy(selected)
            self._last_output_directory = saved.parent
            self._review_copy_notices[session_key] = saved.name
        return True

    def _register_job(
        self,
        snapshot: JobSnapshot,
        *,
        success_href: str,
        retry_href: str,
        back_href: str,
        step: int,
    ) -> JobSnapshot:
        with self._lock:
            self._job_routes[snapshot.job_id] = _JobRoute(
                success_href,
                retry_href,
                back_href,
                step,
            )
        return snapshot

    def _submit_session_job(
        self,
        session_key: str,
        operation: str,
        task: Callable[[ProgressReporter], Mapping[str, Any]],
    ) -> JobSnapshot:
        """Reserve a session writer while holding the deletion coordination lock."""

        with self._lock:
            return self.jobs.submit(session_key, operation, task)

    def job_route(self, job_id: str) -> _JobRoute:
        if _JOB_KEY_RE.fullmatch(job_id) is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid job key")
        with self._lock:
            route = self._job_routes.get(job_id)
        if route is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "job route is unavailable")
        return route

    def submit_new_export(self, selection_token: str) -> JobSnapshot:
        selected = self.selected_project(selection_token)
        session_key = f"session_{secrets.token_hex(16)}"
        run_root = self._session_root(session_key, must_exist=False)

        def task(progress: ProgressReporter) -> Mapping[str, Any]:
            progress.update(10, "正在创建论文只读副本")
            session = ApplicationSession.create(
                selected.main_file.parent,
                run_root,
                main_document=selected.main_file.name,
            )
            progress.update(50, "正在处理论文图片并生成审阅 Word")
            status = session.export_review(timeout_s=_EXPORT_TIMEOUT_SECONDS)
            return {"session_key": session_key, "status": status}

        snapshot = self._submit_session_job(session_key, "export_review", task)
        return self._register_job(
            snapshot,
            success_href=f"/session/{session_key}",
            retry_href=f"/preflight/{selection_token}",
            back_href="/",
            step=1,
        )

    def submit_existing_export(self, session_key: str) -> JobSnapshot:
        session = self._action_session(session_key)

        def task(progress: ProgressReporter) -> Mapping[str, Any]:
            progress.update(20, "正在复核已有任务证据")
            progress.update(50, "正在处理论文图片并生成审阅 Word")
            status = session.export_review(timeout_s=_EXPORT_TIMEOUT_SECONDS)
            return {"session_key": session_key, "status": status}

        snapshot = self._submit_session_job(session_key, "export_review", task)
        return self._register_job(
            snapshot,
            success_href=f"/session/{session_key}",
            retry_href=f"/session/{session_key}",
            back_href="/",
            step=1,
        )

    def submit_receive(self, session_key: str, returned_docx: Path) -> JobSnapshot:
        session = self._action_session(session_key)

        def task(progress: ProgressReporter) -> Mapping[str, Any]:
            progress.update(15, "正在只读归档返回 Word")
            session.receive_review(returned_docx)
            progress.update(75, "正在建立逐项审批清单")
            status = session.begin_approval(actor_id="local-author", actor_name="本机论文作者")
            return {"session_key": session_key, "status": status}

        snapshot = self._submit_session_job(session_key, "receive_review", task)
        return self._register_job(
            snapshot,
            success_href=f"/session/{session_key}",
            retry_href=f"/session/{session_key}",
            back_href=f"/session/{session_key}",
            step=2,
        )

    def submit_finalize(self, session_key: str) -> JobSnapshot:
        session = self._action_session(session_key)

        def task(progress: ProgressReporter) -> Mapping[str, Any]:
            progress.update(25, "正在密封审批决定")
            status = session.finalize_approval()
            return {"session_key": session_key, "status": status}

        snapshot = self._submit_session_job(session_key, "finalize_approval", task)
        return self._register_job(
            snapshot,
            success_href=f"/session/{session_key}",
            retry_href=f"/session/{session_key}",
            back_href=f"/session/{session_key}",
            step=3,
        )

    def submit_revise_approval(
        self,
        session_key: str,
        patch_plan_sha256: str,
    ) -> JobSnapshot:
        session = self._action_session(session_key)

        def task(progress: ProgressReporter) -> Mapping[str, Any]:
            progress.update(25, "正在复核阻断计划")
            status = session.revise_blocked_approval(
                patch_plan_sha256=patch_plan_sha256,
            )
            return {"session_key": session_key, "status": status}

        snapshot = self._submit_session_job(session_key, "revise_blocked_approval", task)
        return self._register_job(
            snapshot,
            success_href=f"/session/{session_key}",
            retry_href=f"/session/{session_key}",
            back_href=f"/session/{session_key}",
            step=3,
        )

    def submit_begin_approval(self, session_key: str) -> JobSnapshot:
        session = self._action_session(session_key)

        def task(progress: ProgressReporter) -> Mapping[str, Any]:
            progress.update(25, "正在复核返回 Word 证据")
            status = session.begin_approval(
                actor_id="local-author",
                actor_name="本机论文作者",
            )
            return {"session_key": session_key, "status": status}

        snapshot = self._submit_session_job(session_key, "begin_approval", task)
        return self._register_job(
            snapshot,
            success_href=f"/session/{session_key}",
            retry_href=f"/session/{session_key}",
            back_href=f"/session/{session_key}",
            step=3,
        )

    def submit_prepare_plan(self, session_key: str) -> JobSnapshot:
        session = self._action_session(session_key)

        def task(progress: ProgressReporter) -> Mapping[str, Any]:
            progress.update(25, "正在复核密封审批")
            status = session.prepare_plan()
            return {"session_key": session_key, "status": status}

        snapshot = self._submit_session_job(session_key, "prepare_plan", task)
        return self._register_job(
            snapshot,
            success_href=f"/session/{session_key}",
            retry_href=f"/session/{session_key}",
            back_href=f"/session/{session_key}",
            step=3,
        )

    def submit_generate(self, session_key: str, patch_plan_sha256: str) -> JobSnapshot:
        session = self._action_session(session_key)

        def task(progress: ProgressReporter) -> Mapping[str, Any]:
            progress.update(10, "正在复核第二道安全闸门")
            progress.update(30, "正在生成新的 LaTeX 副本")
            status = session.generate_results(patch_plan_sha256=patch_plan_sha256)
            return {"session_key": session_key, "status": status}

        snapshot = self._submit_session_job(session_key, "generate_results", task)
        return self._register_job(
            snapshot,
            success_href=f"/session/{session_key}",
            retry_href=f"/session/{session_key}",
            back_href=f"/session/{session_key}",
            step=4,
        )

    def submit_retry(self, session_key: str) -> JobSnapshot:
        session = self._action_session(session_key)

        def task(progress: ProgressReporter) -> Mapping[str, Any]:
            progress.update(15, "正在检查可恢复阶段")
            # Phase selection is deliberately inside the background job so a
            # full evidence reconstruction never delays the redirect to the
            # visible progress page.
            phase = cast("str", session.status()["phase"])
            if phase == "applied":
                status = session.verify_results()
                progress.update(70, "正在整理核验结果")
            elif phase == "partially_completed":
                progress.update(30, "正在创建新的不可变核验尝试")
                status = session.retry_verification()
                progress.update(70, "已保留本次核验证据")
            elif phase in {"verified", "ready_to_bundle"}:
                status = session.status()
            else:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "this session has nothing to retry",
                )

            current_phase = cast("str", status["phase"])
            if current_phase == "partially_completed":
                return {"session_key": session_key, "status": status}
            if current_phase == "verified":
                progress.update(80, "正在生成审阅账本")
                status = session.build_ledger()
                current_phase = cast("str", status["phase"])
            if current_phase == "ready_to_bundle":
                progress.update(90, "正在生成审计包")
                status = session.create_bundle()
            return {"session_key": session_key, "status": status}

        snapshot = self._submit_session_job(session_key, "retry_results", task)
        return self._register_job(
            snapshot,
            success_href=f"/session/{session_key}",
            retry_href=f"/session/{session_key}",
            back_href=f"/session/{session_key}",
            step=4,
        )

    def decide(
        self,
        session_key: str,
        *,
        change_id: str,
        decision: str,
        final_text: str,
        reason: str,
        risk_acknowledgement: str,
    ) -> None:
        if decision not in _DECISIONS:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "unknown approval decision")
        self._action_session(session_key).decide_without_status(
            change_id=change_id,
            decision=cast("Any", decision),
            final_text=final_text if decision == "accepted_with_edit" else None,
            reason=reason or None,
            risk_acknowledgement=risk_acknowledgement or None,
        )

    def accept_all_safe(self, session_key: str) -> None:
        self._action_session(session_key).decide_bulk_without_status(operation="accept_all_safe")

    def _artifact_path(self, session_key: str, artifact_key: str) -> Path:
        if _ARTIFACT_KEY_RE.fullmatch(artifact_key) is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "invalid artifact key")
        session = self.load_session(session_key)
        status = session.status()
        artifacts = cast("Mapping[str, object]", status["artifacts"])
        relative_value = artifacts.get(artifact_key)
        if not isinstance(relative_value, str):
            raise ContractError(ErrorCode.SCHEMA_INVALID, "artifact is unavailable")
        relative = validate_relative_path(relative_value)
        return resolve_within(session.run_root, relative)

    def open_review_docx(self, session_key: str) -> None:
        path = self._artifact_path(session_key, "review_docx")
        if _is_link_or_junction(path) or not path.is_file():
            raise ContractError(ErrorCode.SCHEMA_INVALID, "review Word is unavailable")
        self._path_opener(path)

    def open_results_folder(self, session_key: str) -> None:
        session = self.load_session(session_key)
        delivery = session.run_root / "delivery"
        target = delivery if delivery.exists() else session.run_root
        self._path_opener(_require_real_directory(target, "results folder"))

    def downloadable_artifact(self, session_key: str, artifact_key: str) -> tuple[Path, str]:
        if artifact_key not in _DOWNLOADABLE_ARTIFACTS:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "artifact is not downloadable")
        path = self._artifact_path(session_key, artifact_key)
        if _is_link_or_junction(path) or not path.is_file():
            raise ContractError(ErrorCode.SCHEMA_INVALID, "artifact file is unavailable")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "artifact cannot be inspected") from exc
        if size > MAX_ARTIFACT_BYTES:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "artifact exceeds download limit")
        media_types = {
            ".html": "text/html; charset=utf-8",
            ".json": "application/json",
            ".pdf": "application/pdf",
            ".zip": "application/zip",
        }
        return path, media_types.get(path.suffix.casefold(), "application/octet-stream")


def _error_view(
    error: ContractError | ErrorCode | str,
    *,
    step: int = 1,
    csrf_token: str | None = None,
    session_key: str | None = None,
    retry_href: str | None = None,
    support_token: str | None = None,
    back_href: str = "/",
    diagnostic_details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    details = error.violation.details if isinstance(error, ContractError) else diagnostic_details
    message = user_message_for_error(
        error.code if isinstance(error, ContractError) else error,
        technical_message=(error.code.value if isinstance(error, ContractError) else None),
    )
    primary_action = build_error_recovery_control(
        message.action,
        message.action_label,
        csrf_token=csrf_token,
        session_key=session_key,
        retry_href=retry_href,
        support_token=support_token,
    )
    card: dict[str, object] = {
        "code": message.code,
        "title": message.title,
        "message": f"{message.explanation} {message.safety}",
        "guidance": [message.action_label],
        "technical_details": format_safe_technical_details(message.code, details),
        "back_href": back_href,
    }
    if primary_action is not None:
        card["primary_action"] = primary_action
    return {"page": "error", "step": step, "error": card}


def _job_view(snapshot: JobSnapshot) -> dict[str, object]:
    view: dict[str, object] = {
        "page": "progress",
        "step": 1,
        "progress": snapshot.progress,
        "stage": snapshot.stage,
        "message": "论文只在本机处理；完成后会自动进入下一步。",
        "refresh_href": f"/jobs/{snapshot.job_id}",
    }
    if snapshot.operation == "export_review":
        view.update(
            {
                "progress_indeterminate": True,
                "message": "页面会自动刷新；保持此页面打开或切到其他窗口都不会中断处理。",
                "duration_hint": ("少量图片通常很快；包含大量 PDF 图片的论文通常约需 1–2 分钟。"),
                "progress_note": (
                    "这里显示真实处理阶段，不按图片数量伪造百分比；"
                    "阶段文字暂时不变并不表示程序卡死。"
                ),
            }
        )
    return view


class AppHTTPServer(ThreadingHTTPServer):
    """Threaded server bound to one exact IPv4 loopback origin."""

    daemon_threads = True
    block_on_close = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], state: AppState) -> None:
        self.app_state = state
        self._state_closed = False
        super().__init__(address, AppRequestHandler)

    @property
    def expected_host(self) -> str:
        host, port = cast("tuple[str, int]", self.server_address)
        return f"{host}:{port}"

    @property
    def origin(self) -> str:
        return f"http://{self.expected_host}"

    @property
    def session_cookie_name(self) -> str:
        """Return a port-scoped cookie name so parallel local instances do not collide."""

        _host, port = cast("tuple[str, int]", self.server_address)
        return f"{SESSION_COOKIE}_{port}"

    def shutdown_request(self, request: object) -> None:
        request_socket = cast("socket.socket", request)
        with suppress(OSError):
            request_socket.shutdown(socket.SHUT_WR)
        try:
            request_socket.settimeout(_GRACEFUL_DRAIN_TIMEOUT_SECONDS)
            remaining = _GRACEFUL_DRAIN_BYTES
            while remaining:
                chunk = request_socket.recv(min(_GRACEFUL_DRAIN_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.close_request(request_socket)

    def server_close(self) -> None:
        super().server_close()
        if not self._state_closed:
            self._state_closed = True
            self.app_state.close()

    def handle_error(self, request: object, client_address: object) -> None:
        """Never write tracebacks to a missing console in the windowed build."""

        del request, client_address


class AppRequestHandler(BaseHTTPRequestHandler):
    """Strict request handler with no arbitrary filesystem endpoint."""

    server_version = "latex-word-review-app"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    _response_started = False

    @property
    def app_server(self) -> AppHTTPServer:
        return cast("AppHTTPServer", self.server)

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        set_cookie: bool = False,
        location: str | None = None,
        refresh: str | None = None,
        download_name: str | None = None,
    ) -> None:
        disposition: str | None = None
        if download_name is not None:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", download_name) is None:
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "download filename is not a fixed safe token",
                )
            disposition = 'attachment; filename="' + download_name + '"'
        if self._response_started:
            self.close_connection = True
            return
        self._response_started = True
        self.close_connection = True
        try:
            self.send_response(status)
            for name, value in _SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if disposition is not None:
                self.send_header("Content-Disposition", disposition)
            if set_cookie:
                token = self.app_server.app_state.session_token
                self.send_header(
                    "Set-Cookie",
                    f"{self.app_server.session_cookie_name}={token}; "
                    "Path=/; HttpOnly; SameSite=Strict",
                )
            if location is not None:
                self.send_header("Location", location)
            if refresh is not None:
                self.send_header("Refresh", refresh)
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def _send_page(
        self,
        status: HTTPStatus,
        view: Mapping[str, object],
        *,
        set_cookie: bool = False,
        refresh: str | None = None,
    ) -> None:
        body = render_app_page(view).encode("utf-8")
        self._send(status, body, set_cookie=set_cookie, refresh=refresh)

    def _send_error(
        self,
        status: HTTPStatus,
        error: ContractError | ErrorCode | str,
        *,
        step: int = 1,
        session_key: str | None = None,
        retry_href: str | None = None,
        back_href: str = "/",
    ) -> None:
        csrf_token = self.app_server.app_state.csrf_token if self._cookie_valid() else None
        support_token: str | None = None
        if csrf_token is not None and session_key is not None:
            normalized_error = error.code if isinstance(error, ContractError) else error
            message = user_message_for_error(normalized_error)
            support_token = self.app_server.app_state.issue_support_token(
                session_key,
                message.code,
                "request_dispatch",
            )
        self._send_page(
            status,
            _error_view(
                error,
                step=step,
                csrf_token=csrf_token,
                session_key=session_key,
                retry_href=retry_href,
                support_token=support_token,
                back_href=back_href,
            ),
        )

    def _redirect(self, location: str, *, set_cookie: bool = False) -> None:
        if not location.startswith("/") or location.startswith("//"):
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "unsafe redirect target")
        self._send(HTTPStatus.SEE_OTHER, b"", location=location, set_cookie=set_cookie)

    def _host_valid(self) -> bool:
        hosts = self.headers.get_all("Host", failobj=[])
        return len(hosts) == 1 and secrets.compare_digest(hosts[0], self.app_server.expected_host)

    def _request_target(self) -> tuple[str, str] | None:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return None
        if "%" in parsed.path or chr(92) in parsed.path or ".." in parsed.path:
            return None
        return parsed.path, parsed.query

    def _cookie_valid(self) -> bool:
        headers = self.headers.get_all("Cookie", failobj=[])
        if len(headers) != 1:
            return False
        values: list[str] = []
        for item in headers[0].split(";"):
            if "=" not in item:
                return False
            name, value = (part.strip() for part in item.split("=", 1))
            if name == self.app_server.session_cookie_name:
                values.append(value)
        return len(values) == 1 and secrets.compare_digest(
            values[0], self.app_server.app_state.session_token
        )

    def _post_origin_valid(self) -> bool:
        origins = self.headers.get_all("Origin", failobj=[])
        if len(origins) != 1:
            return False
        fetch_sites = self.headers.get_all("Sec-Fetch-Site", failobj=[])
        if fetch_sites and not (len(fetch_sites) == 1 and fetch_sites[0] == "same-origin"):
            return False
        if secrets.compare_digest(origins[0], self.app_server.origin):
            return True
        # Some embedded Chromium surfaces serialize a loopback form's opaque
        # origin as "null" even though Fetch Metadata still proves that the
        # request is same-origin. Requiring both this exact value and an exact
        # same-origin Fetch-Site signal preserves the Cookie + CSRF gates while
        # allowing those local browser shells.
        return origins[0] == "null" and fetch_sites == ["same-origin"]

    @staticmethod
    def _percent_encoding_valid(value: str) -> bool:
        index = 0
        while index < len(value):
            if value[index] == "%":
                if index + 2 >= len(value) or any(
                    character not in _HEX for character in value[index + 1 : index + 3]
                ):
                    return False
                index += 3
            else:
                index += 1
        return True

    def _query(self, encoded: str, expected_fields: set[str]) -> dict[str, str] | None:
        if not encoded:
            return {} if not expected_fields else None
        if not encoded.isascii() or not self._percent_encoding_valid(encoded):
            return None
        try:
            pairs = parse_qsl(
                encoded,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=2,
                encoding="utf-8",
                errors="strict",
            )
        except (UnicodeDecodeError, ValueError):
            return None
        fields: dict[str, str] = {}
        for name, value in pairs:
            if name in fields:
                return None
            fields[name] = value
        return fields if set(fields) == expected_fields else None

    def _form(self, expected_fields: set[str]) -> dict[str, str] | None:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.SCHEMA_INVALID)
            return None
        if self.headers.get_all("Content-Type", failobj=[]) != [
            "application/x-www-form-urlencoded"
        ]:
            self._send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, ErrorCode.SCHEMA_INVALID)
            return None
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 10:
            self._send_error(HTTPStatus.LENGTH_REQUIRED, ErrorCode.SCHEMA_INVALID)
            return None
        length = int(lengths[0])
        if length > MAX_FORM_BYTES:
            self._send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, ErrorCode.SCHEMA_INVALID)
            return None
        raw = self.rfile.read(length)
        try:
            encoded = raw.decode("ascii", errors="strict")
        except UnicodeDecodeError:
            self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.SCHEMA_INVALID)
            return None
        if not self._percent_encoding_valid(encoded):
            self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.SCHEMA_INVALID)
            return None
        try:
            pairs = parse_qsl(
                encoded,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=_MAX_FIELDS,
                encoding="utf-8",
                errors="strict",
            )
        except (UnicodeDecodeError, ValueError):
            self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.SCHEMA_INVALID)
            return None
        fields: dict[str, str] = {}
        for name, value in pairs:
            if name in fields:
                self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.SCHEMA_INVALID)
                return None
            fields[name] = value
        oversized = any(len(value) > 16 * 1024 for value in fields.values())
        if set(fields) != expected_fields or oversized:
            self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.SCHEMA_INVALID)
            return None
        return fields

    def _csrf_valid(self, form: Mapping[str, str]) -> bool:
        return secrets.compare_digest(form["csrf"], self.app_server.app_state.csrf_token)

    def _send_artifact(
        self,
        path: Path,
        content_type: str,
        *,
        download_name: str | None = None,
    ) -> None:
        disposition = "attachment"
        if download_name is not None:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", download_name) is None:
                raise ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "download filename is not a fixed safe token",
                )
            disposition += f'; filename="{download_name}"'
        try:
            size = path.stat().st_size
            stream = path.open("rb")
        except OSError as exc:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "artifact cannot be opened") from exc
        if self._response_started:
            stream.close()
            self.close_connection = True
            return
        self._response_started = True
        self.close_connection = True
        try:
            self.send_response(HTTPStatus.OK)
            for name, value in _SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(size))
            self.send_header("Connection", "close")
            self.end_headers()
            with stream:
                while chunk := stream.read(64 * 1024):
                    self.wfile.write(chunk)
        except OSError:
            stream.close()
            self.close_connection = True

    def _request_app_shutdown(self) -> None:
        waiting_for_jobs = self.app_server.app_state.jobs.has_active_jobs()
        self._send_page(
            HTTPStatus.OK,
            {
                "page": "shutdown",
                "waiting_for_jobs": waiting_for_jobs,
            },
        )
        worker = threading.Thread(
            target=self.app_server.shutdown,
            name="latex-word-review-shutdown",
            daemon=True,
        )
        worker.start()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._host_valid():
            self._send_error(HTTPStatus.MISDIRECTED_REQUEST, ErrorCode.SCHEMA_INVALID)
            return
        target = self._request_target()
        if target is None:
            self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.PATH_TRAVERSAL)
            return
        path, query = target
        request_session_key: str | None = None
        try:
            if path == APP_STYLESHEET_PATH and not query:
                css = (
                    importlib.resources.files("latex_word_review")
                    .joinpath("assets", "app.css")
                    .read_bytes()
                )
                self._send(HTTPStatus.OK, css, content_type="text/css; charset=utf-8")
                return
            if path == "/" and not query:
                self._send_page(
                    HTTPStatus.OK,
                    self.app_server.app_state.home_view(),
                    set_cookie=True,
                )
                return
            if not self._cookie_valid():
                self._send_error(HTTPStatus.FORBIDDEN, ErrorCode.SCHEMA_INVALID)
                return
            preflight_match = re.fullmatch(r"/preflight/(selection_[0-9a-f]{32})", path)
            delete_match = re.fullmatch(r"/session/(session_[0-9a-f]{32})/delete", path)
            session_match = re.fullmatch(r"/session/(session_[0-9a-f]{32})", path)
            job_match = re.fullmatch(r"/jobs/(job_[0-9a-f]{32})", path)
            artifact_match = re.fullmatch(
                r"/artifact/(session_[0-9a-f]{32})/([a-z][a-z0-9_]{0,63})",
                path,
            )
            support_match = re.fullmatch(r"/support/(support_[0-9a-f]{32})", path)
            if preflight_match is not None and not query:
                self._send_page(
                    HTTPStatus.OK,
                    self.app_server.app_state.preflight_view(preflight_match.group(1)),
                )
                return
            if delete_match is not None and not query:
                request_session_key = delete_match.group(1)
                self._send_page(
                    HTTPStatus.OK,
                    self.app_server.app_state.delete_confirmation_view(request_session_key),
                )
                return
            if session_match is not None:
                request_session_key = session_match.group(1)
                fields = self._query(query, {"filter"} if query else set())
                if fields is None:
                    self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.SCHEMA_INVALID)
                    return
                selected_filter = fields.get("filter", "all")
                self._send_page(
                    HTTPStatus.OK,
                    self.app_server.app_state.session_view(
                        request_session_key,
                        selected_filter=selected_filter,
                    ),
                )
                return
            if job_match is not None and not query:
                job_id = job_match.group(1)
                snapshot = self.app_server.app_state.jobs.snapshot(job_id)
                route = self.app_server.app_state.job_route(job_id)
                if snapshot.status == "succeeded":
                    self._redirect(route.success_href)
                    return
                if snapshot.status == "failed":
                    code = (
                        cast("str", snapshot.error["code"])
                        if snapshot.error is not None
                        else ErrorCode.INTERNAL_INVARIANT.value
                    )
                    diagnostics = (
                        snapshot.error.get("diagnostics") if snapshot.error is not None else None
                    )
                    self._send_page(
                        HTTPStatus.CONFLICT,
                        _error_view(
                            code,
                            step=route.step,
                            csrf_token=self.app_server.app_state.csrf_token,
                            session_key=snapshot.session_key,
                            retry_href=route.retry_href,
                            support_token=self.app_server.app_state.issue_support_token(
                                snapshot.session_key,
                                code,
                                snapshot.operation,
                            ),
                            back_href=route.back_href,
                            diagnostic_details=(
                                cast("Mapping[str, object]", diagnostics)
                                if isinstance(diagnostics, Mapping)
                                else None
                            ),
                        ),
                    )
                    return
                view = _job_view(snapshot)
                view["step"] = route.step
                self._send_page(
                    HTTPStatus.OK,
                    view,
                    refresh=f"1; url=/jobs/{job_id}",
                )
                return
            if artifact_match is not None and not query:
                request_session_key = artifact_match.group(1)
                artifact, content_type = self.app_server.app_state.downloadable_artifact(
                    request_session_key,
                    artifact_match.group(2),
                )
                self._send_artifact(artifact, content_type)
                return
            if support_match is not None and not query:
                support_bytes = self.app_server.app_state.claim_support_download(
                    support_match.group(1)
                )
                self._send(
                    HTTPStatus.OK,
                    support_bytes,
                    content_type="application/zip",
                    download_name=SUPPORT_BUNDLE_FILENAME,
                )
                return
            self._send_error(HTTPStatus.NOT_FOUND, ErrorCode.SCHEMA_INVALID)
        except ContractError as exc:
            self._send_error(
                HTTPStatus.CONFLICT,
                exc,
                session_key=request_session_key,
            )
        except Exception:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                ErrorCode.INTERNAL_INVARIANT,
                session_key=request_session_key,
            )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._host_valid():
            self._send_error(HTTPStatus.MISDIRECTED_REQUEST, ErrorCode.SCHEMA_INVALID)
            return
        if not self._post_origin_valid():
            self._send_error(HTTPStatus.FORBIDDEN, ErrorCode.SCHEMA_INVALID)
            return
        if not self._cookie_valid():
            # A stale local tab or a pre-fix shared cookie must not replay the
            # requested action. Refresh only the per-instance cookie and send
            # the user back to the safe home page.
            self._redirect("/", set_cookie=True)
            return
        target = self._request_target()
        if target is None or target[1]:
            self._send_error(HTTPStatus.BAD_REQUEST, ErrorCode.PATH_TRAVERSAL)
            return
        path = target[0]
        expected_by_path = {
            "/new": {"csrf"},
            "/app/exit": {"csrf"},
            "/session/export": {"csrf", "selection"},
            "/session/export-existing": {"csrf", "session"},
            "/session/open-review-docx": {"csrf", "session"},
            "/session/save-review-copy": {"csrf", "session"},
            "/session/receive": {"csrf", "session"},
            "/session/delete": {"csrf", "session", "confirm_delete"},
            "/approval/decision": {
                "csrf",
                "session",
                "change_id",
                "decision",
                "final_text",
                "reason",
                "risk_acknowledgement",
            },
            "/approval/accept-safe": {"csrf", "session"},
            "/approval/start": {"csrf", "session"},
            "/approval/revise": {"csrf", "session", "patch_plan_sha256"},
            "/approval/finalize": {"csrf", "session"},
            "/patch/prepare": {"csrf", "session"},
            "/result/generate": {
                "csrf",
                "session",
                "patch_plan_sha256",
                "confirm_apply",
            },
            "/result/open-folder": {"csrf", "session"},
            "/result/retry": {"csrf", "session"},
            "/support/export": {"csrf", "session", "support_token"},
        }
        expected = expected_by_path.get(path)
        if expected is None:
            self._send_error(HTTPStatus.NOT_FOUND, ErrorCode.SCHEMA_INVALID)
            return
        form = self._form(expected)
        if form is None:
            return
        if not self._csrf_valid(form):
            # A page restored from browser history can carry the previous
            # form token even though this instance's cookie is already
            # current. Never replay the submitted action: refresh the local
            # session cookie and render a fresh form instead.
            self._redirect("/", set_cookie=True)
            return
        state = self.app_server.app_state
        session_key: str | None = None
        try:
            if path == "/new":
                selected = state.select_project()
                self._redirect("/" if selected is None else f"/preflight/{selected.token}")
                return
            if path == "/app/exit":
                self._request_app_shutdown()
                return
            if path == "/session/export":
                job = state.submit_new_export(form["selection"])
                self._redirect(f"/jobs/{job.job_id}")
                return
            session_key = form.get("session")
            if session_key is None:
                raise ContractError(ErrorCode.SCHEMA_INVALID, "session key is required")
            session_href = f"/session/{session_key}"
            if path == "/session/delete":
                if form["confirm_delete"] != "yes":
                    raise ContractError(ErrorCode.SCHEMA_INVALID, "delete confirmation is required")
                state.delete_session(session_key)
                self._redirect("/")
                return
            if path == "/support/export":
                download_key = state.create_support_download(
                    session_key,
                    form["support_token"],
                )
                self._redirect(f"/support/{download_key}")
                return
            if path == "/session/export-existing":
                job = state.submit_existing_export(session_key)
                self._redirect(f"/jobs/{job.job_id}")
                return
            if path == "/session/open-review-docx":
                state.open_review_docx(session_key)
                self._redirect(session_href)
                return
            if path == "/session/save-review-copy":
                state.save_review_copy(session_key)
                self._redirect(session_href)
                return
            if path == "/session/receive":
                returned = state.choose_returned_word(session_key)
                if returned is None:
                    self._redirect(session_href)
                    return
                job = state.submit_receive(session_key, returned)
                self._redirect(f"/jobs/{job.job_id}")
                return
            if path == "/approval/decision":
                state.decide(
                    session_key,
                    change_id=form["change_id"],
                    decision=form["decision"],
                    final_text=form["final_text"],
                    reason=form["reason"],
                    risk_acknowledgement=form["risk_acknowledgement"],
                )
                self._redirect(session_href)
                return
            if path == "/approval/start":
                job = state.submit_begin_approval(session_key)
                self._redirect(f"/jobs/{job.job_id}")
                return
            if path == "/approval/revise":
                job = state.submit_revise_approval(
                    session_key,
                    form["patch_plan_sha256"],
                )
                self._redirect(f"/jobs/{job.job_id}")
                return
            if path == "/approval/accept-safe":
                state.accept_all_safe(session_key)
                self._redirect(session_href)
                return
            if path == "/approval/finalize":
                job = state.submit_finalize(session_key)
                self._redirect(f"/jobs/{job.job_id}")
                return
            if path == "/patch/prepare":
                job = state.submit_prepare_plan(session_key)
                self._redirect(f"/jobs/{job.job_id}")
                return
            if path == "/result/generate":
                if form["confirm_apply"] != "yes":
                    raise ContractError(ErrorCode.SCHEMA_INVALID, "apply confirmation is required")
                job = state.submit_generate(session_key, form["patch_plan_sha256"])
                self._redirect(f"/jobs/{job.job_id}")
                return
            if path == "/result/open-folder":
                state.open_results_folder(session_key)
                self._redirect(session_href)
                return
            if path == "/result/retry":
                job = state.submit_retry(session_key)
                self._redirect(f"/jobs/{job.job_id}")
                return
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "unhandled application action")
        except ContractError as exc:
            status = (
                HTTPStatus.CONFLICT
                if path == "/session/delete"
                or exc.code
                in {
                    ErrorCode.HASH_APPROVAL_MISMATCH,
                    ErrorCode.HASH_PATCHPLAN_MISMATCH,
                    ErrorCode.PATCH_SOURCE_DRIFT,
                    ErrorCode.APPROVAL_NOT_FINAL,
                }
                else HTTPStatus.BAD_REQUEST
            )
            back_href = (
                "/"
                if path == "/session/delete"
                else (session_href if "session_href" in locals() else "/")
            )
            self._send_error(
                status,
                exc,
                session_key=(
                    session_key
                    if session_key is not None
                    and _SESSION_KEY_RE.fullmatch(session_key) is not None
                    else None
                ),
                retry_href=(
                    f"{session_href}/delete"
                    if path == "/session/delete"
                    else (session_href if "session_href" in locals() else None)
                ),
                back_href=back_href,
            )
        except Exception as exc:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                ContractError(
                    ErrorCode.INTERNAL_INVARIANT,
                    "unexpected application request failure",
                    details={
                        "stage": "request_dispatch",
                        "exception_type": type(exc).__name__,
                    },
                ),
                session_key=(
                    session_key
                    if session_key is not None
                    and _SESSION_KEY_RE.fullmatch(session_key) is not None
                    else None
                ),
                back_href="/",
            )

    def _method_not_allowed(self) -> None:
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, ErrorCode.SCHEMA_INVALID)

    do_DELETE = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_PUT = _method_not_allowed


def create_app_server(
    data_root: Path,
    *,
    host: str = LOOPBACK_HOST,
    port: int = 0,
    main_picker: PathPicker = _default_main_picker,
    word_picker: PathPicker = _default_word_picker,
    review_copy_picker: PathPicker = _default_review_copy_picker,
    path_opener: PathOpener = _default_path_opener,
) -> AppHTTPServer:
    """Create, but do not start, the fixed-origin local application server."""

    if host != LOOPBACK_HOST:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "application server may bind only to 127.0.0.1",
        )
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "application server port is invalid")
    state = AppState(
        data_root,
        main_picker=main_picker,
        word_picker=word_picker,
        review_copy_picker=review_copy_picker,
        path_opener=path_opener,
    )
    try:
        return AppHTTPServer((LOOPBACK_HOST, port), state)
    except OSError as exc:
        state.close()
        raise ContractError(ErrorCode.SCHEMA_INVALID, "application server could not bind") from exc


def serve_app(
    data_root: Path | None = None,
    *,
    port: int = 0,
    launch_browser: bool = True,
    browser_opener: BrowserOpener = webbrowser.open,
    browser_failure_notifier: BrowserFailureNotifier = _default_browser_failure_notifier,
) -> None:
    """Serve until interrupted and optionally open the user's default browser."""

    server = create_app_server(data_root or default_app_data_root(), port=port)
    url = server.origin + "/"
    if launch_browser:
        timer = threading.Timer(
            0.25,
            _open_browser_or_notify,
            args=(url, browser_opener, browser_failure_notifier),
        )
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


__all__ = [
    "AppHTTPServer",
    "AppState",
    "LOOPBACK_HOST",
    "MAX_ARTIFACT_BYTES",
    "MAX_FORM_BYTES",
    "SESSION_COOKIE",
    "SelectedProject",
    "create_app_server",
    "default_app_data_root",
    "serve_app",
]
