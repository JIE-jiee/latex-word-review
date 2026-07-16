"""Public CLI behavior for approval, plan/apply, verification, and rollback gates."""

from __future__ import annotations

import copy
import runpy
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

import latex_word_review.cli as cli_module
from latex_word_review.canonical import compute_payload_sha256, seal_envelope
from latex_word_review.cli import main
from latex_word_review.contracts import validate_contract
from latex_word_review.doctor import DoctorReport
from latex_word_review.errors import ErrorCode, ExitCode
from latex_word_review.jsonio import read_contract_file, write_new_bytes, write_new_json
from latex_word_review.latex_verify import VerificationPolicy, VerificationResult
from tests.test_contracts import _golden_contracts
from tests.test_plan_apply import _changeset, _final_approval, _write_source

TIME = "2026-07-16T15:00:00+09:00"
ACTOR_ID = "synthetic-actor"
ACTOR_NAME = "Synthetic Actor"
FIXTURE_DOCX = Path(__file__).parent / "fixtures/e0-minimal-paper/base/review-base.docx"


def _write_contracts(root: Path, *names: str) -> dict[str, Path]:
    root.mkdir()
    documents = _golden_contracts()
    paths: dict[str, Path] = {}
    for name in names:
        path = root / f"{name}.json"
        write_new_json(path, documents[name], contract=True)
        paths[name] = path
    return paths


def _plan_documents(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    source = tmp_path / "source"
    tree_sha = _write_source(source, "Hello\n")
    source_manifest = copy.deepcopy(_golden_contracts()["SourceManifest"])
    source_manifest["payload"]["source_tree_sha256"] = tree_sha
    source_manifest = seal_envelope(source_manifest)
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])
    changeset["payload"]["source_manifest_sha256"] = compute_payload_sha256(source_manifest)
    changeset = seal_envelope(changeset)
    approval = _final_approval(changeset, [("accepted", None)])
    objects = tmp_path / "objects"
    objects.mkdir()
    documents = {
        "SourceManifest": source_manifest,
        "ChangeSet": changeset,
        "ApprovalSet": approval,
    }
    paths: dict[str, Path] = {}
    for name, document in documents.items():
        path = objects / f"{name}.json"
        write_new_json(path, document, contract=True)
        paths[name] = path
    return source, paths


def test_cli_new_run_doctor_inspect_and_validate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert main(["new-run"]) == 0
    assert '"run_id":"run_' in capsys.readouterr().out

    available = DoctorReport("ready", "test", "test", "3.12", (), ())
    blocked = DoctorReport("blocked", "test", "test", "3.12", (), ())
    monkeypatch.setattr(cli_module, "diagnose_environment", lambda cwd: available)
    assert main(["doctor", "--cwd", str(tmp_path)]) == 0
    assert '"status":"ready"' in capsys.readouterr().out
    monkeypatch.setattr(cli_module, "diagnose_environment", lambda cwd: blocked)
    assert main(["doctor", "--cwd", str(tmp_path)]) == int(ExitCode.TOOL_OR_ENVIRONMENT)
    assert '"status":"blocked"' in capsys.readouterr().out

    assert main(["inspect", str(FIXTURE_DOCX)]) == 0
    inspect_output = capsys.readouterr().out
    assert '"package_valid":true' in inspect_output
    assert '"metrics"' in inspect_output

    paths = _write_contracts(tmp_path / "objects", "ChangeSet")
    assert main(["validate", str(paths["ChangeSet"]), "--schema", "ChangeSet"]) == 0
    receipt = capsys.readouterr().out
    assert '"schema_name":"ChangeSet"' in receipt
    assert '"document_sha256"' in receipt


def test_cli_approval_state_machine_is_separate_from_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "Welcome")])
    change_id = cast("str", changeset["payload"]["changes"][0]["change_id"])
    changeset_path = tmp_path / "changeset.json"
    initial_path = tmp_path / "approval-r1.json"
    decided_path = tmp_path / "approval-r2.json"
    final_path = tmp_path / "approval-r3.json"
    write_new_json(changeset_path, changeset, contract=True)

    assert (
        main(
            [
                "approve",
                "init",
                str(changeset_path),
                str(initial_path),
                "--actor-id",
                ACTOR_ID,
                "--actor-name",
                ACTOR_NAME,
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    assert '"revision":1' in capsys.readouterr().out
    assert (
        main(
            [
                "approve",
                "set",
                str(changeset_path),
                str(initial_path),
                str(decided_path),
                "--change-id",
                change_id,
                "--decision",
                "accepted_with_edit",
                "--final-text",
                "Greetings",
                "--reason",
                "clarity",
                "--risk-acknowledgement",
                "reviewed",
                "--decided-at",
                TIME,
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    assert '"revision":2' in capsys.readouterr().out
    assert (
        main(
            [
                "approve",
                "finalize",
                str(changeset_path),
                str(decided_path),
                str(final_path),
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    assert '"status":"final"' in capsys.readouterr().out
    assert validate_contract(read_contract_file(final_path)).schema_name == "ApprovalSet"
    assert not list(tmp_path.glob("*.tex"))


def test_cli_plan_then_apply_only_to_new_worktree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, paths = _plan_documents(tmp_path)
    original = (source / "main.tex").read_bytes()
    plan_dir = tmp_path / "plan"
    assert (
        main(
            [
                "plan",
                str(source),
                str(paths["SourceManifest"]),
                str(paths["ChangeSet"]),
                str(paths["ApprovalSet"]),
                str(plan_dir),
                "--generated-at",
                TIME,
            ]
        )
        == 0
    )
    assert '"status":"ready"' in capsys.readouterr().out
    assert (source / "main.tex").read_bytes() == original
    assert (plan_dir / "patch-plan.json").is_file()
    assert (plan_dir / "changes.patch").is_file()

    revised = tmp_path / "revised"
    assert (
        main(
            [
                "apply",
                str(source),
                str(plan_dir),
                str(paths["ChangeSet"]),
                str(paths["ApprovalSet"]),
                str(revised),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"reused":false' in output
    assert (revised / "main.tex").read_text(encoding="utf-8") == "Welcome\n"
    assert (source / "main.tex").read_bytes() == original


def test_cli_blocked_plan_returns_stable_gate_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    tree_sha = _write_source(source, "Hello\n")
    source_manifest = copy.deepcopy(_golden_contracts()["SourceManifest"])
    source_manifest["payload"]["source_tree_sha256"] = tree_sha
    source_manifest = seal_envelope(source_manifest)
    changeset = _changeset("Hello\n", [("replacement", 0, 5, "50%")])
    changeset["payload"]["source_manifest_sha256"] = compute_payload_sha256(source_manifest)
    changeset = seal_envelope(changeset)
    approval = _final_approval(changeset, [("accepted", None)])
    objects = tmp_path / "objects"
    objects.mkdir()
    paths = []
    for name, document in (
        ("source.json", source_manifest),
        ("changes.json", changeset),
        ("approval.json", approval),
    ):
        path = objects / name
        write_new_json(path, document, contract=True)
        paths.append(path)

    code = main(
        [
            "plan",
            str(source),
            *(str(path) for path in paths),
            str(tmp_path / "blocked-plan"),
            "--generated-at",
            TIME,
        ]
    )
    assert code == int(ExitCode.APPROVAL_OR_PLAN)
    assert '"status":"blocked"' in capsys.readouterr().out


def test_cli_verify_maps_pass_and_fail_statuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_contracts(
        tmp_path / "objects",
        "SourceManifest",
        "ChangeSet",
        "ApprovalSet",
        "PatchPlan",
    )
    report = _golden_contracts()["VerificationReport"]
    observed: dict[str, object] = {}

    def fake_verify(*args: object, **kwargs: object) -> VerificationResult:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return VerificationResult(report, tmp_path / "verification", "sha256:" + "a" * 64, "pass")

    monkeypatch.setattr(cli_module, "verify_latex_project", fake_verify)
    arguments = [
        "verify",
        str(tmp_path / "original"),
        str(tmp_path / "revised"),
        str(tmp_path / "returned.docx"),
        str(paths["SourceManifest"]),
        str(paths["ChangeSet"]),
        str(paths["ApprovalSet"]),
        str(paths["PatchPlan"]),
        str(tmp_path / "verification"),
        "--latexmk",
        "safe-latexmk",
        "--latexdiff",
        "safe-latexdiff",
        "--timeout",
        "5",
        "--max-output-bytes",
        "1024",
        "--allow-missing-latexdiff",
        "--generated-at",
        TIME,
    ]
    assert main(arguments) == 0
    assert '"status":"pass"' in capsys.readouterr().out
    policy = cast(
        "VerificationPolicy",
        cast("dict[str, object]", observed["kwargs"])["policy"],
    )
    assert policy.require_latexdiff is False

    def fake_fail(*args: object, **kwargs: object) -> VerificationResult:
        del args, kwargs
        return VerificationResult(report, tmp_path / "verification", "sha256:" + "a" * 64, "fail")

    monkeypatch.setattr(cli_module, "verify_latex_project", fake_fail)
    assert main(arguments) == int(ExitCode.VERIFY_OR_BUNDLE)
    assert '"status":"fail"' in capsys.readouterr().out


@dataclass(slots=True)
class _FakeReviewServer:
    origin: str = "http://127.0.0.1:12345"
    served: bool = False
    shutdown_called: bool = False
    close_called: bool = False

    def serve_forever(self, *, poll_interval: float) -> None:
        assert poll_interval == 0.25
        self.served = True
        raise KeyboardInterrupt

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        self.close_called = True


def test_cli_local_review_server_entry_and_limits(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_contracts(tmp_path / "objects", "ChangeSet", "ApprovalSet")
    server = _FakeReviewServer()
    opened: list[tuple[str, int]] = []
    monkeypatch.setattr(cli_module, "create_review_server", lambda *args, **kwargs: server)

    def fake_open(url: str, new: int = 0) -> bool:
        opened.append((url, new))
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)
    assert (
        main(
            [
                "approve",
                "serve",
                str(paths["ChangeSet"]),
                str(paths["ApprovalSet"]),
                str(tmp_path / "review-state"),
                "--max-revisions",
                "2",
                "--open-browser",
            ]
        )
        == 0
    )
    assert '"apply_enabled":false' in capsys.readouterr().out
    assert server.served and server.shutdown_called and server.close_called
    assert opened == [(server.origin, 2)]

    code = main(
        [
            "approve",
            "serve",
            str(paths["ChangeSet"]),
            str(paths["ApprovalSet"]),
            str(tmp_path / "invalid"),
            "--max-revisions",
            "0",
        ]
    )
    assert code == int(ExitCode.USAGE_OR_SCHEMA)
    assert ErrorCode.SCHEMA_INVALID.value in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["plan", "ledger"])
def test_cli_directory_publication_rolls_back_partial_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    destination = tmp_path / kind
    original = write_new_bytes

    def fail_second(path: Path, data: bytes, *, mode: int = 0o600) -> Path:
        if path.name in {"changes.patch", "ledger.html"}:
            raise OSError("synthetic write failure")
        return original(path, data, mode=mode)

    monkeypatch.setattr("latex_word_review.cli.write_new_bytes", fail_second)
    if kind == "plan":
        with pytest.raises(OSError):
            cli_module._publish_plan_directory(
                destination,
                _golden_contracts()["PatchPlan"],
                b"diff",
            )
    else:
        with pytest.raises(OSError):
            cli_module._publish_ledger_directory(destination, b"json", b"html")
    assert not destination.exists()
    assert not list(tmp_path.glob(f".{kind}.{kind}-*"))


def test_cli_stable_contract_and_unexpected_error_translation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "changeset.json"
    write_new_json(path, _golden_contracts()["ChangeSet"], contract=True)
    code = main(["validate", str(path), "--schema", "SourceMap"])
    assert code == int(ExitCode.USAGE_OR_SCHEMA)
    assert ErrorCode.SCHEMA_INVALID.value in capsys.readouterr().err

    monkeypatch.setattr(
        cli_module, "validate_contract", lambda document: (_ for _ in ()).throw(OSError())
    )
    code = main(["validate", str(path)])
    assert code == int(ExitCode.INTERNAL)
    assert ErrorCode.INTERNAL_INVARIANT.value in capsys.readouterr().err


def test_python_module_entry_executes_cli_in_current_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["latex-word-review"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("latex_word_review.__main__", run_name="__main__")
    assert stopped.value.code == 0
    assert "approval-gated" in capsys.readouterr().out
