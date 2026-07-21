"""Opt-in four-step product journey in the installed Microsoft Edge.

The gate uses the redistributable E0 fixture, the system Edge channel, and no
downloaded Playwright browser.  It exercises the rendered application from
project selection through the protected final-evidence responses while
rejecting every external request or browser-side error.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from latex_word_review.app_server import AppHTTPServer, create_app_server
from latex_word_review.application import ApplicationSession
from latex_word_review.hashing import digest_file
from tests.test_e2e_public_roundtrip import _fake_tools
from tests.test_workflow import _make_returned

_BROWSER_E2E_ENV = "LATEX_WORD_REVIEW_BROWSER_E2E"
_FIXTURE_SOURCE = (Path(__file__).parent / "fixtures/e0-minimal-paper/source").resolve()
_MAIN_TEX = _FIXTURE_SOURCE / "main.tex"
_MAX_TEST_DOCX_BYTES = 64 * 1024 * 1024

pytestmark = [
    pytest.mark.skipif(
        os.environ.get(_BROWSER_E2E_ENV) != "1",
        reason=f"set {_BROWSER_E2E_ENV}=1 to run the installed-Edge gate",
    ),
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="the supported real-browser gate requires Windows",
    ),
]


@contextmanager
def _serving(data_root: Path, returned_docx: Path) -> Iterator[AppHTTPServer]:
    server = create_app_server(
        data_root,
        main_picker=lambda _initial: _MAIN_TEX,
        word_picker=lambda _initial: returned_docx,
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


def _source_bytes() -> dict[Path, bytes]:
    return {
        path.relative_to(_FIXTURE_SOURCE): path.read_bytes()
        for path in _FIXTURE_SOURCE.rglob("*")
        if path.is_file()
    }


def test_installed_edge_completes_the_four_step_product_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete the public review, both human gates, and evidence responses."""

    assert _MAIN_TEX.is_file()
    source_before = _source_bytes()
    data_root = tmp_path / "app-data"
    returned_docx = tmp_path / "returned.docx"
    try:
        playwright_api = importlib.import_module("playwright.sync_api")
    except ImportError as exc:  # pragma: no cover - enabled gate must fail explicitly
        pytest.fail(f"enabled browser gate is missing Playwright: {exc}")
    playwright_error = playwright_api.Error
    expect = playwright_api.expect
    sync_playwright = playwright_api.sync_playwright

    page_errors: list[str] = []
    console_errors: list[str] = []
    request_failures: list[str] = []
    external_requests: list[str] = []
    bad_responses: list[str] = []
    with _serving(data_root, returned_docx) as server, sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=tmp_path / "edge-profile",
                channel="msedge",
                headless=True,
                accept_downloads=False,
                downloads_path=tmp_path / "downloads",
                args=["--disable-background-networking", "--no-first-run"],
            )
        except playwright_error as exc:  # pragma: no cover - depends on runner image
            pytest.fail(f"installed Microsoft Edge could not be launched: {exc}")

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            def record_console(message: object) -> None:
                if getattr(message, "type", "") == "error":
                    console_errors.append(str(getattr(message, "text", "unknown console error")))

            def record_request(url: str) -> None:
                if not url.startswith(f"{server.origin}/"):
                    external_requests.append(url)

            def record_failed_request(request: object) -> None:
                url = str(getattr(request, "url", "unknown request"))
                failure = str(getattr(request, "failure", "unknown failure"))
                request_failures.append(f"{url}: {failure}")

            def record_response(response: object) -> None:
                status = getattr(response, "status", 0)
                if isinstance(status, int) and status >= 400:
                    bad_responses.append(f"{status} {getattr(response, 'url', 'unknown response')}")

            page.on("console", record_console)
            page.on("request", lambda request: record_request(request.url))
            page.on("requestfailed", record_failed_request)
            page.on("response", record_response)
            response = page.goto(server.origin, wait_until="domcontentloaded")
            assert response is not None
            assert response.status == 200
            expect(page.get_by_role("button", name="新建审阅", exact=True)).to_be_visible()

            page.get_by_role("button", name="新建审阅", exact=True).click()
            page.wait_for_url(
                re.compile(rf"^{re.escape(server.origin)}/preflight/selection_[0-9a-f]{{32}}$")
            )
            expect(page.get_by_role("heading", name="确认论文与环境", exact=True)).to_be_visible()
            expect(page.get_by_text("main.tex", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="生成审阅 Word", exact=True)).to_be_enabled()

            page.get_by_role("button", name="生成审阅 Word", exact=True).click()
            expect(
                page.get_by_role("heading", name="Word 审阅交接与修改稿导入", exact=True)
            ).to_be_visible(timeout=90_000)
            expect(page.get_by_role("button", name="另存可编辑 Word", exact=True)).to_be_visible()
            session_match = re.fullmatch(
                rf"{re.escape(server.origin)}/session/(session_[0-9a-f]{{32}})",
                page.url,
            )
            assert session_match is not None
            session_key = session_match.group(1)
            run_root = data_root / "runs" / session_key
            review_docx = run_root / "export/review.docx"
            assert review_docx.is_file()
            review_digest = digest_file(review_docx, max_bytes=_MAX_TEST_DOCX_BYTES)

            _make_returned(run_root, returned_docx)
            returned_digest = digest_file(returned_docx, max_bytes=_MAX_TEST_DOCX_BYTES)
            page.get_by_role(
                "button",
                name="选择修改后的 Word，开始逐项审批",
                exact=True,
            ).click()
            expect(
                page.get_by_role("heading", name="逐项审批 Word 修改", exact=True)
            ).to_be_visible(timeout=90_000)
            expect(page.get_by_text("已决定 0 / 1 项", exact=True)).to_be_visible()

            page.get_by_role("button", name="采用", exact=True).click()
            expect(page.get_by_text("已决定 1 / 1 项", exact=True)).to_be_visible()
            page.get_by_role("button", name="完成审批并预览补丁", exact=True).click()
            expect(
                page.get_by_role("heading", name="预览并回填到新 LaTeX 副本", exact=True)
            ).to_be_visible(timeout=90_000)
            expect(
                page.get_by_role("heading", name="生成新 LaTeX 副本", exact=True)
            ).to_be_visible()

            tool_calls: list[tuple[str, tuple[str, ...]]] = []
            monkeypatch.setattr(
                "latex_word_review.latex_verify.run_command",
                _fake_tools(tool_calls),
            )
            page.get_by_role(
                "checkbox",
                name="我已检查补丁摘要，并确认只回填到新的 LaTeX 工作副本。",
                exact=True,
            ).check()
            page.get_by_role(
                "button",
                name="确认回填到新 LaTeX 副本并生成结果",
                exact=True,
            ).click()
            expect(
                page.get_by_role("heading", name="新 LaTeX 副本与全部结果已生成", exact=True)
            ).to_be_visible(timeout=90_000)
            assert tool_calls

            ledger_card = page.locator("li.artifact-card").filter(has_text="机器可读账本")
            audit_card = page.locator("li.artifact-card").filter(has_text="审计包")
            expect(ledger_card).to_have_count(1)
            expect(audit_card).to_have_count(1)
            ledger_href = ledger_card.get_by_role("link", name="打开", exact=True).get_attribute(
                "href"
            )
            audit_href = audit_card.get_by_role("link", name="打开", exact=True).get_attribute(
                "href"
            )
            assert ledger_href == f"/artifact/{session_key}/ledger_json"
            assert audit_href == f"/artifact/{session_key}/audit_bundle"
            ledger_response = context.request.get(server.origin + ledger_href)
            audit_response = context.request.get(server.origin + audit_href)
            try:
                assert ledger_response.status == 200
                assert ledger_response.headers["content-type"] == "application/json"
                assert ledger_response.headers["content-disposition"] == "attachment"
                assert ledger_response.body() == (run_root / "delivery/ledger.json").read_bytes()
                assert audit_response.status == 200
                assert audit_response.headers["content-type"] == "application/zip"
                assert audit_response.headers["content-disposition"] == "attachment"
                assert audit_response.body() == (run_root / "audit.zip").read_bytes()
            finally:
                ledger_response.dispose()
                audit_response.dispose()

            assert ApplicationSession.load(run_root).status()["phase"] == "completed"
            assert digest_file(review_docx, max_bytes=_MAX_TEST_DOCX_BYTES) == review_digest
            assert digest_file(returned_docx, max_bytes=_MAX_TEST_DOCX_BYTES) == returned_digest
            assert _source_bytes() == source_before
            assert page_errors == []
            assert console_errors == []
            assert request_failures == []
            assert external_requests == []
            assert bad_responses == []
        finally:
            context.close()
