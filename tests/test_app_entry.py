"""GUI/CLI entry-point routing tests."""

from __future__ import annotations

import sys
from pathlib import Path

from latex_word_review import __main__ as entry
from latex_word_review import app_server, cli, windows_dialogs
from latex_word_review.errors import ContractError, ErrorCode, ExitCode


def test_cli_app_command_passes_explicit_windows_options(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path | None, int, bool]] = []

    def fake_serve(
        data_root: Path | None = None,
        *,
        port: int = 0,
        launch_browser: bool = True,
        **_: object,
    ) -> None:
        calls.append((data_root, port, launch_browser))

    monkeypatch.setattr(app_server, "serve_app", fake_serve)  # type: ignore[attr-defined]

    result = cli.main(
        [
            "app",
            "--data-root",
            str(tmp_path),
            "--port",
            "43123",
            "--no-browser",
        ]
    )

    assert result == 0
    assert calls == [(tmp_path, 43123, False)]


def test_internal_worker_dispatch_precedes_all_user_facing_routing(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(entry, "dispatch_internal_worker", lambda _: 27)  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "argv", ["LatexWordReview.exe", "untrusted"])  # type: ignore[attr-defined]

    assert entry.main() == 27


def test_frozen_windowed_executable_opens_app_by_default(
    monkeypatch: object,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(entry, "dispatch_internal_worker", lambda _: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "frozen", True, raising=False)  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\LatexWordReview.exe")  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "argv", ["LatexWordReview.exe"])  # type: ignore[attr-defined]
    monkeypatch.setattr(app_server, "serve_app", lambda: calls.append(True))  # type: ignore[attr-defined]

    assert entry.main() == 0
    assert calls == [True]


def test_frozen_windowed_executable_reports_startup_failure(
    monkeypatch: object,
) -> None:
    shown: list[str] = []

    def record_failure(code: str) -> bool:
        shown.append(code)
        return True

    monkeypatch.setattr(entry, "dispatch_internal_worker", lambda _: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "frozen", True, raising=False)  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\LatexWordReview.exe")  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "argv", ["LatexWordReview.exe"])  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        app_server,
        "serve_app",
        lambda: (_ for _ in ()).throw(ContractError(ErrorCode.TOOL_MISSING, "missing")),
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        windows_dialogs,
        "show_startup_failure",
        record_failure,
    )

    assert entry.main() == int(ExitCode.TOOL_OR_ENVIRONMENT)
    assert shown == [ErrorCode.TOOL_MISSING.value]
