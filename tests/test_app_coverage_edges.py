"""Fail-closed edge coverage for the local product boundary."""

from __future__ import annotations

import io
import os
import stat
import threading
from collections.abc import Callable, Mapping
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import latex_word_review.app_presenter as p
import latex_word_review.app_server as s
from latex_word_review.app_jobs import JobSnapshot
from latex_word_review.app_server import AppRequestHandler, AppState
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_limits import DEFAULT_EXPORT_TIMEOUT_SECONDS
from latex_word_review.hashing import digest_bytes
from tests.test_app_presenter import _as_application_session, _StatusSession
from tests.test_app_server import _credentials, _post_form, _running_server


def code(exc: pytest.ExceptionInfo[ContractError], expected: ErrorCode) -> None:
    assert exc.value.code is expected


@pytest.mark.parametrize(
    "call",
    [
        lambda: p._mapping([], "x"),
        lambda: p._mapping({1: 2}, "x"),
        lambda: p._sequence("x", "x"),
        lambda: p._sequence(object(), "x"),
        lambda: p._string("", "x"),
        lambda: p._string(1, "x"),
        lambda: p._common_fields("", "session_x"),
    ],
)
def test_presenter_shape_guards(call: Callable[[], object]) -> None:
    with pytest.raises(p.PresenterModelError):
        call()


@pytest.mark.parametrize(
    "href",
    [
        "relative",
        "//evil/x",
        "https://evil/x",
        "/x#f",
        "/back\\slash",
        "/x\x1f",
    ],
)
def test_recovery_href_guard(href: str) -> None:
    with pytest.raises(p.PresenterModelError):
        p._safe_recovery_href(href)


def test_recovery_controls_cover_invalid_and_omitted_actions() -> None:
    session = "session_" + "a" * 32
    bad: list[Callable[[], object]] = [
        lambda: p.build_error_recovery_control("retry", "", retry_href="/"),
        lambda: p.build_error_recovery_control("choose_project", "x", csrf_token=""),
        lambda: p.build_error_recovery_control(
            "create_support_bundle", "x", csrf_token="x", session_key=session, support_token="bad"
        ),
        lambda: p.build_error_recovery_control(
            cast("Any", "bad"), "x", csrf_token="x", session_key=session
        ),
    ]
    for call in bad:
        with pytest.raises(p.PresenterModelError):
            call()
    assert p.build_error_recovery_control("open_results", "x", session_key=session) is None
    assert (
        p.build_error_recovery_control(
            "choose_returned_word",
            "x",
            csrf_token="x",
            session_key=session,
            allow_session_actions=False,
        )
        is None
    )


def test_change_card_absent_location_range_and_edit() -> None:
    assert p._source_context({"comment": "note"}) == (
        "note",
        [{"label": "源位置", "value": "未精确定位"}],
    )
    change: dict[str, Any] = {
        "change_id": "c",
        "kind": "replacement",
        "native_kind": "replacement",
        "before": "old",
        "after": "new",
        "author": "R",
        "timestamp": "T",
        "initial_decision": "pending",
        "safety_class": "plain_text_candidate",
        "change_fingerprint": "f" * 64,
        "resolution": {"status": "exact", "method": "bookmark", "confidence": 1.0},
        "source_location": {
            "path": "a.tex",
            "start_line": 3,
            "end_line": 5,
            "start_byte": 1,
            "end_byte": 2,
            "slice_sha256": "a" * 64,
        },
    }
    card = p._change_card(change, {"decision": "accepted_with_edit", "final_text": "edit"})
    assert card["edited_text"] == "edit" and "3–5" in cast("str", card["context"])
    change["source_location"] = None
    change["resolution"] = {"status": "unsupported"}
    change["initial_decision"] = "conflict"
    card = p._change_card(change, None)
    assert card["safety"] == "conflict" and "context" not in card


@pytest.mark.parametrize(
    "data",
    [
        b"\xff",
        b"--- a/a.tex\n",
        b"--- a/a.tex\n+++ b/b.tex\n",
        b"junk\n",
        b"--- a/a.tex\n+++ b/a.tex\nx\n--- a/a.tex\n+++ b/a.tex\ny\n",
    ],
)
def test_diff_parser_rejects_bad_evidence(data: bytes) -> None:
    with pytest.raises(ContractError) as exc:
        p._split_unified_diff(data)
    code(exc, ErrorCode.HASH_PATCHPLAN_MISMATCH)


def test_diff_parser_empty_and_multifile() -> None:
    assert p._split_unified_diff(b"") == []
    data = b"--- a/a.tex\n+++ b/a.tex\nx\n--- a/b.tex\n+++ b/b.tex\ny\n"
    assert [x[0] for x in p._split_unified_diff(data)] == ["a.tex", "b.tex"]


@pytest.mark.parametrize("case", ["path", "summary", "noop", "digest", "files", "confirm"])
def test_patch_view_rejects_sealed_disagreement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    (tmp_path / "plan.json").write_text("{}", encoding="utf-8")
    (tmp_path / "diff.patch").write_bytes(b"")
    status: dict[str, Any] = {
        "step": 4,
        "phase": "ready_to_plan",
        "main_document": "main.tex",
        "artifacts": {"patch_plan": "plan.json", "patch_diff": "diff.patch"},
        "plan": {"path": "plan.json", "payload_sha256": "sealed"},
    }
    payload: dict[str, Any] = {
        "status": "ready",
        "unified_diff": None,
        "operations": [],
        "excluded_changes": [],
        "accepted_but_blocked": [],
        "summary": {"planned": 0, "overlaps": 0},
    }
    data = b""
    if case == "path":
        status["plan"]["path"] = "other.json"
    elif case == "summary":
        status["plan"]["payload_sha256"] = "wrong"
    elif case == "noop":
        data = b"x"
    elif case == "digest":
        data, payload["unified_diff"] = b"x", {"sha256": "0" * 64, "size_bytes": 1}
    elif case == "files":
        data = b"--- a/a.tex\n+++ b/a.tex\nx\n"
        d = digest_bytes(data)
        payload["unified_diff"] = {"sha256": d.sha256, "size_bytes": d.size_bytes}
        payload["operations"] = [{"target": {"path": "b.tex"}}]
    else:
        status["phase"] = "awaiting_apply_confirmation"
        status["apply_confirmation"] = {"patch_plan_sha256": "wrong"}
    monkeypatch.setattr(p, "read_contract_file", lambda *_a, **_k: {"payload": payload})
    monkeypatch.setattr(p, "compute_payload_sha256", lambda _x: "sealed")
    monkeypatch.setattr(p, "read_stable_bytes", lambda *_a, **_k: data)
    with pytest.raises(ContractError) as exc:
        p._patch_view(tmp_path, status, csrf_token="x", session_key="session_" + "a" * 32)
    code(exc, ErrorCode.HASH_PATCHPLAN_MISMATCH)


def test_warning_messages_and_unknown_phase(tmp_path: Path) -> None:
    warnings = p._warning_messages(
        {
            "blockers": [
                {"code": "verification_failed", "attempt": 2, "retry_available": False},
                {"code": "approval_pending"},
                {"code": "custom"},
                {"code": 2},
            ]
        },
        "partially_completed",
    )
    assert any("2 次" in x for x in warnings) and any("custom" in x for x in warnings)
    session = _as_application_session(_StatusSession(tmp_path, {"phase": "future", "step": 3}))
    view = p.render_session_view(session, session_key="session_" + "a" * 32, csrf_token="x")
    assert view["page"] == "error" and view["step"] == 3


class Scan:
    def __init__(self, entries: list[object] | None = None, error: OSError | None = None) -> None:
        self.entries, self.error = entries or [], error

    def __enter__(self) -> object:
        if self.error:
            raise self.error
        return iter(self.entries)

    def __exit__(self, *_a: object) -> None:
        pass


class Entry:
    def __init__(self, path: Path, meta: object | BaseException, link: bool = False):
        self.path, self.meta, self.link = str(path), meta, link

    def stat(self, *, follow_symlinks: bool) -> object:
        if isinstance(self.meta, BaseException):
            raise self.meta
        return self.meta

    def is_symlink(self) -> bool:
        return self.link


@pytest.mark.parametrize("case", ["root", "stat", "link", "special", "scan"])
def test_delete_inventory_rejects_unprovable_topology(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(s, "_is_link_or_junction", lambda _x: case == "root")
    if case == "root":
        with pytest.raises(ContractError) as exc:
            s._validated_delete_tree(root)
        code(exc, ErrorCode.PATH_LINK_ESCAPE)
        return
    reg = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)
    scans = {
        "stat": Scan([Entry(root / "x", OSError())]),
        "link": Scan([Entry(root / "x", reg, True)]),
        "special": Scan(
            [Entry(root / "x", SimpleNamespace(st_mode=stat.S_IFIFO, st_file_attributes=0))]
        ),
        "scan": Scan(error=OSError()),
    }
    monkeypatch.setattr(os, "scandir", lambda _x: scans[case])
    with pytest.raises(ContractError):
        s._validated_delete_tree(root)


class Node:
    def __init__(self, actions: list[BaseException | None]):
        self.actions, self.calls = actions, 0

    def act(self) -> None:
        action = self.actions[min(self.calls, len(self.actions) - 1)]
        self.calls += 1
        if action:
            raise action

    unlink = act
    rmdir = act


@pytest.mark.parametrize(
    "kind,actions,ok",
    [
        ("f", [PermissionError(), None], True),
        ("f", [PermissionError(), OSError()], False),
        ("f", [OSError()], False),
        ("d", [PermissionError(), None], True),
        ("d", [PermissionError(), OSError()], False),
        ("d", [OSError()], False),
    ],
)
def test_delete_removal_readonly_and_io(
    monkeypatch: pytest.MonkeyPatch, kind: str, actions: list[BaseException | None], ok: bool
) -> None:
    node = Node(actions)
    monkeypatch.setattr(s, "_is_link_or_junction", lambda _x: False)
    monkeypatch.setattr(s, "_make_owned_entry_writable", lambda _x: None)
    args = ((cast("Path", node),), ()) if kind == "f" else ((), (cast("Path", node),))
    if ok:
        s._remove_validated_delete_tree(*args)
        assert node.calls == 2
    else:
        with pytest.raises(ContractError):
            s._remove_validated_delete_tree(*args)


def test_delete_topology_change_and_directory_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(s, "_is_link_or_junction", lambda _x: True)
    for args in [((tmp_path / "f",), ()), ((), (tmp_path / "d",))]:
        with pytest.raises(ContractError) as exc:
            s._remove_validated_delete_tree(*args)
        code(exc, ErrorCode.PATH_LINK_ESCAPE)
    monkeypatch.undo()
    with pytest.raises(ContractError):
        s._require_real_directory(tmp_path / "missing", "x")
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(ContractError):
        s._require_real_directory(file, "x")
    with pytest.raises(ContractError):
        s.default_app_data_root(environ={})
    with pytest.raises(ContractError):
        s.default_app_data_root(environ={"LOCALAPPDATA": "relative"})
    assert (
        s.default_app_data_root(environ={"LOCALAPPDATA": str(tmp_path)}).name == "LatexWordReview"
    )


def test_default_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    chosen = Path("x")
    seen: list[str] = []
    monkeypatch.setattr(s, "choose_main_tex", lambda **_k: chosen)
    monkeypatch.setattr(s, "choose_returned_docx", lambda **_k: chosen)
    monkeypatch.setattr(s, "choose_review_copy_destination", lambda **_k: chosen)
    monkeypatch.setattr(s, "show_browser_open_failure", seen.append)
    assert (
        s._default_main_picker(None)
        == s._default_word_picker(None)
        == s._default_review_copy_picker(None)
    )
    s._default_browser_failure_notifier("url")
    assert seen == ["url"]


def state(tmp_path: Path) -> AppState:
    return AppState(tmp_path / "app")


def test_state_input_and_support_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ContractError):
        AppState(Path("relative"))
    st = state(tmp_path)
    session = "session_" + "a" * 32
    try:
        for args in [
            ("bad", ErrorCode.SCHEMA_INVALID, "x"),
            (session, "BAD", "x"),
            (session, ErrorCode.SCHEMA_INVALID, "bad stage"),
        ]:
            with pytest.raises(ContractError):
                st.issue_support_token(*args)
        with pytest.raises(ContractError):
            st._support_request_from_token(session, "bad")
        token = st.issue_support_token(session, ErrorCode.SCHEMA_INVALID, "stage")
        with pytest.raises(ContractError):
            st._support_request_from_token(session, token[:-1] + ("0" if token[-1] != "0" else "1"))
        views: list[dict[str, object]] = [
            {"page": "error"},
            {"page": "error", "error": {}},
        ]
        for view in views:
            with pytest.raises(ContractError):
                st.bind_support_action(view, session_key=session, stage="x")
        assert (
            st.bind_support_action({"page": "home"}, session_key=session, stage="x")["page"]
            == "home"
        )
    finally:
        st.close()


def test_home_and_selection_route_failures(tmp_path: Path) -> None:
    wrong = tmp_path / "paper.txt"
    wrong.write_text("x", encoding="utf-8")
    st = AppState(tmp_path / "app", main_picker=lambda _x: wrong)
    try:
        root = st.runs_root / ("session_" + "a" * 32)
        root.mkdir()
        recent = cast("list[dict[str, object]]", st.home_view()["recent_sessions"])
        assert recent[0]["status"] == "需要检查"
        calls: list[Callable[[], object]] = [
            st.select_project,
            lambda: st.selected_project("bad"),
            lambda: st.selected_project("selection_" + "a" * 32),
            lambda: st.session_view("session_" + "a" * 32, selected_filter="bad"),
            lambda: st.job_route("bad"),
            lambda: st.job_route("job_" + "a" * 32),
        ]
        for call in calls:
            with pytest.raises(ContractError):
                call()
        with pytest.raises(ContractError):
            st.decide(
                "session_" + "a" * 32,
                change_id="c",
                decision="bad",
                final_text="",
                reason="",
                risk_acknowledgement="",
            )
    finally:
        st.close()


class InlineJobs:
    def __init__(self) -> None:
        self.result: Mapping[str, Any] | None = None

    def submit(
        self, key: str, operation: str, task: Callable[[Any], Mapping[str, Any]]
    ) -> JobSnapshot:
        self.result = task(SimpleNamespace(update=lambda *_a: None))
        return JobSnapshot(
            job_id="job_" + "a" * 32,
            session_key=key,
            operation=operation,
            status="succeeded",
            progress=100,
            stage="done",
            result=dict(self.result),
            error=None,
        )

    def close(self, *, wait: bool = True) -> None:
        pass


def inline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: object
) -> tuple[AppState, InlineJobs]:
    st = state(tmp_path)
    st.jobs.close()
    jobs = InlineJobs()
    st.jobs = cast("Any", jobs)
    monkeypatch.setattr(st, "_action_session", lambda _k: session)
    return st, jobs


def test_inline_existing_receive_prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    export_options: dict[str, object] = {}

    def export_review(**kwargs: object) -> dict[str, str]:
        export_options.update(kwargs)
        return {"phase": "waiting"}

    session = SimpleNamespace(
        export_review=export_review,
        receive_review=lambda _p: None,
        begin_approval=lambda **_k: {"phase": "approval"},
        prepare_plan=lambda: {"phase": "plan"},
    )
    st, jobs = inline(monkeypatch, tmp_path, session)
    key = "session_" + "a" * 32
    try:
        st.submit_existing_export(key)
        assert export_options == {"timeout_s": DEFAULT_EXPORT_TIMEOUT_SECONDS}
        st.submit_receive(key, tmp_path / "r.docx")
        st.submit_prepare_plan(key)
        assert jobs.result is not None
    finally:
        st.close()


@pytest.mark.parametrize(
    "phase,after,expected",
    [
        ("applied", "verified", "completed"),
        ("partially_completed", "partially_completed", "partially_completed"),
        ("verified", "verified", "completed"),
        ("ready_to_bundle", "ready_to_bundle", "completed"),
    ],
)
def test_retry_state_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str, after: str, expected: str
) -> None:
    calls = 0

    def status() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"phase": phase if calls == 1 else after}

    session = SimpleNamespace(
        status=status,
        verify_results=lambda: {"phase": after},
        retry_verification=lambda: {"phase": after},
        build_ledger=lambda: {"phase": "ready_to_bundle"},
        create_bundle=lambda: {"phase": "completed"},
    )
    st, jobs = inline(monkeypatch, tmp_path, session)
    try:
        st.submit_retry("session_" + "a" * 32)
        assert cast("Any", jobs.result)["status"]["phase"] == expected
    finally:
        st.close()


class DummyState:
    session_token = "token"
    csrf_token = "csrf"


class DummyServer:
    expected_host = "127.0.0.1:1"
    origin = "http://127.0.0.1:1"
    session_cookie_name = "c_1"
    app_state = DummyState()


def handler(headers: list[tuple[str, str]] | None = None, body: bytes = b"") -> AppRequestHandler:
    h = object.__new__(AppRequestHandler)
    msg = Message()
    for k, v in headers or []:
        msg.add_header(k, v)
    h.headers = msg
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.server = cast("Any", DummyServer())
    h._response_started = False
    h.close_connection = False
    return h


@pytest.mark.parametrize("encoded", ["%", "%GG", "é=1", "a=%FF", "a=1&a=2", "a=1&b=2&c=3", "a"])
def test_query_parser_bad_values(encoded: str) -> None:
    assert handler()._query(encoded, {"a"}) is None


def test_query_parser_valid_and_empty() -> None:
    h = handler()
    assert h._query("", set()) == {} and h._query("", {"a"}) is None
    assert h._query("a=%E5%AE%89%E5%85%A8", {"a"}) == {"a": "安全"}


@pytest.mark.parametrize(
    "headers,body,fields,status",
    [
        ([("Transfer-Encoding", "chunked")], b"", set(), 400),
        ([("Content-Type", "text/plain"), ("Content-Length", "0")], b"", set(), 415),
        ([("Content-Type", "application/x-www-form-urlencoded")], b"", set(), 411),
        (
            [("Content-Type", "application/x-www-form-urlencoded"), ("Content-Length", "bad")],
            b"",
            set(),
            411,
        ),
        (
            [
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", "99999999999"),
            ],
            b"",
            set(),
            411,
        ),
        (
            [
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(s.MAX_FORM_BYTES + 1)),
            ],
            b"",
            set(),
            413,
        ),
        (
            [("Content-Type", "application/x-www-form-urlencoded"), ("Content-Length", "1")],
            b"\xff",
            set(),
            400,
        ),
        (
            [("Content-Type", "application/x-www-form-urlencoded"), ("Content-Length", "1")],
            b"%",
            set(),
            400,
        ),
        (
            [("Content-Type", "application/x-www-form-urlencoded"), ("Content-Length", "7")],
            b"a=1&a=2",
            {"a"},
            400,
        ),
        (
            [("Content-Type", "application/x-www-form-urlencoded"), ("Content-Length", "3")],
            b"a=1",
            {"b"},
            400,
        ),
    ],
)
def test_form_parser_failures(
    monkeypatch: pytest.MonkeyPatch,
    headers: list[tuple[str, str]],
    body: bytes,
    fields: set[str],
    status: int,
) -> None:
    h = handler(headers, body)
    seen: list[int] = []

    def record_error(value: HTTPStatus, *_args: object, **_kwargs: object) -> None:
        seen.append(value)

    monkeypatch.setattr(h, "_send_error", record_error)
    assert h._form(fields) is None and seen == [status]


def test_response_artifact_and_method_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    h = handler([("Cookie", "broken")])
    assert not h._cookie_valid()
    calls: list[Callable[[], object]] = [
        lambda: h._redirect("https://evil"),
        lambda: h._send(HTTPStatus.OK, b"x", download_name="bad/name"),
        lambda: h._send_artifact(tmp_path / "missing", "x"),
        lambda: h._send_artifact(tmp_path / "missing", "x", download_name="bad/name"),
    ]
    for call in calls:
        with pytest.raises(ContractError):
            call()
    f = tmp_path / "f"
    f.write_bytes(b"x")
    h._response_started = True
    h._send_artifact(f, "x")
    assert h.close_connection
    seen: list[int] = []

    def record_error(value: HTTPStatus, *_args: object, **_kwargs: object) -> None:
        seen.append(value)

    monkeypatch.setattr(h, "_send_error", record_error)
    h._method_not_allowed()
    assert seen == [405]


def test_post_routes_previously_unreached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _running_server(tmp_path / "app") as app:
        cookie, csrf, _, _ = _credentials(app)
        st = app.app_state
        key = "session_" + "a" * 32
        job = JobSnapshot(
            job_id="job_" + "b" * 32,
            session_key=key,
            operation="test",
            status="queued",
            progress=0,
            stage="queued",
            result=None,
            error=None,
        )
        calls: list[str] = []

        def submit_existing(_key: str) -> JobSnapshot:
            calls.append("export")
            return job

        def submit_retry(_key: str) -> JobSnapshot:
            calls.append("retry")
            return job

        monkeypatch.setattr(st, "submit_existing_export", submit_existing)
        monkeypatch.setattr(st, "open_review_docx", lambda _k: calls.append("open"))
        monkeypatch.setattr(
            st,
            "open_existing_changes_display",
            lambda _k: calls.append("open-display"),
        )
        monkeypatch.setattr(
            st,
            "save_existing_changes_display_copy",
            lambda _k: calls.append("save-display"),
        )
        monkeypatch.setattr(st, "choose_returned_word", lambda _k: None)
        monkeypatch.setattr(st, "accept_all_safe", lambda _k: calls.append("accept"))
        monkeypatch.setattr(st, "open_revised_source", lambda _k: calls.append("revised"))
        monkeypatch.setattr(st, "open_delivery_folder", lambda _k: calls.append("delivery"))
        monkeypatch.setattr(st, "submit_retry", submit_retry)
        for path in [
            "/session/export-existing",
            "/session/open-review-docx",
            "/session/open-existing-changes-display",
            "/session/save-existing-changes-copy",
            "/session/receive",
            "/approval/accept-safe",
            "/result/open-revised",
            "/result/open-delivery",
            "/result/retry",
        ]:
            assert _post_form(app, path, {"csrf": csrf, "session": key}, cookie=cookie)[0] == 303
        assert calls == [
            "export",
            "open",
            "open-display",
            "save-display",
            "accept",
            "revised",
            "delivery",
            "retry",
        ]
        assert (
            _post_form(
                app,
                "/result/generate",
                {
                    "csrf": csrf,
                    "session": key,
                    "patch_plan_sha256": "a" * 64,
                    "confirm_apply": "no",
                },
                cookie=cookie,
            )[0]
            == 400
        )


@pytest.mark.parametrize("port", [True, -1, 65536, "80"])
def test_bad_ports(tmp_path: Path, port: object) -> None:
    with pytest.raises(ContractError):
        s.create_app_server(tmp_path / "app", port=cast("Any", port))


class FakeServe:
    origin = "http://127.0.0.1:1"

    def __init__(self) -> None:
        self.served = self.closed = False

    def serve_forever(self, *, poll_interval: float) -> None:
        self.served = True

    def server_close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("launch", [False, True])
def test_serve_closes_and_schedules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launch: bool
) -> None:
    fake = FakeServe()
    timers: list[int] = []
    monkeypatch.setattr(s, "create_app_server", lambda *_a, **_k: fake)

    class Timer:
        daemon = False

        def __init__(self, *_a: object, **_k: object) -> None:
            timers.append(1)

        def start(self) -> None:
            pass

    monkeypatch.setattr(threading, "Timer", Timer)
    s.serve_app(tmp_path, launch_browser=launch)
    assert fake.served and fake.closed and bool(timers) is launch
