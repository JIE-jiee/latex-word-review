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
    finalize_approval_set,
    record_decision,
    write_approval_json,
)
from latex_word_review.contracts import compute_payload_sha256, validate_contract
from latex_word_review.domain_values import ACTION_DECISION_VALUE_SET
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.planner import evaluate_patch_eligibility

LOOPBACK_HOST: Final = "127.0.0.1"
SESSION_COOKIE: Final = "lwr_review_session"
MAX_REQUEST_BYTES: Final = 32 * 1024
_MAX_FIELDS: Final = 8
_GRACEFUL_DRAIN_BYTES: Final = MAX_REQUEST_BYTES + 1
_GRACEFUL_DRAIN_CHUNK_BYTES: Final = 8 * 1024
_GRACEFUL_DRAIN_TIMEOUT_SECONDS: Final = 0.25
_CHANGE_PATH_RE: Final = re.compile(r"^/change/(chg_[a-z0-9]{26,64})$")
_FILTER_PATH_RE: Final = re.compile(r"^/filter/(all|pending|manual|comment|high-risk)$")
_FILTER_CHANGE_PATH_RE: Final = re.compile(
    r"^/filter/(all|pending|manual|comment|high-risk)/change/(chg_[a-z0-9]{26,64})$"
)
_FILTER_NAMES: Final[tuple[str, ...]] = ("all", "pending", "manual", "comment", "high-risk")
_FILTER_LABELS: Final[dict[str, str]] = {
    "all": "All",
    "pending": "Pending",
    "manual": "Manual",
    "comment": "Comments",
    "high-risk": "High risk",
}
_HEX: Final = frozenset("0123456789abcdefABCDEF")
_DECISIONS = ACTION_DECISION_VALUE_SET
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


def _render_optional(value: object) -> str:
    if value is None:
        return "<em>Not recorded</em>"
    if value == "":
        return "<pre>(empty string)</pre>"
    return f"<pre>{_escape(value)}</pre>"


def _render_values(values: Sequence[object]) -> str:
    if not values:
        return "<em>None recorded</em>"
    return "<ul>" + "".join(f"<li>{_escape(value)}</li>" for value in values) + "</ul>"


def _render_baseline_verification(changeset_payload: Mapping[str, Any]) -> str:
    baseline_value = changeset_payload.get("baseline_verification")
    if baseline_value is None:
        return ""
    baseline = cast("Mapping[str, Any]", baseline_value)
    verified_scope = cast("Sequence[object]", baseline["verified_scope"])
    unverified_scope = cast("Sequence[object]", baseline["unverified_scope"])
    manual_review_required = baseline["manual_integrity_review_required"]
    manual_review_notice = (
        "<p><strong>Manual integrity review is required</strong> for the unverified "
        "document semantics before relying on the final deliverables.</p>"
        if manual_review_required is True
        else ""
    )
    return (
        '<section aria-labelledby="baseline-verification-scope">'
        '<h2 id="baseline-verification-scope">Baseline verification scope</h2>'
        "<p>This verification is scoped to safe plain-text patch planning; it is not a "
        "claim that every Word document semantic was verified.</p><dl>"
        f"<dt><code>status</code></dt><dd>{_escape(baseline['status'])}</dd>"
        "<dt><code>automatic_patch_scope</code></dt>"
        f"<dd>{_escape(baseline['automatic_patch_scope'])}</dd>"
        "<dt><code>verified_scope</code></dt>"
        f"<dd>{_render_values(verified_scope)}</dd>"
        "<dt><code>unverified_scope</code></dt>"
        f"<dd>{_render_values(unverified_scope)}</dd>"
        "<dt><code>manual_integrity_review_required</code></dt>"
        f"<dd>{_escape(manual_review_required)}</dd>"
        f"</dl>{manual_review_notice}</section>"
    )


def _change_matches_filter(
    change: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
    selected_filter: str,
) -> bool:
    if selected_filter == "all":
        return True
    if selected_filter == "pending":
        return decision is None
    if selected_filter == "manual":
        return change["initial_decision"] == "manual" or (
            decision is not None and decision["decision"] == "manual"
        )
    if selected_filter == "comment":
        return bool(change["kind"] == "comment")
    if selected_filter == "high-risk":
        return change["safety_class"] in {"manual_high_risk", "denied_unknown"}
    raise ContractError(ErrorCode.SCHEMA_INVALID, "unknown review filter")


def _change_href(change_id: str, selected_filter: str) -> str:
    if selected_filter == "all":
        return f"/change/{change_id}"
    return f"/filter/{selected_filter}/change/{change_id}"


def _linked_raw_events(
    change: Mapping[str, Any], raw_events: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], ...]:
    index = {cast("str", item["raw_event_id"]): item for item in raw_events}
    return tuple(index[cast("str", raw_id)] for raw_id in change["raw_event_ids"])


def _relevant_diagnostics(
    change: Mapping[str, Any],
    raw_events: Sequence[Mapping[str, Any]],
    changeset_diagnostics: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    selected: list[Mapping[str, Any]] = []
    seen: set[str] = set()

    def add(diagnostic: Mapping[str, Any]) -> None:
        diagnostic_id = cast("str", diagnostic["diagnostic_id"])
        if diagnostic_id not in seen:
            selected.append(diagnostic)
            seen.add(diagnostic_id)

    for event in raw_events:
        for diagnostic in cast("Sequence[Mapping[str, Any]]", event["diagnostics"]):
            add(diagnostic)
    change_id = change["change_id"]
    unit_id = change["unit_id"]
    for diagnostic in changeset_diagnostics:
        if diagnostic["change_id"] == change_id or (
            unit_id is not None and diagnostic["unit_id"] == unit_id
        ):
            add(diagnostic)
    return tuple(selected)


def _render_diagnostics(diagnostics: Sequence[Mapping[str, Any]]) -> str:
    if not diagnostics:
        return "<p>No diagnostics are linked to this change.</p>"
    items = []
    for diagnostic in diagnostics:
        remediation = _render_optional(diagnostic["remediation"])
        items.append(
            "<li>"
            f"<p><strong>{_escape(diagnostic['severity'])}: "
            f"{_escape(diagnostic['code'])}</strong></p>"
            f"<p>{_escape(diagnostic['message'])}</p>"
            "<dl>"
            f"<dt>Phase</dt><dd>{_escape(diagnostic['phase'])}</dd>"
            f"<dt>Recoverable</dt><dd>{_escape(diagnostic['recoverable'])}</dd>"
            f"<dt>Remediation</dt><dd>{remediation}</dd>"
            f"<dt>Diagnostic ID</dt><dd><code>{_escape(diagnostic['diagnostic_id'])}</code></dd>"
            "</dl></li>"
        )
    return "<ol>" + "".join(items) + "</ol>"


def _render_raw_event(event: Mapping[str, Any]) -> str:
    summary = (
        f"{event['raw_event_id']} — {event['kind']} — {event['part_uri']} "
        f"(order {event['document_order']})"
    )
    diagnostic_codes = [
        item["code"] for item in cast("Sequence[Mapping[str, Any]]", event["diagnostics"])
    ]
    return (
        "<details>"
        f"<summary>{_escape(summary)}</summary><dl>"
        f"<dt>Native ID</dt><dd>{_escape(event['native_id'])}</dd>"
        f"<dt>Native kind</dt><dd>{_escape(event['native_kind'])}</dd>"
        f"<dt>Author</dt><dd>{_escape(event['author'])}</dd>"
        f"<dt>Timestamp</dt><dd>{_escape(event['timestamp'])}</dd>"
        f"<dt>Content</dt><dd><pre>{_escape(event['content'])}</pre></dd>"
        f"<dt>Range</dt><dd><pre>{_escape(event['range'])}</pre></dd>"
        f"<dt>Evidence</dt><dd><pre>{_escape(event['evidence'])}</pre></dd>"
        f"<dt>Diagnostic codes</dt><dd>{_render_values(diagnostic_codes)}</dd>"
        "</dl></details>"
    )


def _automatic_applicability(
    change: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
    approval_status: object,
) -> tuple[str, str]:
    eligibility = evaluate_patch_eligibility(change, decision)
    if eligibility.block_code is not None:
        return "No", cast("str", eligibility.reason)
    if eligibility.source_context_required:
        return "Not yet", cast("str", eligibility.reason)
    if decision is None:
        return (
            "Not yet",
            "technically eligible, but an explicit acceptance and final approval are still "
            "required",
        )
    decision_name = cast("str", decision["decision"])
    if decision_name not in {"accepted", "accepted_with_edit"}:
        return "No", f"the current decision is {decision_name}"
    if approval_status != "final":
        return (
            "Not yet",
            "accepted and technically eligible, but the ApprovalSet is still draft",
        )
    return (
        "Eligible",
        "the separate PatchPlan/apply gate must still revalidate the source bytes and policy",
    )


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
                decision=decision,
                final_text=final_text if decision == "accepted_with_edit" else None,
                reason=form["reason"] or None,
                risk_acknowledgement=form["risk_acknowledgement"] or None,
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

    def _render_review(self, requested_change_id: str | None, selected_filter: str) -> bytes:
        if selected_filter not in _FILTER_NAMES:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "unknown review filter")
        changeset, approval = self.review_server.review_state.snapshot()
        changeset_payload = cast("Mapping[str, Any]", changeset["payload"])
        changes = cast("Sequence[Mapping[str, Any]]", changeset_payload["changes"])
        raw_events = cast("Sequence[Mapping[str, Any]]", changeset_payload["raw_events"])
        changeset_diagnostics = cast(
            "Sequence[Mapping[str, Any]]", changeset_payload["diagnostics"]
        )
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
        filtered_changes = [
            change
            for change in changes
            if _change_matches_filter(
                change,
                decisions.get(cast("str", change["change_id"])),
                selected_filter,
            )
        ]
        filtered_ids = [cast("str", item["change_id"]) for item in filtered_changes]
        if requested_change_id is not None and requested_change_id not in filtered_ids:
            raise ContractError(ErrorCode.APPROVAL_CHANGE_UNKNOWN, "change is outside filter")
        filter_counts = {
            filter_name: sum(
                _change_matches_filter(
                    change,
                    decisions.get(cast("str", change["change_id"])),
                    filter_name,
                )
                for change in changes
            )
            for filter_name in _FILTER_NAMES
        }
        filter_navigation = "".join(
            (
                f'<li><a href="/filter/{_escape(filter_name)}"'
                + (' aria-current="page"' if selected_filter == filter_name else "")
                + f">{_escape(_FILTER_LABELS[filter_name])} "
                f"({_escape(filter_counts[filter_name])})</a></li>"
            )
            for filter_name in _FILTER_NAMES
        )
        total = len(changes)
        decided_count = len(decisions)
        pending_count = total - decided_count
        progress = (
            f"<p>Progress: total <strong>{total}</strong>; decided "
            f"<strong>{decided_count}</strong>; pending <strong>{pending_count}</strong>.</p>"
            + (
                f'<progress value="{decided_count}" max="{total}">'
                f"{decided_count}/{total}</progress>"
                if total
                else "<p>There are no decisions to record.</p>"
            )
        )
        baseline_verification = _render_baseline_verification(changeset_payload)
        navigation_items: list[str] = []
        for item in filtered_changes:
            item_id = cast("str", item["change_id"])
            item_decision = decisions.get(item_id)
            item_status = "pending" if item_decision is None else item_decision["decision"]
            item_href = _change_href(item_id, selected_filter)
            navigation_items.append(
                f'<li><a href="{_escape(item_href)}">'
                f"{_escape(item['kind'])} — {_escape(item_id)}</a> "
                f"[{_escape(item_status)}]</li>"
            )
        navigation = "".join(navigation_items)
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
                "<!doctype html><html lang=en><head><meta charset=utf-8>"
                '<meta name=viewport content="width=device-width, initial-scale=1">'
                "<title>Local change review</title></head><body><header>"
                "<h1>Local change review</h1>"
                f"<p>Approval revision: {revision}; status: {_escape(payload['status'])}</p>"
                f"{progress}{baseline_verification}"
                f'<nav aria-label="Change filters"><ul>{filter_navigation}</ul></nav>'
                "</header><main>"
                "<p>No changes were found.</p>"
                f"{finalize_form}</main></body></html>"
            ).encode()
        if not filtered_changes:
            return (
                "<!doctype html><html lang=en><head><meta charset=utf-8>"
                '<meta name=viewport content="width=device-width, initial-scale=1">'
                "<title>Local change review</title></head><body><header>"
                "<h1>Local change review</h1>"
                f"<p>Approval revision: {revision}; status: {_escape(payload['status'])}</p>"
                f"{progress}{baseline_verification}"
                f'<nav aria-label="Change filters"><ul>{filter_navigation}</ul></nav>'
                "</header><main>"
                f"<p>No changes match the {_escape(_FILTER_LABELS[selected_filter])} filter.</p>"
                f"{finalize_form}</main></body></html>"
            ).encode()
        if requested_change_id is None:
            pending = [change_id for change_id in filtered_ids if change_id not in decisions]
            selected_id = pending[0] if pending else filtered_ids[0]
        else:
            selected_id = requested_change_id
        change = next(item for item in filtered_changes if item["change_id"] == selected_id)
        decision = decisions.get(selected_id)
        linked_raw_events = _linked_raw_events(change, raw_events)
        diagnostics = _relevant_diagnostics(
            change,
            linked_raw_events,
            changeset_diagnostics,
        )
        evidence = {
            "raw_event_ids": change["raw_event_ids"],
            "resolution": change["resolution"],
            "change_fingerprint": change["change_fingerprint"],
        }
        resolution = cast("Mapping[str, Any]", change["resolution"])
        authors = cast("Sequence[object]", change["authors"])
        timestamps = cast("Sequence[object]", change["timestamps"])
        auto_state, auto_reason = _automatic_applicability(change, decision, payload["status"])
        current_decision = "pending" if decision is None else cast("str", decision["decision"])
        current_final_text = None if decision is None else decision["final_text"]
        current_reason = None if decision is None else decision["reason"]
        current_risk = None if decision is None else decision["risk_acknowledgement"]
        current_decided_at = None if decision is None else decision["decided_at"]
        current_source = None if decision is None else decision["decision_source"]
        anchor_texts = [
            cast("Mapping[str, Any]", event["content"])["text"]
            for event in linked_raw_events
            if event["kind"] == "comment"
        ]
        if anchor_texts:
            rendered_anchors = (
                "<ul>"
                + "".join(
                    (
                        "<li><em>Point anchor; no text was selected.</em></li>"
                        if anchor == ""
                        else f"<li><pre>{_escape(anchor)}</pre></li>"
                    )
                    for anchor in anchor_texts
                )
                + "</ul>"
            )
        else:
            rendered_anchors = "<em>Not recorded</em>"
        comment_section = ""
        if change["kind"] == "comment":
            comment_section = (
                "<section><h3>Comment</h3><dl>"
                f"<dt>Comment body</dt><dd>{_render_optional(change['comment'])}</dd>"
                f"<dt>Anchored text</dt><dd>{rendered_anchors}</dd>"
                "</dl></section>"
            )
        selected_index = filtered_ids.index(selected_id)
        previous_id = filtered_ids[selected_index - 1] if selected_index else None
        current_global_index = change_ids.index(selected_id)
        pending_after = [
            change_id
            for change_id in (
                change_ids[current_global_index + 1 :] + change_ids[:current_global_index]
            )
            if change_id not in decisions
        ]
        next_pending_id = pending_after[0] if pending_after else None
        previous_navigation = (
            '<a rel="prev" '
            f'href="{_escape(_change_href(previous_id, selected_filter))}">'
            "Previous item</a>"
            if previous_id is not None
            else "<span>Previous item unavailable</span>"
        )
        next_pending_navigation = (
            '<a rel="next" '
            f'href="{_escape(_change_href(next_pending_id, "pending"))}">'
            "Next pending</a>"
            if next_pending_id is not None
            else "<span>No other pending item</span>"
        )
        decision_form = ""
        if payload["status"] != "final":
            decision_form = (
                '<section><h3>Record decision</h3><form method="post" action="/decision">'
                + hidden
                + f'<input type="hidden" name="change_id" value="{_escape(selected_id)}">'
                '<p><label for="final_text">Edited final text</label><br>'
                '<textarea id="final_text" name="final_text" rows="4" cols="80">'
                f"{_escape(current_final_text)}</textarea></p>"
                "<p>Final text is used only with <strong>Accept edited</strong>; clear it before "
                "submitting another decision.</p>"
                '<p><label for="reason">Reason</label><br>'
                '<textarea id="reason" name="reason" rows="3" cols="80">'
                f"{_escape(current_reason)}</textarea></p>"
                '<p><label for="risk_acknowledgement">Risk acknowledgement</label><br>'
                '<textarea id="risk_acknowledgement" name="risk_acknowledgement" '
                f'rows="3" cols="80">{_escape(current_risk)}</textarea></p>'
                '<button name="decision" value="accepted">Accept</button>'
                '<button name="decision" value="accepted_with_edit">Accept edited</button>'
                '<button name="decision" value="rejected">Reject</button>'
                '<button name="decision" value="manual">Manual</button>'
                '<button name="decision" value="conflict">Conflict</button></form></section>'
            )
        raw_evidence = "".join(_render_raw_event(event) for event in linked_raw_events)
        change_raw_event_ids = cast("Sequence[object]", change["raw_event_ids"])
        return (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width, initial-scale=1">'
            "<title>Local change review</title></head><body><header>"
            "<h1>Local change review</h1>"
            f"<p>Approval revision: {revision}; status: {_escape(payload['status'])}</p>"
            f"{progress}{baseline_verification}"
            f'<nav aria-label="Change filters"><ul>{filter_navigation}</ul></nav>'
            "</header>"
            f"<aside><h2>{_escape(_FILTER_LABELS[selected_filter])} changes</h2>"
            f"<p>Showing {selected_index + 1} of {len(filtered_changes)} matching changes.</p>"
            f'<nav aria-label="Filtered changes"><ol>{navigation}</ol></nav></aside><main>'
            f'<nav aria-label="Review navigation">{previous_navigation} | '
            f"{next_pending_navigation}</nav>"
            f"<h2>{_escape(change['change_id'])}</h2>"
            "<section><h3>Change metadata</h3><dl>"
            f"<dt>Type</dt><dd>{_escape(change['kind'])}</dd>"
            f"<dt>Kind</dt><dd>{_escape(change['kind'])}</dd>"
            f"<dt>Native kind</dt><dd>{_escape(change['native_kind'])}</dd>"
            f"<dt>Author</dt><dd>{_escape(change['author'])}</dd>"
            f"<dt>Authors</dt><dd>{_render_values(authors)}</dd>"
            f"<dt>Time</dt><dd>{_escape(change['timestamp'])}</dd>"
            f"<dt>Timestamps</dt><dd>{_render_values(timestamps)}</dd>"
            "</dl></section>"
            f"{comment_section}"
            "<section><h3>Before and after</h3><table>"
            '<thead><tr><th scope="col">Before</th><th scope="col">After</th></tr></thead>'
            "<tbody><tr>"
            f"<td>{_render_optional(change['before'])}</td>"
            f"<td>{_render_optional(change['after'])}</td>"
            "</tr></tbody></table></section>"
            "<section><h3>Source mapping and safety</h3><dl>"
            f"<dt>Source</dt><dd><pre>{_escape(change['source_location'])}</pre></dd>"
            f"<dt>Unit ID</dt><dd>{_escape(change['unit_id'])}</dd>"
            f"<dt>Resolution</dt><dd>{_escape(resolution['status'])}</dd>"
            f"<dt>Resolution method</dt><dd>{_escape(resolution['method'])}</dd>"
            f"<dt>Confidence</dt><dd>{_escape(resolution['confidence'])}</dd>"
            f"<dt>Resolution candidates</dt><dd><pre>{_escape(resolution['candidates'])}</pre></dd>"
            f"<dt>Safety class</dt><dd>{_escape(change['safety_class'])}</dd>"
            f"<dt>Automatically applicable</dt><dd><strong>{_escape(auto_state)}</strong> — "
            f"{_escape(auto_reason)}.</dd>"
            "</dl><p>This is a review-time assessment only. The separate planner/apply gate "
            "still checks the finalized approval, source hashes, exact bytes, overlap, and "
            "policy.</p>"
            "</section>"
            "<section><h3>Audit evidence</h3><dl>"
            f"<dt>Evidence</dt><dd><pre>{_escape(evidence)}</pre></dd>"
            f"<dt>Raw event IDs</dt><dd>{_render_values(change_raw_event_ids)}</dd>"
            "<dt>Change fingerprint</dt><dd><code>"
            f"{_escape(change['change_fingerprint'])}</code></dd>"
            f"</dl><h4>Raw evidence summary</h4>{raw_evidence}"
            f"<h4>Change diagnostics</h4>{_render_diagnostics(diagnostics)}</section>"
            "<section><h3>Current decision</h3><dl>"
            f"<dt>Decision</dt><dd>{_escape(current_decision)}</dd>"
            f"<dt>Final text</dt><dd>{_render_optional(current_final_text)}</dd>"
            f"<dt>Reason</dt><dd>{_render_optional(current_reason)}</dd>"
            f"<dt>Risk acknowledgement</dt><dd>{_render_optional(current_risk)}</dd>"
            f"<dt>Decided at</dt><dd>{_escape(current_decided_at)}</dd>"
            f"<dt>Decision source</dt><dd>{_escape(current_source)}</dd>"
            "</dl></section>"
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
        selected_filter = "all"
        if path != "/":
            change_match = _CHANGE_PATH_RE.fullmatch(path)
            filter_match = _FILTER_PATH_RE.fullmatch(path)
            filtered_change_match = _FILTER_CHANGE_PATH_RE.fullmatch(path)
            if change_match is not None:
                requested = change_match.group(1)
            elif filter_match is not None:
                selected_filter = filter_match.group(1)
            elif filtered_change_match is not None:
                selected_filter = filtered_change_match.group(1)
                requested = filtered_change_match.group(2)
            else:
                self._error(HTTPStatus.NOT_FOUND, "E_SCHEMA_INVALID", "endpoint not found")
                return
        try:
            body = self._render_review(requested, selected_filter)
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
                "risk_acknowledgement",
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
