"""Security and state-transition tests for the loopback approval server."""

from __future__ import annotations

import copy
import re
import socket
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import pytest

from latex_word_review.approval import (
    create_approval_set,
    finalize_approval_set,
    record_decision,
)
from latex_word_review.canonical import seal_envelope, sha256_canonical
from latex_word_review.contracts import compute_payload_sha256, load_contract_json
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.ids import stable_id
from latex_word_review.review_server import (
    MAX_REQUEST_BYTES,
    ReviewHTTPServer,
    create_review_server,
)
from tests.test_contracts import _golden_contracts

TIME = "2026-07-16T16:00:00+09:00"
ACTOR = {"id": "synthetic-author", "display_name": "Synthetic Author"}
Response = tuple[int, dict[str, str], bytes]


class _RecordingSocket:
    def __init__(
        self,
        unread_bytes: int,
        *,
        fail_shutdown: bool = False,
        fail_recv: bool = False,
    ) -> None:
        self.unread_bytes = unread_bytes
        self.fail_shutdown = fail_shutdown
        self.fail_recv = fail_recv
        self.shutdown_modes: list[int] = []
        self.timeouts: list[float] = []
        self.recv_sizes: list[int] = []
        self.closed = False

    def shutdown(self, mode: int) -> None:
        self.shutdown_modes.append(mode)
        if self.fail_shutdown:
            raise ConnectionAbortedError

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def recv(self, size: int) -> bytes:
        self.recv_sizes.append(size)
        if self.fail_recv:
            raise ConnectionAbortedError
        consumed = min(size, self.unread_bytes)
        self.unread_bytes -= consumed
        return b"x" * consumed

    def close(self) -> None:
        self.closed = True


def _draft() -> tuple[dict[str, Any], dict[str, Any]]:
    changeset = copy.deepcopy(_golden_contracts()["ChangeSet"])
    approval = create_approval_set(changeset, decided_by=ACTOR, generated_at=TIME)
    return changeset, approval


def _policy_state(
    *,
    before: str,
    after: str,
    accepted: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    changeset = copy.deepcopy(_golden_contracts()["ChangeSet"])
    change = cast("dict[str, Any]", changeset["payload"]["changes"][0])
    change.update(
        {
            "kind": "replacement",
            "before": before,
            "after": after,
            "change_fingerprint": sha256_canonical(["review-server-policy", before, after]),
        }
    )
    changeset = seal_envelope(changeset)
    approval = create_approval_set(changeset, decided_by=ACTOR, generated_at=TIME)
    if accepted:
        approval = record_decision(
            changeset,
            approval,
            change_id=cast("str", change["change_id"]),
            decision="accepted",
            decided_at=TIME,
            generated_at=TIME,
        )
        approval = finalize_approval_set(changeset, approval, generated_at=TIME)
    return changeset, approval


def _rich_draft() -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    changeset, _ = _draft()
    payload = cast("dict[str, Any]", changeset["payload"])
    base_raw = cast("dict[str, Any]", payload["raw_events"][0])
    base_change = cast("dict[str, Any]", payload["changes"][0])

    comment_raw_id = stable_id("rev_", ["review-server", "comment"])
    comment_change_id = stable_id("chg_", ["review-server", "comment"])
    diagnostic_id = stable_id("diag_", ["review-server", "comment-context"])
    comment_raw = copy.deepcopy(base_raw)
    comment_raw.update(
        {
            "raw_event_id": comment_raw_id,
            "kind": "comment",
            "native_id": "201",
            "author": "Reviewer Two",
            "timestamp": "2026-01-16T10:00:00+08:00",
            "document_order": 1,
            "content": {
                "text": "Anchored <phrase>",
                "deleted_text": None,
                "comment_text": "Please verify <this comment>.",
                "format_before": None,
                "format_after": None,
            },
            "range": {
                "start_native_id": "201",
                "end_native_id": "201",
                "pair_native_id": "201",
                "anchor_kind": "range",
            },
            "evidence": {
                "node_ordinal": 1,
                "fragment_sha256": sha256_canonical("comment-fragment"),
                "artifact": None,
            },
            "diagnostics": [
                {
                    "diagnostic_id": diagnostic_id,
                    "code": "W_COMMENT_CONTEXT",
                    "severity": "warning",
                    "phase": "ingest",
                    "message": "Review <comment context> manually.",
                    "recoverable": True,
                    "unit_id": None,
                    "change_id": comment_change_id,
                    "source_location": None,
                    "evidence": None,
                    "remediation": "Inspect <the anchor>.",
                }
            ],
        }
    )
    comment_change = copy.deepcopy(base_change)
    comment_change.update(
        {
            "change_id": comment_change_id,
            "kind": "comment",
            "raw_event_ids": [comment_raw_id],
            "author": None,
            "authors": ["Reviewer Two", "Reviewer Three"],
            "timestamp": None,
            "timestamps": [
                "2026-01-16T10:00:00+08:00",
                "2026-01-16T10:05:00+08:00",
            ],
            "before": None,
            "after": None,
            "comment": "Please verify <this comment>.",
            "unit_id": None,
            "source_location": None,
            "resolution": {
                "status": "unmatched",
                "method": "none",
                "confidence": 0.0,
                "candidates": [],
            },
            "safety_class": "ledger_only",
            "initial_decision": "pending",
            "change_fingerprint": sha256_canonical([comment_raw_id, "comment-change"]),
        }
    )

    format_raw_id = stable_id("rev_", ["review-server", "format"])
    format_change_id = stable_id("chg_", ["review-server", "format"])
    format_raw = copy.deepcopy(base_raw)
    format_raw.update(
        {
            "raw_event_id": format_raw_id,
            "kind": "run_format",
            "native_id": "301",
            "author": "Reviewer Four",
            "timestamp": "2026-01-16T11:00:00+08:00",
            "document_order": 2,
            "content": {
                "text": "format target",
                "deleted_text": None,
                "comment_text": None,
                "format_before": {"bold": False},
                "format_after": {"bold": True},
            },
            "range": None,
            "evidence": {
                "node_ordinal": 2,
                "fragment_sha256": sha256_canonical("format-fragment"),
                "artifact": None,
            },
            "diagnostics": [],
        }
    )
    format_change = copy.deepcopy(base_change)
    format_change.update(
        {
            "change_id": format_change_id,
            "kind": "format",
            "native_kind": "run_format",
            "raw_event_ids": [format_raw_id],
            "author": "Reviewer Four",
            "authors": ["Reviewer Four"],
            "timestamp": "2026-01-16T11:00:00+08:00",
            "timestamps": ["2026-01-16T11:00:00+08:00"],
            "before": {"bold": False},
            "after": {"bold": True},
            "comment": None,
            "unit_id": None,
            "source_location": None,
            "resolution": {
                "status": "unsupported",
                "method": "manual",
                "confidence": 0.0,
                "candidates": [],
            },
            "safety_class": "manual_high_risk",
            "initial_decision": "manual",
            "change_fingerprint": sha256_canonical([format_raw_id, "format-change"]),
        }
    )

    payload["raw_events"] = [base_raw, comment_raw, format_raw]
    payload["changes"] = [base_change, comment_change, format_change]
    payload["counts"] = {
        "raw_events": 3,
        "changes": 3,
        "unmatched": 1,
        "conflicts": 0,
    }
    changeset = seal_envelope(changeset)
    approval = create_approval_set(changeset, decided_by=ACTOR, generated_at=TIME)
    return (
        changeset,
        approval,
        {
            "plain": cast("str", base_change["change_id"]),
            "comment": comment_change_id,
            "format": format_change_id,
        },
    )


def _outputs(root: Path, revisions: range = range(2, 7)) -> dict[int, Path]:
    return {revision: root / f"approval-r{revision}.json" for revision in revisions}


@contextmanager
def _running_server(
    changeset: Mapping[str, Any],
    approval: Mapping[str, Any],
    output_paths: Mapping[int, Path],
) -> Iterator[ReviewHTTPServer]:
    server = create_review_server(
        changeset,
        approval,
        approval_output_paths=output_paths,
    )

    def serve() -> None:
        server.serve_forever(poll_interval=0.01)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def _request(
    server: ReviewHTTPServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    host, port = cast("tuple[str, int]", server.server_address)
    connection = HTTPConnection(host, port, timeout=5)
    try:
        connection.request(method, path, body=body, headers={} if headers is None else headers)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {name.casefold(): value for name, value in response.getheaders()}
        return response.status, response_headers, response_body
    finally:
        connection.close()


def _credentials(server: ReviewHTTPServer) -> tuple[str, str, bytes, dict[str, str]]:
    status, headers, body = _request(
        server,
        "GET",
        "/",
        headers={"Host": server.expected_host},
    )
    assert status == HTTPStatus.OK
    csrf_match = re.search(rb'name="csrf" value="([A-Za-z0-9_-]+)"', body)
    assert csrf_match is not None
    cookie = headers["set-cookie"].split(";", 1)[0]
    return cookie, csrf_match.group(1).decode("ascii"), body, headers


def _decision_fields(
    server: ReviewHTTPServer,
    csrf: str,
    *,
    decision: str = "accepted",
    final_text: str = "",
    reason: str = "reviewed locally",
    risk_acknowledgement: str = "",
    change_id: str | None = None,
) -> dict[str, str]:
    approval = server.current_approval
    changeset, _ = server.review_state.snapshot()
    change = cast("Mapping[str, Any]", changeset["payload"]["changes"][0])
    return {
        "csrf": csrf,
        "revision": str(approval["payload"]["revision"]),
        "approval_sha256": compute_payload_sha256(approval),
        "change_id": cast("str", change["change_id"]) if change_id is None else change_id,
        "decision": decision,
        "final_text": final_text,
        "reason": reason,
        "risk_acknowledgement": risk_acknowledgement,
    }


def _finalize_fields(server: ReviewHTTPServer, csrf: str) -> dict[str, str]:
    approval = server.current_approval
    return {
        "csrf": csrf,
        "revision": str(approval["payload"]["revision"]),
        "approval_sha256": compute_payload_sha256(approval),
    }


def _post(
    server: ReviewHTTPServer,
    path: str,
    fields: Mapping[str, str],
    *,
    cookie: str,
    host: str | None = None,
    origin: str | None = None,
    fetch_site: str = "same-origin",
) -> Response:
    return _request(
        server,
        "POST",
        path,
        body=urlencode(fields).encode("ascii"),
        headers={
            "Host": server.expected_host if host is None else host,
            "Origin": server.origin if origin is None else origin,
            "Sec-Fetch-Site": fetch_site,
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def test_shutdown_request_bounded_drains_before_close() -> None:
    server = object.__new__(ReviewHTTPServer)
    request = _RecordingSocket(MAX_REQUEST_BYTES + 2)

    server.shutdown_request(cast("socket.socket", request))

    assert request.shutdown_modes == [socket.SHUT_WR]
    assert request.timeouts == [0.25]
    assert request.recv_sizes == [8192, 8192, 8192, 8192, 1]
    assert request.unread_bytes == 1
    assert request.closed


def test_shutdown_request_closes_when_peer_aborts_drain() -> None:
    server = object.__new__(ReviewHTTPServer)
    request = _RecordingSocket(MAX_REQUEST_BYTES, fail_shutdown=True, fail_recv=True)

    server.shutdown_request(cast("socket.socket", request))

    assert request.shutdown_modes == [socket.SHUT_WR]
    assert request.recv_sizes == [8192]
    assert request.closed


def test_page_escapes_change_data_and_sets_strict_security_headers(tmp_path: Path) -> None:
    changeset, _ = _draft()
    change = cast("dict[str, Any]", changeset["payload"]["changes"][0])
    malicious_author = '<script>alert("author")</script>'
    malicious_before = '<img src=x onerror="alert(1)">'
    malicious_after = "<& edited &>"
    malicious_evidence = '</pre><script>alert("evidence")</script>'
    malicious_diagnostic = '<svg onload="alert(2)">diagnostic</svg>'
    change["author"] = malicious_author
    change["authors"] = [malicious_author]
    change["before"] = malicious_before
    change["after"] = malicious_after
    raw_event = cast("dict[str, Any]", changeset["payload"]["raw_events"][0])
    raw_event["content"]["text"] = malicious_evidence
    raw_event["diagnostics"] = [
        {
            "diagnostic_id": stable_id("diag_", ["review-server", "escaping"]),
            "code": "W_UI_ESCAPE",
            "severity": "warning",
            "phase": "review",
            "message": malicious_diagnostic,
            "recoverable": True,
            "unit_id": change["unit_id"],
            "change_id": change["change_id"],
            "source_location": change["source_location"],
            "evidence": None,
            "remediation": malicious_evidence,
        }
    ]
    changeset = seal_envelope(changeset)
    approval = create_approval_set(changeset, decided_by=ACTOR, generated_at=TIME)

    with _running_server(changeset, approval, _outputs(tmp_path)) as server:
        cookie, csrf, body, headers = _credentials(server)

    page = body.decode("utf-8")
    assert malicious_author not in page
    assert malicious_before not in page
    assert malicious_after not in page
    assert malicious_evidence not in page
    assert malicious_diagnostic not in page
    assert "&lt;script&gt;alert(&quot;author&quot;)&lt;/script&gt;" in page
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page
    assert "&lt;&amp; edited &amp;&gt;" in page
    assert "&lt;/pre&gt;&lt;script&gt;alert(&quot;evidence&quot;)&lt;/script&gt;" in page
    assert "&lt;svg onload=&quot;alert(2)&quot;&gt;diagnostic&lt;/svg&gt;" in page
    for label in (
        "Type",
        "Kind",
        "Author",
        "Authors",
        "Time",
        "Timestamps",
        "Source",
        "Resolution",
        "Confidence",
        "Safety class",
        "Automatically applicable",
        "Evidence",
        "Decision",
        "Final text",
        "Reason",
        "Risk acknowledgement",
    ):
        assert f"<dt>{label}</dt>" in page
    assert '<th scope="col">Before</th><th scope="col">After</th>' in page
    for value in ("raw_event_ids", "resolution", "change_fingerprint"):
        assert value in page
    for heading in ("Raw evidence summary", "Change diagnostics", "Current decision"):
        assert heading in page
    assert "<script" not in page.casefold()
    assert "<style" not in page.casefold()
    assert "<link" not in page.casefold()
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["connection"] == "close"
    assert "access-control-allow-origin" not in headers
    assert "HttpOnly" in headers["set-cookie"]
    assert "SameSite=Strict" in headers["set-cookie"]
    assert len(cookie.split("=", 1)[1]) >= 64
    assert len(csrf) >= 64
    assert cookie.split("=", 1)[1] != csrf


def test_page_displays_scoped_baseline_verification_and_keeps_legacy_optional(
    tmp_path: Path,
) -> None:
    scoped_changeset, _ = _draft()
    scoped_payload = cast("dict[str, Any]", scoped_changeset["payload"])
    scoped_payload["baseline_verification"] = {
        "status": "verified_for_text_patch",
        "exported_docx_sha256": sha256_canonical("scoped-exported-docx"),
        "returned_original_docx_sha256": sha256_canonical("scoped-returned-docx"),
        "drift_status": "clean",
        "expected_bookmarks": 1,
        "verified_bookmarks": 1,
        "verified_scope": [
            "visible_text",
            "paragraph_table_structure",
            "bookmarks",
            "insert_delete_revisions",
            "move_revisions",
        ],
        "unverified_scope": [
            "paragraph_mark_revisions",
            "formatting",
            "omml",
            "images",
            "hyperlink_and_relationship_targets",
            "content_controls_and_custom_xml",
            "embedded_objects_and_alternate_content",
        ],
        "automatic_patch_scope": "plain_text_only",
        "manual_integrity_review_required": True,
        "semantic_profile": {
            "name": "word-reject-view-baseline",
            "version": "word-semantic-projection-v1",
            "configuration_sha256": sha256_canonical("scoped-baseline-profile"),
        },
    }
    scoped_changeset = seal_envelope(scoped_changeset)
    scoped_approval = create_approval_set(
        scoped_changeset,
        decided_by=ACTOR,
        generated_at=TIME,
    )
    legacy_changeset, legacy_approval = _draft()
    scoped_outputs = tmp_path / "scoped"
    legacy_outputs = tmp_path / "legacy"
    scoped_outputs.mkdir()
    legacy_outputs.mkdir()

    with _running_server(
        scoped_changeset,
        scoped_approval,
        _outputs(scoped_outputs),
    ) as server:
        scoped_status, _, scoped_body = _request(
            server,
            "GET",
            "/",
            headers={"Host": server.expected_host},
        )
    with _running_server(
        legacy_changeset,
        legacy_approval,
        _outputs(legacy_outputs),
    ) as server:
        legacy_status, _, legacy_body = _request(
            server,
            "GET",
            "/",
            headers={"Host": server.expected_host},
        )

    assert scoped_status == HTTPStatus.OK
    scoped_page = scoped_body.decode("utf-8")
    assert "Baseline verification scope" in scoped_page
    for field in (
        "status",
        "automatic_patch_scope",
        "verified_scope",
        "unverified_scope",
        "manual_integrity_review_required",
    ):
        assert f"<dt><code>{field}</code></dt>" in scoped_page
    for value in (
        "verified_for_text_patch",
        "plain_text_only",
        "visible_text",
        "paragraph_table_structure",
        "bookmarks",
        "insert_delete_revisions",
        "move_revisions",
        "paragraph_mark_revisions",
        "formatting",
        "omml",
        "images",
        "hyperlink_and_relationship_targets",
        "content_controls_and_custom_xml",
        "embedded_objects_and_alternate_content",
        "True",
    ):
        assert value in scoped_page
    assert "Manual integrity review is required" in scoped_page
    assert "not a claim that every Word document semantic was verified" in scoped_page

    assert legacy_status == HTTPStatus.OK
    assert "Baseline verification scope" not in legacy_body.decode("utf-8")


def test_comment_details_filters_progress_and_navigation(tmp_path: Path) -> None:
    changeset, approval, ids = _rich_draft()

    with _running_server(changeset, approval, _outputs(tmp_path)) as server:
        root_status, _, root_body = _request(
            server,
            "GET",
            "/",
            headers={"Host": server.expected_host},
        )
        comment_status, _, comment_body = _request(
            server,
            "GET",
            "/filter/comment",
            headers={"Host": server.expected_host},
        )
        manual_status, _, manual_body = _request(
            server,
            "GET",
            "/filter/manual",
            headers={"Host": server.expected_host},
        )
        high_risk_status, _, high_risk_body = _request(
            server,
            "GET",
            "/filter/high-risk",
            headers={"Host": server.expected_host},
        )
        all_format_status, _, all_format_body = _request(
            server,
            "GET",
            f"/change/{ids['format']}",
            headers={"Host": server.expected_host},
        )
        mismatched_status, _, _ = _request(
            server,
            "GET",
            f"/filter/comment/change/{ids['format']}",
            headers={"Host": server.expected_host},
        )

    assert root_status == HTTPStatus.OK
    root_page = root_body.decode("utf-8")
    assert (
        "Progress: total <strong>3</strong>; decided <strong>0</strong>; "
        "pending <strong>3</strong>."
    ) in root_page
    for label in ("All (3)", "Pending (3)", "Manual (1)", "Comments (1)", "High risk (1)"):
        assert label in root_page
    assert "Previous item unavailable" in root_page
    assert f'href="/filter/pending/change/{ids["comment"]}">Next pending</a>' in root_page

    assert comment_status == HTTPStatus.OK
    comment_page = comment_body.decode("utf-8")
    assert "Please verify &lt;this comment&gt;." in comment_page
    assert "Anchored &lt;phrase&gt;" in comment_page
    assert "Reviewer Two" in comment_page
    assert "Reviewer Three" in comment_page
    assert "2026-01-16T10:00:00+08:00" in comment_page
    assert "2026-01-16T10:05:00+08:00" in comment_page
    assert "ledger_only" in comment_page
    assert "<dt>Automatically applicable</dt><dd><strong>No</strong>" in comment_page
    assert "W_COMMENT_CONTEXT" in comment_page
    assert "Review &lt;comment context&gt; manually." in comment_page
    assert "Inspect &lt;the anchor&gt;." in comment_page
    assert "fragment_sha256" in comment_page
    assert "Raw evidence summary" in comment_page

    assert manual_status == HTTPStatus.OK
    assert ids["format"] in manual_body.decode("utf-8")
    assert high_risk_status == HTTPStatus.OK
    assert "manual_high_risk" in high_risk_body.decode("utf-8")
    assert all_format_status == HTTPStatus.OK
    assert f'href="/change/{ids["comment"]}">Previous item</a>' in all_format_body.decode("utf-8")
    assert mismatched_status == HTTPStatus.NOT_FOUND


def test_reopened_page_restores_decision_text_reason_and_risk(tmp_path: Path) -> None:
    changeset, approval = _draft()
    outputs = _outputs(tmp_path)
    final_text = "Approved <word>"
    reason = 'Reason <with & "quotes">'
    risk = "Acknowledged <manual risk>"

    with _running_server(changeset, approval, outputs) as server:
        cookie, csrf, _, _ = _credentials(server)
        status, _, _ = _post(
            server,
            "/decision",
            _decision_fields(
                server,
                csrf,
                decision="accepted_with_edit",
                final_text=final_text,
                reason=reason,
                risk_acknowledgement=risk,
            ),
            cookie=cookie,
        )
        reopened_status, _, reopened_body = _request(
            server,
            "GET",
            "/",
            headers={"Host": server.expected_host},
        )
        current = server.current_approval

    assert status == HTTPStatus.SEE_OTHER
    assert reopened_status == HTTPStatus.OK
    saved_decision = current["payload"]["decisions"][0]
    assert saved_decision["decision"] == "accepted_with_edit"
    assert saved_decision["final_text"] == final_text
    assert saved_decision["reason"] == reason
    assert saved_decision["risk_acknowledgement"] == risk

    page = reopened_body.decode("utf-8")
    assert final_text not in page
    assert reason not in page
    assert risk not in page
    assert "Approved &lt;word&gt;" in page
    assert "Reason &lt;with &amp; &quot;quotes&quot;&gt;" in page
    assert "Acknowledged &lt;manual risk&gt;" in page
    assert "<dt>Decision</dt><dd>accepted_with_edit</dd>" in page
    assert (
        '<textarea id="final_text" name="final_text" rows="4" cols="80">'
        "Approved &lt;word&gt;</textarea>"
    ) in page
    assert (
        "Progress: total <strong>1</strong>; decided <strong>1</strong>; "
        "pending <strong>0</strong>."
    ) in page
    assert outputs[2].is_file()


@pytest.mark.parametrize("unsafe", ["safe\ttext", "safe\u00a0text", "safe\u2028text"])
def test_ui_uses_fail_closed_unicode_whitespace_policy(
    tmp_path: Path,
    unsafe: str,
) -> None:
    changeset, approval = _policy_state(before="old", after=unsafe)

    with _running_server(changeset, approval, _outputs(tmp_path)) as server:
        _, _, body, _ = _credentials(server)

    page = body.decode("utf-8")
    assert "<dt>Automatically applicable</dt><dd><strong>No</strong>" in page
    assert "Unicode whitespace other than U+0020 SPACE" in page


def test_ui_does_not_claim_boundary_sensitive_edit_is_eligible(
    tmp_path: Path,
) -> None:
    changeset, approval = _policy_state(before="old", after=" revised", accepted=True)

    with _running_server(changeset, approval, _outputs(tmp_path, range(4, 8))) as server:
        status, _, body = _request(
            server,
            "GET",
            "/",
            headers={"Host": server.expected_host},
        )

    page = body.decode("utf-8")
    assert status == HTTPStatus.OK
    assert "<dt>Automatically applicable</dt><dd><strong>Not yet</strong>" in page
    assert "exact source prefix/suffix context is required to prove whitespace safety" in page
    assert "<strong>Eligible</strong>" not in page


def test_ui_keeps_ordinary_safe_text_eligible_after_final_approval(
    tmp_path: Path,
) -> None:
    changeset, approval = _policy_state(before="old", after="revised", accepted=True)

    with _running_server(changeset, approval, _outputs(tmp_path, range(4, 8))) as server:
        status, _, body = _request(
            server,
            "GET",
            "/",
            headers={"Host": server.expected_host},
        )

    page = body.decode("utf-8")
    assert status == HTTPStatus.OK
    assert "<dt>Automatically applicable</dt><dd><strong>Eligible</strong>" in page
    assert "separate PatchPlan/apply gate" in page


@pytest.mark.parametrize(
    ("decision", "final_text"),
    [
        ("accepted", ""),
        ("accepted_with_edit", "Author-approved wording"),
        ("rejected", ""),
        ("manual", ""),
        ("conflict", ""),
    ],
)
def test_each_decision_publishes_a_new_approval_only(
    tmp_path: Path,
    decision: str,
    final_text: str,
) -> None:
    source = tmp_path / "source.tex"
    source.write_bytes(b"immutable source\n")
    changeset, approval = _draft()
    outputs = _outputs(tmp_path)

    with _running_server(changeset, approval, outputs) as server:
        cookie, csrf, _, _ = _credentials(server)
        status, headers, _ = _post(
            server,
            "/decision",
            _decision_fields(server, csrf, decision=decision, final_text=final_text),
            cookie=cookie,
        )
        current = server.current_approval

    assert status == HTTPStatus.SEE_OTHER
    assert headers["location"] == "/"
    assert outputs[2].is_file()
    written = load_contract_json(outputs[2].read_bytes())
    written_decision = written["payload"]["decisions"][0]
    assert written["payload"]["revision"] == 2
    assert written["payload"]["status"] == "draft"
    assert written_decision["decision"] == decision
    assert written_decision["decision_source"] == "local_ui"
    assert written_decision["final_text"] == (final_text or None)
    assert current == written
    assert source.read_bytes() == b"immutable source\n"
    assert not outputs[3].exists()


def test_complete_approval_can_finalize_but_never_applies_tex(tmp_path: Path) -> None:
    source = tmp_path / "main.tex"
    original = b"\\section{Original}\n"
    source.write_bytes(original)
    changeset, approval = _draft()
    outputs = _outputs(tmp_path)

    with _running_server(changeset, approval, outputs) as server:
        cookie, csrf, _, _ = _credentials(server)
        decided, _, _ = _post(
            server,
            "/decision",
            _decision_fields(
                server,
                csrf,
                decision="accepted_with_edit",
                final_text="Carefully edited text",
            ),
            cookie=cookie,
        )
        finalized, _, _ = _post(
            server,
            "/finalize",
            _finalize_fields(server, csrf),
            cookie=cookie,
        )

    revision_two = load_contract_json(outputs[2].read_bytes())
    revision_three = load_contract_json(outputs[3].read_bytes())
    assert decided == HTTPStatus.SEE_OTHER
    assert finalized == HTTPStatus.SEE_OTHER
    assert revision_two["payload"]["status"] == "draft"
    assert revision_three["payload"]["status"] == "final"
    assert revision_three["payload"]["revision"] == 3
    assert revision_three["payload"]["decisions"][0]["final_text"] == ("Carefully edited text")
    assert source.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "approval-r2.json",
        "approval-r3.json",
        "main.tex",
    ]


def test_host_origin_fetch_site_cookie_and_csrf_are_all_required(tmp_path: Path) -> None:
    changeset, approval = _draft()
    outputs = _outputs(tmp_path)

    with _running_server(changeset, approval, outputs) as server:
        cookie, csrf, _, _ = _credentials(server)
        fields = _decision_fields(server, csrf)
        encoded = urlencode(fields).encode("ascii")
        attempts = [
            _post(server, "/decision", fields, cookie=cookie, host="evil.example"),
            _post(server, "/decision", fields, cookie=cookie, origin="http://evil.example"),
            _post(server, "/decision", fields, cookie="lwr_review_session=wrong"),
            _post(server, "/decision", fields, cookie=cookie, fetch_site="cross-site"),
            _post(server, "/decision", fields | {"csrf": "wrong"}, cookie=cookie),
            _request(
                server,
                "POST",
                "/decision",
                body=encoded,
                headers={
                    "Host": server.expected_host,
                    "Origin": server.origin,
                    "Sec-Fetch-Site": "same-origin",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            ),
            _post(server, "/decision", fields, cookie="malformed-cookie"),
            _post(server, "/decision", fields, cookie=f"{cookie}; {cookie}"),
        ]
        current = server.current_approval

    assert [attempt[0] for attempt in attempts] == [421, 403, 403, 403, 403, 403, 403, 403]
    assert all("access-control-allow-origin" not in attempt[1] for attempt in attempts)
    assert current["payload"]["revision"] == 1
    assert not outputs[2].exists()


def test_form_limits_duplicates_unknown_fields_and_paths_fail_closed(tmp_path: Path) -> None:
    changeset, approval = _draft()
    outputs = _outputs(tmp_path)

    with _running_server(changeset, approval, outputs) as server:
        cookie, csrf, _, _ = _credentials(server)
        fields = _decision_fields(server, csrf)
        base_headers = {
            "Host": server.expected_host,
            "Origin": server.origin,
            "Sec-Fetch-Site": "same-origin",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        encoded = urlencode(fields).encode("ascii")
        oversized = _request(
            server,
            "POST",
            "/decision",
            body=b"x" * (MAX_REQUEST_BYTES + 1),
            headers=base_headers,
        )
        duplicate = _request(
            server,
            "POST",
            "/decision",
            body=encoded + b"&csrf=duplicate",
            headers=base_headers,
        )
        unknown = _request(
            server,
            "POST",
            "/decision",
            body=urlencode(fields | {"unexpected": "true"}).encode("ascii"),
            headers=base_headers,
        )
        missing_risk = _request(
            server,
            "POST",
            "/decision",
            body=urlencode(
                {name: value for name, value in fields.items() if name != "risk_acknowledgement"}
            ).encode("ascii"),
            headers=base_headers,
        )
        wrong_media = _request(
            server,
            "POST",
            "/decision",
            body=b"{}",
            headers=base_headers | {"Content-Type": "application/json"},
        )
        traversal = _request(
            server,
            "GET",
            "/%2e%2e/secret",
            headers={"Host": server.expected_host},
        )
        filter_query = _request(
            server,
            "GET",
            "/filter/pending?unexpected=true",
            headers={"Host": server.expected_host},
        )
        options = _request(
            server,
            "OPTIONS",
            "/decision",
            headers={"Host": server.expected_host},
        )
        missing_get = _request(
            server,
            "GET",
            "/missing",
            headers={"Host": server.expected_host},
        )
        unknown_change = _request(
            server,
            "GET",
            "/change/chg_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            headers={"Host": server.expected_host},
        )
        missing_post = _request(
            server,
            "POST",
            "/missing",
            body=encoded,
            headers=base_headers,
        )

    attempts = (
        oversized,
        duplicate,
        unknown,
        missing_risk,
        wrong_media,
        traversal,
        filter_query,
        options,
        missing_get,
        unknown_change,
        missing_post,
    )
    assert [attempt[0] for attempt in attempts] == [
        413,
        400,
        400,
        400,
        415,
        400,
        400,
        405,
        404,
        404,
        404,
    ]
    assert all("access-control-allow-origin" not in attempt[1] for attempt in attempts)
    assert not outputs[2].exists()


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "192.0.2.1"])
def test_non_loopback_bind_configuration_is_rejected(tmp_path: Path, host: str) -> None:
    changeset, approval = _draft()
    with pytest.raises(ContractError) as raised:
        create_review_server(
            changeset,
            approval,
            approval_output_paths=_outputs(tmp_path),
            host=host,
        )
    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_concurrent_stale_revision_has_exactly_one_winner(tmp_path: Path) -> None:
    changeset, approval = _draft()
    outputs = _outputs(tmp_path)

    with _running_server(changeset, approval, outputs) as server:
        cookie, csrf, _, _ = _credentials(server)
        stale_fields = _decision_fields(server, csrf)

        def submit() -> int:
            return _post(server, "/decision", stale_fields, cookie=cookie)[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            statuses = sorted(future.result(timeout=5) for future in futures)
        current = server.current_approval

    assert statuses == [HTTPStatus.SEE_OTHER, HTTPStatus.CONFLICT]
    assert current["payload"]["revision"] == 2
    assert outputs[2].is_file()
    assert not outputs[3].exists()


def test_preexisting_approval_output_is_never_clobbered(tmp_path: Path) -> None:
    changeset, approval = _draft()
    outputs = _outputs(tmp_path)
    outputs[2].write_bytes(b"occupied")

    with _running_server(changeset, approval, outputs) as server:
        cookie, csrf, _, _ = _credentials(server)
        status, _, _ = _post(
            server,
            "/decision",
            _decision_fields(server, csrf),
            cookie=cookie,
        )
        current = server.current_approval

    assert status == HTTPStatus.BAD_REQUEST
    assert current["payload"]["revision"] == 1
    assert outputs[2].read_bytes() == b"occupied"
    assert not outputs[3].exists()


def test_empty_changeset_renders_and_finalizes_safely(tmp_path: Path) -> None:
    changeset, _ = _draft()
    changeset["payload"]["changes"] = []
    changeset["payload"]["counts"]["changes"] = 0
    changeset = seal_envelope(changeset)
    approval = create_approval_set(changeset, decided_by=ACTOR, generated_at=TIME)
    outputs = _outputs(tmp_path)

    with _running_server(changeset, approval, outputs) as server:
        cookie, csrf, body, _ = _credentials(server)
        status, _, _ = _post(
            server,
            "/finalize",
            _finalize_fields(server, csrf),
            cookie=cookie,
        )

    final = load_contract_json(outputs[2].read_bytes())
    assert b"No changes were found." in body
    assert status == HTTPStatus.SEE_OTHER
    assert final["payload"]["status"] == "final"
    assert final["payload"]["decisions"] == []
