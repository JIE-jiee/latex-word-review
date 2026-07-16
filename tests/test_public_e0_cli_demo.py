"""The copyable public E0 tutorial executes the real CLI core loop."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from scripts.run_public_e0_cli_demo import _decision_for

from latex_word_review.jsonio import read_contract_file

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts/run_public_e0_cli_demo.py"
FIXTURE_SOURCE = REPOSITORY / "tests/fixtures/e0-minimal-paper/source"


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=str):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def _plain_text_change(kind: str) -> dict[str, object]:
    return {
        "kind": kind,
        "safety_class": "plain_text_candidate",
        "resolution": {"status": "exact", "confidence": 1.0},
        "source_location": {"path": "sections/methods.tex"},
    }


@pytest.mark.parametrize("kind", ["insertion", "deletion", "replacement"])
def test_public_demo_accepts_only_exact_safe_text_kinds(kind: str) -> None:
    assert _decision_for(_plain_text_change(kind)) == "accepted"


@pytest.mark.parametrize(
    "change",
    [
        _plain_text_change("move"),
        _plain_text_change("format"),
        _plain_text_change("comment"),
        {
            **_plain_text_change("replacement"),
            "resolution": {"status": "unmatched", "confidence": 0.0},
        },
        {**_plain_text_change("replacement"), "safety_class": "manual_high_risk"},
        {**_plain_text_change("replacement"), "source_location": None},
    ],
)
def test_public_demo_routes_everything_else_to_manual(change: dict[str, object]) -> None:
    assert _decision_for(change) == "manual"


def test_public_e0_demo_runs_snapshot_through_apply_via_cli(tmp_path: Path) -> None:
    fixture_before = _tree_sha256(FIXTURE_SOURCE)
    output = tmp_path / "public-e0-demo"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--skip-verification",
        ],
        cwd=REPOSITORY,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    final_line = [line for line in completed.stdout.splitlines() if line.strip()][-1]
    summary = cast("dict[str, Any]", json.loads(final_line))
    assert summary["demo_status"] == "core_pass"
    assert summary["core_loop"] == {
        "accepted": 1,
        "change_count": 1,
        "manual": 0,
        "returned_word_unchanged_after_archive": True,
        "source_fixture_unchanged": True,
        "status": "pass",
    }
    assert summary["verification"]["status"] == "skipped"
    assert summary["verification"]["reason"] == "user_requested"

    changeset = read_contract_file(output / "objects/changeset.json", expected_schema="ChangeSet")
    approval = read_contract_file(
        output / "objects/approval-final.json", expected_schema="ApprovalSet"
    )
    patch_plan = read_contract_file(output / "plan/patch-plan.json", expected_schema="PatchPlan")
    assert changeset["payload"]["counts"]["changes"] == 1
    assert changeset["payload"]["changes"][0]["kind"] == "replacement"
    assert changeset["payload"]["changes"][0]["resolution"]["status"] == "exact"
    assert approval["payload"]["status"] == "final"
    assert approval["payload"]["decision_summary"]["accepted"] == 1
    assert patch_plan["payload"]["status"] == "ready"
    assert patch_plan["payload"]["summary"]["planned"] == 1
    assert (output / "plan/changes.patch").stat().st_size > 0

    original = (output / "snapshot/sections/methods.tex").read_text(encoding="utf-8")
    revised = (output / "revised-clean/sections/methods.tex").read_text(encoding="utf-8")
    assert "kinetic energy is defined by" in original
    assert "kinetic energy is evaluated by" in revised
    assert "kinetic energy is evaluated by" not in original
    assert _tree_sha256(FIXTURE_SOURCE) == fixture_before

    archived = output / "returned/returned-original.docx"
    received = output / "received/returned-reviewed.docx"
    assert (
        hashlib.sha256(archived.read_bytes()).digest()
        == hashlib.sha256(received.read_bytes()).digest()
    )
    persisted_summary = cast(
        "dict[str, Any]", json.loads((output / "demo-summary.json").read_text(encoding="utf-8"))
    )
    assert "output_root" not in persisted_summary
    assert persisted_summary["run_id"] == summary["run_id"]
