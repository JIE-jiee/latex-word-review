from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_models import export_report_commitment
from latex_word_review.workflow import (
    _SOURCE_FEATURE_METRICS,
    _CoreState,
    _validate_export_generation,
    _validate_source_feature_report_payload,
)
from latex_word_review.workflow_objects import (
    EXPORT_REPORT_INTERFACE_VERSION,
    PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
    SOURCE_MANIFEST_INTERFACE_VERSION,
    SOURCE_MAP_INTERFACE_VERSION,
)

LEGACY_PRODUCER = "export-report-builder-v1"
PREVIOUS_PRODUCER = PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION
CURRENT_PRODUCER = EXPORT_REPORT_INTERFACE_VERSION


def _feature(
    name: str,
    *,
    source_count: int,
    output_count: int | None,
    status: str = "preserved",
    diagnostic_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "feature": name,
        "status": status,
        "source_count": source_count,
        "output_count": output_count,
        "diagnostic_ids": diagnostic_ids or [],
    }


def _diagnostic(diagnostic_id: str) -> dict[str, object]:
    return {
        "diagnostic_id": diagnostic_id,
        "code": ErrorCode.EXPORT_DEGRADED.value,
        "phase": "inspect",
        "recoverable": True,
        "source_location": None,
    }


def _payload() -> dict[str, Any]:

    return {
        "status": "partial",
        "metrics": {
            "source": {
                "source_feature_inventory_version": 1,
                "source_tex_files": 1,
                "inline_math_instances": 1,
                "display_math_instances": 1,
                "skipped_dynamic_regions": 0,
                "image_instances": 1,
                "math_objects": 2,
                "table_instances": 1,
                "non_equivalent_table_instances": 0,
                "reference_instances": 2,
                "non_equivalent_reference_instances": 0,
                "label_instances": 1,
                "dynamic_label_instances": 0,
                "citation_instances": 1,
            },
            "output": {
                "image_instances": 1,
                "omml_objects": 2,
                "tables": 1,
                "ref_fields": 1,
                "pageref_fields": 1,
            },
        },
        "feature_results": [
            _feature("body_text", source_count=3, output_count=3),
            _feature("images", source_count=1, output_count=1),
            _feature("math", source_count=2, output_count=2),
            _feature("tables", source_count=1, output_count=1),
            _feature("references", source_count=2, output_count=2),
            _feature("labels", source_count=1, output_count=1),
            _feature(
                "citations",
                source_count=1,
                output_count=None,
                status="unsupported",
                diagnostic_ids=["diag_citations_unsupported"],
            ),
        ],
        "findings": [_diagnostic("diag_citations_unsupported")],
    }


def _result(payload: dict[str, Any], feature: str) -> dict[str, Any]:
    results = payload["feature_results"]
    assert isinstance(results, list)
    return next(
        item for item in results if isinstance(item, dict) and item.get("feature") == feature
    )


def _validate(
    payload: dict[str, Any],
    *,
    producer: object = CURRENT_PRODUCER,
    source_interface: object | None = None,
    expected_source: dict[str, int] | None = None,
    expected_output: dict[str, int] | None = None,
    expected_locations: dict[str, dict[str, object] | None] | None = None,
    expected_feature_output: dict[str, int | None] | None = None,
) -> None:
    _validate_source_feature_report_payload(
        payload,
        producer_interface_version=producer,
        source_manifest_interface_version=source_interface,
        expected_source_metrics=expected_source,
        expected_output_metrics=expected_output,
        expected_feature_locations=expected_locations,
        expected_feature_output_counts=expected_feature_output,
    )


def _committed_report_payload() -> dict[str, Any]:
    payload = deepcopy(_payload())
    payload.update(
        source_map_sha256="sha256:" + "a" * 64,
        review_docx={"sha256": "sha256:" + "b" * 64, "size_bytes": 1},
        validation={
            "ooxml": "pass",
            "openability": "pass",
            "relationships": "pass",
            "structure": "pass",
        },
    )
    return payload


def _validate_generation(
    report_payload: dict[str, Any],
    map_payload: dict[str, Any],
    *,
    manifest: str = SOURCE_MANIFEST_INTERFACE_VERSION,
    source_map: str = SOURCE_MAP_INTERFACE_VERSION,
    report: str = EXPORT_REPORT_INTERFACE_VERSION,
) -> None:
    core = cast(
        _CoreState,
        SimpleNamespace(source_manifest={"producer": {"interface_version": manifest}}),
    )
    _validate_export_generation(
        core,
        {"producer": {"interface_version": source_map}},
        {"producer": {"interface_version": report}},
        map_payload,
        report_payload,
    )


@pytest.mark.parametrize(
    "changed",
    ["body_text", "coverage", "status", "findings", "math", "validation", "artifact"],
)
def test_single_report_reseal_cannot_wash_committed_semantics(changed: str) -> None:
    original = _committed_report_payload()
    map_payload = {"export_report_commitment": export_report_commitment(original)}
    altered = deepcopy(original)
    if changed == "body_text":
        _result(altered, "body_text")["status"] = "degraded"
    elif changed == "coverage":
        _result(altered, "body_text")["output_count"] = 2
    elif changed == "status":
        altered["status"] = "success"
    elif changed == "findings":
        altered["findings"].append(_diagnostic("diag_report_only"))
    elif changed == "math":
        _result(altered, "math")["status"] = "degraded"
    elif changed == "validation":
        altered["validation"]["openability"] = "fail"
    else:
        altered["review_docx"]["sha256"] = "sha256:" + "c" * 64

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate_generation(altered, map_payload)


def test_current_generation_requires_exact_commitment_but_normalizes_map_hash() -> None:
    report_payload = _committed_report_payload()
    map_payload = {"export_report_commitment": export_report_commitment(report_payload)}
    _validate_generation(report_payload, map_payload)
    report_payload["source_map_sha256"] = "sha256:" + "d" * 64
    _validate_generation(report_payload, map_payload)

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate_generation(report_payload, {})


def test_previous_v2_generation_remains_compatible_with_exact_commitment() -> None:
    report_payload = _committed_report_payload()
    map_payload = {"export_report_commitment": export_report_commitment(report_payload)}

    _validate_generation(
        report_payload,
        map_payload,
        report=PREVIOUS_EXPORT_REPORT_INTERFACE_VERSION,
    )
    _validate(
        _payload(),
        producer=PREVIOUS_PRODUCER,
    )


@pytest.mark.parametrize("changed", ["manifest", "source_map", "report"])
def test_mixed_export_object_generations_are_rejected(changed: str) -> None:
    report_payload = _committed_report_payload()
    map_payload = {"export_report_commitment": export_report_commitment(report_payload)}
    versions = {
        "manifest": SOURCE_MANIFEST_INTERFACE_VERSION,
        "source_map": SOURCE_MAP_INTERFACE_VERSION,
        "report": EXPORT_REPORT_INTERFACE_VERSION,
    }
    versions[changed] = {
        "manifest": "source-manifest-builder-v1",
        "source_map": "source-map-builder-v1",
        "report": "export-report-builder-v1",
    }[changed]
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate_generation(report_payload, map_payload, **versions)


def test_true_all_v1_generation_remains_compatible_without_commitment() -> None:
    report_payload = _committed_report_payload()
    legacy = {
        "manifest": "source-manifest-builder-v1",
        "source_map": "source-map-builder-v1",
        "report": "export-report-builder-v1",
    }
    _validate_generation(report_payload, {}, **legacy)
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate_generation(
            report_payload,
            {"export_report_commitment": {"profile": {}, "payload_sha256": "bad"}},
            **legacy,
        )


def test_feature_report_validation_accepts_current_and_real_legacy_reports() -> None:
    _validate(_payload())
    legacy = {
        "metrics": {
            "source": {"paragraphs": 1, "image_instances": 1},
            "output": {"image_instances": 1},
        },
        "feature_results": [
            _feature("body_text", source_count=1, output_count=1),
            _feature("images", source_count=1, output_count=1),
        ],
    }
    _validate(
        legacy,
        producer=LEGACY_PRODUCER,
        source_interface="source-manifest-builder-v1",
    )


def test_current_producer_and_v2_markers_cannot_be_downgraded_to_legacy() -> None:
    legacy_shape = {"metrics": {"source": {"paragraphs": 1}, "output": {}}}
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(legacy_shape)

    resealed_downgrade = _payload()
    metrics = resealed_downgrade["metrics"]
    assert isinstance(metrics, dict)
    source = metrics["source"]
    assert isinstance(source, dict)
    del source["source_feature_inventory_version"]
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(resealed_downgrade, producer=LEGACY_PRODUCER)


def test_unknown_export_report_producer_is_rejected() -> None:
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(
            {"metrics": {"source": {"paragraphs": 1}, "output": {}}},
            producer="export-report-builder-v0",
        )


def test_feature_report_validation_rejects_incomplete_or_inconsistent_evidence() -> None:
    missing = _payload()
    missing["feature_results"] = missing["feature_results"][:-1]
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(missing)

    source_mismatch = _payload()
    _result(source_mismatch, "tables")["source_count"] = 0
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(source_mismatch)

    output_mismatch = _payload()
    _result(output_mismatch, "images")["output_count"] = 0
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(output_mismatch)


def test_feature_report_validation_blocks_failed_or_false_preserved_results() -> None:
    failed = _payload()
    _result(failed, "tables")["status"] = "failed"
    with pytest.raises(ContractError, match=ErrorCode.EXPORT_SILENT_LOSS.value):
        _validate(failed)

    false_preserved = _payload()
    output = false_preserved["metrics"]["output"]
    assert isinstance(output, dict)
    output["omml_objects"] = 1
    _result(false_preserved, "math")["output_count"] = 1
    with pytest.raises(ContractError, match=ErrorCode.EXPORT_SILENT_LOSS.value):
        _validate(false_preserved)


def test_feature_report_validation_requires_bound_diagnostic_for_degradation() -> None:
    payload = _payload()
    tables = _result(payload, "tables")
    tables["status"] = "degraded"
    tables["diagnostic_ids"] = ["diag_table_degraded"]

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(payload)

    bound = deepcopy(payload)
    bound_findings = bound["findings"]
    assert isinstance(bound_findings, list)
    bound_findings.append(_diagnostic("diag_table_degraded"))
    _validate(bound)


def test_reference_output_gap_is_always_silent_loss_even_with_a_diagnostic() -> None:
    payload = _payload()
    references = _result(payload, "references")
    references["status"] = "degraded"
    references["diagnostic_ids"] = ["diag_reference_fallback"]
    output = payload["metrics"]["output"]
    assert isinstance(output, dict)
    output["ref_fields"] = 0
    references["output_count"] = 1
    findings = payload["findings"]
    assert isinstance(findings, list)
    findings.append(_diagnostic("diag_reference_fallback"))

    with pytest.raises(ContractError, match=ErrorCode.EXPORT_SILENT_LOSS.value):
        _validate(payload)


@pytest.mark.parametrize(
    "feature",
    ["images", "math", "tables", "references", "labels"],
)
def test_feature_report_validation_blocks_every_unsupported_critical_feature(
    feature: str,
) -> None:
    payload = _payload()
    result = _result(payload, feature)
    diagnostic_id = f"diag_{feature}_unsupported"
    result["status"] = "unsupported"
    result["diagnostic_ids"] = [diagnostic_id]
    findings = payload["findings"]
    assert isinstance(findings, list)
    findings.append(_diagnostic(diagnostic_id))

    with pytest.raises(ContractError, match=ErrorCode.EXPORT_SILENT_LOSS.value):
        _validate(payload)


def test_feature_report_validation_requires_recoverable_bound_finding() -> None:
    payload = _payload()
    findings = payload["findings"]
    assert isinstance(findings, list)
    citation_finding = findings[0]
    assert isinstance(citation_finding, dict)
    citation_finding["recoverable"] = False

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(payload)


def test_nonpreserved_feature_requires_partial_export_status() -> None:
    payload = _payload()
    payload["status"] = "success"

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(payload)


def test_zero_count_nonpreserved_feature_still_requires_diagnostic_and_partial_status() -> None:
    payload = _payload()
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    source = metrics["source"]
    assert isinstance(source, dict)
    source["citation_instances"] = 0
    citations = _result(payload, "citations")
    citations["source_count"] = 0
    citations["diagnostic_ids"] = []

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(payload)

    citations["diagnostic_ids"] = ["diag_zero_citations"]
    findings = payload["findings"]
    assert isinstance(findings, list)
    findings.append(_diagnostic("diag_zero_citations"))
    payload["status"] = "success"
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(payload)

    payload["status"] = "partial"
    _validate(payload)


def test_non_equivalent_source_construct_cannot_be_resealed_as_preserved() -> None:
    payload = _payload()
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    source = metrics["source"]
    output = metrics["output"]
    assert isinstance(source, dict)
    assert isinstance(output, dict)
    source["table_instances"] = 0
    source["non_equivalent_table_instances"] = 1
    output["tables"] = 0
    tables = _result(payload, "tables")
    tables["source_count"] = 0
    tables["output_count"] = 0
    tables["status"] = "preserved"

    with pytest.raises(ContractError, match=ErrorCode.EXPORT_SILENT_LOSS.value):
        _validate(payload)


def test_unsupported_citations_are_allowed_with_partial_recoverable_evidence() -> None:
    payload = _payload()

    _validate(payload)


def _independent_metrics(payload: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    source = metrics["source"]
    output = metrics["output"]
    assert isinstance(source, dict)
    assert isinstance(output, dict)
    return deepcopy(source), deepcopy(output)


def test_current_report_accepts_independently_recomputed_metrics() -> None:
    payload = _payload()
    expected_source, expected_output = _independent_metrics(payload)

    _validate(
        payload,
        source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
        expected_source=expected_source,
        expected_output=expected_output,
        expected_locations={feature: None for feature in _SOURCE_FEATURE_METRICS},
    )


def test_resealed_full_downgrade_cannot_override_current_source_manifest_generation() -> None:
    trusted = _payload()
    expected_source, expected_output = _independent_metrics(trusted)
    downgraded = {
        "metrics": {
            "source": {"paragraphs": 3, "image_instances": 1},
            "output": {"image_instances": 1},
        },
        "feature_results": [
            _feature("body_text", source_count=3, output_count=3),
            _feature("images", source_count=1, output_count=1),
        ],
    }

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(
            downgraded,
            producer=LEGACY_PRODUCER,
            source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
            expected_source=expected_source,
            expected_output=expected_output,
        )


def test_resealed_zeroed_inventory_cannot_override_independent_snapshot_and_docx_counts() -> None:
    trusted = _payload()
    expected_source, expected_output = _independent_metrics(trusted)
    zeroed = deepcopy(trusted)
    metrics = zeroed["metrics"]
    assert isinstance(metrics, dict)
    source = metrics["source"]
    output = metrics["output"]
    assert isinstance(source, dict)
    assert isinstance(output, dict)
    for metric, value in list(source.items()):
        if metric != "source_feature_inventory_version" and isinstance(value, int):
            source[metric] = 0
    for metric, value in list(output.items()):
        if isinstance(value, int):
            output[metric] = 0
    results = zeroed["feature_results"]
    assert isinstance(results, list)
    for result in results:
        assert isinstance(result, dict)
        if result["feature"] == "body_text":
            continue
        result["source_count"] = 0
        result["output_count"] = None if result["feature"] == "citations" else 0
        result["status"] = "preserved"
        result["diagnostic_ids"] = []
    zeroed["status"] = "success"
    zeroed["findings"] = []

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(
            zeroed,
            source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
            expected_source=expected_source,
            expected_output=expected_output,
        )


def test_resealed_diagnostic_cannot_be_bound_to_the_wrong_feature_evidence() -> None:
    payload = _payload()
    expected_source, expected_output = _independent_metrics(payload)
    table = _result(payload, "tables")
    table["status"] = "degraded"
    table["diagnostic_ids"] = ["diag_citations_unsupported"]
    locations: dict[str, dict[str, object] | None] = {
        feature: None for feature in _SOURCE_FEATURE_METRICS
    }
    locations["tables"] = {"path": "main.tex", "start_byte": 100}

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(
            payload,
            source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
            expected_source=expected_source,
            expected_output=expected_output,
            expected_locations=locations,
        )


def test_resealed_label_preservation_cannot_invent_missing_final_docx_bookmark() -> None:
    payload = _payload()
    expected_source, expected_output = _independent_metrics(payload)

    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(
            payload,
            source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
            expected_source=expected_source,
            expected_output=expected_output,
            expected_feature_output={"labels": 0},
        )


def test_label_output_gap_is_allowed_only_as_explicit_degradation() -> None:
    payload = _payload()
    expected_source, expected_output = _independent_metrics(payload)
    labels = _result(payload, "labels")
    labels["output_count"] = 0
    labels["status"] = "degraded"
    labels["diagnostic_ids"] = ["diag_labels_degraded"]
    findings = payload["findings"]
    assert isinstance(findings, list)
    findings.append(_diagnostic("diag_labels_degraded"))

    _validate(
        payload,
        source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
        expected_source=expected_source,
        expected_output=expected_output,
        expected_feature_output={"labels": 0},
    )

    labels["status"] = "preserved"
    labels["diagnostic_ids"] = []
    payload["findings"] = [
        finding
        for finding in findings
        if isinstance(finding, dict) and finding.get("diagnostic_id") != "diag_labels_degraded"
    ]
    with pytest.raises(ContractError, match=ErrorCode.EXPORT_SILENT_LOSS.value):
        _validate(
            payload,
            source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
            expected_source=expected_source,
            expected_output=expected_output,
            expected_feature_output={"labels": 0},
        )


def test_resealed_citations_cannot_be_promoted_to_preserved_with_real_counts() -> None:
    payload = _payload()
    expected_source, expected_output = _independent_metrics(payload)
    citations = _result(payload, "citations")
    citations["status"] = "preserved"
    citations["diagnostic_ids"] = []
    payload["findings"] = []
    payload["status"] = "success"

    with pytest.raises(ContractError, match=ErrorCode.EXPORT_SILENT_LOSS.value):
        _validate(
            payload,
            source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
            expected_source=expected_source,
            expected_output=expected_output,
            expected_feature_output={"labels": 1},
        )


def test_dynamic_regions_require_explicit_partial_degradation_and_bound_diagnostic() -> None:
    payload = _payload()
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    source = metrics["source"]
    assert isinstance(source, dict)
    source["skipped_dynamic_regions"] = 1
    dynamic_id = "diag_dynamic_regions_degraded"
    results = payload["feature_results"]
    findings = payload["findings"]
    assert isinstance(results, list)
    assert isinstance(findings, list)
    results.append(
        _feature(
            "dynamic_regions",
            source_count=1,
            output_count=None,
            status="degraded",
            diagnostic_ids=[dynamic_id],
        )
    )
    findings.append(_diagnostic(dynamic_id))
    expected_source, expected_output = _independent_metrics(payload)

    _validate(
        payload,
        source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
        expected_source=expected_source,
        expected_output=expected_output,
        expected_feature_output={"labels": 1},
    )

    stripped = deepcopy(payload)
    stripped["feature_results"] = [
        item
        for item in stripped["feature_results"]
        if isinstance(item, dict) and item.get("feature") != "dynamic_regions"
    ]
    stripped["findings"] = [
        item
        for item in stripped["findings"]
        if isinstance(item, dict) and item.get("diagnostic_id") != dynamic_id
    ]
    stripped["status"] = "success"
    with pytest.raises(ContractError, match=ErrorCode.HASH_SOURCE_MISMATCH.value):
        _validate(
            stripped,
            source_interface=SOURCE_MANIFEST_INTERFACE_VERSION,
            expected_source=expected_source,
            expected_output=expected_output,
            expected_feature_output={"labels": 1},
        )
