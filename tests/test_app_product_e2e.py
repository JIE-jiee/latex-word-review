"""HTTP-level product journey for the Windows local application.

The test uses only the redistributable E0 fixture.  It exercises the rendered
forms, cookie/CSRF gates, background jobs, durable restart recovery, both human
approval gates, and the downloadable final evidence.  No sealed contract is
written by the test.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import pytest

from latex_word_review.app_server import (
    SESSION_COOKIE,
    AppHTTPServer,
    create_app_server,
)
from latex_word_review.application import ApplicationSession
from latex_word_review.contracts import compute_payload_sha256
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_file
from latex_word_review.jsonio import read_contract_file
from tests.test_e2e_public_roundtrip import _fake_tools
from tests.test_workflow import _make_returned

Response = tuple[int, dict[str, str], bytes]
Picker = Callable[[Path | None], Path | None]

FIXTURE_SOURCE = (Path(__file__).parent / "fixtures/e0-minimal-paper/source").resolve()
MAIN_TEX = FIXTURE_SOURCE / "main.tex"
MAX_TEST_DOCX_BYTES = 64 * 1024 * 1024


@contextmanager
def _serving(
    data_root: Path,
    *,
    main_picker: Picker,
    word_picker: Picker,
) -> Iterator[AppHTTPServer]:
    server = create_app_server(
        data_root,
        main_picker=main_picker,
        word_picker=word_picker,
    )
    worker = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def _request(
    server: AppHTTPServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    host, port = cast("tuple[str, int]", server.server_address)
    connection = HTTPConnection(host, port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {name.casefold(): value for name, value in response.getheaders()}
        return response.status, response_headers, response_body
    finally:
        connection.close()


def _credentials(server: AppHTTPServer) -> tuple[str, str, str]:
    status, headers, body = _request(
        server,
        "GET",
        "/",
        headers={"Host": server.expected_host},
    )
    assert status == HTTPStatus.OK
    match = re.search(rb'name="csrf" value="([A-Za-z0-9_-]+)"', body)
    assert match is not None
    cookie = headers["set-cookie"].split(";", 1)[0]
    assert cookie.startswith(f"{server.session_cookie_name}=")
    assert server.session_cookie_name.startswith(f"{SESSION_COOKIE}_")
    return cookie, match.group(1).decode("ascii"), body.decode("utf-8")


def _post(
    server: AppHTTPServer,
    path: str,
    fields: Mapping[str, str],
    *,
    cookie: str,
) -> Response:
    return _request(
        server,
        "POST",
        path,
        body=urlencode(fields).encode("ascii"),
        headers={
            "Host": server.expected_host,
            "Origin": server.origin,
            "Sec-Fetch-Site": "same-origin",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def _private_get(server: AppHTTPServer, path: str, *, cookie: str) -> Response:
    return _request(
        server,
        "GET",
        path,
        headers={"Host": server.expected_host, "Cookie": cookie},
    )


def _job_location(response: Response) -> str:
    assert response[0] == HTTPStatus.SEE_OTHER
    location = response[1]["location"]
    assert re.fullmatch(r"/jobs/job_[0-9a-f]{32}", location) is not None
    return location


def _await_job(
    server: AppHTTPServer,
    job_location: str,
    *,
    cookie: str,
    timeout_s: float = 90.0,
) -> Response:
    deadline = time.monotonic() + timeout_s
    last: Response | None = None
    while time.monotonic() < deadline:
        last = _private_get(server, job_location, cookie=cookie)
        if last[0] in {HTTPStatus.SEE_OTHER, HTTPStatus.CONFLICT}:
            return last
        assert last[0] == HTTPStatus.OK, last[2].decode("utf-8", errors="replace")
        time.sleep(0.02)
    pytest.fail(f"background job did not finish within {timeout_s}s: {last!r}")


def _source_bytes() -> dict[Path, bytes]:
    return {
        path.relative_to(FIXTURE_SOURCE): path.read_bytes()
        for path in FIXTURE_SOURCE.rglob("*")
        if path.is_file()
    }


def test_local_app_full_product_journey_survives_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete one public review through real HTTP forms and sealed evidence."""

    assert MAIN_TEX.is_file()
    source_before = _source_bytes()
    # Keep this Windows-only journey root deliberately short: pytest's
    # descriptive temporary directory is already close to legacy MAX_PATH,
    # while this test is about the user workflow rather than long-path policy.
    data_root = tmp_path
    returned_docx = tmp_path / "r.docx"

    def main_picker(_initial: Path | None) -> Path | None:
        return MAIN_TEX

    def word_picker(_initial: Path | None) -> Path | None:
        return returned_docx

    original_begin = ApplicationSession.begin_approval
    interrupted = False

    def interrupt_first_begin(
        self: ApplicationSession,
        *,
        actor_id: str,
        actor_name: str,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "synthetic interruption after the returned Word was sealed",
            )
        return original_begin(
            self,
            actor_id=actor_id,
            actor_name=actor_name,
            generated_at=generated_at,
        )

    monkeypatch.setattr(ApplicationSession, "begin_approval", interrupt_first_begin)

    with _serving(
        data_root,
        main_picker=main_picker,
        word_picker=word_picker,
    ) as server:
        cookie, csrf, home = _credentials(server)
        assert "新建审阅" in home

        selected = _post(server, "/new", {"csrf": csrf}, cookie=cookie)
        assert selected[0] == HTTPStatus.SEE_OTHER
        preflight_location = selected[1]["location"]
        selection_match = re.fullmatch(
            r"/preflight/(selection_[0-9a-f]{32})",
            preflight_location,
        )
        assert selection_match is not None
        selection_token = selection_match.group(1)
        preflight = _private_get(server, preflight_location, cookie=cookie)
        assert preflight[0] == HTTPStatus.OK
        preflight_page = preflight[2].decode("utf-8")
        assert "main.tex" in preflight_page
        assert 'action="/session/export"' in preflight_page

        export = _post(
            server,
            "/session/export",
            {"csrf": csrf, "selection": selection_token},
            cookie=cookie,
        )
        exported = _await_job(server, _job_location(export), cookie=cookie)
        assert exported[0] == HTTPStatus.SEE_OTHER
        session_href = exported[1]["location"]
        session_match = re.fullmatch(r"/session/(session_[0-9a-f]{32})", session_href)
        assert session_match is not None
        session_key = session_match.group(1)
        run_root = data_root / "runs" / session_key

        waiting = _private_get(server, session_href, cookie=cookie)
        assert waiting[0] == HTTPStatus.OK
        waiting_page = waiting[2].decode("utf-8")
        assert "Word 审阅交接与修改稿导入" in waiting_page
        assert 'action="/session/receive"' in waiting_page

        review_docx = run_root / "export/review.docx"
        assert review_docx.is_file()
        review_digest = digest_file(review_docx, max_bytes=MAX_TEST_DOCX_BYTES)
        _make_returned(run_root, returned_docx)
        returned_digest = digest_file(returned_docx, max_bytes=MAX_TEST_DOCX_BYTES)

        receive = _post(
            server,
            "/session/receive",
            {"csrf": csrf, "session": session_key},
            cookie=cookie,
        )
        receive_failed = _await_job(server, _job_location(receive), cookie=cookie)
        assert receive_failed[0] == HTTPStatus.CONFLICT
        assert ErrorCode.INTERNAL_INVARIANT.value in receive_failed[2].decode("utf-8")
        assert interrupted is True

        recovery_page = _private_get(server, session_href, cookie=cookie)
        assert recovery_page[0] == HTTPStatus.OK
        assert "恢复审批账本" in recovery_page[2].decode("utf-8")
        assert ApplicationSession.load(run_root).status()["phase"] == "approval_required"

    # A brand-new AppState has no in-memory selection or job registry.  It must
    # discover the interrupted session from the sealed run directory alone.
    with _serving(
        data_root,
        main_picker=main_picker,
        word_picker=word_picker,
    ) as server:
        cookie, csrf, restored_home = _credentials(server)
        assert f'href="{session_href}"' in restored_home
        restored = _private_get(server, session_href, cookie=cookie)
        assert restored[0] == HTTPStatus.OK
        restored_page = restored[2].decode("utf-8")
        assert "恢复审批账本" in restored_page
        assert 'action="/approval/start"' in restored_page

        start = _post(
            server,
            "/approval/start",
            {"csrf": csrf, "session": session_key},
            cookie=cookie,
        )
        started = _await_job(server, _job_location(start), cookie=cookie)
        assert started[0] == HTTPStatus.SEE_OTHER
        assert started[1]["location"] == session_href

        approval = _private_get(server, session_href, cookie=cookie)
        assert approval[0] == HTTPStatus.OK
        approval_page = approval[2].decode("utf-8")
        change_ids = set(re.findall(r'name="change_id" value="([^"]+)"', approval_page))
        assert len(change_ids) == 1
        change_id = change_ids.pop()
        assert "修改前" in approval_page
        assert "修改后" in approval_page

        decision = _post(
            server,
            "/approval/decision",
            {
                "csrf": csrf,
                "session": session_key,
                "change_id": change_id,
                "decision": "accepted",
                "final_text": "",
                "reason": "public E0 product journey",
                "risk_acknowledgement": "",
                "return_filter": "all",
                "return_page": "1",
            },
            cookie=cookie,
        )
        assert decision[0] == HTTPStatus.SEE_OTHER
        assert decision[1]["location"] == f"{session_href}?filter=all&page=1#approval-change-list"
        decided = _private_get(server, session_href, cookie=cookie)
        decided_page = decided[2].decode("utf-8")
        assert "已决定 1 / 1 项" in decided_page
        assert 'action="/approval/finalize"' in decided_page

        finalize = _post(
            server,
            "/approval/finalize",
            {"csrf": csrf, "session": session_key},
            cookie=cookie,
        )
        finalized = _await_job(server, _job_location(finalize), cookie=cookie)
        assert finalized[0] == HTTPStatus.SEE_OTHER
        assert finalized[1]["location"] == session_href

        patch = _private_get(server, session_href, cookie=cookie)
        assert patch[0] == HTTPStatus.OK
        patch_page = patch[2].decode("utf-8")
        assert "第二道闸门" in patch_page
        assert "--- a/" in patch_page and "+++ b/" in patch_page
        assert " reviewed" in patch_page
        hash_match = re.search(
            r'name="patch_plan_sha256" value="(sha256:[0-9a-f]{64})"',
            patch_page,
        )
        assert hash_match is not None
        plan_hash: str = hash_match.group(1)

        sealed = ApplicationSession.load(run_root).status()
        artifacts = cast("Mapping[str, str]", sealed["artifacts"])
        plan = read_contract_file(
            run_root / artifacts["patch_plan"],
            expected_schema="PatchPlan",
        )
        confirmation = cast("Mapping[str, object]", sealed["apply_confirmation"])
        confirmation_hash = confirmation["patch_plan_sha256"]
        assert isinstance(confirmation_hash, str)
        assert plan_hash == compute_payload_sha256(plan)
        assert confirmation_hash == plan_hash

        wrong_gate = _post(
            server,
            "/result/generate",
            {
                "csrf": csrf,
                "session": session_key,
                "patch_plan_sha256": "sha256:" + "0" * 64,
                "confirm_apply": "yes",
            },
            cookie=cookie,
        )
        rejected = _await_job(server, _job_location(wrong_gate), cookie=cookie)
        assert rejected[0] == HTTPStatus.CONFLICT
        assert ErrorCode.HASH_PATCHPLAN_MISMATCH.value in rejected[2].decode("utf-8")
        assert not (run_root / "revised-clean").exists()

        tool_calls: list[tuple[str, tuple[str, ...]]] = []
        monkeypatch.setattr(
            "latex_word_review.latex_verify.run_command",
            _fake_tools(tool_calls),
        )
        generate = _post(
            server,
            "/result/generate",
            {
                "csrf": csrf,
                "session": session_key,
                "patch_plan_sha256": plan_hash,
                "confirm_apply": "yes",
            },
            cookie=cookie,
        )
        generated = _await_job(server, _job_location(generate), cookie=cookie)
        assert generated[0] == HTTPStatus.SEE_OTHER
        assert generated[1]["location"] == session_href
        assert tool_calls

        result = _private_get(server, session_href, cookie=cookie)
        assert result[0] == HTTPStatus.OK
        result_page = result[2].decode("utf-8")
        assert "全部结果已生成" in result_page
        assert f"/artifact/{session_key}/ledger_json" in result_page
        assert f"/artifact/{session_key}/audit_bundle" in result_page

        final_status = ApplicationSession.load(run_root).status()
        assert final_status["phase"] == "completed"
        assert (run_root / "delivery/ledger.json").is_file()
        assert (run_root / "delivery/ledger.html").is_file()
        assert (run_root / "audit.zip").is_file()
        assert digest_file(review_docx, max_bytes=MAX_TEST_DOCX_BYTES) == review_digest
        assert digest_file(returned_docx, max_bytes=MAX_TEST_DOCX_BYTES) == returned_digest
        assert _source_bytes() == source_before

    # Completion is also durable: a second fresh process can render and serve
    # the final artifacts without any old in-memory job state.
    with _serving(
        data_root,
        main_picker=main_picker,
        word_picker=word_picker,
    ) as server:
        cookie, _csrf, restored_home = _credentials(server)
        assert f'href="{session_href}"' in restored_home
        assert "打开时核验" in restored_home
        restored_result = _private_get(server, session_href, cookie=cookie)
        assert restored_result[0] == HTTPStatus.OK
        assert "全部结果已生成" in restored_result[2].decode("utf-8")
        assert ApplicationSession.load(run_root).status()["phase"] == "completed"

        ledger = _private_get(
            server,
            f"/artifact/{session_key}/ledger_json",
            cookie=cookie,
        )
        bundle = _private_get(
            server,
            f"/artifact/{session_key}/audit_bundle",
            cookie=cookie,
        )
        assert ledger[0] == HTTPStatus.OK
        assert ledger[1]["content-type"] == "application/json"
        assert ledger[2] == (run_root / "delivery/ledger.json").read_bytes()
        assert bundle[0] == HTTPStatus.OK
        assert bundle[1]["content-type"] == "application/zip"
        assert bundle[2] == (run_root / "audit.zip").read_bytes()
