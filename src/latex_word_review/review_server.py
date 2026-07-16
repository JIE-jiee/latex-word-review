"""Loopback-only browser UI for immutable, approval-only review decisions."""

from __future__ import annotations

import copy
import html
import json
import re
import secrets
import socket
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import parse_qsl, urlsplit

from latex_word_review.approval import (
    Decision,
    finalize_approval_set,
    record_decision,
    write_approval_json,
)
from latex_word_review.contracts import compute_payload_sha256, validate_contract
from latex_word_review.errors import ContractError, ErrorCode

LOOPBACK_HOST: Final = "127.0.0.1"
SESSION_COOKIE: Final = "lwr_review_session"
MAX_REQUEST_BYTES: Final = 32 * 1024
_MAX_FIELDS: Final = 8
_GRACEFUL_DRAIN_BYTES: Final = MAX_REQUEST_BYTES + 1
_GRACEFUL_DRAIN_CHUNK_BYTES: Final = 8 * 1024
_GRACEFUL_DRAIN_TIMEOUT_SECONDS: Final = 0.25
_CHANGE_PATH_RE: Final = re.compile(r"^/change/(chg_[a-z0-9]{26,64})$")
_HEX: Final = frozenset("0123456789abcdefABCDEF")
_DECISIONS: Final = {
    "accepted",
    "accepted_with_edit",
    "rejected",
    "manual",
    "conflict",
}
_SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
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


def _escape(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        rendered = str(value)
    return html.escape(rendered, quote=True)


def _validate_initial_state(changeset: Mapping[str, Any], approval: Mapping[str, Any]) -> None:
    changeset_receipt = validate_contract(changeset)
    approval_receipt = validate_contract(approval)
    if changeset_receipt.schema_name != "ChangeSet":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "review input must be a ChangeSet")
    if approval_receipt.schema_name != "ApprovalSet":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "review state must be an ApprovalSet")
    changeset_payload = cast("Mapping[str, Any]", changeset["payload"])
    approval_payload = cast("Mapping[str, Any]", approval["payload"])
    if approval_payload["changeset_sha256"] != changeset_receipt.payload_sha256:
        raise ContractError(
            ErrorCode.HASH_CHANGESET_MISMATCH,
            "ApprovalSet is bound to a different ChangeSet",
        )
    if approval_payload["source_manifest_sha256"] != changeset_payload["source_manifest_sha256"]:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "source bindings differ")
    if changeset["run_id"] != approval["run_id"]:
        raise ContractError(ErrorCode.HASH_CHANGESET_MISMATCH, "run bindings differ")

    changes = cast("Sequence[Mapping[str, Any]]", changeset_payload["changes"])
    change_index = {cast("str", item["change_id"]): item for item in changes}
    decisions = cast("Sequence[Mapping[str, Any]]", approval_payload["decisions"])
    decision_ids = tuple(cast("str", item["change_id"]) for item in decisions)
    undecided_ids = tuple(cast("Sequence[str]", approval_payload["undecided_change_ids"]))
    expected_decided = tuple(
        item["change_id"] for item in changes if item["change_id"] in decision_ids
    )
    expected_undecided = tuple(
        cast("str", item["change_id"]) for item in changes if item["change_id"] not in decision_ids
    )
    if decision_ids != expected_decided or undecided_ids != expected_undecided:
        raise ContractError(
            ErrorCode.APPROVAL_CHANGE_UNKNOWN,
            "ApprovalSet must partition ChangeSet changes in source order",
        )
    for decision in decisions:
        change_id = cast("str", decision["change_id"])
        change = change_index.get(change_id)
        if change is None or decision["change_fingerprint"] != change["change_fingerprint"]:
            raise ContractError(
                ErrorCode.HASH_CHANGESET_MISMATCH,
                "approval decision fingerprint differs from ChangeSet",
            )


class _ReviewState:
    def __init__(
        self,
        changeset: Mapping[str, Any],
        approval: Mapping[str, Any],
        approval_output_paths: Mapping[int, str | Path],
    ) -> None:
        _validate_initial_state(changeset, approval)
        self.changeset = copy.deepcopy(dict(changeset))
        self.approval = copy.deepcopy(dict(approval))
        self.lock = threading.RLock()
        self.session_token = secrets.token_urlsafe(48)
        self.csrf_token = secrets.token_urlsafe(48)
        current_revision = cast("int", self.approval["payload"]["revision"])
        resolved_paths: dict[int, Path] = {}
        seen: set[Path] = set()
        for revision, configured_path in approval_output_paths.items():
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision <= current_revision
            ):
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "approval output revisions must be future positive integers",
                )
            path = Path(configured_path)
            if path.suffix.casefold() != ".json" or not path.name:
                raise ContractError(ErrorCode.SCHEMA_INVALID, "approval output must be JSON")
            try:
                parent = path.parent.resolve(strict=True)
            except OSError as exc:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "approval output parent is unavailable",
                ) from exc
            if not parent.is_dir():
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "approval output parent must be a directory",
                )
            resolved = parent / path.name
            if resolved in seen:
                raise ContractError(ErrorCode.SCHEMA_INVALID, "approval output paths repeat")
            seen.add(resolved)
            resolved_paths[revision] = resolved
        self.approval_output_paths = resolved_paths

    @property
    def change_index(self) -> dict[str, Mapping[str, Any]]:
        changes = cast("Sequence[Mapping[str, Any]]", self.changeset["payload"]["changes"])
        return {cast("str", item["change_id"]): item for item in changes}

    def snapshot(self) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(self.changeset), copy.deepcopy(self.approval)

    def _require_fresh(self, revision: str, payload_sha256: str) -> None:
        current_revision = cast("int", self.approval["payload"]["revision"])
        if revision != str(current_revision) or not secrets.compare_digest(
            payload_sha256,
            compute_payload_sha256(self.approval),
        ):
            raise ContractError(
                ErrorCode.HASH_APPROVAL_MISMATCH,
                "stale approval revision",
            )

    def _publish(self, updated: Mapping[str, Any]) -> None:
        new_revision = cast("int", updated["payload"]["revision"])
        output = self.approval_output_paths.get(new_revision)
        if output is None:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "no explicit output path was configured for this approval revision",
            )
        write_approval_json(self.changeset, updated, output)
        self.approval = copy.deepcopy(dict(updated))

    def decide(self, form: Mapping[str, str]) -> None:
        with self.lock:
            self._require_fresh(form["revision"], form["approval_sha256"])
            if self.approval["payload"]["status"] == "final":
                raise ContractError(ErrorCode.APPROVAL_NOT_FINAL, "final approval is read-only")
            change_id = form["change_id"]
            if change_id not in self.change_index:
                raise ContractError(ErrorCode.APPROVAL_CHANGE_UNKNOWN, "unknown change")
            decision = form["decision"]
            if decision not in _DECISIONS:
                raise ContractError(
                    ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD,
                    "unknown decision",
                )
            final_text = form["final_text"]
            if decision != "accepted_with_edit" and final_text:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "final text is only allowed for edited acceptance",
                )
            updated = record_decision(
                self.changeset,
                self.approval,
                change_id=change_id,
                decision=cast("Decision", decision),
                final_text=final_text if decision == "accepted_with_edit" else None,
                reason=form["reason"] or None,
                decision_source="local_ui",
            )
            if updated != self.approval:
                self._publish(updated)

    def finalize(self, form: Mapping[str, str]) -> None:
        with self.lock:
            self._require_fresh(form["revision"], form["approval_sha256"])
            if self.approval["payload"]["status"] == "final":
                return
            updated = finalize_approval_set(self.changeset, self.approval)
            if updated != self.approval:
                self._publish(updated)


class ReviewHTTPServer(ThreadingHTTPServer):
    """Thread-safe server carrying one immutable ChangeSet approval session."""

    daemon_threads = True
    block_on_close = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        state: _ReviewState,
    ) -> None:
        self.review_state = state
        super().__init__(address, ReviewRequestHandler)

    def shutdown_request(self, request: object) -> None:
        """Close one HTTP connection without resetting a small unread request body."""

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

    @property
    def expected_host(self) -> str:
        host, port = cast("tuple[str, int]", self.server_address)
        return f"{host}:{port}"

    @property
    def origin(self) -> str:
        return f"http://{self.expected_host}"

    @property
    def current_approval(self) -> dict[str, Any]:
        return self.review_state.snapshot()[1]


class ReviewRequestHandler(BaseHTTPRequestHandler):
    """Strict origin-form HTTP handler; it never exposes a filesystem endpoint."""

    server_version = "latex-word-review-local"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def review_server(self) -> ReviewHTTPServer:
        return cast("ReviewHTTPServer", self.server)

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
    ) -> None:
        self.close_connection = True
        self.send_response(status)
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            token = self.review_server.review_state.session_token
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict",
            )
        if location is not None:
            self.send_header("Location", location)
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset=utf-8><title>Request rejected</title>"
            "</head><body><h1>Request rejected</h1>"
            f"<p><code>{_escape(code)}</code></p><p>{_escape(message)}</p></body></html>"
        ).encode()
        self._send(status, body)

    def _host_valid(self) -> bool:
        hosts = self.headers.get_all("Host", failobj=[])
        return len(hosts) == 1 and secrets.compare_digest(
            hosts[0], self.review_server.expected_host
        )

    def _request_path(self) -> str | None:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return None
        if "%" in parsed.path or "\\" in parsed.path or ".." in parsed.path:
            return None
        return parsed.path

    def _cookie_valid(self) -> bool:
        headers = self.headers.get_all("Cookie", failobj=[])
        if len(headers) != 1:
            return False
        values: list[str] = []
        for item in headers[0].split(";"):
            if "=" not in item:
                return False
            name, value = (part.strip() for part in item.split("=", 1))
            if name == SESSION_COOKIE:
                values.append(value)
        return len(values) == 1 and secrets.compare_digest(
            values[0], self.review_server.review_state.session_token
        )

    def _post_origin_valid(self) -> bool:
        origins = self.headers.get_all("Origin", failobj=[])
        if len(origins) != 1 or not secrets.compare_digest(origins[0], self.review_server.origin):
            return False
        fetch_sites = self.headers.get_all("Sec-Fetch-Site", failobj=[])
        return not fetch_sites or (len(fetch_sites) == 1 and fetch_sites[0] == "same-origin")

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

    def _form(self, expected_fields: set[str]) -> dict[str, str] | None:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            self._error(HTTPStatus.BAD_REQUEST, "E_SCHEMA_INVALID", "transfer encoding denied")
            return None
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if content_types != ["application/x-www-form-urlencoded"]:
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "E_SCHEMA_INVALID", "invalid media type")
            return None
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1 or not lengths[0].isdigit():
            self._error(HTTPStatus.LENGTH_REQUIRED, "E_SCHEMA_INVALID", "invalid content length")
            return None
        if len(lengths[0]) > 10:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "E_SCHEMA_INVALID",
                "request body exceeds limit",
            )
            return None
        length = int(lengths[0])
        if length > MAX_REQUEST_BYTES:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "E_SCHEMA_INVALID",
                "request body exceeds limit",
            )
            return None
        raw = self.rfile.read(length)
        try:
            encoded = raw.decode("ascii", errors="strict")
        except UnicodeDecodeError:
            self._error(HTTPStatus.BAD_REQUEST, "E_SCHEMA_INVALID", "form is not ASCII encoded")
            return None
        if not self._percent_encoding_valid(encoded):
            self._error(HTTPStatus.BAD_REQUEST, "E_SCHEMA_INVALID", "invalid percent encoding")
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
            self._error(HTTPStatus.BAD_REQUEST, "E_SCHEMA_INVALID", "invalid form body")
            return None
        fields: dict[str, str] = {}
        for name, value in pairs:
            if name in fields:
                self._error(HTTPStatus.BAD_REQUEST, "E_SCHEMA_INVALID", "duplicate form field")
                return None
            fields[name] = value
        if set(fields) != expected_fields:
            self._error(HTTPStatus.BAD_REQUEST, "E_SCHEMA_INVALID", "unknown or missing form field")
            return None
        if any(len(value) > 16 * 1024 for value in fields.values()):
            self._error(HTTPStatus.BAD_REQUEST, "E_SCHEMA_INVALID", "form field exceeds limit")
            return None
        return fields

    def _render_review(self, requested_change_id: str | None) -> bytes:
        changeset, approval = self.review_server.review_state.snapshot()
        changes = cast("Sequence[Mapping[str, Any]]", changeset["payload"]["changes"])
        decisions = {
            cast("str", item["change_id"]): item
            for item in cast("Sequence[Mapping[str, Any]]", approval["payload"]["decisions"])
        }
        change_ids = [cast("str", item["change_id"]) for item in changes]
        if requested_change_id is not None and requested_change_id not in change_ids:
            raise ContractError(ErrorCode.APPROVAL_CHANGE_UNKNOWN, "unknown change")
        payload = cast("Mapping[str, Any]", approval["payload"])
        revision = cast("int", payload["revision"])
        approval_sha256 = compute_payload_sha256(approval)
        csrf = self.review_server.review_state.csrf_token
        navigation = "".join(
            f'<li><a href="/change/{_escape(change_id)}">{_escape(change_id)}</a></li>'
            for change_id in change_ids
        )
        hidden = ""
        finalize_form = ""
        if payload["status"] != "final":
            hidden = (
                f'<input type="hidden" name="csrf" value="{_escape(csrf)}">'
                f'<input type="hidden" name="revision" value="{revision}">'
                f'<input type="hidden" name="approval_sha256" value="{_escape(approval_sha256)}">'
            )
            finalize_form = (
                '<form method="post" action="/finalize">'
                + hidden
                + '<button type="submit">Finalize approval</button></form>'
            )
        if not change_ids:
            return (
                "<!doctype html><html><head><meta charset=utf-8>"
                "<title>Local change review</title></head><body>"
                "<h1>Local change review</h1>"
                f"<p>Approval revision: {revision}; status: {_escape(payload['status'])}</p>"
                "<p>No changes were found.</p>"
                f"{finalize_form}</body></html>"
            ).encode()
        if requested_change_id is None:
            pending = [change_id for change_id in change_ids if change_id not in decisions]
            selected_id = pending[0] if pending else change_ids[0]
        else:
            selected_id = requested_change_id
        change = next(item for item in changes if item["change_id"] == selected_id)
        decision = decisions.get(selected_id)
        evidence = {
            "raw_event_ids": change["raw_event_ids"],
            "resolution": change["resolution"],
            "change_fingerprint": change["change_fingerprint"],
        }
        decision_form = ""
        if payload["status"] != "final":
            decision_form = (
                '<form method="post" action="/decision">'
                + hidden
                + f'<input type="hidden" name="change_id" value="{_escape(selected_id)}">'
                '<label>Edited final text<textarea name="final_text"></textarea></label>'
                '<label>Reason<textarea name="reason"></textarea></label>'
                '<button name="decision" value="accepted">Accept</button>'
                '<button name="decision" value="accepted_with_edit">Accept edited</button>'
                '<button name="decision" value="rejected">Reject</button>'
                '<button name="decision" value="manual">Manual</button>'
                '<button name="decision" value="conflict">Conflict</button></form>'
            )
        return (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>Local change review</title></head><body>"
            "<h1>Local change review</h1>"
            f"<p>Approval revision: {revision}; status: {_escape(payload['status'])}</p>"
            f"<nav><ol>{navigation}</ol></nav><main>"
            f"<h2>{_escape(change['change_id'])}</h2>"
            f"<dl><dt>Type</dt><dd>{_escape(change['kind'])}</dd>"
            f"<dt>Author</dt><dd>{_escape(change['author'])}</dd>"
            f"<dt>Time</dt><dd>{_escape(change['timestamp'])}</dd>"
            f"<dt>Before</dt><dd><pre>{_escape(change['before'])}</pre></dd>"
            f"<dt>After</dt><dd><pre>{_escape(change['after'])}</pre></dd>"
            f"<dt>Source</dt><dd><pre>{_escape(change['source_location'])}</pre></dd>"
            f"<dt>Evidence</dt><dd><pre>{_escape(evidence)}</pre></dd>"
            "<dt>Decision</dt><dd>"
            f"{_escape('pending' if decision is None else decision['decision'])}"
            "</dd></dl>"
            f"{decision_form}{finalize_form}</main></body></html>"
        ).encode()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._host_valid():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "E_SCHEMA_INVALID", "invalid Host")
            return
        path = self._request_path()
        if path is None:
            self._error(HTTPStatus.BAD_REQUEST, "E_PATH_TRAVERSAL", "invalid request target")
            return
        requested: str | None = None
        if path != "/":
            match = _CHANGE_PATH_RE.fullmatch(path)
            if match is None:
                self._error(HTTPStatus.NOT_FOUND, "E_SCHEMA_INVALID", "endpoint not found")
                return
            requested = match.group(1)
        try:
            body = self._render_review(requested)
        except ContractError as exc:
            self._error(HTTPStatus.NOT_FOUND, exc.code.value, "change not found")
            return
        self._send(HTTPStatus.OK, body, set_cookie=True)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._host_valid():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "E_SCHEMA_INVALID", "invalid Host")
            return
        if not self._post_origin_valid() or not self._cookie_valid():
            self._error(HTTPStatus.FORBIDDEN, "E_SCHEMA_INVALID", "cross-site request denied")
            return
        path = self._request_path()
        if path not in {"/decision", "/finalize"}:
            self._error(HTTPStatus.NOT_FOUND, "E_SCHEMA_INVALID", "endpoint not found")
            return
        expected = (
            {
                "csrf",
                "revision",
                "approval_sha256",
                "change_id",
                "decision",
                "final_text",
                "reason",
            }
            if path == "/decision"
            else {"csrf", "revision", "approval_sha256"}
        )
        form = self._form(expected)
        if form is None:
            return
        if not secrets.compare_digest(form["csrf"], self.review_server.review_state.csrf_token):
            self._error(HTTPStatus.FORBIDDEN, "E_SCHEMA_INVALID", "CSRF token differs")
            return
        try:
            if path == "/decision":
                self.review_server.review_state.decide(form)
            else:
                self.review_server.review_state.finalize(form)
        except ContractError as exc:
            status = (
                HTTPStatus.CONFLICT
                if exc.code
                in {
                    ErrorCode.HASH_APPROVAL_MISMATCH,
                    ErrorCode.APPROVAL_NOT_FINAL,
                    ErrorCode.APPROVAL_CHANGE_UNKNOWN,
                }
                else HTTPStatus.BAD_REQUEST
            )
            self._error(status, exc.code.value, "approval update rejected")
            return
        self._send(HTTPStatus.SEE_OTHER, b"", location="/")

    def _method_not_allowed(self) -> None:
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "E_SCHEMA_INVALID", "method not allowed")

    do_DELETE = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_PUT = _method_not_allowed


def create_review_server(
    changeset: Mapping[str, Any],
    approval: Mapping[str, Any],
    *,
    approval_output_paths: Mapping[int, str | Path],
    host: str = LOOPBACK_HOST,
    port: int = 0,
) -> ReviewHTTPServer:
    """Create, but do not start, one loopback-only approval server."""

    if host != LOOPBACK_HOST:
        raise ContractError(
            ErrorCode.SCHEMA_INVALID,
            "review server may bind only to 127.0.0.1",
        )
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "review server port is invalid")
    state = _ReviewState(changeset, approval, approval_output_paths)
    try:
        return ReviewHTTPServer((LOOPBACK_HOST, port), state)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "review server could not bind") from exc


__all__ = [
    "LOOPBACK_HOST",
    "MAX_REQUEST_BYTES",
    "SESSION_COOKIE",
    "ReviewHTTPServer",
    "create_review_server",
]
