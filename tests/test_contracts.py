"""Golden, negative and tamper tests for the v1alpha public contracts."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from latex_word_review import (
    SCHEMA_VERSION,
    ContractError,
    ErrorCode,
    ExitCode,
    available_schema_names,
    canonical_json_text,
    compute_payload_sha256,
    compute_source_tree_sha256,
    derive_artifact_id,
    derive_source_manifest_id,
    load_contract_json,
    load_schema,
    make_envelope,
    new_run_id,
    seal_envelope,
    sha256_bytes,
    sha256_canonical,
    stable_id,
    validate_contract,
    verify_payload_binding,
)
from latex_word_review.schema_catalog import read_schema_resource, schema_catalog

RUN_ID = new_run_id(timestamp_ms=1_768_464_000_000, random_bits=7)
GENERATED_AT = "2026-01-15T08:00:00+08:00"


def _hash(label: str) -> str:
    return sha256_bytes(label.encode("utf-8"))


def _producer() -> dict[str, Any]:
    return {
        "name": "latex-word-review",
        "version": "0.1.0b1",
        "interface_version": "contracts-v1alpha1",
        "distribution": "test-wheel",
        "executable_sha256": None,
        "configuration_sha256": _hash("configuration"),
    }


def _profile(name: str) -> dict[str, Any]:
    return {"name": name, "version": "1", "configuration_sha256": _hash(name)}


def _artifact(
    label: str,
    *,
    role: str,
    media_type: str = "application/octet-stream",
    confidentiality: str = "public_fixture",
) -> dict[str, Any]:
    digest = _hash(label)
    return {
        "artifact_id": derive_artifact_id(digest),
        "path": f"artifacts/{label}.bin",
        "path_base": "run_root",
        "role": role,
        "media_type": media_type,
        "size_bytes": len(label.encode("utf-8")),
        "sha256": digest,
        "immutable": True,
        "confidentiality": confidentiality,
    }


def _location(*, start: int = 0, end: int = 5) -> dict[str, Any]:
    return {
        "path": "main.tex",
        "start_byte": start,
        "end_byte": end,
        "slice_sha256": _hash("" if start == end else "Hello"),
        "encoding": "utf-8",
        "newline": "lf",
        "start_line": 1,
        "end_line": 1,
        "start_column": start + 1,
        "end_column": end + 1,
    }


def _envelope(schema_name: str, object_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return make_envelope(
        schema_name=schema_name,
        object_id=object_id,
        run_id=None if schema_name == "BackendCapabilities" else RUN_ID,
        generated_at=GENERATED_AT,
        producer=_producer(),
        payload=payload,
    )


def _golden_contracts() -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    source_file = {
        "path": "main.tex",
        "role": "tex",
        "media_type": "text/x-tex",
        "size_bytes": 6,
        "sha256": _hash("Hello\n"),
        "encoding": "utf-8",
        "newline": "lf",
    }
    source_tree = compute_source_tree_sha256([source_file])
    source_id = derive_source_manifest_id(source_tree)
    source_payload = {
        "source_manifest_id": source_id,
        "main_document": "main.tex",
        "snapshot_artifact": _artifact("snapshot", role="source_snapshot"),
        "source_tree_sha256": source_tree,
        "files": [source_file],
        "dependency_edges": [],
        "engine_hints": [],
        "external_references": [],
        "discovery_profile": _profile("source-discovery"),
        "immutability": {
            "snapshot_read_only": True,
            "source_origin_sha256": _hash("source-origin"),
        },
    }
    source = _envelope("SourceManifest", source_id, source_payload)

    capabilities = _envelope(
        "BackendCapabilities",
        stable_id("cap_", ["canonical-reader", "1"]),
        {
            "backend_id": "canonical-ooxml-reader",
            "backend_role": "revision_reader",
            "tool": _producer(),
            "probe_platform": {
                "os": "test",
                "architecture": "x86_64",
                "python_version": "3.12",
                "external_tools": {},
            },
            "operations": ["read_revisions"],
            "features": {
                "raw_insert": {
                    "support": "full",
                    "representations": ["ooxml"],
                    "preserves_metadata": ["author", "timestamp"],
                    "diagnostic_codes": [],
                    "evidence_sha256": _hash("capability-evidence"),
                }
            },
            "determinism": "verified",
            "tested_contracts": [
                {
                    "fixture": "synthetic-minimal",
                    "result": "pass",
                    "evidence_sha256": _hash("contract-evidence"),
                }
            ],
            "limitations": [],
            "security_requirements": ["read_only"],
            "probe_evidence": [],
        },
    )

    unit_id = stable_id("unit_", [source_tree, "main.tex", 0, 5])
    review_ir = _envelope(
        "ReviewIR",
        stable_id("ir_", [source_tree, "review-ir"]),
        {
            "source_manifest_sha256": compute_payload_sha256(source),
            "source_tree_sha256": source_tree,
            "normalization_profile": _profile("review-text"),
            "document_language_hints": ["en"],
            "units": [
                {
                    "unit_id": unit_id,
                    "kind": "paragraph",
                    "ordinal": 0,
                    "parent_unit_id": None,
                    "source_location": _location(),
                    "normalized_text": "Hello",
                    "normalized_text_sha256": _hash("Hello"),
                    "semantic_role": "body",
                    "risk_class": "plain_text_low",
                    "neighbor_unit_ids": [],
                    "diagnostic_ids": [],
                }
            ],
            "relations": [],
            "diagnostics": [],
        },
    )

    review_docx_sha = _hash("review.docx")
    overlay_manifest = {
        **_artifact(
            "image-overlay-manifest",
            role="image_overlay_manifest",
            media_type="application/json",
        ),
        "path": "artifacts/review.docx.image-overlay/image-overlay-manifest.json",
    }
    image_overlay = {
        "status": "ready",
        "manifest": overlay_manifest,
        "original_source_tree_sha256": source_tree,
        "derived_source_tree_sha256": _hash("derived-source-tree"),
        "source_image_instances": 0,
        "materialized_pdf_instances": 0,
        "passthrough_raster_instances": 0,
    }
    source_map = _envelope(
        "SourceMap",
        stable_id("map_", [source_tree, review_docx_sha]),
        {
            "source_manifest_sha256": compute_payload_sha256(source),
            "review_ir_sha256": compute_payload_sha256(review_ir),
            "review_docx_sha256": review_docx_sha,
            "anchor_profile": _profile("bookmark"),
            "image_overlay": image_overlay,
            "mappings": [
                {
                    "unit_id": unit_id,
                    "source_location": _location(),
                    "docx_anchor": {
                        "part_uri": "word/document.xml",
                        "kind": "bookmark",
                        "name": "lwr_unit_1",
                        "paragraph_id": None,
                        "object_id": None,
                    },
                    "source_fingerprint": {
                        "slice_sha256": _hash("Hello"),
                        "normalized_text_sha256": _hash("Hello"),
                        "neighbor_sha256": _hash("neighbors"),
                    },
                    "text_provenance": {
                        "profile": _profile("normalized-text-to-utf8"),
                        "review_length": 5,
                        "segments": [
                            {
                                "review_start": 0,
                                "review_end": 5,
                                "source_start_byte": 0,
                                "source_end_byte": 5,
                                "transformation": "identity",
                                "auto_patchable": True,
                            }
                        ],
                    },
                    "mapping_method": "exact_source_span",
                    "confidence": 1.0,
                    "status": "exact",
                }
            ],
            "coverage": {"total": 1, "exact": 1, "degraded": 0, "unmapped": 0, "conflict": 0},
            "diagnostics": [],
        },
    )

    export_report = _envelope(
        "ExportReport",
        stable_id("export_", [review_docx_sha, "report"]),
        {
            "status": "success",
            "source_manifest_sha256": compute_payload_sha256(source),
            "backend_capabilities_sha256": compute_payload_sha256(capabilities),
            "review_ir_sha256": compute_payload_sha256(review_ir),
            "source_map_sha256": compute_payload_sha256(source_map),
            "review_docx": {
                **_artifact(
                    "review-docx",
                    role="review_docx",
                    media_type=(
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ),
                ),
                "sha256": review_docx_sha,
                "artifact_id": derive_artifact_id(review_docx_sha),
            },
            "image_overlay": image_overlay,
            "metrics": {"source": {"paragraphs": 1}, "output": {"paragraphs": 1}},
            "feature_results": [
                {
                    "feature": "body_text",
                    "status": "preserved",
                    "source_count": 1,
                    "output_count": 1,
                    "diagnostic_ids": [],
                }
            ],
            "findings": [],
            "validation": {
                "ooxml": "pass",
                "openability": "pass",
                "relationships": "pass",
                "structure": "pass",
            },
        },
    )

    returned = _artifact(
        "returned-original",
        role="returned_original",
        media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        confidentiality="local_private",
    )
    raw_id = stable_id("rev_", [returned["sha256"], "word/document.xml", 0])
    raw_payload = {
        "raw_event_id": raw_id,
        "returned_docx_sha256": returned["sha256"],
        "part_uri": "word/document.xml",
        "part_sha256": _hash("document.xml"),
        "kind": "insert",
        "native_id": "101",
        "native_kind": None,
        "author": "Synthetic Reviewer",
        "timestamp": "2026-01-16T09:00:00+08:00",
        "document_order": 0,
        "content": {
            "text": "carefully ",
            "deleted_text": None,
            "comment_text": None,
            "format_before": None,
            "format_after": None,
        },
        "range": None,
        "evidence": {
            "node_ordinal": 0,
            "fragment_sha256": _hash("revision-fragment"),
            "artifact": None,
        },
        "diagnostics": [],
    }
    raw_event = _envelope("RawRevisionEvent", raw_id, raw_payload)

    change_id = stable_id("chg_", [[raw_id], "combine-v1"])
    change = {
        "change_id": change_id,
        "kind": "insertion",
        "native_kind": None,
        "raw_event_ids": [raw_id],
        "author": "Synthetic Reviewer",
        "authors": ["Synthetic Reviewer"],
        "timestamp": "2026-01-16T09:00:00+08:00",
        "timestamps": ["2026-01-16T09:00:00+08:00"],
        "before": "",
        "after": "carefully ",
        "comment": None,
        "unit_id": unit_id,
        "source_location": _location(start=0, end=0),
        "resolution": {
            "status": "exact",
            "method": "bookmark",
            "confidence": 1.0,
            "candidates": [],
        },
        "safety_class": "plain_text_candidate",
        "initial_decision": "pending",
        "change_fingerprint": _hash("change-fingerprint"),
    }
    changeset = _envelope(
        "ChangeSet",
        stable_id("changes_", [returned["sha256"], source_tree]),
        {
            "returned_original": returned,
            "source_manifest_sha256": compute_payload_sha256(source),
            "source_map_sha256": compute_payload_sha256(source_map),
            "revision_reader_capabilities_sha256": compute_payload_sha256(capabilities),
            "ingest_profile": _profile("word-review-ingest"),
            "raw_events": [raw_payload],
            "changes": [change],
            "counts": {"raw_events": 1, "changes": 1, "unmatched": 0, "conflicts": 0},
            "diagnostics": [],
        },
    )

    approval_id = stable_id("apr_", [compute_payload_sha256(changeset), "accepted"])
    approval = _envelope(
        "ApprovalSet",
        approval_id,
        {
            "approval_set_id": approval_id,
            "revision": 1,
            "supersedes_payload_sha256": None,
            "changeset_sha256": compute_payload_sha256(changeset),
            "source_manifest_sha256": compute_payload_sha256(source),
            "status": "final",
            "decided_by": {"id": "paper-author", "display_name": "Paper Author"},
            "decisions": [
                {
                    "change_id": change_id,
                    "change_fingerprint": change["change_fingerprint"],
                    "decision": "accepted",
                    "final_text": None,
                    "reason": "Accepted synthetic wording",
                    "decided_at": "2026-01-16T09:30:00+08:00",
                    "risk_acknowledgement": None,
                    "decision_source": "local_ui",
                }
            ],
            "undecided_change_ids": [],
            "decision_summary": {
                "accepted": 1,
                "accepted_with_edit": 0,
                "rejected": 0,
                "manual": 0,
                "conflict": 0,
                "pending": 0,
            },
            "audit": {"bulk_operations": [], "previous_payload_sha256": None},
        },
    )

    policy_sha = _hash("v0.1-plain-text-policy")
    operation_id = stable_id("op_", [change_id, "main.tex", 0, "carefully "])
    plan_id = stable_id("plan_", [compute_payload_sha256(approval), operation_id])
    patch_plan = _envelope(
        "PatchPlan",
        plan_id,
        {
            "patch_plan_id": plan_id,
            "status": "ready",
            "mode": "dry_run",
            "source_manifest_sha256": compute_payload_sha256(source),
            "source_tree_sha256": source_tree,
            "changeset_sha256": compute_payload_sha256(changeset),
            "approval_set_sha256": compute_payload_sha256(approval),
            "policy_sha256": policy_sha,
            "planner": _producer(),
            "operations": [
                {
                    "operation_id": operation_id,
                    "change_id": change_id,
                    "kind": "insert",
                    "target": _location(start=0, end=0),
                    "target_file_sha256": source_file["sha256"],
                    "expected_bytes_sha256": _hash(""),
                    "before_text": "",
                    "replacement_text": "carefully ",
                    "encoding": "utf-8",
                    "newline_policy": "preserve",
                    "unit_id": unit_id,
                    "unit_source_slice_sha256": _hash("Hello"),
                    "context_fingerprint": _hash("context"),
                    "confidence": 1.0,
                    "safety_class": "plain_text_candidate",
                    "safety_checks": {
                        "approved": True,
                        "exact_unit": True,
                        "slice_hash_match": True,
                        "plain_text_only": True,
                        "no_overlap": True,
                        "no_structural_boundary": True,
                    },
                    "diff_hunk_sha256": _hash("diff-hunk"),
                }
            ],
            "accepted_but_blocked": [],
            "excluded_changes": [],
            "unified_diff": None,
            "summary": {"approved": 1, "planned": 1, "blocked": 0, "excluded": 0, "overlaps": 0},
            "preconditions": [
                {"kind": "source_tree_sha256_equals", "expected": source_tree},
                {
                    "kind": "changeset_payload_sha256_equals",
                    "expected": compute_payload_sha256(changeset),
                },
                {
                    "kind": "approval_payload_sha256_equals",
                    "expected": compute_payload_sha256(approval),
                },
                {"kind": "policy_sha256_equals", "expected": policy_sha},
            ],
        },
    )

    verification = _envelope(
        "VerificationReport",
        stable_id("verify_", [compute_payload_sha256(patch_plan), "pass"]),
        {
            "status": "pass",
            "patch_plan_sha256": compute_payload_sha256(patch_plan),
            "source_manifest_sha256": compute_payload_sha256(source),
            "revised_source_manifest_sha256": _hash("revised-source-manifest"),
            "original_source_pre_sha256": _hash("original-source"),
            "original_source_post_sha256": _hash("original-source"),
            "returned_original_pre_sha256": returned["sha256"],
            "returned_original_post_sha256": returned["sha256"],
            "actual_diff": None,
            "patch_reconciliation": {
                "planned_count": 1,
                "applied_count": 1,
                "missing_count": 0,
                "extra_count": 0,
                "duplicate_count": 0,
                "unapproved_modification_count": 0,
            },
            "compile": {
                "status": "pass",
                "tool": _producer(),
                "exit_code": 0,
                "timed_out": False,
                "log": None,
            },
            "references": {"status": "pass"},
            "structure": {"status": "pass"},
            "latexdiff": {"status": "not_run", "tex": None, "pdf": None},
            "deliverables": [],
            "diagnostics": [],
        },
    )

    run_manifest = _envelope(
        "RunManifest",
        stable_id("runm_", [RUN_ID, 1]),
        {
            "manifest_revision": 1,
            "previous_manifest_payload_sha256": None,
            "status": "active",
            "current_phase": "snapshotted",
            "policy_profile": {
                "name": "v0.1-safe",
                "version": "1",
                "payload_sha256": policy_sha,
            },
            "source_manifest": {
                "schema_name": "SourceManifest",
                "object_id": source["object_id"],
                "payload_sha256": source["integrity"]["payload_sha256"],
                "document_sha256": source["integrity"]["document_sha256"],
            },
            "selected_backends": [
                {
                    "backend_id": "canonical-ooxml-reader",
                    "role": "revision_reader",
                    "capabilities_sha256": compute_payload_sha256(capabilities),
                }
            ],
            "phase_events": [
                {
                    "phase": "snapshotted",
                    "status": "completed",
                    "started_at": GENERATED_AT,
                    "completed_at": GENERATED_AT,
                    "input_sha256s": [],
                    "output_sha256s": [compute_payload_sha256(source)],
                    "diagnostic_ids": [],
                }
            ],
            "artifacts": [],
            "object_bindings": [],
            "source_origin_pre_sha256": _hash("source-origin"),
            "source_origin_post_sha256": None,
            "diagnostics": [],
        },
    )

    audit_bundle = _envelope(
        "AuditBundle",
        stable_id("bundle_", [compute_payload_sha256(verification), "bundle"]),
        {
            "bundle_format_version": "1.0.0-alpha.1",
            "run_manifest_sha256": compute_payload_sha256(run_manifest),
            "verification_report_sha256": compute_payload_sha256(verification),
            "content_classification": "public_fixture",
            "entries": [],
            "excluded_entries": [],
            "privacy_scan": {
                "status": "pass",
                "rules_version": "1",
                "report": _artifact("privacy-report", role="privacy_report"),
            },
            "manifest_sha256": sha256_canonical([]),
            "reproducibility": {
                "entry_order": "lexicographic_path",
                "timestamp_policy": "fixed-1980-01-01",
                "tool": _producer(),
            },
        },
    )

    contracts["RunManifest"] = run_manifest
    contracts["SourceManifest"] = source
    contracts["BackendCapabilities"] = capabilities
    contracts["ReviewIR"] = review_ir
    contracts["SourceMap"] = source_map
    contracts["ExportReport"] = export_report
    contracts["RawRevisionEvent"] = raw_event
    contracts["ChangeSet"] = changeset
    contracts["ApprovalSet"] = approval
    contracts["PatchPlan"] = patch_plan
    contracts["VerificationReport"] = verification
    contracts["AuditBundle"] = audit_bundle
    return contracts


@pytest.fixture(scope="module")
def golden_contracts() -> dict[str, dict[str, Any]]:
    return _golden_contracts()


def test_all_twelve_golden_contracts_validate(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    assert tuple(golden_contracts) == available_schema_names()
    for schema_name, document in golden_contracts.items():
        receipt = validate_contract(document)
        assert receipt.schema_name == schema_name
        assert receipt.schema_version == SCHEMA_VERSION
        assert receipt.payload_sha256 == compute_payload_sha256(document)


def test_packaged_schemas_are_versioned_and_fail_closed() -> None:
    for schema_name in available_schema_names():
        schema = load_schema(schema_name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_name"] == {"const": schema_name}
        assert "extensions" in schema["properties"]


def test_authoritative_schema_catalog_binds_the_exact_runtime_set() -> None:
    catalog = schema_catalog()

    assert catalog.schema_version == SCHEMA_VERSION
    assert tuple(entry.name for entry in catalog.objects) == available_schema_names()
    assert catalog.common.filename == "common.schema.json"
    for entry in (catalog.common, *catalog.objects):
        assert hashlib.sha256(read_schema_resource(entry.filename)).hexdigest() == entry.sha256


def test_rfc8785_golden_vectors() -> None:
    path = Path(__file__).parent / "golden/contracts/v1alpha/canonical-vectors.json"
    vectors = json.loads(path.read_text(encoding="utf-8"))
    for vector in vectors:
        assert canonical_json_text(vector["value"]) == vector["canonical"]
        assert sha256_canonical(vector["value"]) == vector["sha256"]


def test_stable_ids_and_uuidv7_are_deterministic() -> None:
    assert stable_id("chg_", ["same", 1]) == stable_id("chg_", ["same", 1])
    assert len(stable_id("chg_", ["same", 1]).removeprefix("chg_")) == 32
    assert RUN_ID.startswith("run_")
    assert RUN_ID.split("-")[2].startswith("7")


def test_missing_required_field_is_rejected(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    candidate = copy.deepcopy(golden_contracts["ReviewIR"])
    del candidate["producer"]
    candidate = seal_envelope(candidate)
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert caught.value.exit_code is ExitCode.USAGE_OR_SCHEMA


def test_unknown_security_field_is_rejected_even_when_resealed(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    candidate = copy.deepcopy(golden_contracts["ApprovalSet"])
    candidate["payload"]["allow_unapproved_apply"] = True
    candidate = seal_envelope(candidate)
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD


def test_payload_tamper_is_bound_to_changeset_error_code(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    candidate = copy.deepcopy(golden_contracts["ChangeSet"])
    candidate["payload"]["changes"][0]["after"] = "tampered"
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is ErrorCode.HASH_CHANGESET_MISMATCH


def test_envelope_metadata_tamper_is_detected(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    candidate = copy.deepcopy(golden_contracts["SourceMap"])
    candidate["generated_at"] = "2026-01-15T09:00:00+08:00"
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is ErrorCode.HASH_INTEGRITY_MISMATCH


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.0.0", ErrorCode.SCHEMA_MAJOR_UNSUPPORTED),
        ("1.0.0-alpha.2", ErrorCode.SCHEMA_INVALID),
    ],
)
def test_unknown_schema_versions_fail_closed(
    golden_contracts: dict[str, dict[str, Any]],
    version: str,
    expected: ErrorCode,
) -> None:
    candidate = copy.deepcopy(golden_contracts["RunManifest"])
    candidate["schema_version"] = version
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is expected


def test_unknown_native_revision_kind_must_normalize_to_unknown(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    candidate = copy.deepcopy(golden_contracts["RawRevisionEvent"])
    candidate["payload"]["kind"] = "macro_execution"
    candidate["payload"]["native_kind"] = "w:macroExecution"
    candidate = seal_envelope(candidate)
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD


def test_unknown_patch_operation_kind_is_never_guessed(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    candidate = copy.deepcopy(golden_contracts["PatchPlan"])
    candidate["payload"]["operations"][0]["kind"] = "rewrite_document"
    candidate = seal_envelope(candidate)
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:/private/main.tex", ErrorCode.PATH_ABSOLUTE),
        ("../private/main.tex", ErrorCode.PATH_TRAVERSAL),
    ],
)
def test_unsafe_paths_are_rejected_with_stable_codes(
    golden_contracts: dict[str, dict[str, Any]],
    path: str,
    expected: ErrorCode,
) -> None:
    candidate = copy.deepcopy(golden_contracts["SourceManifest"])
    candidate["payload"]["main_document"] = path
    candidate = seal_envelope(candidate)
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is expected


def test_payload_binding_accepts_exact_upstream_and_rejects_substitution(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    verify_payload_binding(
        golden_contracts["SourceMap"],
        "source_manifest_sha256",
        golden_contracts["SourceManifest"],
    )
    substituted = copy.deepcopy(golden_contracts["SourceMap"])
    substituted["payload"]["source_manifest_sha256"] = _hash("other-source")
    substituted = seal_envelope(substituted)
    with pytest.raises(ContractError) as caught:
        verify_payload_binding(
            substituted,
            "source_manifest_sha256",
            golden_contracts["SourceManifest"],
        )
    assert caught.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected() -> None:
    with pytest.raises(ContractError) as duplicate:
        load_contract_json('{"schema_name":"A","schema_name":"B"}')
    assert duplicate.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(ContractError):
        load_contract_json('{"value":NaN}')


def test_source_manifest_file_record_tamper_cannot_be_resealed_as_valid(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    candidate = copy.deepcopy(golden_contracts["SourceManifest"])
    candidate["payload"]["files"][0]["sha256"] = _hash("substituted-file")
    candidate = seal_envelope(candidate)
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_final_approval_cannot_hide_undecided_changes(
    golden_contracts: dict[str, dict[str, Any]],
) -> None:
    candidate = copy.deepcopy(golden_contracts["ApprovalSet"])
    candidate["payload"]["undecided_change_ids"] = [stable_id("chg_", ["pending"])]
    candidate["payload"]["decision_summary"]["pending"] = 1
    candidate = seal_envelope(candidate)
    with pytest.raises(ContractError) as caught:
        validate_contract(candidate)
    assert caught.value.code is ErrorCode.APPROVAL_NOT_FINAL
