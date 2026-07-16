#!/usr/bin/env python3
"""Run the public E0 review loop through the real CLI entry point.

The only non-CLI step simulates a reviewer in Word by adding one deterministic
tracked replacement to the DOCX produced by this run.  Every project operation
after that uses ``python -m latex_word_review`` in a child process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from lxml import etree  # type: ignore[import-untyped]

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DOCUMENT_PART = "word/document.xml"
DEFAULT_GENERATED_AT = "2026-07-16T06:30:00Z"
SAFE_KINDS = frozenset({"insertion", "deletion", "replacement"})
FORBIDDEN_LATEX_TEXT = frozenset("\\{}%$&#^_~")


class DemoError(RuntimeError):
    """A public-demo invariant or CLI step failed."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DemoError(f"expected a JSON object: {path.name}")
    return cast("dict[str, Any]", value)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _run_cli(
    repository: Path,
    step: str,
    *arguments: str | Path,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    command = [sys.executable, "-m", "latex_word_review", *(str(item) for item in arguments)]
    try:
        completed = subprocess.run(
            command,
            cwd=repository,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise DemoError(f"CLI step {step!r} exceeded {timeout_s:g} seconds") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        message = detail[-1] if detail else "no structured error was returned"
        raise DemoError(f"CLI step {step!r} exited {completed.returncode}: {message}")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise DemoError(f"CLI step {step!r} returned no JSON receipt")
    try:
        receipt = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise DemoError(f"CLI step {step!r} returned a non-JSON receipt") from exc
    if not isinstance(receipt, dict):
        raise DemoError(f"CLI step {step!r} returned a non-object receipt")
    typed = cast("dict[str, Any]", receipt)
    print(json.dumps({"receipt": typed, "step": step}, ensure_ascii=False, sort_keys=True))
    return typed


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


def _safe_replacement(before: str) -> str:
    if "defined by" in before:
        after = before.replace("defined by", "evaluated by", 1)
    else:
        after = before + " after review"
    if (
        after == before
        or "\n" in after
        or "\r" in after
        or any(character in FORBIDDEN_LATEX_TEXT for character in after)
    ):
        raise DemoError("the selected E0 source unit cannot form a safe plain-text replacement")
    return after


def _select_exact_mapping(
    source_map: Mapping[str, Any], snapshot: Path
) -> tuple[str, str, str, str, int]:
    payload = source_map.get("payload")
    if not isinstance(payload, dict):
        raise DemoError("SourceMap payload is missing")
    mappings = payload.get("mappings")
    if not isinstance(mappings, list):
        raise DemoError("SourceMap mappings are missing")
    for candidate in mappings:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("status") != "exact" or candidate.get("confidence") != 1:
            continue
        anchor = candidate.get("docx_anchor")
        location = candidate.get("source_location")
        if not isinstance(anchor, dict) or not isinstance(location, dict):
            continue
        if anchor.get("kind") != "bookmark" or anchor.get("part_uri") != DOCUMENT_PART:
            continue
        bookmark_name = anchor.get("name")
        relative_path = location.get("path")
        start = location.get("start_byte")
        end = location.get("end_byte")
        expected_sha256 = location.get("slice_sha256")
        if (
            not isinstance(bookmark_name, str)
            or not isinstance(relative_path, str)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not isinstance(expected_sha256, str)
            or start < 0
            or end <= start
        ):
            continue
        source_path = snapshot / Path(relative_path)
        data = source_path.read_bytes()
        if end > len(data):
            continue
        selected = data[start:end]
        if "sha256:" + hashlib.sha256(selected).hexdigest() != expected_sha256:
            continue
        before = selected.decode("utf-8")
        after = _safe_replacement(before)
        return bookmark_name, relative_path, before, after, start
    raise DemoError("the E0 export produced no exact plain-text bookmark mapping")


def _create_tracked_replacement(
    exported_docx: Path,
    returned_docx: Path,
    *,
    bookmark_name: str,
    before: str,
    after: str,
    timestamp: str,
) -> None:
    with zipfile.ZipFile(exported_docx, "r") as source:
        infos = source.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or DOCUMENT_PART not in names:
            raise DemoError("the exported DOCX package is not suitable for the public demo")
        members = {info.filename: source.read(info) for info in infos}

    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
    root = etree.fromstring(members[DOCUMENT_PART], parser=parser)
    starts = root.xpath(
        ".//w:bookmarkStart[@w:name=$name]",
        namespaces={"w": W_NS},
        name=bookmark_name,
    )
    if len(starts) != 1:
        raise DemoError("the selected E0 bookmark is not unique in the exported DOCX")
    start_element = starts[0]
    native_id = start_element.get(f"{{{W_NS}}}id")
    parent = start_element.getparent()
    if parent is None or native_id is None:
        raise DemoError("the selected E0 bookmark is incomplete")
    ends = parent.xpath(
        "./w:bookmarkEnd[@w:id=$native_id]",
        namespaces={"w": W_NS},
        native_id=native_id,
    )
    if len(ends) != 1:
        raise DemoError("the selected E0 bookmark end is incomplete")
    end_element = ends[0]
    start_index = parent.index(start_element)
    end_index = parent.index(end_element)
    if end_index <= start_index:
        raise DemoError("the selected E0 bookmark range is invalid")
    selected_children = list(parent)[start_index + 1 : end_index]
    observed = "".join(
        cast("str", text)
        for child in selected_children
        for text in child.xpath(".//w:t/text()", namespaces={"w": W_NS})
    )
    if observed != before:
        raise DemoError("the exported DOCX text does not match the sealed source slice")
    for child in selected_children:
        parent.remove(child)

    deleted = etree.Element(f"{{{W_NS}}}del")
    deleted.set(f"{{{W_NS}}}id", "9101")
    deleted.set(f"{{{W_NS}}}author", "Public E0 Demo Reviewer")
    deleted.set(f"{{{W_NS}}}date", timestamp)
    deleted_run = etree.SubElement(deleted, f"{{{W_NS}}}r")
    deleted_text = etree.SubElement(deleted_run, f"{{{W_NS}}}delText")
    deleted_text.set(f"{{{XML_NS}}}space", "preserve")
    deleted_text.text = before

    inserted = etree.Element(f"{{{W_NS}}}ins")
    inserted.set(f"{{{W_NS}}}id", "9102")
    inserted.set(f"{{{W_NS}}}author", "Public E0 Demo Reviewer")
    inserted.set(f"{{{W_NS}}}date", timestamp)
    inserted_run = etree.SubElement(inserted, f"{{{W_NS}}}r")
    inserted_text = etree.SubElement(inserted_run, f"{{{W_NS}}}t")
    inserted_text.set(f"{{{XML_NS}}}space", "preserve")
    inserted_text.text = after
    parent.insert(start_index + 1, deleted)
    parent.insert(start_index + 2, inserted)

    members[DOCUMENT_PART] = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )
    returned_docx.parent.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(returned_docx, "x") as target:
        for info in infos:
            target.writestr(info, members[info.filename])


def _decision_for(change: Mapping[str, Any]) -> str:
    resolution = change.get("resolution")
    confidence: object = None
    resolution_status: object = None
    if isinstance(resolution, dict):
        confidence = resolution.get("confidence")
        resolution_status = resolution.get("status")
    source_location = change.get("source_location")
    is_safe = (
        change.get("kind") in SAFE_KINDS
        and change.get("safety_class") == "plain_text_candidate"
        and resolution_status == "exact"
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and confidence >= 0.99
        and isinstance(source_location, dict)
    )
    return "accepted" if is_safe else "manual"


def _copy_delivery(verification: Path, ledger: Path, delivery: Path) -> None:
    delivery.mkdir()
    for name in (
        "verification-report.json",
        "actual.diff",
        "latexdiff.tex",
        "latexdiff.pdf",
        "revised-clean.pdf",
    ):
        source = verification / name
        if not source.is_file():
            raise DemoError(f"verified deliverable is missing: {name}")
        shutil.copyfile(source, delivery / name)
    for name in ("ledger.json", "ledger.html"):
        source = ledger / name
        if not source.is_file():
            raise DemoError(f"ledger deliverable is missing: {name}")
        shutil.copyfile(source, delivery / name)


def _full_verification(
    repository: Path,
    run_root: Path,
    *,
    generated_at: str,
    timeout_s: float,
) -> dict[str, Any]:
    objects = run_root / "objects"
    export_objects = run_root / "export-objects"
    verification = run_root / "verification"
    plan_document = run_root / "plan/patch-plan.json"
    approval = objects / "approval-final.json"
    changeset = objects / "changeset.json"
    source_manifest = objects / "source-manifest.json"
    returned_original = run_root / "returned/returned-original.docx"

    verify_receipt = _run_cli(
        repository,
        "verify",
        "verify",
        run_root / "snapshot",
        run_root / "revised-clean",
        returned_original,
        source_manifest,
        changeset,
        approval,
        plan_document,
        verification,
        "--timeout",
        str(timeout_s),
        "--generated-at",
        generated_at,
        timeout_s=max(600.0, timeout_s * 4),
    )
    if verify_receipt.get("status") != "pass":
        raise DemoError("verification did not pass")

    ledger = run_root / "ledger"
    _run_cli(
        repository,
        "ledger",
        "ledger",
        changeset,
        approval,
        plan_document,
        verification / "verification-report.json",
        ledger,
    )
    delivery = run_root / "delivery"
    _copy_delivery(verification, ledger, delivery)

    run_manifest = objects / "run-manifest.json"
    manifest_arguments: list[str | Path] = [
        "run-manifest",
        source_manifest,
        run_manifest,
        "--phase",
        "verified",
        "--status",
        "completed",
        "--generated-at",
        generated_at,
    ]
    for path in (
        export_objects / "review-ir.json",
        export_objects / "source-map.json",
        export_objects / "export-report.json",
        changeset,
        approval,
        plan_document,
        verification / "verification-report.json",
    ):
        manifest_arguments.extend(("--object", path))
    for path in (
        export_objects / "backend-capabilities.json",
        objects / "revision-reader.json",
    ):
        manifest_arguments.extend(("--backend", path))
    _run_cli(repository, "run-manifest", *manifest_arguments)

    bundle = run_root / "audit.zip"
    bundle_arguments: list[str | Path] = [
        "bundle",
        delivery,
        bundle,
        run_manifest,
        verification / "verification-report.json",
    ]
    for item_path, role, source_object in (
        (
            "verification-report.json",
            "verification_report",
            verification / "verification-report.json",
        ),
        ("actual.diff", "actual_diff", verification / "verification-report.json"),
        ("latexdiff.tex", "latexdiff_tex", verification / "verification-report.json"),
        ("latexdiff.pdf", "latexdiff_pdf", verification / "verification-report.json"),
        ("revised-clean.pdf", "revised_clean_pdf", verification / "verification-report.json"),
        ("ledger.json", "review_ledger_json", changeset),
        ("ledger.html", "review_ledger_html", changeset),
    ):
        bundle_arguments.extend(("--item", item_path, role, source_object))
    bundle_arguments.extend(("--classification", "public_fixture", "--generated-at", generated_at))
    bundle_receipt = _run_cli(repository, "bundle", *bundle_arguments)
    _run_cli(
        repository,
        "verify-bundle",
        "verify-bundle",
        bundle,
        "--run-manifest",
        run_manifest,
        "--verification",
        verification / "verification-report.json",
    )
    return {
        "status": "pass",
        "missing_tools": [],
        "verification_report": "verification/verification-report.json",
        "ledger_json": "ledger/ledger.json",
        "ledger_html": "ledger/ledger.html",
        "audit_bundle": "audit.zip",
        "audit_bundle_sha256": bundle_receipt.get("audit_bundle_sha256"),
    }


def run_demo(arguments: argparse.Namespace) -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[1]
    fixture = repository / "tests/fixtures/e0-minimal-paper/source"
    if not (fixture / "main.tex").is_file():
        raise DemoError("the public E0 fixture is missing")
    source_before = _tree_sha256(fixture)

    new_run = _run_cli(repository, "new-run", "new-run")
    run_id = new_run.get("run_id")
    if not isinstance(run_id, str) or not run_id.startswith("run_"):
        raise DemoError("new-run returned an invalid run ID")
    if arguments.output is None:
        run_root = repository / "build/public-e0-cli-demo" / run_id
    else:
        output = cast("Path", arguments.output)
        run_root = output if output.is_absolute() else repository / output
    run_root = run_root.resolve(strict=False)
    if run_root.exists() or run_root.is_symlink():
        raise DemoError("output directory already exists; choose a fresh path")
    run_root.mkdir(parents=True)

    generated_at = cast("str", arguments.generated_at)
    objects = run_root / "objects"
    source_manifest = objects / "source-manifest.json"
    snapshot = run_root / "snapshot"
    _run_cli(
        repository,
        "snapshot",
        "snapshot",
        fixture,
        snapshot,
        "--main",
        "main.tex",
        "--run-id",
        run_id,
        "--manifest-out",
        source_manifest,
        "--confidentiality",
        "public_fixture",
        "--generated-at",
        generated_at,
    )

    exported = run_root / "export/review.docx"
    export_objects = run_root / "export-objects"
    _run_cli(
        repository,
        "export",
        "export",
        snapshot,
        source_manifest,
        exported,
        export_objects,
        "--backend",
        "tex2word",
        "--confidentiality",
        "public_fixture",
        "--generated-at",
        generated_at,
    )
    source_map_path = export_objects / "source-map.json"
    source_map = _read_json(source_map_path)
    bookmark, relative_path, before, after, start_byte = _select_exact_mapping(source_map, snapshot)
    received = run_root / "received/returned-reviewed.docx"
    _create_tracked_replacement(
        exported,
        received,
        bookmark_name=bookmark,
        before=before,
        after=after,
        timestamp=generated_at,
    )
    received_before = "sha256:" + hashlib.sha256(received.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "change_kind": "replacement",
                "status": "created",
                "step": "simulate-word-review",
            },
            sort_keys=True,
        )
    )

    reader = objects / "revision-reader.json"
    _run_cli(
        repository,
        "reader-capabilities",
        "reader-capabilities",
        reader,
        "--run-id",
        run_id,
        "--generated-at",
        generated_at,
    )
    source_map_payload = source_map.get("payload")
    if not isinstance(source_map_payload, dict):
        raise DemoError("SourceMap payload is missing")
    exported_sha256 = source_map_payload.get("review_docx_sha256")
    if not isinstance(exported_sha256, str):
        raise DemoError("SourceMap review DOCX hash is missing")
    returned_archive = run_root / "returned"
    _run_cli(
        repository,
        "archive",
        "archive",
        received,
        returned_archive,
        "--run-id",
        run_id,
        "--exported-docx-sha256",
        exported_sha256,
        "--confidentiality",
        "public_fixture",
    )

    changeset_path = objects / "changeset.json"
    _run_cli(
        repository,
        "ingest",
        "ingest",
        returned_archive,
        source_manifest,
        source_map_path,
        reader,
        changeset_path,
        "--artifact-path",
        "returned/returned-original.docx",
        "--generated-at",
        generated_at,
    )
    changeset = _read_json(changeset_path)
    changeset_payload = changeset.get("payload")
    if not isinstance(changeset_payload, dict):
        raise DemoError("ChangeSet payload is missing")
    changes = changeset_payload.get("changes")
    if not isinstance(changes, list) or not changes:
        raise DemoError("the synthetic Word review produced no changes")

    approval_current = objects / "approval-r001.json"
    _run_cli(
        repository,
        "approve-init",
        "approve",
        "init",
        changeset_path,
        approval_current,
        "--actor-id",
        "public-e0-demo",
        "--actor-name",
        "Public E0 Demo Approver",
        "--generated-at",
        generated_at,
    )
    accepted = 0
    manual = 0
    for revision, untyped_change in enumerate(changes, start=2):
        if not isinstance(untyped_change, dict):
            raise DemoError("ChangeSet contains a non-object change")
        change = cast("dict[str, Any]", untyped_change)
        change_id = change.get("change_id")
        if not isinstance(change_id, str):
            raise DemoError("ChangeSet change ID is missing")
        decision = _decision_for(change)
        accepted += int(decision == "accepted")
        manual += int(decision == "manual")
        approval_next = objects / f"approval-r{revision:03d}.json"
        _run_cli(
            repository,
            f"approve-{revision:03d}",
            "approve",
            "set",
            changeset_path,
            approval_current,
            approval_next,
            "--change-id",
            change_id,
            "--decision",
            decision,
            "--reason",
            (
                "exact plain-text E0 demo change"
                if decision == "accepted"
                else "outside the v0.1 automatic-apply policy"
            ),
            "--risk-acknowledgement",
            "public fixture reviewed",
            "--decided-at",
            generated_at,
            "--generated-at",
            generated_at,
        )
        approval_current = approval_next
    approval_final = objects / "approval-final.json"
    _run_cli(
        repository,
        "approve-finalize",
        "approve",
        "finalize",
        changeset_path,
        approval_current,
        approval_final,
        "--generated-at",
        generated_at,
    )

    plan = run_root / "plan"
    plan_receipt = _run_cli(
        repository,
        "plan",
        "plan",
        snapshot,
        source_manifest,
        changeset_path,
        approval_final,
        plan,
        "--confidentiality",
        "public_fixture",
        "--generated-at",
        generated_at,
    )
    if plan_receipt.get("status") != "ready":
        raise DemoError("the E0 public plan is not ready")
    revised = run_root / "revised-clean"
    _run_cli(
        repository,
        "apply",
        "apply",
        snapshot,
        plan,
        changeset_path,
        approval_final,
        revised,
    )

    revised_bytes = (revised / relative_path).read_bytes()
    encoded_after = after.encode("utf-8")
    if revised_bytes[start_byte : start_byte + len(encoded_after)] != encoded_after:
        raise DemoError("the revised work copy does not contain the approved text")
    if _tree_sha256(fixture) != source_before:
        raise DemoError("the public E0 source fixture changed during the demo")
    if "sha256:" + hashlib.sha256(received.read_bytes()).hexdigest() != received_before:
        raise DemoError("the simulated returned Word original changed during the demo")

    missing_tools = [name for name in ("latexmk", "latexdiff") if shutil.which(name) is None]
    if cast("bool", arguments.skip_verification):
        verification: dict[str, Any] = {
            "status": "skipped",
            "reason": "user_requested",
            "missing_tools": missing_tools,
        }
        overall_status = "core_pass"
    elif missing_tools:
        verification = {
            "status": "degraded",
            "reason": "missing_external_tools",
            "missing_tools": missing_tools,
        }
        overall_status = "core_pass_degraded"
    else:
        verification = _full_verification(
            repository,
            run_root,
            generated_at=generated_at,
            timeout_s=cast("float", arguments.verify_timeout),
        )
        overall_status = "full_pass"

    summary: dict[str, Any] = {
        "demo_status": overall_status,
        "run_id": run_id,
        "fixture": "tests/fixtures/e0-minimal-paper",
        "core_loop": {
            "status": "pass",
            "change_count": len(changes),
            "accepted": accepted,
            "manual": manual,
            "source_fixture_unchanged": True,
            "returned_word_unchanged_after_archive": True,
        },
        "verification": verification,
        "artifacts": {
            "source_manifest": "objects/source-manifest.json",
            "review_docx": "export/review.docx",
            "returned_archive": "returned/returned-original.docx",
            "changeset": "objects/changeset.json",
            "approval": "objects/approval-final.json",
            "patch_plan": "plan/patch-plan.json",
            "patch": "plan/changes.patch",
            "revised_source": f"revised-clean/{relative_path}",
        },
    }
    _write_json(run_root / "demo-summary.json", summary)
    printable = {**summary, "output_root": str(run_root)}
    print(json.dumps(printable, ensure_ascii=False, sort_keys=True))
    return printable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the repository's public E0 fixture through the real CLI workflow."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="fresh output directory; default: build/public-e0-cli-demo/<run-id>",
    )
    parser.add_argument(
        "--generated-at",
        default=DEFAULT_GENERATED_AT,
        help="ISO 8601 timestamp used by deterministic public-demo objects",
    )
    parser.add_argument(
        "--verify-timeout",
        type=float,
        default=60.0,
        help="per-command timeout passed to LaTeX verification tools",
    )
    parser.add_argument(
        "--skip-verification",
        action="store_true",
        help="stop after apply even when latexmk and latexdiff are installed",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.verify_timeout <= 0 or arguments.verify_timeout > 60:
        build_parser().error("--verify-timeout must be in (0, 60]")
    try:
        run_demo(arguments)
    except (DemoError, OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        print(
            json.dumps(
                {"demo_status": "failed", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
