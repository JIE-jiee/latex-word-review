from __future__ import annotations

from latex_word_review import (
    app_server,
    app_views,
    application,
    approval,
    cli,
    review_server,
    workflow,
    workflow_objects,
)
from latex_word_review.domain_values import (
    ACTION_DECISION_VALUE_SET,
    CONFIDENTIALITY_VALUES,
    DECISION_VALUE_SET,
    DECISION_VALUES,
    Confidentiality,
    Decision,
)
from latex_word_review.paths import validate_relative_path
from latex_word_review.run_layout import (
    APPROVALS_DIR,
    AUDIT_BUNDLE,
    BACKEND_CAPABILITIES,
    CHANGESET,
    DELIVERY_DIR,
    EXISTING_CHANGES_DISPLAY_DOCX,
    EXPORT_DIR,
    EXPORT_IMAGE_OVERLAY_DIR,
    EXPORT_OBJECTS_DIR,
    EXPORT_REPORT,
    LEDGER_DIR,
    OBJECTS_DIR,
    PLANS_DIR,
    RECEIVE_DIR,
    RETURNED_ARCHIVE_DIR,
    RETURNED_ORIGINAL_DOCX,
    REVIEW_DOCX,
    REVIEW_IR,
    REVISED_DIR,
    REVISION_READER,
    SNAPSHOT_DIR,
    SOURCE_MANIFEST,
    SOURCE_MAP,
    STAGING_DIR,
    VERIFICATION_DIR,
    VERIFICATION_RETRIES_DIR,
)


def test_shared_domain_values_keep_legacy_consumers_aligned() -> None:
    assert vars(workflow)["Confidentiality"] is Confidentiality
    assert workflow_objects.Confidentiality is Confidentiality
    assert approval.Decision is Decision
    assert cli._CONFIDENTIALITY is CONFIDENTIALITY_VALUES
    assert cli._DECISIONS is DECISION_VALUES
    assert approval._DECISIONS is DECISION_VALUE_SET
    assert app_views._DECISIONS is DECISION_VALUE_SET
    assert app_server._DECISIONS is ACTION_DECISION_VALUE_SET
    assert review_server._DECISIONS is ACTION_DECISION_VALUE_SET


def test_run_layout_paths_are_portable_and_keep_expected_relationships() -> None:
    paths = (
        APPROVALS_DIR,
        AUDIT_BUNDLE,
        BACKEND_CAPABILITIES,
        CHANGESET,
        DELIVERY_DIR,
        EXISTING_CHANGES_DISPLAY_DOCX,
        EXPORT_DIR,
        EXPORT_IMAGE_OVERLAY_DIR,
        EXPORT_OBJECTS_DIR,
        EXPORT_REPORT,
        LEDGER_DIR,
        OBJECTS_DIR,
        PLANS_DIR,
        RECEIVE_DIR,
        RETURNED_ARCHIVE_DIR,
        RETURNED_ORIGINAL_DOCX,
        REVIEW_DOCX,
        REVIEW_IR,
        REVISED_DIR,
        REVISION_READER,
        SNAPSHOT_DIR,
        SOURCE_MANIFEST,
        SOURCE_MAP,
        STAGING_DIR,
        VERIFICATION_DIR,
        VERIFICATION_RETRIES_DIR,
    )
    assert all(validate_relative_path(path) == path for path in paths)
    assert f"{EXPORT_DIR}/review.docx" == REVIEW_DOCX
    assert f"{EXPORT_DIR}/objects" == EXPORT_OBJECTS_DIR
    assert f"{EXPORT_OBJECTS_DIR}/export-report.json" == EXPORT_REPORT
    assert f"{OBJECTS_DIR}/source-manifest.json" == SOURCE_MANIFEST
    assert f"{RETURNED_ARCHIVE_DIR}/returned-original.docx" == RETURNED_ORIGINAL_DOCX


def test_application_legacy_names_are_aliases_of_shared_layout() -> None:
    expected = {
        "_SOURCE_MANIFEST": SOURCE_MANIFEST,
        "_SNAPSHOT": SNAPSHOT_DIR,
        "_REVIEW_DOCX": REVIEW_DOCX,
        "_EXPORT_REPORT": EXPORT_REPORT,
        "_EXISTING_CHANGES_DISPLAY_DOCX": EXISTING_CHANGES_DISPLAY_DOCX,
        "_CHANGESET": CHANGESET,
        "_RETURNED_ORIGINAL": RETURNED_ORIGINAL_DOCX,
        "_APPROVALS": APPROVALS_DIR,
        "_PLANS": PLANS_DIR,
        "_REVISED": REVISED_DIR,
        "_VERIFICATION": VERIFICATION_DIR,
        "_VERIFICATION_RETRIES": VERIFICATION_RETRIES_DIR,
        "_LEDGER": LEDGER_DIR,
        "_DELIVERY": DELIVERY_DIR,
        "_AUDIT_BUNDLE": AUDIT_BUNDLE,
    }
    assert all(getattr(application, name) is value for name, value in expected.items())
