"""Command-line interface for the auditable review workflow."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from latex_word_review import __version__
from latex_word_review.applier import apply_patch_plan
from latex_word_review.approval import (
    create_approval_set,
    finalize_approval_set,
    record_decision,
    write_approval_json,
)
from latex_word_review.backends import BackendRequest, PandocBackend, Tex2WordBackend
from latex_word_review.bundle import BundleItem, create_audit_bundle, verify_audit_bundle
from latex_word_review.canonical import canonical_json, compute_payload_sha256
from latex_word_review.contracts import validate_contract
from latex_word_review.discovery import discover_project
from latex_word_review.doctor import diagnose_environment
from latex_word_review.errors import ContractError, ErrorCode, ExitCode
from latex_word_review.export import ExportBindings, export_review_docx
from latex_word_review.hashing import digest_bytes, read_stable_bytes
from latex_word_review.ingest import archive_returned_docx, verify_returned_archive
from latex_word_review.inspection import inspect_docx
from latex_word_review.jsonio import (
    read_contract_file,
    read_json_file,
    write_new_bytes,
    write_new_json,
)
from latex_word_review.latex_verify import VerificationPolicy, verify_latex_project
from latex_word_review.ledger import build_ledger
from latex_word_review.planner import plan_patch
from latex_word_review.review_server import create_review_server
from latex_word_review.revisions import build_changeset
from latex_word_review.snapshot import SNAPSHOT_MANIFEST, snapshot_project
from latex_word_review.workflow_objects import (
    bookmark_bindings_from_source_map,
    build_backend_capabilities_document,
    build_export_report_document,
    build_review_ir_document,
    build_revision_reader_capabilities_document,
    build_run_manifest_document,
    build_source_manifest_document,
    build_source_map_document,
    utc_now,
)

Handler = Callable[[argparse.Namespace], int]
_CONFIDENTIALITY = ("public_fixture", "local_private", "derived_private")
_DECISIONS = (
    "pending",
    "accepted",
    "accepted_with_edit",
    "rejected",
    "manual",
    "conflict",
)


def _emit(document: object, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(canonical_json(document).decode("utf-8") + "\n")
    stream.flush()


def _mkdir_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _handle_new_run(args: argparse.Namespace) -> int:
    from latex_word_review.ids import new_run_id

    del args
    _emit({"run_id": new_run_id()})
    return int(ExitCode.SUCCESS)


def _handle_doctor(args: argparse.Namespace) -> int:
    report = diagnose_environment(cwd=args.cwd)
    _emit(report.as_dict())
    return int(ExitCode.TOOL_OR_ENVIRONMENT if report.status == "blocked" else ExitCode.SUCCESS)


def _handle_snapshot(args: argparse.Namespace) -> int:
    _mkdir_parent(args.destination)
    _mkdir_parent(args.manifest_out)
    result = snapshot_project(args.source, args.destination, main_document=args.main)
    discovery = discover_project(args.destination, main_document=result.main_document)
    document = build_source_manifest_document(
        discovery,
        run_id=args.run_id,
        snapshot_manifest_file=args.destination / SNAPSHOT_MANIFEST,
        snapshot_artifact_path=args.artifact_path,
        confidentiality=args.confidentiality,
        generated_at=args.generated_at,
    )
    write_new_json(args.manifest_out, document, contract=True)
    _emit(
        {
            **result.as_dict(),
            "source_manifest_payload_sha256": compute_payload_sha256(document),
        }
    )
    return int(ExitCode.SUCCESS)


def _handle_export(args: argparse.Namespace) -> int:
    source_manifest = read_contract_file(args.source_manifest, expected_schema="SourceManifest")
    source_payload = cast("dict[str, Any]", source_manifest["payload"])
    run_id = cast("str", source_manifest["run_id"])
    main_document = cast("str", source_payload["main_document"])
    discovery = discover_project(args.snapshot, main_document=main_document)
    if discovery.source_tree_sha256 != source_payload["source_tree_sha256"]:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "snapshot differs from SourceManifest")
    if args.objects_dir.exists() or args.objects_dir.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "export objects directory already exists")
    args.objects_dir.mkdir(parents=True)
    _mkdir_parent(args.output)
    backend = Tex2WordBackend() if args.backend == "tex2word" else PandocBackend()
    outcome = export_review_docx(
        backend,
        BackendRequest(
            args.snapshot,
            main_document,
            args.output,
            expected_source_tree_sha256=discovery.source_tree_sha256,
            timeout_s=args.timeout,
        ),
        discovery,
        ExportBindings(
            source_manifest_sha256=compute_payload_sha256(source_manifest),
            artifact_path=args.artifact_path,
            confidentiality=args.confidentiality,
        ),
    )
    generated_at = args.generated_at or utc_now()
    capabilities = build_backend_capabilities_document(
        outcome.backend_result.capabilities,
        run_id=run_id,
        generated_at=generated_at,
    )
    export_report = build_export_report_document(
        outcome,
        run_id=run_id,
        generated_at=generated_at,
    )
    write_new_json(args.objects_dir / "backend-capabilities.json", capabilities, contract=True)
    objects = {
        "backend_capabilities_sha256": compute_payload_sha256(capabilities),
        "export_report_sha256": compute_payload_sha256(export_report),
    }
    if outcome.output_path is not None and outcome.anchoring is not None:
        review_ir = build_review_ir_document(
            discovery,
            outcome,
            run_id=run_id,
            source_manifest_sha256=compute_payload_sha256(source_manifest),
            generated_at=generated_at,
        )
        source_map = build_source_map_document(
            outcome.anchoring,
            run_id=run_id,
            source_manifest_sha256=compute_payload_sha256(source_manifest),
            review_ir_sha256=compute_payload_sha256(review_ir),
            generated_at=generated_at,
        )
        write_new_json(args.objects_dir / "review-ir.json", review_ir, contract=True)
        write_new_json(args.objects_dir / "source-map.json", source_map, contract=True)
        objects.update(
            review_ir_sha256=compute_payload_sha256(review_ir),
            source_map_sha256=compute_payload_sha256(source_map),
        )
    write_new_json(args.objects_dir / "export-report.json", export_report, contract=True)
    _emit({"status": outcome.report.status, **objects})
    return int(ExitCode.SUCCESS if outcome.output_path is not None else ExitCode.BACKEND_OR_EXPORT)


def _handle_inspect(args: argparse.Namespace) -> int:
    inspection = inspect_docx(args.docx)
    _emit(
        {
            "package_valid": inspection.package_valid,
            "structure_inspected": inspection.structure_inspected,
            "metrics": inspection.as_metrics(),
            "validation": inspection.validation.as_contract(),
            "findings": [item.as_diagnostic() for item in inspection.findings],
        }
    )
    return int(ExitCode.SUCCESS if inspection.package_valid else ExitCode.BACKEND_OR_EXPORT)


def _handle_reader_capabilities(args: argparse.Namespace) -> int:
    document = build_revision_reader_capabilities_document(
        run_id=args.run_id,
        generated_at=args.generated_at,
    )
    _mkdir_parent(args.output)
    write_new_json(args.output, document, contract=True)
    _emit({"revision_reader_capabilities_sha256": compute_payload_sha256(document)})
    return int(ExitCode.SUCCESS)


def _handle_archive(args: argparse.Namespace) -> int:
    _mkdir_parent(args.directory)
    result = archive_returned_docx(
        args.source,
        args.directory,
        run_id=args.run_id,
        exported_docx_sha256=args.exported_docx_sha256,
        confidentiality=args.confidentiality,
    )
    _emit(
        {
            "returned_docx_sha256": result.returned_docx_sha256,
            "manifest_sha256": result.manifest_sha256,
            "size_bytes": result.size_bytes,
            "run_id": result.run_id,
            "reused": result.reused,
        }
    )
    return int(ExitCode.SUCCESS)


def _handle_ingest(args: argparse.Namespace) -> int:
    source_manifest = read_contract_file(args.source_manifest, expected_schema="SourceManifest")
    source_map = read_contract_file(args.source_map, expected_schema="SourceMap")
    reader = read_contract_file(args.reader_capabilities, expected_schema="BackendCapabilities")
    run_id = cast("str", source_manifest["run_id"])
    if source_map["run_id"] != run_id or reader["run_id"] not in {None, run_id}:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "ingest run bindings differ")
    source_sha = compute_payload_sha256(source_manifest)
    map_payload = cast("dict[str, Any]", source_map["payload"])
    if map_payload["source_manifest_sha256"] != source_sha:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "SourceMap source binding differs")
    reader_payload = cast("dict[str, Any]", reader["payload"])
    if reader_payload["backend_role"] != "revision_reader":
        raise ContractError(ErrorCode.BACKEND_CAPABILITY_MISSING, "reader role is invalid")
    archive = verify_returned_archive(args.archive, expected_run_id=run_id)
    if archive.exported_docx_sha256 != map_payload["review_docx_sha256"]:
        raise ContractError(
            ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH,
            "returned archive is bound to a different exported DOCX",
        )
    archive_manifest = read_json_file(archive.manifest_path, max_bytes=64 * 1024)
    confidentiality = cast(
        "str", cast("dict[str, Any]", archive_manifest["artifact"])["confidentiality"]
    )
    changeset = build_changeset(
        archive.docx_path,
        run_id=run_id,
        source_manifest_sha256=source_sha,
        source_map_sha256=compute_payload_sha256(source_map),
        revision_reader_capabilities_sha256=compute_payload_sha256(reader),
        returned_artifact_path=args.artifact_path,
        confidentiality=confidentiality,
        bookmark_bindings=bookmark_bindings_from_source_map(source_map),
        generated_at=args.generated_at,
    )
    _mkdir_parent(args.output)
    write_new_json(args.output, changeset, contract=True)
    payload = cast("dict[str, Any]", changeset["payload"])
    _emit(
        {
            "changeset_payload_sha256": compute_payload_sha256(changeset),
            "counts": payload["counts"],
        }
    )
    return int(ExitCode.SUCCESS)


def _handle_approve_init(args: argparse.Namespace) -> int:
    changeset = read_contract_file(args.changeset, expected_schema="ChangeSet")
    approval = create_approval_set(
        changeset,
        decided_by={"id": args.actor_id, "display_name": args.actor_name},
        generated_at=args.generated_at,
    )
    _mkdir_parent(args.output)
    write_approval_json(changeset, approval, args.output)
    _emit({"approval_payload_sha256": compute_payload_sha256(approval), "revision": 1})
    return int(ExitCode.SUCCESS)


def _handle_approve_set(args: argparse.Namespace) -> int:
    changeset = read_contract_file(args.changeset, expected_schema="ChangeSet")
    current = read_contract_file(args.approval, expected_schema="ApprovalSet")
    approval = record_decision(
        changeset,
        current,
        change_id=args.change_id,
        decision=args.decision,
        final_text=args.final_text,
        reason=args.reason,
        risk_acknowledgement=args.risk_acknowledgement,
        decision_source="cli",
        decided_at=args.decided_at,
        generated_at=args.generated_at,
    )
    _mkdir_parent(args.output)
    write_approval_json(changeset, approval, args.output)
    payload = cast("dict[str, Any]", approval["payload"])
    _emit(
        {
            "approval_payload_sha256": compute_payload_sha256(approval),
            "revision": payload["revision"],
            "summary": payload["decision_summary"],
        }
    )
    return int(ExitCode.SUCCESS)


def _handle_approve_finalize(args: argparse.Namespace) -> int:
    changeset = read_contract_file(args.changeset, expected_schema="ChangeSet")
    current = read_contract_file(args.approval, expected_schema="ApprovalSet")
    approval = finalize_approval_set(changeset, current, generated_at=args.generated_at)
    _mkdir_parent(args.output)
    write_approval_json(changeset, approval, args.output)
    payload = cast("dict[str, Any]", approval["payload"])
    _emit(
        {
            "approval_payload_sha256": compute_payload_sha256(approval),
            "revision": payload["revision"],
            "status": payload["status"],
        }
    )
    return int(ExitCode.SUCCESS)


def _handle_approve_serve(args: argparse.Namespace) -> int:
    changeset = read_contract_file(args.changeset, expected_schema="ChangeSet")
    approval = read_contract_file(args.approval, expected_schema="ApprovalSet")
    if args.max_revisions <= 0 or args.max_revisions > 10000:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "max revisions must be in [1, 10000]")
    if args.output_dir.is_symlink():
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "approval output directory is a link")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    current_revision = cast("int", cast("dict[str, Any]", approval["payload"])["revision"])
    output_paths = {
        revision: args.output_dir / f"approval-r{revision}.json"
        for revision in range(current_revision + 1, current_revision + args.max_revisions + 1)
    }
    server = create_review_server(
        changeset,
        approval,
        approval_output_paths=output_paths,
        port=args.port,
    )
    _emit({"origin": server.origin, "status": "serving", "apply_enabled": False})
    if args.open_browser:
        webbrowser.open(server.origin, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return int(ExitCode.SUCCESS)


def _publish_plan_directory(directory: Path, document: dict[str, Any], diff: bytes) -> None:
    if directory.exists() or directory.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "plan output directory already exists")
    directory.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{directory.name}.plan-"
    staged = Path(tempfile.mkdtemp(prefix=prefix, dir=directory.parent))
    try:
        write_new_json(staged / "patch-plan.json", document, contract=True)
        write_new_bytes(staged / "changes.patch", diff)
        staged.rename(directory)
    except Exception:
        if (
            staged.exists()
            and staged.parent.resolve() == directory.parent.resolve()
            and staged.name.startswith(prefix)
        ):
            shutil.rmtree(staged)
        raise


def _handle_plan(args: argparse.Namespace) -> int:
    source_manifest = read_contract_file(args.source_manifest, expected_schema="SourceManifest")
    changeset = read_contract_file(args.changeset, expected_schema="ChangeSet")
    approval = read_contract_file(args.approval, expected_schema="ApprovalSet")
    source_sha = compute_payload_sha256(source_manifest)
    change_payload = cast("dict[str, Any]", changeset["payload"])
    if change_payload["source_manifest_sha256"] != source_sha:
        raise ContractError(ErrorCode.HASH_SOURCE_MISMATCH, "ChangeSet source binding differs")
    source_payload = cast("dict[str, Any]", source_manifest["payload"])
    plan = plan_patch(
        args.source,
        source_tree_sha256=cast("str", source_payload["source_tree_sha256"]),
        changeset=changeset,
        approval=approval,
        generated_at=args.generated_at or utc_now(),
        diff_path=args.diff_artifact_path,
        confidentiality=args.confidentiality,
    )
    _publish_plan_directory(args.output, plan.document, plan.unified_diff)
    payload = cast("dict[str, Any]", plan.document["payload"])
    _emit(
        {
            "patch_plan_payload_sha256": compute_payload_sha256(plan.document),
            "status": payload["status"],
            "summary": payload["summary"],
        }
    )
    return int(ExitCode.APPROVAL_OR_PLAN if payload["status"] == "blocked" else ExitCode.SUCCESS)


def _handle_apply(args: argparse.Namespace) -> int:
    plan = read_contract_file(args.plan / "patch-plan.json", expected_schema="PatchPlan")
    changeset = read_contract_file(args.changeset, expected_schema="ChangeSet")
    approval = read_contract_file(args.approval, expected_schema="ApprovalSet")
    diff = read_stable_bytes(args.plan / "changes.patch", max_bytes=16 * 1024 * 1024)
    result = apply_patch_plan(
        args.source,
        args.destination,
        patch_plan=plan,
        unified_diff=diff,
        changeset=changeset,
        approval=approval,
    )
    _emit(
        {
            "patch_plan_sha256": result.patch_plan_sha256,
            "source_tree_sha256": result.source_tree_sha256,
            "unified_diff_sha256": result.unified_diff_sha256,
            "output_file_count": result.output_file_count,
            "reused": result.reused,
        }
    )
    return int(ExitCode.SUCCESS)


def _handle_verify(args: argparse.Namespace) -> int:
    source_manifest = read_contract_file(args.source_manifest, expected_schema="SourceManifest")
    changeset = read_contract_file(args.changeset, expected_schema="ChangeSet")
    approval = read_contract_file(args.approval, expected_schema="ApprovalSet")
    patch_plan = read_contract_file(args.plan, expected_schema="PatchPlan")
    policy = VerificationPolicy(
        timeout_s=args.timeout,
        max_output_bytes=args.max_output_bytes,
        require_latexdiff=not args.allow_missing_latexdiff,
    )
    result = verify_latex_project(
        args.original,
        args.revised,
        args.returned_original,
        source_manifest,
        changeset,
        approval,
        patch_plan,
        args.output,
        latexmk_executable=args.latexmk,
        latexdiff_executable=args.latexdiff,
        policy=policy,
        generated_at=args.generated_at,
    )
    _emit(
        {
            "status": result.status,
            "verification_report_sha256": compute_payload_sha256(result.report),
            "revised_source_tree_sha256": result.revised_source_tree_sha256,
        }
    )
    return int(ExitCode.SUCCESS if result.status == "pass" else ExitCode.VERIFY_OR_BUNDLE)


def _publish_ledger_directory(directory: Path, json_bytes: bytes, html_bytes: bytes) -> None:
    if directory.exists() or directory.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "ledger output directory already exists")
    directory.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{directory.name}.ledger-"
    staged = Path(tempfile.mkdtemp(prefix=prefix, dir=directory.parent))
    try:
        write_new_bytes(staged / "ledger.json", json_bytes)
        write_new_bytes(staged / "ledger.html", html_bytes)
        staged.rename(directory)
    except Exception:
        if (
            staged.exists()
            and staged.parent.resolve() == directory.parent.resolve()
            and staged.name.startswith(prefix)
        ):
            shutil.rmtree(staged)
        raise


def _handle_ledger(args: argparse.Namespace) -> int:
    changeset = read_contract_file(args.changeset, expected_schema="ChangeSet")
    approval = read_contract_file(args.approval, expected_schema="ApprovalSet")
    patch_plan = read_contract_file(args.plan, expected_schema="PatchPlan")
    verification = read_contract_file(args.verification, expected_schema="VerificationReport")
    ledger = build_ledger(changeset, approval, patch_plan, verification)
    _publish_ledger_directory(args.output, ledger.json_bytes, ledger.html_bytes)
    _emit(
        {
            "ledger_json_sha256": digest_bytes(ledger.json_bytes).sha256,
            "change_count": len(cast("list[Any]", ledger.document["changes"])),
        }
    )
    return int(ExitCode.SUCCESS)


def _handle_run_manifest(args: argparse.Namespace) -> int:
    source_manifest = read_contract_file(args.source_manifest, expected_schema="SourceManifest")
    objects = [read_contract_file(path) for path in args.object]
    backends = [
        read_contract_file(path, expected_schema="BackendCapabilities") for path in args.backend
    ]
    document = build_run_manifest_document(
        source_manifest,
        objects=objects,
        backend_capabilities=backends,
        current_phase=args.phase,
        status=args.status,
        generated_at=args.generated_at,
    )
    _mkdir_parent(args.output)
    write_new_json(args.output, document, contract=True)
    _emit({"run_manifest_payload_sha256": compute_payload_sha256(document)})
    return int(ExitCode.SUCCESS)


def _handle_bundle(args: argparse.Namespace) -> int:
    run_manifest = read_contract_file(args.run_manifest, expected_schema="RunManifest")
    verification = read_contract_file(args.verification, expected_schema="VerificationReport")
    allowlist = [
        BundleItem(path, role, read_contract_file(Path(source_object)))
        for path, role, source_object in args.item
    ]
    _mkdir_parent(args.destination)
    result = create_audit_bundle(
        args.source_root,
        args.destination,
        allowlist=allowlist,
        run_manifest=run_manifest,
        verification_report=verification,
        content_classification=args.classification,
        generated_at=args.generated_at or utc_now(),
    )
    _emit(
        {
            "audit_bundle_sha256": result.file_sha256,
            "size_bytes": result.size_bytes,
            "entry_count": result.entry_count,
            "manifest_payload_sha256": compute_payload_sha256(result.document),
        }
    )
    return int(ExitCode.SUCCESS)


def _handle_verify_bundle(args: argparse.Namespace) -> int:
    run_manifest = (
        None
        if args.run_manifest is None
        else read_contract_file(args.run_manifest, expected_schema="RunManifest")
    )
    verification = (
        None
        if args.verification is None
        else read_contract_file(args.verification, expected_schema="VerificationReport")
    )
    result = verify_audit_bundle(
        args.bundle,
        expected_run_manifest=run_manifest,
        expected_verification_report=verification,
    )
    _emit(
        {
            "audit_bundle_sha256": result.file_sha256,
            "size_bytes": result.size_bytes,
            "entry_count": result.entry_count,
            "status": "pass",
        }
    )
    return int(ExitCode.SUCCESS)


def _handle_validate(args: argparse.Namespace) -> int:
    document = read_contract_file(args.path, expected_schema=args.schema)
    receipt = validate_contract(document)
    _emit(
        {
            "schema_name": receipt.schema_name,
            "schema_version": receipt.schema_version,
            "object_id": receipt.object_id,
            "payload_sha256": receipt.payload_sha256,
            "document_sha256": receipt.document_sha256,
        }
    )
    return int(ExitCode.SUCCESS)


def _add_generated_at(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--generated-at", help="ISO 8601 timestamp; defaults to current UTC")


def build_parser() -> argparse.ArgumentParser:
    """Build the complete parser without performing external I/O."""

    parser = argparse.ArgumentParser(
        prog="latex-word-review",
        description="Auditable, approval-gated LaTeX and Word review bridge.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    new_run = commands.add_parser("new-run", help="generate a new UUIDv7 run ID")
    new_run.set_defaults(handler=_handle_new_run)

    doctor = commands.add_parser("doctor", help="diagnose tools without installing anything")
    doctor.add_argument("--cwd", type=Path, default=Path.cwd())
    doctor.set_defaults(handler=_handle_doctor)

    snapshot = commands.add_parser("snapshot", help="create an immutable source snapshot")
    snapshot.add_argument("source", type=Path)
    snapshot.add_argument("destination", type=Path)
    snapshot.add_argument("--main")
    snapshot.add_argument("--run-id", required=True)
    snapshot.add_argument("--manifest-out", type=Path, required=True)
    snapshot.add_argument("--artifact-path", default="snapshot/snapshot-manifest.json")
    snapshot.add_argument("--confidentiality", choices=_CONFIDENTIALITY, default="derived_private")
    _add_generated_at(snapshot)
    snapshot.set_defaults(handler=_handle_snapshot)

    export = commands.add_parser("export", help="export and anchor a review DOCX")
    export.add_argument("snapshot", type=Path)
    export.add_argument("source_manifest", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("objects_dir", type=Path)
    export.add_argument("--backend", choices=("tex2word", "pandoc"), default="tex2word")
    export.add_argument("--artifact-path", default="export/review.docx")
    export.add_argument("--confidentiality", choices=_CONFIDENTIALITY, default="derived_private")
    export.add_argument("--timeout", type=float, default=60.0)
    _add_generated_at(export)
    export.set_defaults(handler=_handle_export)

    inspect = commands.add_parser("inspect", help="inspect DOCX structure without executing it")
    inspect.add_argument("docx", type=Path)
    inspect.set_defaults(handler=_handle_inspect)

    reader = commands.add_parser(
        "reader-capabilities", help="seal canonical revision-reader capabilities"
    )
    reader.add_argument("output", type=Path)
    reader.add_argument("--run-id", required=True)
    _add_generated_at(reader)
    reader.set_defaults(handler=_handle_reader_capabilities)

    archive = commands.add_parser("archive", help="immutably archive a returned Word original")
    archive.add_argument("source", type=Path)
    archive.add_argument("directory", type=Path)
    archive.add_argument("--run-id", required=True)
    archive.add_argument("--exported-docx-sha256", required=True)
    archive.add_argument("--confidentiality", choices=_CONFIDENTIALITY, default="local_private")
    archive.set_defaults(handler=_handle_archive)

    ingest = commands.add_parser("ingest", help="extract a complete ChangeSet from an archive")
    ingest.add_argument("archive", type=Path)
    ingest.add_argument("source_manifest", type=Path)
    ingest.add_argument("source_map", type=Path)
    ingest.add_argument("reader_capabilities", type=Path)
    ingest.add_argument("output", type=Path)
    ingest.add_argument("--artifact-path", default="returned/returned-original.docx")
    _add_generated_at(ingest)
    ingest.set_defaults(handler=_handle_ingest)

    approve = commands.add_parser("approve", help="record decisions without applying LaTeX")
    approve_commands = approve.add_subparsers(dest="approve_command", required=True)
    approve_init = approve_commands.add_parser("init")
    approve_init.add_argument("changeset", type=Path)
    approve_init.add_argument("output", type=Path)
    approve_init.add_argument("--actor-id", required=True)
    approve_init.add_argument("--actor-name", required=True)
    _add_generated_at(approve_init)
    approve_init.set_defaults(handler=_handle_approve_init)
    approve_set = approve_commands.add_parser("set")
    approve_set.add_argument("changeset", type=Path)
    approve_set.add_argument("approval", type=Path)
    approve_set.add_argument("output", type=Path)
    approve_set.add_argument("--change-id", required=True)
    approve_set.add_argument("--decision", choices=_DECISIONS, required=True)
    approve_set.add_argument("--final-text")
    approve_set.add_argument("--reason")
    approve_set.add_argument("--risk-acknowledgement")
    approve_set.add_argument("--decided-at")
    _add_generated_at(approve_set)
    approve_set.set_defaults(handler=_handle_approve_set)
    approve_final = approve_commands.add_parser("finalize")
    approve_final.add_argument("changeset", type=Path)
    approve_final.add_argument("approval", type=Path)
    approve_final.add_argument("output", type=Path)
    _add_generated_at(approve_final)
    approve_final.set_defaults(handler=_handle_approve_finalize)
    approve_serve = approve_commands.add_parser("serve")
    approve_serve.add_argument("changeset", type=Path)
    approve_serve.add_argument("approval", type=Path)
    approve_serve.add_argument("output_dir", type=Path)
    approve_serve.add_argument("--port", type=int, default=0)
    approve_serve.add_argument("--max-revisions", type=int, default=1024)
    approve_serve.add_argument("--open-browser", action="store_true")
    approve_serve.set_defaults(handler=_handle_approve_serve)

    plan = commands.add_parser("plan", help="build a dry-run PatchPlan and exact diff")
    plan.add_argument("source", type=Path)
    plan.add_argument("source_manifest", type=Path)
    plan.add_argument("changeset", type=Path)
    plan.add_argument("approval", type=Path)
    plan.add_argument("output", type=Path)
    plan.add_argument("--diff-artifact-path", default="plan/changes.patch")
    plan.add_argument("--confidentiality", choices=_CONFIDENTIALITY, default="derived_private")
    _add_generated_at(plan)
    plan.set_defaults(handler=_handle_plan)

    apply = commands.add_parser("apply", help="apply a ready plan to a new work copy")
    apply.add_argument("source", type=Path)
    apply.add_argument("plan", type=Path)
    apply.add_argument("changeset", type=Path)
    apply.add_argument("approval", type=Path)
    apply.add_argument("destination", type=Path)
    apply.set_defaults(handler=_handle_apply)

    verify = commands.add_parser(
        "verify", help="reconcile, compile, and generate clean plus latexdiff deliverables"
    )
    verify.add_argument("original", type=Path)
    verify.add_argument("revised", type=Path)
    verify.add_argument("returned_original", type=Path)
    verify.add_argument("source_manifest", type=Path)
    verify.add_argument("changeset", type=Path)
    verify.add_argument("approval", type=Path)
    verify.add_argument("plan", type=Path)
    verify.add_argument("output", type=Path)
    verify.add_argument("--latexmk", default="latexmk")
    verify.add_argument("--latexdiff", default="latexdiff")
    verify.add_argument("--timeout", type=float, default=60.0)
    verify.add_argument("--max-output-bytes", type=int, default=4 * 1024 * 1024)
    verify.add_argument("--allow-missing-latexdiff", action="store_true")
    _add_generated_at(verify)
    verify.set_defaults(handler=_handle_verify)

    ledger = commands.add_parser("ledger", help="render a bound JSON and HTML review ledger")
    ledger.add_argument("changeset", type=Path)
    ledger.add_argument("approval", type=Path)
    ledger.add_argument("plan", type=Path)
    ledger.add_argument("verification", type=Path)
    ledger.add_argument("output", type=Path)
    ledger.set_defaults(handler=_handle_ledger)

    run_manifest = commands.add_parser(
        "run-manifest", help="seal a path-free inventory of completed workflow objects"
    )
    run_manifest.add_argument("source_manifest", type=Path)
    run_manifest.add_argument("output", type=Path)
    run_manifest.add_argument("--object", type=Path, action="append", default=[])
    run_manifest.add_argument("--backend", type=Path, action="append", default=[])
    run_manifest.add_argument(
        "--phase",
        choices=(
            "initialized",
            "snapshotted",
            "exported",
            "ingested",
            "reviewed",
            "planned",
            "applied",
            "verified",
            "bundled",
        ),
        default="verified",
    )
    run_manifest.add_argument(
        "--status",
        choices=("active", "blocked", "failed", "completed", "aborted"),
        default="completed",
    )
    _add_generated_at(run_manifest)
    run_manifest.set_defaults(handler=_handle_run_manifest)

    bundle = commands.add_parser("bundle", help="create a deterministic allowlisted audit ZIP")
    bundle.add_argument("source_root", type=Path)
    bundle.add_argument("destination", type=Path)
    bundle.add_argument("run_manifest", type=Path)
    bundle.add_argument("verification", type=Path)
    bundle.add_argument(
        "--item",
        action="append",
        nargs=3,
        metavar=("PATH", "ROLE", "SOURCE_OBJECT"),
        required=True,
    )
    bundle.add_argument(
        "--classification", choices=("public_fixture", "local_private"), required=True
    )
    _add_generated_at(bundle)
    bundle.set_defaults(handler=_handle_bundle)

    verify_bundle = commands.add_parser("verify-bundle", help="verify an audit ZIP offline")
    verify_bundle.add_argument("bundle", type=Path)
    verify_bundle.add_argument("--run-manifest", type=Path)
    verify_bundle.add_argument("--verification", type=Path)
    verify_bundle.set_defaults(handler=_handle_verify_bundle)

    validate = commands.add_parser("validate", help="validate one sealed public JSON object")
    validate.add_argument("path", type=Path)
    validate.add_argument("--schema")
    validate.set_defaults(handler=_handle_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one command and translate contract errors to stable exit codes."""

    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast("Handler | None", getattr(args, "handler", None))
    if handler is None:
        parser.print_help()
        return int(ExitCode.SUCCESS)
    try:
        return handler(args)
    except ContractError as exc:
        _emit({"error": exc.as_dict()}, error=True)
        return int(exc.exit_code)
    except (OSError, UnicodeError, ValueError):
        error = ContractError(ErrorCode.INTERNAL_INVARIANT, "command failed unexpectedly")
        _emit({"error": error.as_dict()}, error=True)
        return int(ExitCode.INTERNAL)


__all__ = ["build_parser", "main"]
