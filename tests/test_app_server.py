"""Focused security and UX tests for the local Windows application server."""

from __future__ import annotations

import re
import shutil
import socket
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import urlencode

import pytest

import latex_word_review.app_server as app_server_module
from latex_word_review.app_jobs import JobSnapshot
from latex_word_review.app_server import (
    LOOPBACK_HOST,
    MAX_FORM_BYTES,
    SESSION_COOKIE,
    AppHTTPServer,
    AppRequestHandler,
    AppState,
    create_app_server,
)
from latex_word_review.app_views import render_app_page
from latex_word_review.application import ApplicationSession
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.jsonio import read_contract_file
from tests.test_workflow import FIXTURE, _make_returned

Response = tuple[int, dict[str, str], bytes]
Picker = Callable[[Path | None], Path | None]


def _cancel_picker(_initial: Path | None) -> Path | None:
    return None


@contextmanager
def _serving(server: AppHTTPServer) -> Iterator[AppHTTPServer]:
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


@contextmanager
def _running_server(
    data_root: Path,
    *,
    main_picker: Picker = _cancel_picker,
) -> Iterator[AppHTTPServer]:
    with _serving(create_app_server(data_root, main_picker=main_picker)) as server:
        yield server


def _request(
    server: AppHTTPServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    host, port = cast("tuple[str, int]", server.server_address)
    connection = HTTPConnection(host, port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {name.casefold(): value for name, value in response.getheaders()}
        return response.status, response_headers, response_body
    finally:
        connection.close()


def _manual_get(
    server: AppHTTPServer,
    headers: tuple[tuple[str, str], ...],
) -> Response:
    host, port = cast("tuple[str, int]", server.server_address)
    connection = HTTPConnection(host, port, timeout=5)
    try:
        connection.putrequest(
            "GET",
            "/",
            skip_host=True,
            skip_accept_encoding=True,
        )
        for name, value in headers:
            connection.putheader(name, value)
        connection.putheader("Connection", "close")
        connection.endheaders()
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {name.casefold(): value for name, value in response.getheaders()}
        return response.status, response_headers, response_body
    finally:
        connection.close()


def _credentials(server: AppHTTPServer) -> tuple[str, str, bytes, dict[str, str]]:
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


def _post_headers(server: AppHTTPServer, cookie: str) -> dict[str, str]:
    return {
        "Host": server.expected_host,
        "Origin": server.origin,
        "Sec-Fetch-Site": "same-origin",
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _post_form(
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
        headers=_post_headers(server, cookie),
    )


def _redirected_job_id(response: Response) -> str:
    assert response[0] == HTTPStatus.SEE_OTHER
    match = re.fullmatch(r"/jobs/(job_[0-9a-f]{32})", response[1]["location"])
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "192.0.2.1"])
def test_server_rejects_every_non_ipv4_loopback_bind(tmp_path: Path, host: str) -> None:
    with pytest.raises(ContractError) as raised:
        create_app_server(tmp_path / "app", host=host)

    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_server_binds_exactly_to_ipv4_loopback(tmp_path: Path) -> None:
    with _running_server(tmp_path / "app") as server:
        host, _port = cast("tuple[str, int]", server.server_address)

    assert host == LOOPBACK_HOST
    assert server.origin.startswith("http://127.0.0.1:")


def test_host_header_must_be_the_single_exact_bound_authority(tmp_path: Path) -> None:
    with _running_server(tmp_path / "app") as server:
        wrong = _request(
            server,
            "GET",
            "/",
            headers={"Host": "evil.example"},
        )
        missing = _manual_get(server, ())
        duplicate = _manual_get(
            server,
            (("Host", server.expected_host), ("Host", server.expected_host)),
        )

    assert [wrong[0], missing[0], duplicate[0]] == [421, 421, 421]
    assert all(
        "access-control-allow-origin" not in response[1] for response in (wrong, missing, duplicate)
    )


def test_home_bootstraps_cookie_csrf_and_self_only_styles(tmp_path: Path) -> None:
    with _running_server(tmp_path / "app") as server:
        cookie, csrf, body, headers = _credentials(server)
        cookie_name = server.session_cookie_name

    page = body.decode("utf-8")
    csp = headers["content-security-policy"]
    assert cookie.startswith(f"{cookie_name}=")
    assert cookie_name.startswith(f"{SESSION_COOKIE}_")
    assert "HttpOnly" in headers["set-cookie"]
    assert "SameSite=Strict" in headers["set-cookie"]
    assert cookie.split("=", 1)[1] != csrf
    assert len(cookie.split("=", 1)[1]) >= 64
    assert len(csrf) >= 64
    assert "default-src 'none'" in csp
    assert "style-src 'self'" in csp
    assert "form-action 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert '<link rel="stylesheet" href="/assets/app.css">' in page
    assert "<style" not in page.casefold()
    assert "<script" not in page.casefold()
    assert headers["cache-control"] == "no-store, max-age=0"
    assert headers["x-frame-options"] == "DENY"
    assert headers["connection"] == "close"


def test_windowed_app_exit_is_visible_post_only_and_waits_for_active_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _running_server(tmp_path / "app") as server:
        cookie, csrf, body, _headers = _credentials(server)
        home = body.decode("utf-8")
        exit_form = re.search(
            r'<form method="post" action="/app/exit">(.*?)</form>',
            home,
            flags=re.DOTALL,
        )
        assert exit_form is not None
        assert 'name="csrf"' in exit_form.group(1)
        assert 'name="session"' not in exit_form.group(1)

        get_attempt = _request(
            server,
            "GET",
            "/app/exit",
            headers={"Host": server.expected_host, "Cookie": cookie},
        )
        cross_origin = _request(
            server,
            "POST",
            "/app/exit",
            body=urlencode({"csrf": csrf}).encode("ascii"),
            headers=_post_headers(server, cookie) | {"Origin": "http://evil.example"},
        )
        monkeypatch.setattr(server.app_state.jobs, "has_active_jobs", lambda: True)
        accepted = _post_form(
            server,
            "/app/exit",
            {"csrf": csrf},
            cookie=cookie,
        )

    accepted_page = accepted[2].decode("utf-8")
    assert get_attempt[0] == HTTPStatus.NOT_FOUND
    assert cross_origin[0] == HTTPStatus.FORBIDDEN
    assert accepted[0] == HTTPStatus.OK
    assert "已请求退出" in accepted_page
    assert "正在等待后台任务安全结束" in accepted_page
    assert "<form" not in accepted_page


def test_private_get_requires_bootstrap_cookie_but_css_does_not(tmp_path: Path) -> None:
    session_key = f"session_{'a' * 32}"
    with _running_server(tmp_path / "app") as server:
        private = _request(
            server,
            "GET",
            f"/session/{session_key}",
            headers={"Host": server.expected_host},
        )
        css = _request(
            server,
            "GET",
            "/assets/app.css",
            headers={"Host": server.expected_host},
        )

    assert private[0] == HTTPStatus.FORBIDDEN
    assert css[0] == HTTPStatus.OK
    assert css[1]["content-type"] == "text/css; charset=utf-8"
    assert css[1]["content-security-policy"].startswith("default-src 'none'")
    assert "set-cookie" not in css[1]
    assert b":root" in css[2]
    assert b"--primary:" in css[2]


def test_post_requires_origin_cookie_and_csrf_as_independent_gates(tmp_path: Path) -> None:
    picker_calls = 0

    def cancel_picker(_initial: Path | None) -> Path | None:
        nonlocal picker_calls
        picker_calls += 1
        return None

    with _running_server(tmp_path / "app", main_picker=cancel_picker) as server:
        cookie, csrf, _body, _headers = _credentials(server)
        valid_body = urlencode({"csrf": csrf}).encode("ascii")
        valid_headers = _post_headers(server, cookie)
        rejected_origins = [
            _request(
                server,
                "POST",
                "/new",
                body=valid_body,
                headers={name: value for name, value in valid_headers.items() if name != "Origin"},
            ),
            _request(
                server,
                "POST",
                "/new",
                body=valid_body,
                headers=valid_headers | {"Origin": "http://evil.example"},
            ),
            _request(
                server,
                "POST",
                "/new",
                body=valid_body,
                headers=valid_headers | {"Sec-Fetch-Site": "cross-site"},
            ),
        ]
        stale_form = _request(
            server,
            "POST",
            "/new",
            body=urlencode({"csrf": "wrong"}).encode("ascii"),
            headers=valid_headers,
        )
        stale_cookie = _request(
            server,
            "POST",
            "/new",
            body=valid_body,
            headers=valid_headers | {"Cookie": f"{server.session_cookie_name}=wrong"},
        )
        missing_cookie = _request(
            server,
            "POST",
            "/new",
            body=valid_body,
            headers={name: value for name, value in valid_headers.items() if name != "Cookie"},
        )
        legacy_cookie = _request(
            server,
            "POST",
            "/new",
            body=valid_body,
            headers=valid_headers | {"Cookie": f"{SESSION_COOKIE}=legacy"},
        )
        valid_without_fetch_metadata = _request(
            server,
            "POST",
            "/new",
            body=valid_body,
            headers={
                name: value for name, value in valid_headers.items() if name != "Sec-Fetch-Site"
            },
        )
        valid_with_legacy_cookie = _request(
            server,
            "POST",
            "/new",
            body=valid_body,
            headers=valid_headers | {"Cookie": f"{SESSION_COOKIE}=legacy; {cookie}"},
        )
        valid = _post_form(server, "/new", {"csrf": csrf}, cookie=cookie)

    assert [response[0] for response in rejected_origins] == [403, 403, 403]
    assert all("access-control-allow-origin" not in response[1] for response in rejected_origins)
    assert stale_form[0] == HTTPStatus.SEE_OTHER
    assert stale_form[1]["location"] == "/"
    assert stale_form[1]["set-cookie"].startswith(f"{server.session_cookie_name}=")
    assert stale_cookie[0] == HTTPStatus.SEE_OTHER
    assert stale_cookie[1]["location"] == "/"
    assert stale_cookie[1]["set-cookie"].startswith(f"{server.session_cookie_name}=")
    assert [missing_cookie[0], legacy_cookie[0]] == [
        HTTPStatus.SEE_OTHER,
        HTTPStatus.SEE_OTHER,
    ]
    assert missing_cookie[1]["set-cookie"].startswith(f"{server.session_cookie_name}=")
    assert legacy_cookie[1]["set-cookie"].startswith(f"{server.session_cookie_name}=")
    assert valid_without_fetch_metadata[0] == HTTPStatus.SEE_OTHER
    assert valid_with_legacy_cookie[0] == HTTPStatus.SEE_OTHER
    assert valid[0] == HTTPStatus.SEE_OTHER
    assert valid[1]["location"] == "/"
    assert picker_calls == 3


def test_opaque_origin_requires_exact_same_origin_fetch_metadata(tmp_path: Path) -> None:
    picker_calls = 0

    def cancel_picker(_initial: Path | None) -> Path | None:
        nonlocal picker_calls
        picker_calls += 1
        return None

    with _running_server(tmp_path / "app", main_picker=cancel_picker) as server:
        cookie, csrf, _body, _headers = _credentials(server)
        body = urlencode({"csrf": csrf}).encode("ascii")
        valid_headers = _post_headers(server, cookie) | {"Origin": "null"}
        accepted = _request(server, "POST", "/new", body=body, headers=valid_headers)
        missing_fetch = _request(
            server,
            "POST",
            "/new",
            body=body,
            headers={
                name: value for name, value in valid_headers.items() if name != "Sec-Fetch-Site"
            },
        )
        cross_site = _request(
            server,
            "POST",
            "/new",
            body=body,
            headers=valid_headers | {"Sec-Fetch-Site": "cross-site"},
        )
        same_site = _request(
            server,
            "POST",
            "/new",
            body=body,
            headers=valid_headers | {"Sec-Fetch-Site": "same-site"},
        )
        no_site = _request(
            server,
            "POST",
            "/new",
            body=body,
            headers=valid_headers | {"Sec-Fetch-Site": "none"},
        )
        stale_form = _request(
            server,
            "POST",
            "/new",
            body=urlencode({"csrf": "wrong"}).encode("ascii"),
            headers=valid_headers,
        )
        stale_cookie = _request(
            server,
            "POST",
            "/new",
            body=body,
            headers=valid_headers | {"Cookie": f"{server.session_cookie_name}=wrong"},
        )

    assert accepted[0] == HTTPStatus.SEE_OTHER
    assert accepted[1]["location"] == "/"
    assert [missing_fetch[0], cross_site[0], same_site[0], no_site[0]] == [
        HTTPStatus.FORBIDDEN,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.FORBIDDEN,
    ]
    assert stale_form[0] == HTTPStatus.SEE_OTHER
    assert stale_form[1]["location"] == "/"
    assert stale_cookie[0] == HTTPStatus.SEE_OTHER
    assert stale_cookie[1]["location"] == "/"
    assert picker_calls == 1


def test_parallel_instances_use_port_scoped_cookie_names(tmp_path: Path) -> None:
    calls: list[str] = []

    def cancel_a(_initial: Path | None) -> Path | None:
        calls.append("a")
        return None

    def cancel_b(_initial: Path | None) -> Path | None:
        calls.append("b")
        return None

    with (
        _running_server(tmp_path / "a", main_picker=cancel_a) as server_a,
        _running_server(tmp_path / "b", main_picker=cancel_b) as server_b,
    ):
        cookie_a, csrf_a, _body_a, _headers_a = _credentials(server_a)
        cookie_b, csrf_b, _body_b, _headers_b = _credentials(server_b)
        combined_cookie = f"{cookie_a}; {cookie_b}"
        selected_a = _post_form(server_a, "/new", {"csrf": csrf_a}, cookie=combined_cookie)
        selected_b = _post_form(server_b, "/new", {"csrf": csrf_b}, cookie=combined_cookie)

    assert server_a.session_cookie_name != server_b.session_cookie_name
    assert selected_a[0] == HTTPStatus.SEE_OTHER
    assert selected_b[0] == HTTPStatus.SEE_OTHER
    assert calls == ["a", "b"]


def test_forms_reject_duplicates_unknown_fields_and_oversize_bodies(tmp_path: Path) -> None:
    with _running_server(tmp_path / "app") as server:
        cookie, csrf, _body, _headers = _credentials(server)
        headers = _post_headers(server, cookie)
        duplicate = _request(
            server,
            "POST",
            "/new",
            body=f"csrf={csrf}&csrf={csrf}".encode("ascii"),
            headers=headers,
        )
        unknown = _request(
            server,
            "POST",
            "/new",
            body=urlencode({"csrf": csrf, "unexpected": "1"}).encode("ascii"),
            headers=headers,
        )
        oversized = _request(
            server,
            "POST",
            "/new",
            body=b"x" * (MAX_FORM_BYTES + 1),
            headers=headers,
        )

    assert [duplicate[0], unknown[0], oversized[0]] == [400, 400, 413]


def test_request_target_rejects_traversal_and_absolute_uri(tmp_path: Path) -> None:
    with _running_server(tmp_path / "app") as server:
        traversal = _request(
            server,
            "GET",
            "/%2e%2e/secret",
            headers={"Host": server.expected_host},
        )
        dotted = _request(
            server,
            "GET",
            "/assets/../app.css",
            headers={"Host": server.expected_host},
        )
        absolute = _request(
            server,
            "GET",
            server.origin + "/",
            headers={"Host": server.expected_host},
        )

    assert [traversal[0], dotted[0], absolute[0]] == [400, 400, 400]


def test_picker_cancel_is_a_safe_noop_and_preflight_uses_selected_copy(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path / "cancel") as server:
        cookie, csrf, _body, _headers = _credentials(server)
        cancelled = _post_form(server, "/new", {"csrf": csrf}, cookie=cookie)

    project = tmp_path / "paper"
    project.mkdir()
    main_file = project / "main.tex"
    main_file.write_text(
        "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n",
        encoding="utf-8",
    )

    def choose_main(_initial: Path | None) -> Path | None:
        return main_file

    with _running_server(tmp_path / "selected", main_picker=choose_main) as server:
        cookie, csrf, _body, _headers = _credentials(server)
        selected = _post_form(server, "/new", {"csrf": csrf}, cookie=cookie)
        location = selected[1]["location"]
        preflight = _request(
            server,
            "GET",
            location,
            headers={"Host": server.expected_host, "Cookie": cookie},
        )

    assert cancelled[0] == HTTPStatus.SEE_OTHER
    assert cancelled[1]["location"] == "/"
    assert selected[0] == HTTPStatus.SEE_OTHER
    assert re.fullmatch(r"/preflight/selection_[0-9a-f]{32}", location) is not None
    assert preflight[0] == HTTPStatus.OK
    page = preflight[2].decode("utf-8")
    assert "main.tex" in page
    assert "生成审阅 Word" in page
    assert "原稿" in page
    choose_form = page.split('action="/new"', 1)[1].split("</form>", 1)[0]
    assert 'name="csrf"' in choose_form
    assert 'name="selection"' not in choose_form


def test_picker_file_disappearing_after_selection_is_an_actionable_input_error(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "removed-after-picker.tex"
    state = AppState(tmp_path / "app", main_picker=lambda _initial: missing)
    try:
        with pytest.raises(ContractError) as raised:
            state.select_project()
    finally:
        state.close()

    assert raised.value.code is ErrorCode.SCHEMA_INVALID


def test_returned_word_picker_falls_back_to_source_and_never_internal_run_root(
    tmp_path: Path,
) -> None:
    picker_initials: list[Path | None] = []
    returned = tmp_path / "received" / "reviewed.docx"

    def pick_returned_word(initial: Path | None) -> Path:
        picker_initials.append(initial)
        return returned

    state = AppState(
        tmp_path / "app",
        word_picker=pick_returned_word,
    )
    session_key = f"session_{'1' * 32}"
    (state.runs_root / session_key).mkdir()
    source_directory = tmp_path / "paper"
    state._last_source_directory = source_directory

    try:
        assert state.choose_returned_word(session_key) == returned
        assert state._last_return_directory == returned.parent
        assert state._last_source_directory == source_directory
        state._last_return_directory = None
        state._last_output_directory = None
        state._last_source_directory = None
        assert state.choose_returned_word(session_key) == returned
    finally:
        state.close()

    assert picker_initials == [source_directory, None]
    assert all(
        initial is None or state.runs_root not in initial.parents for initial in picker_initials
    )


def test_source_picker_memory_is_isolated_from_return_and_output(
    tmp_path: Path,
) -> None:
    picker_initials: list[Path | None] = []
    previous_source = tmp_path / "previous-source"
    selected_source = tmp_path / "selected-source"
    selected_source.mkdir()
    main_file = selected_source / "paper.tex"
    main_file.write_text(
        "\\documentclass{article}\n\\begin{document}Review\\end{document}\n",
        encoding="utf-8",
    )

    def pick_main(initial: Path | None) -> Path:
        picker_initials.append(initial)
        return main_file

    state = AppState(tmp_path / "app", main_picker=pick_main)
    returned_directory = tmp_path / "returned"
    output_directory = tmp_path / "output"
    state._last_source_directory = previous_source
    state._last_return_directory = returned_directory
    state._last_output_directory = output_directory

    try:
        selected = state.select_project()
    finally:
        state.close()

    assert selected is not None
    assert picker_initials == [previous_source]
    assert state._last_source_directory == selected_source
    assert state._last_return_directory == returned_directory
    assert state._last_output_directory == output_directory


def test_returned_word_picker_uses_return_output_source_fallback_order(
    tmp_path: Path,
) -> None:
    picker_initials: list[Path | None] = []

    def cancel_returned_word(initial: Path | None) -> None:
        picker_initials.append(initial)

    state = AppState(tmp_path / "app", word_picker=cancel_returned_word)
    session_key = f"session_{'3' * 32}"
    (state.runs_root / session_key).mkdir()
    source_directory = tmp_path / "paper"
    output_directory = tmp_path / "sent-review"
    returned_directory = tmp_path / "received-review"
    state._last_source_directory = source_directory
    state._last_output_directory = output_directory
    state._last_return_directory = returned_directory

    try:
        assert state.choose_returned_word(session_key) is None
        state._last_return_directory = None
        assert state.choose_returned_word(session_key) is None
        state._last_output_directory = None
        assert state.choose_returned_word(session_key) is None
        state._last_source_directory = None
        assert state.choose_returned_word(session_key) is None
    finally:
        state.close()

    assert picker_initials == [
        returned_directory,
        output_directory,
        source_directory,
        None,
    ]


def test_returned_picker_and_job_queue_do_not_repeat_action_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    returned = tmp_path / "returned.docx"
    returned.write_bytes(b"synthetic")
    inspections: list[Path] = []

    def reject_after_inspection(session: ApplicationSession) -> NoReturn:
        inspections.append(session.run_root)
        raise ContractError(ErrorCode.SCHEMA_INVALID, "synthetic action stop")

    monkeypatch.setattr(ApplicationSession, "_inspect", reject_after_inspection)
    state = AppState(
        tmp_path / "app",
        word_picker=lambda _initial: returned,
        max_workers=1,
    )
    session_key = f"session_{'a' * 32}"
    session_root = state.runs_root / session_key
    session_root.mkdir()

    try:
        assert state.choose_returned_word(session_key) == returned
        assert inspections == []
        job = state.submit_receive(session_key, returned)
        completed = state.jobs.wait(job.job_id, timeout=5)
    finally:
        state.close()

    assert completed.status == "failed"
    assert inspections == [session_root.resolve()]


def test_home_summary_defers_full_status_validation_until_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    session_key = f"session_{'2' * 32}"
    objects = state.runs_root / session_key / "objects"
    objects.mkdir(parents=True)
    (objects / "source-manifest.json").write_text("{}", encoding="utf-8")

    def read_summary(
        _path: Path,
        *,
        expected_schema: str,
        max_bytes: int,
    ) -> dict[str, object]:
        assert expected_schema == "SourceManifest"
        assert max_bytes == 4 * 1024 * 1024
        return {"payload": {"main_document": "paper/main.tex"}}

    class Loader:
        @staticmethod
        def load(_run_root: Path) -> NoReturn:
            raise AssertionError("home must not reconstruct the full application session")

    monkeypatch.setattr(app_server_module, "read_contract_file", read_summary)
    monkeypatch.setattr(app_server_module, "ApplicationSession", Loader)
    try:
        view = state.home_view()
    finally:
        state.close()

    recent = cast("list[dict[str, object]]", view["recent_sessions"])
    assert recent[0]["name"] == "main"
    assert recent[0]["status"] == "打开时核验"
    assert recent[0]["action_label"] == "打开并核验"
    assert "完整核验密封证据" in cast("str", recent[0]["next_step"])
    assert recent[0]["delete_href"] == f"/session/{session_key}/delete"


def test_error_view_keeps_only_allowlisted_path_free_picker_diagnostics() -> None:
    error = ContractError(
        ErrorCode.INTERNAL_INVARIANT,
        "Windows file picker failed",
        details={
            "provider": "GetOpenFileNameW",
            "stage": "native_picker",
            "name": "FNERR_INVALIDFILENAME",
            "hex": "0x3002",
            "code": 0x3002,
            "path": r"C:\private\paper.tex",
        },
    )

    view = app_server_module._error_view(error)
    card = cast("dict[str, object]", view["error"])
    technical = cast("str", card["technical_details"])

    assert "E_INTERNAL_INVARIANT" in technical
    assert "GetOpenFileNameW" in technical
    assert "native_picker" in technical
    assert "FNERR_INVALIDFILENAME" in technical
    assert "0x3002" in technical
    assert "private" not in technical
    assert "primary_action" not in card
    assert "retry_href" not in card


def test_server_error_view_elevates_image_summary_without_private_fields() -> None:
    error = ContractError(
        ErrorCode.BACKEND_CAPABILITY_MISSING,
        "private export message",
        details={
            "diagnostics": [
                {
                    "diagnostic_id": "image_diag_" + "a" * 64,
                    "code": "E_PATH_TRAVERSAL",
                    "message": "private first message",
                    "source_path": "chapters/private-first.tex",
                    "line": 464,
                    "column": 5,
                    "disposition": "manual",
                },
                {
                    "diagnostic_id": "image_diag_" + "b" * 64,
                    "code": "E_PATH_TRAVERSAL",
                    "message": "private second message",
                    "source_path": "chapters/private-second.tex",
                    "line": 490,
                    "column": 9,
                    "disposition": "manual",
                },
            ]
        },
    )

    view = app_server_module._error_view(error)
    html = render_app_page(view)
    card = cast("dict[str, object]", view["error"])
    technical = cast("str", card["technical_details"])

    assert "问题摘要：2处图片写法当前无法处理" in technical
    assert html.count("2处图片写法当前无法处理") == 1
    assert html.index("2处图片写法当前无法处理") < html.index("技术详情")
    assert "第464行，第5列（E_PATH_TRAVERSAL）" in html
    assert "第490行，第9列（E_PATH_TRAVERSAL）" in html
    assert "private" not in html


def test_new_picker_contract_error_has_one_clear_return_action(
    tmp_path: Path,
) -> None:
    def failed_picker(_initial: Path | None) -> Path | None:
        raise ContractError(
            ErrorCode.INTERNAL_INVARIANT,
            "Windows file picker failed",
            details={
                "provider": "GetOpenFileNameW",
                "stage": "native_picker",
                "name": "CDERR_DIALOGFAILURE",
                "hex": "0xFFFF",
                "code": 0xFFFF,
            },
        )

    with _running_server(tmp_path / "app", main_picker=failed_picker) as server:
        cookie, csrf, _body, _headers = _credentials(server)
        response = _post_form(server, "/new", {"csrf": csrf}, cookie=cookie)

    page = response[2].decode("utf-8")
    assert response[0] == HTTPStatus.BAD_REQUEST
    assert "CDERR_DIALOGFAILURE" in page
    assert "0xFFFF" in page
    assert ">重试<" not in page


def test_session_keys_are_exact_and_valid_sessions_resolve_under_runs_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    key = f"session_{'b' * 32}"
    session_root = state.runs_root / key
    session_root.mkdir()
    loaded: list[Path] = []
    sentinel = object()

    class Loader:
        @staticmethod
        def load(run_root: Path) -> object:
            loaded.append(run_root)
            return sentinel

    monkeypatch.setattr(app_server_module, "ApplicationSession", Loader)
    try:
        assert state.load_session(key) is sentinel
        for invalid in (
            "../session_" + "b" * 32,
            "session_" + "b" * 31,
            "session_" + "B" * 32,
            key + "/child",
        ):
            with pytest.raises(ContractError) as raised:
                state.load_session(invalid)
            assert raised.value.code is ErrorCode.SCHEMA_INVALID
    finally:
        state.close()

    assert loaded == [session_root.resolve()]
    assert loaded[0].parent == state.runs_root


def test_session_resolution_fails_closed_if_real_path_escapes_runs_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    outside = tmp_path / "outside"
    outside.mkdir()
    key = f"session_{'c' * 32}"
    original = app_server_module._require_real_directory

    def escaped_directory(path: Path, label: str) -> Path:
        if label == "review session":
            return outside.resolve()
        return original(path, label)

    monkeypatch.setattr(app_server_module, "_require_real_directory", escaped_directory)
    try:
        with pytest.raises(ContractError) as raised:
            state.load_session(key)
    finally:
        state.close()

    assert raised.value.code is ErrorCode.PATH_TRAVERSAL


class _ArtifactSession:
    def __init__(self, run_root: Path, artifacts: dict[str, object]) -> None:
        self.run_root = run_root
        self._artifacts = artifacts

    def status(self) -> dict[str, object]:
        return {"artifacts": self._artifacts}


def test_artifact_route_enforces_status_paths_and_explicit_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    session_key = f"session_{'d' * 32}"
    run_root = tmp_path / "sealed-run"
    run_root.mkdir()
    bundle = run_root / "audit.zip"
    bundle.write_bytes(b"synthetic-audit")
    review_docx = run_root / "review.docx"
    review_docx.write_bytes(b"not-downloadable-through-this-route")
    artifacts: dict[str, object] = {
        "audit_bundle": "audit.zip",
        "review_docx": "review.docx",
    }
    session = _ArtifactSession(run_root, artifacts)
    monkeypatch.setattr(state, "load_session", lambda _key: cast("Any", session))
    server = AppHTTPServer((LOOPBACK_HOST, 0), state)

    with _serving(server):
        cookie, _csrf, _body, _headers = _credentials(server)
        allowed = _request(
            server,
            "GET",
            f"/artifact/{session_key}/audit_bundle",
            headers={"Host": server.expected_host, "Cookie": cookie},
        )
        denied = _request(
            server,
            "GET",
            f"/artifact/{session_key}/review_docx",
            headers={"Host": server.expected_host, "Cookie": cookie},
        )

    assert allowed[0] == HTTPStatus.OK
    assert allowed[1]["content-type"] == "application/zip"
    assert allowed[1]["content-disposition"] == "attachment"
    assert allowed[2] == b"synthetic-audit"
    assert denied[0] == HTTPStatus.CONFLICT

    artifacts["audit_bundle"] = "../outside.zip"
    with pytest.raises(ContractError) as raised:
        state.downloadable_artifact(session_key, "audit_bundle")
    assert raised.value.code is ErrorCode.PATH_TRAVERSAL


class _FakeJobs:
    def __init__(self, snapshots: Mapping[str, JobSnapshot]) -> None:
        self._snapshots = dict(snapshots)
        self.closed = False

    def snapshot(self, job_id: str) -> JobSnapshot:
        try:
            return self._snapshots[job_id]
        except KeyError as exc:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "unknown test job") from exc

    def close(self, *, wait: bool = True) -> None:
        del wait
        self.closed = True


def _job_snapshot(
    job_id: str,
    *,
    status: str,
    progress: int,
    stage: str,
    operation: str = "synthetic",
    error: dict[str, object] | None = None,
) -> JobSnapshot:
    return JobSnapshot(
        job_id=job_id,
        session_key=f"session_{'e' * 32}",
        operation=operation,
        status=cast("Any", status),
        progress=progress,
        stage=stage,
        result={} if status == "succeeded" else None,
        error=error,
    )


def test_recovery_endpoints_are_explicit_posts_with_exact_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = f"session_{'f' * 32}"
    begin_job = _job_snapshot(
        f"job_{'4' * 32}",
        status="queued",
        progress=0,
        stage="等待开始",
    )
    plan_job = _job_snapshot(
        f"job_{'5' * 32}",
        status="queued",
        progress=0,
        stage="等待开始",
    )
    with _running_server(tmp_path / "app") as server:
        calls: list[tuple[str, str]] = []

        def begin(key: str) -> JobSnapshot:
            calls.append(("approval", key))
            return begin_job

        def prepare(key: str) -> JobSnapshot:
            calls.append(("plan", key))
            return plan_job

        monkeypatch.setattr(server.app_state, "submit_begin_approval", begin)
        monkeypatch.setattr(server.app_state, "submit_prepare_plan", prepare)
        cookie, csrf, _body, _headers = _credentials(server)
        approval = _post_form(
            server,
            "/approval/start",
            {"csrf": csrf, "session": session_key},
            cookie=cookie,
        )
        plan = _post_form(
            server,
            "/patch/prepare",
            {"csrf": csrf, "session": session_key},
            cookie=cookie,
        )
        unknown = _post_form(
            server,
            "/approval/start",
            {"csrf": csrf, "session": session_key, "unexpected": "1"},
            cookie=cookie,
        )

    assert calls == [("approval", session_key), ("plan", session_key)]
    assert approval[0] == HTTPStatus.SEE_OTHER
    assert approval[1]["location"] == f"/jobs/{begin_job.job_id}"
    assert plan[0] == HTTPStatus.SEE_OTHER
    assert plan[1]["location"] == f"/jobs/{plan_job.job_id}"
    assert unknown[0] == HTTPStatus.BAD_REQUEST


def test_blocked_plan_revise_post_reopens_draft_and_invalidates_old_token(
    tmp_path: Path,
) -> None:
    data_root = tmp_path
    session_key = f"session_{'8' * 32}"
    source = tmp_path / "s"
    shutil.copytree(FIXTURE, source)
    (tmp_path / "runs").mkdir()
    session = ApplicationSession.create(
        source,
        tmp_path / "runs" / session_key,
        main_document="main.tex",
        confidentiality="public_fixture",
    )
    session.export_review(confidentiality="public_fixture")
    returned = tmp_path / "r.docx"
    _make_returned(session.run_root, returned)
    session.receive_review(returned, confidentiality="public_fixture")
    session.begin_approval(actor_id="author", actor_name="Author")
    change_id = cast(
        "str",
        cast(
            "list[dict[str, Any]]",
            read_contract_file(
                session.run_root / "receive/changeset.json",
                expected_schema="ChangeSet",
            )["payload"]["changes"],
        )[0]["change_id"],
    )
    session.decide(
        change_id=change_id,
        decision="accepted_with_edit",
        final_text="50%",
    )
    blocked = session.finalize_approval()
    old_hash = cast("str", blocked["plan"]["payload_sha256"])
    approval_r3 = session.run_root / "approvals/approval-r3.json"
    plan_r1 = session.run_root / "plans/plan-r1/patch-plan.json"
    immutable_before = {
        approval_r3: approval_r3.read_bytes(),
        plan_r1: plan_r1.read_bytes(),
    }

    with _running_server(data_root) as server:
        cookie, csrf, _body, _headers = _credentials(server)
        session_href = f"/session/{session_key}"
        get_headers = {"Host": server.expected_host, "Cookie": cookie}
        first_get = _request(server, "GET", session_href, headers=get_headers)
        second_get = _request(server, "GET", session_href, headers=get_headers)
        revise_fields = {
            "csrf": csrf,
            "session": session_key,
            "patch_plan_sha256": old_hash,
        }
        encoded = urlencode(revise_fields).encode("ascii")
        valid_headers = _post_headers(server, cookie)
        wrong_host = _request(
            server,
            "POST",
            "/approval/revise",
            body=encoded,
            headers=valid_headers | {"Host": "evil.example"},
        )
        no_origin = _request(
            server,
            "POST",
            "/approval/revise",
            body=encoded,
            headers={key: value for key, value in valid_headers.items() if key != "Origin"},
        )
        wrong_csrf = _post_form(
            server,
            "/approval/revise",
            revise_fields | {"csrf": "wrong"},
            cookie=cookie,
        )
        missing_hash = _post_form(
            server,
            "/approval/revise",
            {"csrf": csrf, "session": session_key},
            cookie=cookie,
        )
        unknown = _post_form(
            server,
            "/approval/revise",
            revise_fields | {"unexpected": "1"},
            cookie=cookie,
        )
        get_action = _request(
            server,
            "GET",
            "/approval/revise",
            headers=get_headers,
        )

        assert first_get[0] == HTTPStatus.OK
        assert second_get[0] == HTTPStatus.OK
        blocked_page = first_get[2].decode("utf-8")
        assert 'method="post" action="/approval/revise"' in blocked_page
        assert "重新审批阻断项" in blocked_page
        assert [
            wrong_host[0],
            no_origin[0],
            wrong_csrf[0],
            missing_hash[0],
            unknown[0],
        ] == [
            421,
            403,
            303,
            400,
            400,
        ]
        assert wrong_csrf[1]["location"] == "/"
        assert get_action[0] == HTTPStatus.NOT_FOUND
        assert ApplicationSession.load(session.run_root).status() == blocked
        assert not (session.run_root / "approvals/approval-r4.json").exists()
        assert {path: path.read_bytes() for path in immutable_before} == immutable_before

        revise = _post_form(
            server,
            "/approval/revise",
            revise_fields,
            cookie=cookie,
        )
        revise_job = _redirected_job_id(revise)
        revised = server.app_state.jobs.wait(revise_job, timeout=20)
        assert revised.status == "succeeded"
        revised_redirect = _request(
            server,
            "GET",
            f"/jobs/{revise_job}",
            headers=get_headers,
        )
        assert revised_redirect[0] == HTTPStatus.SEE_OTHER
        assert revised_redirect[1]["location"] == session_href

        draft = ApplicationSession.load(session.run_root).status()
        assert draft["phase"] == "approval_ready_to_finalize"
        assert draft["approval"]["revision"] == 4
        assert draft["approval"]["status"] == "draft"
        assert draft["plan"] is None
        assert {path: path.read_bytes() for path in immutable_before} == immutable_before

        decision = _post_form(
            server,
            "/approval/decision",
            {
                "csrf": csrf,
                "session": session_key,
                "change_id": change_id,
                "decision": "manual",
                "final_text": "",
                "reason": "requires author review",
                "risk_acknowledgement": "",
            },
            cookie=cookie,
        )
        assert decision[0] == HTTPStatus.SEE_OTHER
        changed = ApplicationSession.load(session.run_root).status()
        assert changed["approval"]["revision"] == 5

        finalize = _post_form(
            server,
            "/approval/finalize",
            {"csrf": csrf, "session": session_key},
            cookie=cookie,
        )
        finalize_job = _redirected_job_id(finalize)
        finalized = server.app_state.jobs.wait(finalize_job, timeout=20)
        assert finalized.status == "succeeded"
        second = ApplicationSession.load(session.run_root).status()
        assert second["approval"]["revision"] == 6
        assert second["plan"]["number"] == 2
        assert second["plan"]["status"] == "noop"
        assert second["phase"] == "awaiting_apply_confirmation"
        new_hash = cast("str", second["apply_confirmation"]["patch_plan_sha256"])
        assert new_hash != old_hash

        stale_apply = _post_form(
            server,
            "/result/generate",
            {
                "csrf": csrf,
                "session": session_key,
                "patch_plan_sha256": old_hash,
                "confirm_apply": "yes",
            },
            cookie=cookie,
        )
        stale_job = _redirected_job_id(stale_apply)
        stale = server.app_state.jobs.wait(stale_job, timeout=20)
        assert stale.status == "failed"
        assert stale.error is not None
        assert stale.error["code"] == ErrorCode.HASH_PATCHPLAN_MISMATCH.value
        assert not (session.run_root / "revised-clean").exists()


def test_partial_retry_runs_new_verification_then_only_missing_downstream_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = f"session_{'9' * 32}"

    class RecoverySession:
        def __init__(self, *, retry_phase: str) -> None:
            self.phase = "partially_completed"
            self.retry_phase = retry_phase
            self.calls: list[str] = []

        def status(self) -> dict[str, object]:
            return {"phase": self.phase}

        def retry_verification(self) -> dict[str, object]:
            self.calls.append("retry_verification")
            self.phase = self.retry_phase
            return self.status()

        def build_ledger(self) -> dict[str, object]:
            self.calls.append("build_ledger")
            self.phase = "ready_to_bundle"
            return self.status()

        def create_bundle(self) -> dict[str, object]:
            self.calls.append("create_bundle")
            self.phase = "completed"
            return self.status()

    state = AppState(tmp_path / "app", max_workers=1)
    still_partial = RecoverySession(retry_phase="partially_completed")
    monkeypatch.setattr(state, "_action_session", lambda _key: cast("Any", still_partial))
    first = state.submit_retry(session_key)
    first_done = state.jobs.wait(first.job_id, timeout=5)
    assert first_done.status == "succeeded"
    assert still_partial.calls == ["retry_verification"]
    assert cast("Mapping[str, Any]", first_done.result)["status"]["phase"] == (
        "partially_completed"
    )

    completes = RecoverySession(retry_phase="verified")
    monkeypatch.setattr(state, "_action_session", lambda _key: cast("Any", completes))
    second = state.submit_retry(session_key)
    second_done = state.jobs.wait(second.job_id, timeout=5)
    state.close()

    assert second_done.status == "succeeded"
    assert completes.calls == ["retry_verification", "build_ledger", "create_bundle"]
    assert cast("Mapping[str, Any]", second_done.result)["status"]["phase"] == "completed"


def test_job_route_renders_progress_redirects_success_and_explains_failure(
    tmp_path: Path,
) -> None:
    running_id = f"job_{'1' * 32}"
    succeeded_id = f"job_{'2' * 32}"
    failed_id = f"job_{'3' * 32}"
    export_id = f"job_{'4' * 32}"
    snapshots = {
        running_id: _job_snapshot(
            running_id,
            status="running",
            progress=37,
            stage="正在解析修订",
        ),
        succeeded_id: _job_snapshot(
            succeeded_id,
            status="succeeded",
            progress=100,
            stage="已完成",
        ),
        failed_id: _job_snapshot(
            failed_id,
            status="failed",
            progress=51,
            stage="源文件已变化",
            error={
                "code": ErrorCode.PATCH_SOURCE_DRIFT.value,
                "diagnostics": {
                    "provider": "tex2word",
                    "failure_kind": "timeout",
                    "timed_out": "yes",
                    "returncode": -9,
                    "path": r"C:\private\paper.tex",
                },
            },
        ),
        export_id: _job_snapshot(
            export_id,
            status="running",
            progress=50,
            stage="正在处理论文图片并生成审阅 Word",
            operation="export_review",
        ),
    }
    state = AppState(tmp_path / "app")
    state.jobs.close()
    fake_jobs = _FakeJobs(snapshots)
    state.jobs = cast("Any", fake_jobs)
    session_href = f"/session/session_{'e' * 32}"
    for snapshot in snapshots.values():
        state._register_job(
            snapshot,
            success_href=session_href,
            retry_href=session_href,
            back_href="/",
            step=2,
        )
    server = AppHTTPServer((LOOPBACK_HOST, 0), state)

    with _serving(server):
        cookie, _csrf, _body, _headers = _credentials(server)
        headers = {"Host": server.expected_host, "Cookie": cookie}
        running = _request(server, "GET", f"/jobs/{running_id}", headers=headers)
        succeeded = _request(server, "GET", f"/jobs/{succeeded_id}", headers=headers)
        failed = _request(server, "GET", f"/jobs/{failed_id}", headers=headers)
        export = _request(server, "GET", f"/jobs/{export_id}", headers=headers)

    progress_page = running[2].decode("utf-8")
    failed_page = failed[2].decode("utf-8")
    export_page = export[2].decode("utf-8")
    assert running[0] == HTTPStatus.OK
    assert running[1]["refresh"] == f"1; url=/jobs/{running_id}"
    assert "正在解析修订" in progress_page
    assert "37%" in progress_page
    assert succeeded[0] == HTTPStatus.SEE_OTHER
    assert succeeded[1]["location"] == session_href
    assert failed[0] == HTTPStatus.CONFLICT
    assert ErrorCode.PATCH_SOURCE_DRIFT.value in failed_page
    assert "tex2word" in failed_page
    assert "timeout" in failed_page
    assert "-9" in failed_page
    assert "private" not in failed_page
    assert 'method="post" action="/new"' in failed_page
    assert "重新选择论文并新建一轮" in failed_page
    assert ">重试<" not in failed_page
    assert export[0] == HTTPStatus.OK
    assert "通常约需 1–2 分钟" in export_page
    assert "阶段文字暂时不变并不表示程序卡死" in export_page
    assert "保持此页面打开或切到其他窗口都不会中断处理" in export_page
    assert '<progress max="100"' in export_page
    assert '<progress value="50"' not in export_page
    assert fake_jobs.closed


def test_app_server_shutdown_boundedly_drains_unread_request_data() -> None:
    class RecordingSocket:
        def __init__(self) -> None:
            self.unread = MAX_FORM_BYTES + 2
            self.shutdown_modes: list[int] = []
            self.timeouts: list[float] = []
            self.recv_sizes: list[int] = []
            self.closed = False

        def shutdown(self, mode: int) -> None:
            self.shutdown_modes.append(mode)

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

        def recv(self, size: int) -> bytes:
            self.recv_sizes.append(size)
            consumed = min(size, self.unread)
            self.unread -= consumed
            return b"x" * consumed

        def close(self) -> None:
            self.closed = True

    server = object.__new__(AppHTTPServer)
    request = RecordingSocket()

    server.shutdown_request(cast("socket.socket", request))

    assert request.shutdown_modes == [socket.SHUT_WR]
    assert request.timeouts == [0.25]
    assert request.recv_sizes == [8192] * 8 + [1]
    assert request.unread == 1
    assert request.closed


def test_client_disconnect_never_attempts_a_second_response() -> None:
    class BrokenWriter:
        def __init__(self) -> None:
            self.calls = 0

        def write(self, _data: bytes) -> None:
            self.calls += 1
            raise BrokenPipeError

    handler = cast("Any", object.__new__(AppRequestHandler))
    responses: list[HTTPStatus] = []
    handler._response_started = False
    handler.close_connection = False
    handler.wfile = BrokenWriter()
    handler.send_response = responses.append
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    handler._send(HTTPStatus.OK, b"first")
    handler._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"must-not-send")

    assert responses == [HTTPStatus.OK]
    assert handler.wfile.calls == 1
    assert handler.close_connection is True


def test_server_handle_error_is_safe_without_console_streams() -> None:
    server = object.__new__(AppHTTPServer)

    server.handle_error(object(), ("127.0.0.1", 1))


@pytest.mark.parametrize("failure_mode", ["returned_false", "raised"])
def test_browser_launch_failure_is_reported(failure_mode: str) -> None:
    url = "http://127.0.0.1:43129/"
    shown: list[str] = []

    def opener(_url: str) -> bool:
        if failure_mode == "raised":
            raise OSError("no default browser")
        return False

    app_server_module._open_browser_or_notify(url, opener, shown.append)

    assert shown == [url]


def test_successful_browser_launch_does_not_show_failure() -> None:
    shown: list[str] = []

    app_server_module._open_browser_or_notify(
        "http://127.0.0.1:43129/",
        lambda _url: True,
        shown.append,
    )

    assert shown == []
