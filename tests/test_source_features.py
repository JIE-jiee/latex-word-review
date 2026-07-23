"""Conservative source feature inventory and reconciliation contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from latex_word_review import source_features as source_features_module
from latex_word_review.backends.base import BackendCapabilities, BackendResult
from latex_word_review.backends.tex2word import (
    SUPPORTED_TEX2WORD_VERSION,
    TEX2WORD_INTERFACE_VERSION,
)
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.export_models import ExportValidation
from latex_word_review.inspection import DocxInspection
from latex_word_review.source_features import (
    SourceFeatureInventory,
    reconcile_source_features,
    scan_source_features,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures/e0-minimal-paper/source"


def _locked_capabilities() -> BackendCapabilities:
    return BackendCapabilities(
        backend_id="tex2word-public-api",
        tool_name="tex2word",
        tool_version=SUPPORTED_TEX2WORD_VERSION,
        interface_version=TEX2WORD_INTERFACE_VERSION,
        distribution="tex2word",
        configuration_sha256="sha256:" + "a" * 64,
        features=(),
        determinism="not_verified",
        tested_contracts=(),
        limitations=(),
        security_requirements=(),
    )


def _backend_result(**native_report: object) -> BackendResult:
    report: dict[str, object] = {
        "math_omml": 0,
        "math_image": 0,
        "math_raw": 0,
    }
    report.update(native_report)
    return BackendResult(
        status="success",
        capabilities=_locked_capabilities(),
        artifact_name="review.docx",
        artifact_sha256="sha256:" + "b" * 64,
        artifact_size_bytes=1,
        findings=(),
        native_report=report,
    )


def _inspection(**changes: Any) -> DocxInspection:
    values: dict[str, Any] = {
        "package_valid": True,
        "structure_inspected": True,
        "file_sha256": "sha256:" + "c" * 64,
        "body_paragraphs": 1,
        "paragraphs": 1,
        "omml_objects": 0,
        "omml_paragraphs": 0,
        "images": 0,
        "image_instances": 0,
        "tables": 0,
        "bookmarks": 0,
        "bookmark_names": (),
        "seq_fields": 0,
        "ref_fields": 0,
        "pageref_fields": 0,
        "relationships": 0,
        "external_relationships": 0,
        "findings": (),
        "validation": ExportValidation("pass", "pass", "pass", "pass"),
    }
    values.update(changes)
    if "bookmark_names" in changes and "bookmarks" not in changes:
        values["bookmarks"] = len(values["bookmark_names"])
    return DocxInspection(**values)


def _inventory(**changes: Any) -> SourceFeatureInventory:
    inventory = SourceFeatureInventory(
        source_tree_sha256="sha256:" + "d" * 64,
        tex_files=1,
        inline_math_instances=0,
        display_math_instances=0,
        math_objects=0,
        table_instances=0,
        non_equivalent_table_instances=0,
        reference_instances=0,
        non_equivalent_reference_instances=0,
        label_instances=0,
        dynamic_label_instances=0,
        citation_instances=0,
        image_instances=0,
        skipped_dynamic_regions=0,
        static_labels=(),
        reference_warning_constructs=(),
        first_locations=(),
    )
    return replace(inventory, **changes)


def test_e0_source_feature_inventory_has_expected_conservative_counts() -> None:
    discovery = discover_project(FIXTURE_ROOT, main_document="main.tex")

    inventory = scan_source_features(FIXTURE_ROOT, discovery, image_instances=1)

    assert inventory.tex_files == 4
    assert inventory.inline_math_instances == 2
    assert inventory.display_math_instances == 2
    assert inventory.math_objects == 5
    assert inventory.table_instances == 1
    assert inventory.non_equivalent_table_instances == 0
    assert inventory.reference_instances == 6
    assert inventory.non_equivalent_reference_instances == 0
    assert inventory.label_instances == 8
    assert inventory.dynamic_label_instances == 0
    assert inventory.citation_instances == 1
    assert inventory.image_instances == 1
    assert inventory.skipped_dynamic_regions == 0
    assert set(inventory.static_labels) == {
        "eq:balance",
        "eq:energy",
        "fig:response",
        "sec:conclusion",
        "sec:intro",
        "sec:methods",
        "sec:results",
        "tab:metrics",
    }
    assert set(dict(inventory.first_locations)) == {
        "citations",
        "labels",
        "math",
        "references",
        "tables",
    }


def test_scanner_ignores_inert_regions_but_counts_math_in_normal_environment(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        r"""\documentclass{article}
\newcommand{\fake}{Comment-like $macro$ \ref{macro} \label{macro}}
\def\alsofake{$primitive$ \begin{tabular}{c}x\end{tabular}}
\newenvironment{fakeenv}[1][$default$]{$begin$}{$end$}
\NewDocumentCommand{\xfake}{m}{$xparse$ \ref{xparse} \label{xparse}}
\newtheorem{faketheorem}{A $theorem$}
\begin{document}
% $comment$ \ref{comment} \label{comment}
\begin{abstract}
Ordinary environment math $counted$.
\end{abstract}
\verb|$verb$ \ref{verb} \label{verb}|
\verb*|$starred$ \ref{starred} \label{starred}|
\begin{verbatim}
$environment$ \ref{environment} \label{environment}
\begin{tabular}{c}hidden\end{tabular}
\end{verbatim}
\ifdefined\never
$conditional$ \ref{conditional} \label{conditional}
\else
$alternate$ \begin{tabular}{c}hidden\end{tabular}
\fi
\end{document}
""",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")

    inventory = scan_source_features(source, discovery, image_instances=0)

    assert inventory.inline_math_instances == 1
    assert inventory.display_math_instances == 0
    assert inventory.math_objects == 1
    assert inventory.table_instances == 0
    assert inventory.reference_instances == 0
    assert inventory.label_instances == 0
    assert inventory.skipped_dynamic_regions == 1


def test_scanner_classifies_common_complex_latex_features_without_guessing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        r"""\documentclass{article}
\begin{document}
Inline \(a+b\), display \[c=d\], and legacy display $$e=f$$.
\begin{longtable}{c}
cell
\end{longtable}
\crefrange{sec:first}{sec:last}
\nameref{sec:first}
\label{bad key}
\label{\dynamic}
\end{document}
""",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")

    inventory = scan_source_features(source, discovery, image_instances=0)

    assert inventory.inline_math_instances == 1
    assert inventory.display_math_instances == 2
    assert inventory.math_objects == 3
    assert inventory.table_instances == 0
    assert inventory.non_equivalent_table_instances == 1
    assert inventory.reference_instances == 2
    assert inventory.non_equivalent_reference_instances == 1
    assert inventory.label_instances == 0
    assert inventory.dynamic_label_instances == 2
    assert inventory.static_labels == ()
    assert set(inventory.reference_warning_constructs) == {"\\crefrange", "\\ref"}
    assert set(dict(inventory.first_locations)) == {
        "labels",
        "math",
        "references",
        "tables",
    }


def test_inventory_rejects_source_drift_after_discovery(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    main = source / "main.tex"
    main.write_text(
        "\\documentclass{article}\n\\begin{document}\nPlain.\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    main.write_text(
        "\\documentclass{article}\n\\begin{document}\nOther.\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractError) as caught:
        scan_source_features(source, discovery, image_instances=0)

    assert caught.value.code is ErrorCode.HASH_SOURCE_MISMATCH


def test_reference_inventory_includes_tex2word_normalized_warning_construct(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Equation~\\eqref{missing}.\n"
        "\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")

    inventory = scan_source_features(source, discovery, image_instances=0)

    assert inventory.reference_instances == 1
    assert inventory.reference_warning_constructs == ("\\eqref", "\\ref")


def test_empty_math_inventory_does_not_require_backend_math_counters() -> None:
    backend_result = replace(_backend_result(), native_report={})

    reconciliation = reconcile_source_features(
        _inventory(),
        _inspection(),
        backend_result,
    )

    assert not reconciliation.failed
    assert all(result.status == "preserved" for result in reconciliation.feature_results)


def test_backend_omml_claim_cannot_exceed_the_final_docx() -> None:
    reconciliation = reconcile_source_features(
        _inventory(math_objects=1),
        _inspection(omml_objects=1),
        _backend_result(math_omml=2),
    )

    result = next(item for item in reconciliation.feature_results if item.feature == "math")
    assert reconciliation.failed
    assert result.status == "failed"
    assert result.source_count == 1
    assert result.output_count == 1
    assert len(result.diagnostic_ids) == 1
    finding = next(
        item for item in reconciliation.findings if item.diagnostic_id in result.diagnostic_ids
    )
    assert finding.code is ErrorCode.EXPORT_SILENT_LOSS
    assert finding.recoverable is False
    assert finding.fingerprint is not None
    assert "fewer OMML objects" in finding.message


@pytest.mark.parametrize(
    ("backend_changes", "inspection_changes"),
    [
        ({"math_raw": 1}, {}),
        ({"math_image": 1}, {"image_instances": 1}),
    ],
)
def test_explicit_math_fallback_is_degraded_when_final_omml_is_complete(
    backend_changes: dict[str, object],
    inspection_changes: dict[str, Any],
) -> None:
    reconciliation = reconcile_source_features(
        _inventory(math_objects=1),
        _inspection(omml_objects=1, **inspection_changes),
        _backend_result(math_omml=1, **backend_changes),
    )

    result = next(item for item in reconciliation.feature_results if item.feature == "math")
    assert not reconciliation.failed
    assert result.status == "degraded"
    assert result.source_count == 1
    assert result.output_count == 1
    assert len(result.diagnostic_ids) == 1
    finding = next(
        item for item in reconciliation.findings if item.diagnostic_id in result.diagnostic_ids
    )
    assert finding.code is ErrorCode.EXPORT_DEGRADED
    assert finding.recoverable is True
    assert finding.fingerprint is not None
    assert "non-OMML fallback" in finding.message


def test_skipped_dynamic_regions_are_explicit_recoverable_degradation() -> None:
    reconciliation = reconcile_source_features(
        _inventory(skipped_dynamic_regions=2),
        _inspection(),
        _backend_result(),
    )

    result = next(
        item for item in reconciliation.feature_results if item.feature == "dynamic_regions"
    )
    assert not reconciliation.failed
    assert result.status == "degraded"
    assert result.source_count == 2
    assert result.output_count is None
    assert len(result.diagnostic_ids) == 1
    assert reconciliation.findings[-1].code is ErrorCode.EXPORT_DEGRADED
    assert reconciliation.findings[-1].recoverable is True


@pytest.mark.parametrize(
    ("feature", "inventory_changes", "inspection_changes", "backend_changes"),
    [
        ("tables", {"table_instances": 1}, {"tables": 0}, {}),
        ("math", {"math_objects": 1}, {"omml_objects": 0}, {}),
        (
            "references",
            {
                "reference_instances": 1,
                "reference_warning_constructs": ("\\ref",),
            },
            {"ref_fields": 0},
            {},
        ),
        (
            "labels",
            {"label_instances": 1, "static_labels": ("sec:intro",)},
            {"bookmark_names": ()},
            {},
        ),
        ("images", {"image_instances": 1}, {"image_instances": 0}, {}),
    ],
)
def test_missing_equivalent_structure_is_a_silent_loss(
    feature: str,
    inventory_changes: dict[str, Any],
    inspection_changes: dict[str, Any],
    backend_changes: dict[str, object],
) -> None:
    reconciliation = reconcile_source_features(
        _inventory(**inventory_changes),
        _inspection(**inspection_changes),
        _backend_result(**backend_changes),
    )

    result = next(item for item in reconciliation.feature_results if item.feature == feature)
    assert reconciliation.failed
    assert result.status == "failed"
    assert any(
        finding.code is ErrorCode.EXPORT_SILENT_LOSS and not finding.recoverable
        for finding in reconciliation.findings
    )


@pytest.mark.parametrize(
    ("feature", "inventory_changes"),
    [
        ("tables", {"non_equivalent_table_instances": 1}),
        ("references", {"non_equivalent_reference_instances": 1}),
    ],
)
def test_non_equivalent_construct_is_recoverable_degradation(
    feature: str,
    inventory_changes: dict[str, Any],
) -> None:
    reconciliation = reconcile_source_features(
        _inventory(**inventory_changes),
        _inspection(),
        _backend_result(),
    )

    result = next(item for item in reconciliation.feature_results if item.feature == feature)
    assert not reconciliation.failed
    assert result.status == "degraded"
    assert reconciliation.findings
    assert all(finding.recoverable for finding in reconciliation.findings)
    assert {finding.code for finding in reconciliation.findings} == {ErrorCode.EXPORT_DEGRADED}


def test_group_conditionals_and_endinput_dead_code_are_not_inventoried(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        r"""\documentclass{article}
\begin{document}
\ifthenelse{$condition$}{\ref{true}}{\label{false}}
\IfFileExists{maybe.sty}{$exists$}{\begin{tabular}{c}hidden\end{tabular}}
Visible $kept$.
\endinput
$dead$ \ref{dead} \label{dead} \begin{tabular}{c}dead\end{tabular}
\end{document}
""",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")

    inventory = scan_source_features(source, discovery, image_instances=0)

    assert inventory.inline_math_instances == 1
    assert inventory.math_objects == 1
    assert inventory.table_instances == 0
    assert inventory.reference_instances == 0
    assert inventory.label_instances == 0
    assert inventory.skipped_dynamic_regions == 2


def test_explicit_input_dependency_is_scanned_independent_of_discovery_role(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\input{chapter.ltx}\n\\end{document}\n",
        encoding="utf-8",
        newline="\n",
    )
    (source / "chapter.ltx").write_text(
        "$included$\\n"
        "\\begin{tabular}{c}cell\\end{tabular}\\n"
        "\\label{sec:included}\\ref{sec:included}\\n",
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    dependency = next(item for item in discovery.files if item.path == "chapter.ltx")
    assert dependency.role != "tex"

    inventory = scan_source_features(source, discovery, image_instances=0)

    assert inventory.tex_files == 2
    assert inventory.math_objects == 1
    assert inventory.table_instances == 1
    assert inventory.reference_instances == 1
    assert inventory.label_instances == 1


def test_unbalanced_feature_group_stops_after_one_bounded_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n" + "\\label{" * 5_000,
        encoding="utf-8",
        newline="\n",
    )
    discovery = discover_project(source, main_document="main.tex")
    balanced_calls = 0
    original = source_features_module._balanced

    def counted_balanced(*args: Any, **kwargs: Any) -> Any:
        nonlocal balanced_calls
        balanced_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(source_features_module, "_balanced", counted_balanced)

    with pytest.raises(ContractError) as caught:
        scan_source_features(source, discovery, image_instances=0)

    assert caught.value.code is ErrorCode.SCHEMA_INVALID
    assert balanced_calls == 2


@pytest.mark.parametrize("source_count", [1, 2])
def test_reference_warning_cannot_account_for_any_missing_instance(source_count: int) -> None:
    reconciliation = reconcile_source_features(
        _inventory(
            reference_instances=source_count,
            reference_warning_constructs=("\\ref",),
        ),
        _inspection(ref_fields=0),
        _backend_result(warning_constructs=["\\ref"]),
    )

    result = next(item for item in reconciliation.feature_results if item.feature == "references")
    assert reconciliation.failed
    assert result.status == "failed"
    assert result.output_count == 0


@pytest.mark.parametrize(
    ("inventory_changes", "bookmark_names"),
    [
        (
            {
                "label_instances": 1,
                "dynamic_label_instances": 1,
                "static_labels": ("unique",),
            },
            (),
        ),
        (
            {
                "label_instances": 3,
                "static_labels": ("a-b", "a_b", "unique"),
            },
            ("a_b",),
        ),
    ],
)
def test_dynamic_or_colliding_labels_do_not_mask_missing_unique_bookmarks(
    inventory_changes: dict[str, Any],
    bookmark_names: tuple[str, ...],
) -> None:
    reconciliation = reconcile_source_features(
        _inventory(**inventory_changes),
        _inspection(bookmark_names=bookmark_names),
        _backend_result(),
    )

    result = next(item for item in reconciliation.feature_results if item.feature == "labels")
    assert reconciliation.failed
    assert result.status == "failed"


def test_colliding_labels_remain_recoverable_when_all_unique_bookmarks_exist() -> None:
    reconciliation = reconcile_source_features(
        _inventory(
            label_instances=3,
            static_labels=("a-b", "a_b", "unique"),
        ),
        _inspection(bookmark_names=("a_b", "unique")),
        _backend_result(),
    )

    result = next(item for item in reconciliation.feature_results if item.feature == "labels")
    assert not reconciliation.failed
    assert result.status == "degraded"
    assert result.output_count == 1


@pytest.mark.parametrize(
    ("backend_changes", "inspection_changes"),
    [
        ({"math_raw": 1}, {}),
        ({"math_image": 1}, {"image_instances": 1}),
    ],
)
def test_backend_only_math_fallback_is_not_final_docx_visibility_evidence(
    backend_changes: dict[str, object],
    inspection_changes: dict[str, Any],
) -> None:
    reconciliation = reconcile_source_features(
        _inventory(math_objects=1),
        _inspection(**inspection_changes),
        _backend_result(**backend_changes),
    )

    result = next(item for item in reconciliation.feature_results if item.feature == "math")
    assert reconciliation.failed
    assert result.status == "failed"


def test_unsupported_citations_bind_a_recoverable_finding() -> None:
    reconciliation = reconcile_source_features(
        _inventory(citation_instances=1),
        _inspection(),
        _backend_result(),
    )

    result = next(item for item in reconciliation.feature_results if item.feature == "citations")
    assert not reconciliation.failed
    assert result.status == "unsupported"
    assert len(result.diagnostic_ids) == 1
    assert all(finding.recoverable for finding in reconciliation.findings)


def test_unlocked_backend_unsupported_labels_are_a_blocking_loss() -> None:
    capabilities = replace(
        _locked_capabilities(),
        backend_id="other-backend",
        tool_name="other",
        tool_version="1",
        interface_version="other-v1",
    )
    backend_result = replace(_backend_result(), capabilities=capabilities)

    reconciliation = reconcile_source_features(
        _inventory(label_instances=1, static_labels=("sec:intro",)),
        _inspection(),
        backend_result,
    )

    result = next(item for item in reconciliation.feature_results if item.feature == "labels")
    assert reconciliation.failed
    assert result.status == "unsupported"
    assert len(result.diagnostic_ids) == 1
    assert any(
        finding.code is ErrorCode.EXPORT_SILENT_LOSS and not finding.recoverable
        for finding in reconciliation.findings
    )
