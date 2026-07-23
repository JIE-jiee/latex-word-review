"""Public contract builders over a real E0 export."""

from __future__ import annotations

import shutil
from pathlib import Path

from latex_word_review.backends import BackendRequest, Tex2WordBackend
from latex_word_review.contracts import compute_payload_sha256, validate_contract
from latex_word_review.discovery import discover_project
from latex_word_review.export import ExportBindings, export_review_docx
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
)
from tests.test_contracts import _golden_contracts

FIXTURE_ROOT = Path(__file__).parent / "fixtures/e0-minimal-paper"
RUN_ID = "run_019b0000-0000-7000-8000-000000000001"
TIME = "2026-07-16T15:00:00+09:00"


def test_builders_seal_the_real_e0_export_chain(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    snapshot = tmp_path / "run" / "snapshot"
    shutil.copytree(FIXTURE_ROOT / "source", origin)
    snapshot_result = snapshot_project(origin, snapshot, main_document="main.tex")
    discovery = discover_project(snapshot, main_document="main.tex")
    source_manifest = build_source_manifest_document(
        discovery,
        run_id=RUN_ID,
        snapshot_manifest_file=snapshot / SNAPSHOT_MANIFEST,
        snapshot_artifact_path="snapshot/snapshot-manifest.json",
        confidentiality="public_fixture",
        generated_at=TIME,
    )
    source_receipt = validate_contract(source_manifest)
    assert source_receipt.schema_name == "SourceManifest"
    assert source_manifest["payload"]["source_tree_sha256"] == snapshot_result.source_tree_sha256

    output = tmp_path / "run" / "export" / "review.docx"
    outcome = export_review_docx(
        Tex2WordBackend(),
        BackendRequest(snapshot, "main.tex", output),
        discovery,
        ExportBindings(
            source_manifest_sha256=source_receipt.payload_sha256,
            artifact_path="export/review.docx",
            confidentiality="public_fixture",
        ),
    )
    capabilities = build_backend_capabilities_document(
        outcome.backend_result.capabilities,
        run_id=RUN_ID,
        generated_at=TIME,
    )
    review_ir = build_review_ir_document(
        discovery,
        outcome,
        run_id=RUN_ID,
        source_manifest_sha256=source_receipt.payload_sha256,
        generated_at=TIME,
    )
    assert outcome.anchoring is not None
    source_map = build_source_map_document(
        outcome.anchoring,
        run_id=RUN_ID,
        source_manifest_sha256=source_receipt.payload_sha256,
        review_ir_sha256=compute_payload_sha256(review_ir),
        generated_at=TIME,
        export_report_payload=outcome.report.as_payload(),
    )
    export_report = build_export_report_document(outcome, run_id=RUN_ID, generated_at=TIME)
    revision_reader = build_revision_reader_capabilities_document(
        run_id=RUN_ID,
        generated_at=TIME,
    )

    for document, schema in (
        (capabilities, "BackendCapabilities"),
        (review_ir, "ReviewIR"),
        (source_map, "SourceMap"),
        (export_report, "ExportReport"),
        (revision_reader, "BackendCapabilities"),
    ):
        assert validate_contract(document).schema_name == schema
    assert outcome.report.review_ir_sha256 == compute_payload_sha256(review_ir)
    assert outcome.report.source_map_sha256 == compute_payload_sha256(source_map)
    assert outcome.report.backend_capabilities_sha256 == compute_payload_sha256(capabilities)
    bindings = bookmark_bindings_from_source_map(source_map)
    assert bindings
    assert len(bindings) == outcome.anchoring.coverage["exact"]
    assert {binding.unit_id for binding in bindings.values()} == {
        mapping.unit.unit_id for mapping in outcome.anchoring.mappings if mapping.status == "exact"
    }
    assert all(name.startswith("lwr_") for name in bindings)


def test_completed_run_manifest_binds_objects_backends_and_artifacts() -> None:
    contracts = _golden_contracts()
    document = build_run_manifest_document(
        contracts["SourceManifest"],
        objects=[
            contracts["ChangeSet"],
            contracts["ApprovalSet"],
            contracts["PatchPlan"],
            contracts["VerificationReport"],
        ],
        backend_capabilities=[contracts["BackendCapabilities"]],
        generated_at=TIME,
    )

    assert validate_contract(document).schema_name == "RunManifest"
    assert document["payload"]["status"] == "completed"
    assert document["payload"]["current_phase"] == "verified"
    assert document["payload"]["source_manifest"]["payload_sha256"] == (
        compute_payload_sha256(contracts["SourceManifest"])
    )
    assert document["payload"]["artifacts"]
