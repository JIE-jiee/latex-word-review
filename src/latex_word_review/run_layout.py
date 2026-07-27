"""Single source of truth for the on-disk review-run layout.

These are portable, run-root-relative paths.  Modules that read or publish
sealed workflow artifacts should import them instead of repeating string
literals.  This module intentionally contains no filesystem operations.
"""

from __future__ import annotations

from typing import Final

OBJECTS_DIR: Final = "objects"
SOURCE_MANIFEST: Final = f"{OBJECTS_DIR}/source-manifest.json"

SNAPSHOT_DIR: Final = "snapshot"

EXPORT_DIR: Final = "export"
REVIEW_DOCX: Final = f"{EXPORT_DIR}/review.docx"
EXISTING_CHANGES_DISPLAY_DOCX: Final = f"{EXPORT_DIR}/existing-changes-display.docx"
EXPORT_OBJECTS_DIR: Final = f"{EXPORT_DIR}/objects"
BACKEND_CAPABILITIES: Final = f"{EXPORT_OBJECTS_DIR}/backend-capabilities.json"
REVIEW_IR: Final = f"{EXPORT_OBJECTS_DIR}/review-ir.json"
SOURCE_MAP: Final = f"{EXPORT_OBJECTS_DIR}/source-map.json"
EXPORT_REPORT: Final = f"{EXPORT_OBJECTS_DIR}/export-report.json"
EXPORT_IMAGE_OVERLAY_DIR: Final = f"{REVIEW_DOCX}.image-overlay"

RECEIVE_DIR: Final = "receive"
RETURNED_ARCHIVE_DIR: Final = f"{RECEIVE_DIR}/original"
REVISION_READER: Final = f"{RECEIVE_DIR}/revision-reader.json"
CHANGESET: Final = f"{RECEIVE_DIR}/changeset.json"
RETURNED_ORIGINAL_DOCX: Final = f"{RETURNED_ARCHIVE_DIR}/returned-original.docx"

APPROVALS_DIR: Final = "approvals"
PLANS_DIR: Final = "plans"
REVISED_DIR: Final = "revised-clean"
VERIFICATION_DIR: Final = "verification"
VERIFICATION_RETRIES_DIR: Final = "verification-retries"
LEDGER_DIR: Final = "ledger"
DELIVERY_DIR: Final = "delivery"
AUDIT_BUNDLE: Final = "audit.zip"
STAGING_DIR: Final = ".lwr-staging"

__all__ = [
    "APPROVALS_DIR",
    "AUDIT_BUNDLE",
    "BACKEND_CAPABILITIES",
    "CHANGESET",
    "DELIVERY_DIR",
    "EXISTING_CHANGES_DISPLAY_DOCX",
    "EXPORT_DIR",
    "EXPORT_IMAGE_OVERLAY_DIR",
    "EXPORT_OBJECTS_DIR",
    "EXPORT_REPORT",
    "LEDGER_DIR",
    "OBJECTS_DIR",
    "PLANS_DIR",
    "RECEIVE_DIR",
    "RETURNED_ARCHIVE_DIR",
    "RETURNED_ORIGINAL_DOCX",
    "REVIEW_DOCX",
    "REVIEW_IR",
    "REVISED_DIR",
    "REVISION_READER",
    "SNAPSHOT_DIR",
    "SOURCE_MANIFEST",
    "SOURCE_MAP",
    "STAGING_DIR",
    "VERIFICATION_DIR",
    "VERIFICATION_RETRIES_DIR",
]
