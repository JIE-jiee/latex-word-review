"""Focused contracts for the export-specific conversion timeout budget."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import latex_word_review.application as application_module
import latex_word_review.workflow as workflow_module
from latex_word_review.application import ApplicationSession
from latex_word_review.backends.base import BackendRequest, prepare_export
from latex_word_review.cli import build_parser
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_limits import (
    DEFAULT_EXPORT_TIMEOUT_SECONDS,
    MAX_EXPORT_TIMEOUT_SECONDS,
    validate_export_timeout,
)
from latex_word_review.runtime import minimal_environment, run_command, run_conversion_command


class _StopAfterRequest(RuntimeError):
    """Stop a plumbing test immediately after observing its backend request."""


def _assert_schema_error(operation: Any) -> None:
    with pytest.raises(ContractError) as caught:
        operation()
    assert caught.value.code is ErrorCode.SCHEMA_INVALID


@pytest.mark.parametrize(
    "timeout_s",
    [True, 0.0, -1.0, float("inf"), float("nan"), MAX_EXPORT_TIMEOUT_SECONDS + 0.001],
)
def test_shared_export_timeout_rejects_invalid_values(timeout_s: float) -> None:
    _assert_schema_error(lambda: validate_export_timeout(timeout_s))


def test_shared_export_timeout_contract_and_backend_request(tmp_path: Path) -> None:
    assert DEFAULT_EXPORT_TIMEOUT_SECONDS == 300.0
    assert MAX_EXPORT_TIMEOUT_SECONDS == 600.0
    assert validate_export_timeout(MAX_EXPORT_TIMEOUT_SECONDS) == 600.0

    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    default_request = BackendRequest(source, "main.tex", tmp_path / "out/default.docx")
    assert default_request.timeout_s == DEFAULT_EXPORT_TIMEOUT_SECONDS
    prepared = prepare_export(
        BackendRequest(
            source,
            "main.tex",
            tmp_path / "out/maximum.docx",
            timeout_s=MAX_EXPORT_TIMEOUT_SECONDS,
        ),
        owner="timeout-test",
    )
    assert prepared.temporary_path.parent == (tmp_path / "out").resolve()
    _assert_schema_error(
        lambda: prepare_export(
            BackendRequest(
                source,
                "main.tex",
                tmp_path / "out/too-long.docx",
                timeout_s=MAX_EXPORT_TIMEOUT_SECONDS + 0.001,
            ),
            owner="timeout-test",
        )
    )


def test_conversion_runner_does_not_relax_general_runtime_limit(tmp_path: Path) -> None:
    environment = minimal_environment(temp_root=tmp_path)
    _assert_schema_error(
        lambda: run_command(
            sys.executable,
            ("--version",),
            cwd=tmp_path,
            timeout_s=61.0,
            environment=environment,
        )
    )
    completed = run_conversion_command(
        sys.executable,
        ("--version",),
        cwd=tmp_path,
        timeout_s=61.0,
        environment=environment,
    )
    assert completed.returncode == 0
    assert completed.timed_out is False
    _assert_schema_error(
        lambda: run_conversion_command(
            sys.executable,
            ("--version",),
            cwd=tmp_path,
            timeout_s=MAX_EXPORT_TIMEOUT_SECONDS + 1,
            environment=environment,
        )
    )


def test_cli_and_application_use_shared_export_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = build_parser()
    workflow_args = parser.parse_args(["workflow", "export", "run"])
    direct_args = parser.parse_args(
        ["export", "snapshot", "manifest.json", "review.docx", "objects"]
    )
    assert workflow_args.timeout == DEFAULT_EXPORT_TIMEOUT_SECONDS
    assert direct_args.timeout == DEFAULT_EXPORT_TIMEOUT_SECONDS

    session = ApplicationSession(tmp_path / "run")
    evidence = SimpleNamespace(workflow={"phase": "snapshotted"})
    captured: dict[str, object] = {}

    def fake_export_workflow(run_root: Path, **kwargs: object) -> dict[str, object]:
        captured["run_root"] = run_root
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(session, "_inspect", lambda: evidence)
    monkeypatch.setattr(application_module, "export_workflow", fake_export_workflow)
    monkeypatch.setattr(session, "status", lambda: {"phase": "exported"})

    assert session.export_review() == {"phase": "exported"}
    assert captured["timeout_s"] == DEFAULT_EXPORT_TIMEOUT_SECONDS


def test_clean_and_display_conversions_each_receive_the_full_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[BackendRequest] = []
    alias_counts = SimpleNamespace(add=0, delete=0)
    inventory = SimpleNamespace(total=1, alias_counts=alias_counts)
    core = SimpleNamespace(
        root=tmp_path / "run",
        source_manifest={"payload": {"main_document": "main.tex"}},
        source_manifest_sha256="sha256:" + "a" * 64,
        discovery=SimpleNamespace(source_tree_sha256="sha256:" + "b" * 64),
    )
    payload = tmp_path / "clean-payload"
    payload.mkdir()

    def stop_after_clean_request(
        _backend: object,
        request: BackendRequest,
        _discovery: object,
        _bindings: object,
    ) -> object:
        observed.append(request)
        raise _StopAfterRequest

    monkeypatch.setattr(workflow_module, "scan_revision_macros", lambda *_args: inventory)
    monkeypatch.setattr(workflow_module, "export_review_docx", stop_after_clean_request)
    with pytest.raises(_StopAfterRequest):
        workflow_module._write_export_objects(
            cast("Any", core),
            payload,
            backend_name="tex2word",
            timeout_s=DEFAULT_EXPORT_TIMEOUT_SECONDS,
            confidentiality="derived_private",
            generated_at=None,
        )

    display_payload = tmp_path / "display-payload"
    display_payload.mkdir()
    display_inventory = SimpleNamespace(
        total=1, degraded_display_macro_instances=0, alias_counts=alias_counts
    )
    display_core = SimpleNamespace(discovery=SimpleNamespace(main_document="main.tex"))
    overlay = SimpleNamespace(
        ready=True,
        derived_root=tmp_path / "derived",
        discovery=SimpleNamespace(source_tree_sha256="sha256:" + "c" * 64),
    )
    outcome = SimpleNamespace(image_overlay=overlay, output_path=tmp_path / "clean.docx")

    class CapturingBackend:
        def export(self, request: BackendRequest) -> object:
            observed.append(request)
            raise _StopAfterRequest

    with pytest.raises(_StopAfterRequest):
        workflow_module._write_existing_changes_display(
            cast("Any", display_core),
            display_payload,
            backend=cast("Any", CapturingBackend()),
            outcome=cast("Any", outcome),
            inventory=cast("Any", display_inventory),
            timeout_s=DEFAULT_EXPORT_TIMEOUT_SECONDS,
            confidentiality="derived_private",
        )

    assert [request.timeout_s for request in observed] == [300.0, 300.0]
    assert [request.revision_view for request in observed] == ["clean", "display"]
    assert observed[0] is not observed[1]
