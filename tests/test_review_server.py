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

from latex_word_review.approval import create_approval_set
from latex_word_review.canonical import seal_envelope
from latex_word_review.contracts import compute_payload_sha256, load_contract_json
from latex_word_review.errors import ContractError, ErrorCode
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
) -> dict[str, str]:
    approval = server.current_approval
    changeset, _ = server.review_state.snapshot()
    change = cast("Mapping[str, Any]", changeset["payload"]["changes"][0])
    return {
        "csrf": csrf,
        "revision": str(approval["payload"]["revision"]),
        "approval_sha256": compute_payload_sha256(approval),
        "change_id": cast("str", change["change_id"]),
        "decision": decision,
        "final_text": final_text,
        "reason": "reviewed locally",
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
    change["author"] = malicious_author
    change["authors"] = [malicious_author]
    change["before"] = malicious_before
    change["after"] = malicious_after
    changeset = seal_envelope(changeset)
    approval = create_approval_set(changeset, decided_by=ACTOR, generated_at=TIME)

    with _running_server(changeset, approval, _outputs(tmp_path)) as server:
        cookie, csrf, body, headers = _credentials(server)

    page = body.decode("utf-8")
    assert malicious_author not in page
    assert malicious_before not in page
    assert malicious_after not in page
    assert "&lt;script&gt;alert(&quot;author&quot;)&lt;/script&gt;" in page
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page
    assert "&lt;&amp; edited &amp;&gt;" in page
    for label in ("Type", "Author", "Time", "Before", "After", "Source", "Evidence"):
        assert f"<dt>{label}</dt>" in page
    for value in ("raw_event_ids", "resolution", "change_fingerprint"):
        assert value in page
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
        wrong_media,
        traversal,
        options,
        missing_get,
        unknown_change,
        missing_post,
    )
    assert [attempt[0] for attempt in attempts] == [413, 400, 400, 415, 400, 405, 404, 404, 404]
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
