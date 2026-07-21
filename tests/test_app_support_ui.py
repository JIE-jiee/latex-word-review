"""HTTP integration tests for redacted, one-time support downloads."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import threading
import time
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import cast

import pytest

import latex_word_review.app_server as app_server_module
from latex_word_review.app_server import AppState, _error_view
from latex_word_review.app_views import render_app_page
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.support_bundle import (
    DEFAULT_SUPPORT_BUNDLE_LIMITS,
    SUPPORT_BUNDLE_FILENAME,
    SupportBundleLimits,
    SupportBundleRequest,
    SupportBundleVerification,
    verify_support_bundle_bytes,
)
from latex_word_review.support_bundle import (
    build_support_bundle_bytes as _build_support_bundle_bytes,
)
from tests.test_app_server import _credentials, _post_form, _request, _running_server


def _empty_session(state: AppState) -> str:
    session_key = f"session_{'a' * 32}"
    (state.runs_root / session_key).mkdir()
    return session_key


def test_support_token_is_bound_to_session_code_and_stage(tmp_path: Path) -> None:
    state = AppState(tmp_path / "app")
    session_key = _empty_session(state)
    token = state.issue_support_token(
        session_key,
        ErrorCode.INTERNAL_INVARIANT,
        "session_view",
    )
    payload = (f"{session_key}\0{ErrorCode.INTERNAL_INVARIANT.value}\0session_view").encode("ascii")
    cookie_signature = hmac.new(
        state.session_token.encode("ascii"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    forged_from_cookie = (
        f"support1.{ErrorCode.INTERNAL_INVARIANT.value}.session_view.{cookie_signature}"
    )
    csrf_signature = hmac.new(
        state.csrf_token.encode("ascii"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    forged_from_csrf = (
        f"support1.{ErrorCode.INTERNAL_INVARIANT.value}.session_view.{csrf_signature}"
    )
    second_session = f"session_{'b' * 32}"
    (state.runs_root / second_session).mkdir()
    altered_code = token.replace(
        ErrorCode.INTERNAL_INVARIANT.value,
        ErrorCode.BACKEND_CAPABILITY_MISSING.value,
        1,
    )
    altered_stage = token.replace("session_view", "export_review", 1)

    try:
        for forged in (forged_from_cookie, forged_from_csrf, altered_code, altered_stage):
            assert forged != token
            with pytest.raises(ContractError) as forged_error:
                state.create_support_download(session_key, forged)
            assert forged_error.value.code is ErrorCode.SCHEMA_INVALID
        with pytest.raises(ContractError) as wrong_session:
            state.create_support_download(second_session, token)
        assert wrong_session.value.code is ErrorCode.SCHEMA_INVALID
        replacement = "0" if token[-1] != "0" else "1"
        with pytest.raises(ContractError) as raised:
            state.create_support_download(session_key, token[:-1] + replacement)
    finally:
        state.close()

    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert not (tmp_path / "app" / "support-downloads").exists()

    restarted = AppState(tmp_path / "restarted")
    restarted_session = _empty_session(restarted)
    try:
        with pytest.raises(ContractError) as stale:
            restarted.create_support_download(restarted_session, token)
    finally:
        restarted.close()
    assert stale.value.code is ErrorCode.SCHEMA_INVALID


def test_claim_returns_verified_bytes_without_creating_pending_files(tmp_path: Path) -> None:
    state = AppState(tmp_path / "app")
    session_key = _empty_session(state)
    token = state.issue_support_token(
        session_key,
        ErrorCode.INTERNAL_INVARIANT,
        "session_view",
    )
    download_key = state.create_support_download(session_key, token)

    try:
        data = state.claim_support_download(download_key)
    finally:
        state.close()

    assert isinstance(data, bytes)
    assert not (tmp_path / "app" / "support-downloads").exists()
    assert verify_support_bundle_bytes(data).entry_count == 2


def test_claim_rejects_a_different_valid_bundle_swapped_after_creation(
    tmp_path: Path,
) -> None:
    state = AppState(tmp_path / "app")
    session_key = _empty_session(state)
    first_token = state.issue_support_token(
        session_key,
        ErrorCode.INTERNAL_INVARIANT,
        "session_view",
    )
    second_token = state.issue_support_token(
        session_key,
        ErrorCode.BACKEND_CAPABILITY_MISSING,
        "export_review",
    )
    first_key = state.create_support_download(session_key, first_token)
    second_key = state.create_support_download(session_key, second_token)
    first = state._support_downloads[first_key]
    second = state._support_downloads[second_key]
    state._support_downloads[first_key] = replace(first, data=second.data)

    try:
        with pytest.raises(ContractError) as raised:
            state.claim_support_download(first_key)
    finally:
        state.close()

    assert raised.value.code is ErrorCode.BUNDLE_HASH_MISMATCH
    assert not state._support_downloads
    assert not (tmp_path / "app" / "support-downloads").exists()


def test_close_waits_for_inflight_support_create_and_prevents_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    session_key = _empty_session(state)
    token = state.issue_support_token(
        session_key,
        ErrorCode.INTERNAL_INVARIANT,
        "session_view",
    )
    started = threading.Event()
    release = threading.Event()

    def delayed_build(
        request: SupportBundleRequest,
        *,
        limits: SupportBundleLimits = DEFAULT_SUPPORT_BUNDLE_LIMITS,
    ) -> bytes:
        started.set()
        assert release.wait(timeout=5)
        return _build_support_bundle_bytes(request, limits=limits)

    monkeypatch.setattr(app_server_module, "build_support_bundle_bytes", delayed_build)
    failures: list[BaseException] = []

    def create() -> None:
        try:
            state.create_support_download(session_key, token)
        except BaseException as exc:
            failures.append(exc)

    creator = threading.Thread(target=create)
    closer = threading.Thread(target=state.close)
    creator.start()
    assert started.wait(timeout=5)
    closer.start()
    deadline = time.monotonic() + 5
    while not state._support_closing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state._support_closing
    release.set()
    creator.join(timeout=5)
    closer.join(timeout=5)

    assert not creator.is_alive()
    assert not closer.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ContractError)
    assert not state._support_downloads
    assert not (tmp_path / "app" / "support-downloads").exists()


def test_close_waits_for_inflight_claim_and_rejects_new_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState(tmp_path / "app")
    session_key = _empty_session(state)
    token = state.issue_support_token(
        session_key,
        ErrorCode.INTERNAL_INVARIANT,
        "session_view",
    )
    claimed_key = state.create_support_download(session_key, token)
    pending_key = state.create_support_download(session_key, token)
    started = threading.Event()
    release = threading.Event()

    def delayed_verify(
        data: bytes,
        *,
        limits: SupportBundleLimits = DEFAULT_SUPPORT_BUNDLE_LIMITS,
    ) -> SupportBundleVerification:
        started.set()
        assert release.wait(timeout=5)
        return verify_support_bundle_bytes(data, limits=limits)

    monkeypatch.setattr(
        app_server_module,
        "verify_support_bundle_bytes",
        delayed_verify,
    )
    claimed: list[bytes] = []
    failures: list[BaseException] = []

    def claim() -> None:
        try:
            claimed.append(state.claim_support_download(claimed_key))
        except BaseException as exc:
            failures.append(exc)

    claimer = threading.Thread(target=claim)
    closer = threading.Thread(target=state.close)
    claimer.start()
    assert started.wait(timeout=5)
    closer.start()
    deadline = time.monotonic() + 5
    while not state._support_closing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state._support_closing
    assert closer.is_alive()

    with pytest.raises(ContractError) as raised:
        state.claim_support_download(pending_key)

    release.set()
    claimer.join(timeout=5)
    closer.join(timeout=5)

    assert raised.value.code is ErrorCode.INTERNAL_INVARIANT
    assert not claimer.is_alive()
    assert not closer.is_alive()
    assert not failures
    assert len(claimed) == 1
    assert not state._support_downloads
    assert not (tmp_path / "app" / "support-downloads").exists()


def test_concurrent_claim_is_strictly_one_time(tmp_path: Path) -> None:
    state = AppState(tmp_path / "app")
    session_key = _empty_session(state)
    token = state.issue_support_token(
        session_key,
        ErrorCode.INTERNAL_INVARIANT,
        "session_view",
    )
    download_key = state.create_support_download(session_key, token)
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    results: list[bytes] = []
    failures: list[ContractError] = []

    def claim() -> None:
        barrier.wait()
        try:
            data = state.claim_support_download(download_key)
        except ContractError as exc:
            with lock:
                failures.append(exc)
        else:
            with lock:
                results.append(data)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
    state.close()

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 1
    assert verify_support_bundle_bytes(results[0]).entry_count == 2
    assert [failure.code for failure in failures] == [ErrorCode.SCHEMA_INVALID]
    assert not (tmp_path / "app" / "support-downloads").exists()


def test_pending_support_memory_is_bounded_to_32_fixed_archives(tmp_path: Path) -> None:
    state = AppState(tmp_path / "app")
    session_key = _empty_session(state)
    token = state.issue_support_token(
        session_key,
        ErrorCode.INTERNAL_INVARIANT,
        "session_view",
    )

    try:
        for _ in range(32):
            state.create_support_download(session_key, token)
        with pytest.raises(ContractError) as raised:
            state.create_support_download(session_key, token)
        total_bytes = sum(len(pending.data) for pending in state._support_downloads.values())
    finally:
        state.close()

    assert raised.value.code is ErrorCode.SCHEMA_INVALID
    assert total_bytes <= 32 * DEFAULT_SUPPORT_BUNDLE_LIMITS.max_archive_bytes
    assert not state._support_downloads
    assert not (tmp_path / "app" / "support-downloads").exists()


def test_support_button_creates_one_redacted_zip_then_retires_it(tmp_path: Path) -> None:
    with _running_server(tmp_path / "app") as server:
        cookie, csrf, _body, _headers = _credentials(server)
        state = server.app_state
        session_key = _empty_session(state)
        private_probes = (
            "SYNTHETIC_PRIVATE_PAPER_BODY_7429",
            "SYNTHETIC_RETURNED_DOCX_MARKER_6813",
            str(tmp_path.resolve()),
        )
        (state.runs_root / session_key / "paper.tex").write_text(
            private_probes[0],
            encoding="utf-8",
        )
        (state.runs_root / session_key / "returned.docx").write_bytes(
            private_probes[1].encode("utf-8")
        )
        (state.runs_root / session_key / "diagnostic.log").write_text(
            private_probes[2],
            encoding="utf-8",
        )
        view = _error_view(
            ErrorCode.INTERNAL_INVARIANT,
            csrf_token=csrf,
            session_key=session_key,
        )
        state.bind_support_action(
            view,
            session_key=session_key,
            stage="session_view",
        )
        html = render_app_page(view)
        card = cast("Mapping[str, object]", view["error"])
        control = cast("Mapping[str, object]", card["primary_action"])
        fields = cast("Mapping[str, object]", control["form_fields"])
        support_token = cast("str", fields["support_token"])
        assert control["kind"] == "post"
        assert control["href"] == "/support/export"
        assert 'method="post" action="/support/export"' in html
        assert "保存支持信息" in html
        assert support_token == state.issue_support_token(
            session_key,
            ErrorCode.INTERNAL_INVARIANT,
            "session_view",
        )

        rejected = _post_form(
            server,
            "/support/export",
            {
                "csrf": "wrong",
                "session": session_key,
                "support_token": support_token,
            },
            cookie=cookie,
        )
        assert rejected[0] == HTTPStatus.SEE_OTHER
        assert rejected[1]["location"] == "/"
        assert not state._support_downloads

        created = _post_form(
            server,
            "/support/export",
            {
                "csrf": csrf,
                "session": session_key,
                "support_token": support_token,
            },
            cookie=cookie,
        )
        assert created[0] == HTTPStatus.SEE_OTHER
        location = created[1]["location"]
        assert re.fullmatch(r"/support/support_[0-9a-f]{32}", location) is not None

        unauthorized = _request(
            server,
            "GET",
            location,
            headers={"Host": server.expected_host},
        )
        assert unauthorized[0] == HTTPStatus.FORBIDDEN
        assert location.removeprefix("/support/") in state._support_downloads

        downloaded = _request(
            server,
            "GET",
            location,
            headers={"Host": server.expected_host, "Cookie": cookie},
        )
        repeated = _request(
            server,
            "GET",
            location,
            headers={"Host": server.expected_host, "Cookie": cookie},
        )
        remaining = tuple(state._support_downloads)
        support_directory_exists = (state.data_root / "support-downloads").exists()

    assert downloaded[0] == HTTPStatus.OK
    assert downloaded[1]["content-type"] == "application/zip"
    assert downloaded[1]["content-disposition"] == (
        f'attachment; filename="{SUPPORT_BUNDLE_FILENAME}"'
    )
    assert downloaded[1]["content-length"] == str(len(downloaded[2]))
    assert downloaded[1]["cache-control"] == "no-store, max-age=0"
    assert downloaded[1]["pragma"] == "no-cache"
    assert downloaded[1]["referrer-policy"] == "no-referrer"
    assert downloaded[1]["x-content-type-options"] == "nosniff"
    assert downloaded[1]["cross-origin-resource-policy"] == "same-origin"
    assert downloaded[1]["connection"] == "close"
    assert "set-cookie" not in downloaded[1]
    assert "access-control-allow-origin" not in downloaded[1]
    assert repeated[0] == HTTPStatus.CONFLICT
    assert remaining == ()
    assert not support_directory_exists
    verification = verify_support_bundle_bytes(downloaded[2])
    assert verification.entry_count == 2
    assert session_key.encode("ascii") not in downloaded[2]
    assert cookie.encode("ascii") not in downloaded[2]
    assert csrf.encode("ascii") not in downloaded[2]
    assert support_token.encode("ascii") not in downloaded[2]
    for probe in private_probes:
        assert probe.encode("utf-8") not in downloaded[2]

    with zipfile.ZipFile(io.BytesIO(downloaded[2])) as archive:
        assert archive.namelist() == ["README.txt", "support-metadata.json"]
        metadata = json.loads(archive.read("support-metadata.json"))
    assert metadata["error_code"] == ErrorCode.INTERNAL_INVARIANT.value
    assert metadata["stage"] == "session_view"
    assert metadata["anonymous_task_id"].startswith("task_")
    assert metadata["anonymous_task_id"] != session_key
