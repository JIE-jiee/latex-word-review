"""Product-level facade for one safe, resumable Windows review session.

The facade deliberately stores no authoritative UI state.  Every public
operation starts by rebuilding evidence from the run directory and validating
the sealed workflow objects, immutable approval chain, reproducible PatchPlan,
and any downstream deliverables that already exist.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

from latex_word_review.applier import apply_patch_plan
from latex_word_review.approval import (
    BulkOperation,
    Decision,
    create_approval_set,
    finalize_approval_set,
    record_bulk_decision,
    record_decision,
    reopen_approval_set,
    write_approval_json,
)
from latex_word_review.atomic_publish import publish_new_directory
from latex_word_review.bundle import (
    BundleItem,
    create_audit_bundle,
    verify_audit_bundle,
)
from latex_word_review.canonical import canonical_json, compute_payload_sha256
from latex_word_review.contracts import validate_contract
from latex_word_review.discovery import discover_project
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import FileDigest, digest_bytes, digest_file, read_stable_bytes
from latex_word_review.ids import derive_artifact_id
from latex_word_review.jsonio import read_contract_file, write_new_bytes, write_new_json
from latex_word_review.latex_verify import VerificationPolicy, verify_latex_project
from latex_word_review.ledger import LedgerOutput, build_ledger
from latex_word_review.paths import resolve_within, validate_relative_path
from latex_word_review.planner import PatchPlanResult, plan_patch, verify_plan_identity
from latex_word_review.session_status import SessionStatus
from latex_word_review.workflow import (
    BackendName,
    Confidentiality,
    export_workflow,
    initialize_workflow,
    receive_workflow,
    workflow_status,
)
from latex_word_review.workflow_objects import build_run_manifest_document, utc_now

_SOURCE_MANIFEST: Final = "objects/source-manifest.json"
_SNAPSHOT: Final = "snapshot"
_REVIEW_DOCX: Final = "export/review.docx"
_CHANGESET: Final = "receive/changeset.json"
_RETURNED_ORIGINAL: Final = "receive/original/returned-original.docx"
_APPROVALS: Final = "approvals"
_PLANS: Final = "plans"
_REVISED: Final = "revised-clean"
_VERIFICATION: Final = "verification"
_VERIFICATION_RETRIES: Final = "verification-retries"
_LEDGER: Final = "ledger"
_DELIVERY: Final = "delivery"
_AUDIT_BUNDLE: Final = "audit.zip"
_MAX_CONTRACT_BYTES: Final = 16 * 1024 * 1024
_MAX_ARTIFACT_BYTES: Final = 512 * 1024 * 1024
_APPROVAL_RE: Final = re.compile(r"approval-r([1-9][0-9]*)\.json")
_PLAN_RE: Final = re.compile(r"plan-r([1-9][0-9]*)")
_VERIFICATION_RETRY_RE: Final = re.compile(r"retry-r([1-9][0-9]*)")

ContentClassification = Literal["public_fixture", "local_private"]


@dataclass(frozen=True, slots=True)
class _ApprovalVersion:
    revision: int
    path: str
    document: dict[str, Any]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class _PlanVersion:
    number: int
    directory: str
    document: dict[str, Any]
    unified_diff: bytes
    approval: _ApprovalVersion
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class _VerificationVersion:
    attempt: int
    directory: str
    document: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Evidence:
    workflow: dict[str, Any]
    source_manifest: dict[str, Any]
    changeset: dict[str, Any] | None
    approvals: tuple[_ApprovalVersion, ...]
    plans: tuple[_PlanVersion, ...]
    current_plan: _PlanVersion | None
    revised_exists: bool
    verifications: tuple[_VerificationVersion, ...]
    verification: dict[str, Any] | None
    verification_directory: str | None
    expected_ledger: LedgerOutput | None
    ledger_exists: bool
    run_manifest: dict[str, Any] | None
    audit_exists: bool

    @property
    def latest_approval(self) -> _ApprovalVersion | None:
        return self.approvals[-1] if self.approvals else None


def _is_link_or_junction(path: Path) -> bool:
    probe = getattr(path, "is_junction", None)
    try:
        return path.is_symlink() or bool(probe is not None and probe())
    except OSError as exc:
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, "path link status is unavailable") from exc


def _require_real_directory(path: Path, label: str) -> Path:
    if _is_link_or_junction(path):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, f"{label} must not be a link")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} is unavailable") from exc
    if not resolved.is_dir() or _is_link_or_junction(resolved):
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} must be a directory")
    return resolved


def _require_regular_file(path: Path, label: str) -> Path:
    if _is_link_or_junction(path):
        raise ContractError(ErrorCode.PATH_LINK_ESCAPE, f"{label} must not be a link")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} is unavailable") from exc
    if not resolved.is_file() or _is_link_or_junction(resolved):
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{label} must be a regular file")
    return resolved


def _ensure_real_directory(path: Path, label: str) -> Path:
    if path.exists() or path.is_symlink():
        return _require_real_directory(path, label)
    try:
        path.mkdir()
    except OSError as exc:
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, f"{label} could not be created") from exc
    return _require_real_directory(path, label)


def _publish_directory(
    destination: Path,
    *,
    purpose: str,
    writer: Callable[[Path], None],
) -> None:
    if destination.exists() or destination.is_symlink():
        raise ContractError(ErrorCode.SCHEMA_INVALID, f"{purpose} already exists")
    parent = _require_real_directory(destination.parent, f"{purpose} parent")
    prefix = f".{destination.name}.{purpose}-"
    staged = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    published = False
    try:
        writer(staged)
        try:
            publish_new_directory(staged, destination)
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                f"{purpose} could not be published atomically",
            ) from exc
        published = True
    finally:
        if (
            not published
            and staged.exists()
            and staged.parent.resolve(strict=True) == parent
            and staged.name.startswith(prefix)
        ):
            shutil.rmtree(staged)


def _publish_verified_file_copy(source: Path, destination: Path) -> Path:
    """Stream one immutable artifact to a new user-selected path."""

    if not destination.is_absolute():
        raise ContractError(ErrorCode.PATH_ABSOLUTE, "output path must be absolute")
    if not destination.name or destination.suffix.casefold() != ".docx":
        raise ContractError(ErrorCode.SCHEMA_INVALID, "output path must name a .docx file")
    if destination.exists() or _is_link_or_junction(destination):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "output path already exists")

    source_file = _require_regular_file(source, "review Word")
    parent = _require_real_directory(destination.parent, "output directory")
    target = parent / destination.name
    if target.exists() or _is_link_or_junction(target):
        raise ContractError(ErrorCode.SCHEMA_INVALID, "output path already exists")

    source_before = digest_file(source_file, max_bytes=_MAX_ARTIFACT_BYTES)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.publish-",
        suffix=".tmp",
        dir=parent,
    )
    staged = Path(temporary_name)
    published = False
    linked = False
    try:
        with source_file.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            descriptor = -1
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        staged.chmod(0o600)

        source_after = digest_file(source_file, max_bytes=_MAX_ARTIFACT_BYTES)
        if source_after != source_before:
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "review Word changed while being copied",
            )
        staged_digest = digest_file(staged, max_bytes=_MAX_ARTIFACT_BYTES)
        if staged_digest != source_before:
            raise ContractError(
                ErrorCode.HASH_INTEGRITY_MISMATCH,
                "review Word copy failed byte verification",
            )

        try:
            os.link(staged, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "output path already exists") from exc
        except OSError as exc:
            raise ContractError(
                ErrorCode.INTERNAL_INVARIANT,
                "review Word copy could not be published",
            ) from exc
        linked = True

        target_digest = digest_file(target, max_bytes=_MAX_ARTIFACT_BYTES)
        if target_digest != source_before:
            same_file = False
            with suppress(OSError):
                same_file = staged.samefile(target)
            if same_file:
                with suppress(OSError):
                    target.unlink()
            raise ContractError(
                ErrorCode.HASH_INTEGRITY_MISMATCH,
                "published review Word copy failed verification",
            )
        published = True
        return target
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if linked and not published:
            with suppress(OSError):
                if staged.samefile(target):
                    target.unlink()
        with suppress(FileNotFoundError):
            staged.unlink()


def _artifact_ref(
    path: str,
    role: str,
    data: bytes,
    media_type: str,
) -> dict[str, Any]:
    normalized = validate_relative_path(path)
    digest = digest_bytes(data)
    return {
        "artifact_id": derive_artifact_id(digest.sha256),
        "path": normalized,
        "path_base": "run_root",
        "role": role,
        "media_type": media_type,
        "size_bytes": digest.size_bytes,
        "sha256": digest.sha256,
        "immutable": True,
        "confidentiality": "derived_private",
    }


def _collect_artifact_refs(value: object) -> tuple[dict[str, Any], ...]:
    found: dict[str, dict[str, Any]] = {}

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            if {
                "artifact_id",
                "path",
                "path_base",
                "role",
                "size_bytes",
                "sha256",
                "immutable",
            }.issubset(item):
                path = item.get("path")
                if not isinstance(path, str):
                    raise ContractError(ErrorCode.SCHEMA_INVALID, "artifact path is invalid")
                candidate = dict(item)
                prior = found.get(path)
                if prior is not None and prior != candidate:
                    raise ContractError(
                        ErrorCode.HASH_INTEGRITY_MISMATCH,
                        "artifact path has conflicting bindings",
                    )
                found[path] = candidate
                return
            for nested in item.values():
                visit(nested)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(found[path] for path in sorted(found))


class ApplicationSession:
    """Safe product facade for one fixed-layout run directory."""

    def __init__(self, run_root: Path) -> None:
        self._run_root = run_root.absolute()
        self._loaded_status_evidence: _Evidence | None = None

    @property
    def run_root(self) -> Path:
        """Return the local run root for trusted application plumbing only."""

        return self._run_root

    @classmethod
    def create(
        cls,
        source: Path,
        run_root: Path,
        *,
        main_document: str | None = None,
        confidentiality: Confidentiality = "local_private",
        generated_at: str | None = None,
    ) -> ApplicationSession:
        """Create a new immutable snapshot and return its validated session."""

        initialize_workflow(
            source,
            run_root,
            main_document=main_document,
            confidentiality=confidentiality,
            generated_at=generated_at,
        )
        return cls.load(run_root)

    @classmethod
    def load(cls, run_root: Path) -> ApplicationSession:
        """Load a session only after reconstructing and validating disk evidence."""

        session = cls(run_root)
        session._loaded_status_evidence = session._inspect()
        return session

    def status(self) -> dict[str, Any]:
        """Return validated status without consulting mutable UI cache.

        ``load()`` hands its just-validated evidence to the first immediate
        read-only call. Later status reads rebuild from disk, and every action
        independently revalidates before it can mutate workflow state.
        """

        evidence = self._loaded_status_evidence
        self._loaded_status_evidence = None
        if evidence is None:
            evidence = self._inspect()
        return self._status_from_evidence(evidence).as_dict()

    def export_review(
        self,
        *,
        backend: BackendName = "tex2word",
        timeout_s: float = 60.0,
        confidentiality: Confidentiality = "local_private",
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Generate the immutable Word baseline for step 2."""

        evidence = self._inspect()
        if evidence.workflow["phase"] != "snapshotted":
            if evidence.workflow["phase"] in {"exported", "ingested"}:
                return self._status_from_evidence(evidence).as_dict()
            raise ContractError(ErrorCode.SCHEMA_INVALID, "session is not ready for export")
        export_workflow(
            self._run_root,
            backend=backend,
            timeout_s=timeout_s,
            confidentiality=confidentiality,
            generated_at=generated_at,
        )
        return self.status()

    def save_review_copy(self, destination: Path) -> Path:
        """Save a verified editable copy while preserving the sealed baseline."""

        evidence = self._inspect()
        if evidence.workflow["phase"] not in {"exported", "ingested"}:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "review Word is not available for copying",
            )
        return _publish_verified_file_copy(
            self._run_root / _REVIEW_DOCX,
            destination,
        )

    def receive_review(
        self,
        returned_docx: Path,
        *,
        confidentiality: Confidentiality = "local_private",
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Archive and ingest one returned Word original without approving it."""

        evidence = self._inspect()
        if evidence.workflow["phase"] == "ingested":
            return self._status_from_evidence(evidence).as_dict()
        if evidence.workflow["phase"] != "exported":
            raise ContractError(ErrorCode.SCHEMA_INVALID, "session is not ready to receive Word")
        receive_workflow(
            self._run_root,
            returned_docx,
            confidentiality=confidentiality,
            generated_at=generated_at,
        )
        return self.status()

    def begin_approval(
        self,
        *,
        actor_id: str,
        actor_name: str,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Create approval-r1, or resume an already existing approval chain."""

        evidence = self._inspect()
        changeset = self._require_changeset(evidence)
        if evidence.revised_exists:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "applied sessions cannot be re-approved")
        if evidence.approvals:
            return self._status_from_evidence(evidence).as_dict()
        directory = _ensure_real_directory(self._run_root / _APPROVALS, "approval directory")
        approval = create_approval_set(
            changeset,
            decided_by={"id": actor_id, "display_name": actor_name},
            generated_at=generated_at,
        )
        write_approval_json(changeset, approval, directory / "approval-r1.json")
        return self.status()

    def decide(
        self,
        *,
        change_id: str,
        decision: Decision,
        final_text: str | None = None,
        reason: str | None = None,
        risk_acknowledgement: str | None = None,
        decided_at: str | None = None,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Record one decision and return a fully reconstructed status."""

        self.decide_without_status(
            change_id=change_id,
            decision=decision,
            final_text=final_text,
            reason=reason,
            risk_acknowledgement=risk_acknowledgement,
            decided_at=decided_at,
            generated_at=generated_at,
        )
        return self.status()

    # App redirect path: publish/read back the new ApprovalSet, then let the
    # redirect GET perform the next complete status reconstruction.
    def decide_without_status(
        self,
        *,
        change_id: str,
        decision: Decision,
        final_text: str | None = None,
        reason: str | None = None,
        risk_acknowledgement: str | None = None,
        decided_at: str | None = None,
        generated_at: str | None = None,
    ) -> None:
        """Record one local-UI decision in the next immutable approval revision."""

        evidence = self._inspect()
        changeset = self._require_changeset(evidence)
        current = self._require_latest_approval(evidence)
        self._require_not_applied(evidence)
        updated = record_decision(
            changeset,
            current.document,
            change_id=change_id,
            decision=decision,
            final_text=final_text,
            reason=reason,
            risk_acknowledgement=risk_acknowledgement,
            decision_source="local_ui",
            decided_at=decided_at,
            generated_at=generated_at,
        )
        self._publish_next_approval(changeset, current, updated)

    def decide_bulk(
        self,
        *,
        operation: BulkOperation,
        change_ids: Sequence[str] | None = None,
        reason: str | None = None,
        risk_acknowledgement: str | None = None,
        decided_at: str | None = None,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Apply the restricted bulk policy and return reconstructed status."""

        self.decide_bulk_without_status(
            operation=operation,
            change_ids=change_ids,
            reason=reason,
            risk_acknowledgement=risk_acknowledgement,
            decided_at=decided_at,
            generated_at=generated_at,
        )
        return self.status()

    # Same redirect contract as decide_without_status(); callers must not render
    # or authorize from a status captured before this mutation.
    def decide_bulk_without_status(
        self,
        *,
        operation: BulkOperation,
        change_ids: Sequence[str] | None = None,
        reason: str | None = None,
        risk_acknowledgement: str | None = None,
        decided_at: str | None = None,
        generated_at: str | None = None,
    ) -> None:
        """Apply the core's restricted bulk policy into a new approval revision."""

        evidence = self._inspect()
        changeset = self._require_changeset(evidence)
        current = self._require_latest_approval(evidence)
        self._require_not_applied(evidence)
        updated = record_bulk_decision(
            changeset,
            current.document,
            operation=operation,
            change_ids=change_ids,
            reason=reason,
            risk_acknowledgement=risk_acknowledgement,
            decision_source="local_ui",
            decided_at=decided_at,
            generated_at=generated_at,
        )
        self._publish_next_approval(changeset, current, updated)

    def revise_blocked_approval(
        self,
        *,
        patch_plan_sha256: str,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Reopen the final approval behind a blocked plan as a new draft."""

        evidence = self._inspect()
        changeset = self._require_changeset(evidence)
        current = self._require_latest_approval(evidence)
        self._require_not_applied(evidence)
        approval_payload = cast("Mapping[str, Any]", current.document["payload"])
        plan = evidence.current_plan
        if approval_payload["status"] != "final":
            raise ContractError(
                ErrorCode.APPROVAL_NOT_FINAL,
                "only a final approval can be revised",
            )
        if plan is None:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "a blocked PatchPlan is required before revising approval",
            )
        if plan.payload_sha256 != patch_plan_sha256:
            raise ContractError(
                ErrorCode.HASH_PATCHPLAN_MISMATCH,
                "approval revision does not match the current blocked PatchPlan",
            )
        plan_payload = cast("Mapping[str, Any]", plan.document["payload"])
        blocked = cast(
            "Sequence[Mapping[str, Any]]",
            plan_payload["accepted_but_blocked"],
        )
        if plan_payload["status"] != "blocked" or not blocked:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "only an accepted-but-blocked PatchPlan can reopen approval",
            )
        updated = reopen_approval_set(
            changeset,
            current.document,
            generated_at=generated_at,
        )
        self._publish_next_approval(changeset, current, updated)
        return self.status()

    def finalize_approval(
        self,
        *,
        prepare_plan: bool = True,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Cross gate 1 and optionally create the dry-run preview (never apply)."""

        evidence = self._inspect()
        changeset = self._require_changeset(evidence)
        current = self._require_latest_approval(evidence)
        self._require_not_applied(evidence)
        updated = finalize_approval_set(
            changeset,
            current.document,
            generated_at=generated_at,
        )
        self._publish_next_approval(changeset, current, updated)
        if prepare_plan:
            return self.prepare_plan(generated_at=generated_at)
        return self.status()

    def prepare_plan(
        self,
        *,
        confidentiality: Confidentiality = "local_private",
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Create the next plan-rN dry run bound to the latest final approval."""

        evidence = self._inspect()
        changeset = self._require_changeset(evidence)
        approval = self._require_latest_approval(evidence)
        self._require_not_applied(evidence)
        approval_payload = cast("Mapping[str, Any]", approval.document["payload"])
        if approval_payload["status"] != "final":
            raise ContractError(ErrorCode.APPROVAL_NOT_FINAL, "approval gate is not final")
        if evidence.current_plan is not None:
            return self._status_from_evidence(evidence).as_dict()

        directory = _ensure_real_directory(self._run_root / _PLANS, "plan directory")
        number = len(evidence.plans) + 1
        destination = directory / f"plan-r{number}"
        source_payload = cast("Mapping[str, Any]", evidence.source_manifest["payload"])
        self._validate_bound_main(evidence.source_manifest)
        plan = plan_patch(
            self._run_root / _SNAPSHOT,
            source_tree_sha256=cast("str", source_payload["source_tree_sha256"]),
            changeset=changeset,
            approval=approval.document,
            generated_at=generated_at or utc_now(),
            diff_path=f"{_PLANS}/plan-r{number}/changes.patch",
            confidentiality=confidentiality,
        )
        self._publish_plan(destination, plan)
        return self.status()

    def apply_confirmed(self, *, patch_plan_sha256: str) -> dict[str, Any]:
        """Cross gate 2 only when the UI confirms the exact current plan hash."""

        evidence = self._inspect()
        changeset = self._require_changeset(evidence)
        approval = self._require_latest_approval(evidence)
        plan = evidence.current_plan
        if plan is None:
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "no current PatchPlan exists")
        if plan.payload_sha256 != patch_plan_sha256:
            raise ContractError(
                ErrorCode.HASH_PATCHPLAN_MISMATCH,
                "apply confirmation does not match the current PatchPlan",
            )
        plan_payload = cast("Mapping[str, Any]", plan.document["payload"])
        if plan_payload["status"] == "blocked":
            raise ContractError(
                ErrorCode.PATCH_ACCEPTED_BUT_BLOCKED,
                "blocked PatchPlan cannot cross the apply gate",
            )
        apply_patch_plan(
            self._run_root / _SNAPSHOT,
            self._run_root / _REVISED,
            patch_plan=plan.document,
            unified_diff=plan.unified_diff,
            changeset=changeset,
            approval=approval.document,
        )
        return self.status()

    def verify_results(
        self,
        *,
        latexmk_executable: str | Path = "latexmk",
        latexdiff_executable: str | Path = "latexdiff",
        policy: VerificationPolicy | None = None,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Verify the already applied copy and publish clean/diff deliverables."""

        evidence = self._inspect()
        if evidence.verification is not None:
            return self._status_from_evidence(evidence).as_dict()
        changeset = self._require_changeset(evidence)
        approval = self._require_latest_approval(evidence)
        plan = evidence.current_plan
        if not evidence.revised_exists or plan is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "apply confirmation is required first")
        verify_latex_project(
            self._run_root / _SNAPSHOT,
            self._run_root / _REVISED,
            self._run_root / _RETURNED_ORIGINAL,
            evidence.source_manifest,
            changeset,
            approval.document,
            plan.document,
            self._run_root / _VERIFICATION,
            latexmk_executable=latexmk_executable,
            latexdiff_executable=latexdiff_executable,
            policy=policy or VerificationPolicy(),
            generated_at=generated_at,
        )
        return self.status()

    def retry_verification(
        self,
        *,
        latexmk_executable: str | Path = "latexmk",
        latexdiff_executable: str | Path = "latexdiff",
        policy: VerificationPolicy | None = None,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Run a new immutable verification attempt after a blocked or failed one.

        The original verification directory and every prior retry remain
        sealed.  A later attempt becomes current only because it is the next
        contiguous, fully validated retry revision; no report or deliverable is
        overwritten.
        """

        evidence = self._inspect()
        current = evidence.verification
        if current is None:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "an initial verification attempt is required before retry",
            )
        current_payload = cast("Mapping[str, Any]", current["payload"])
        if current_payload["status"] == "pass":
            return self._status_from_evidence(evidence).as_dict()
        if evidence.ledger_exists or evidence.run_manifest is not None or evidence.audit_exists:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "verification cannot be retried after downstream evidence was sealed",
            )
        changeset = self._require_changeset(evidence)
        approval = self._require_latest_approval(evidence)
        plan = evidence.current_plan
        if not evidence.revised_exists or plan is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "apply confirmation is required first")
        retry_root = _ensure_real_directory(
            self._run_root / _VERIFICATION_RETRIES,
            "verification retry directory",
        )
        retry_number = len(evidence.verifications)
        destination = retry_root / f"retry-r{retry_number}"
        verify_latex_project(
            self._run_root / _SNAPSHOT,
            self._run_root / _REVISED,
            self._run_root / _RETURNED_ORIGINAL,
            evidence.source_manifest,
            changeset,
            approval.document,
            plan.document,
            destination,
            latexmk_executable=latexmk_executable,
            latexdiff_executable=latexdiff_executable,
            policy=policy or VerificationPolicy(),
            generated_at=generated_at,
        )
        return self.status()

    def build_ledger(self) -> dict[str, Any]:
        """Publish deterministic ledger JSON/HTML after verification."""

        evidence = self._inspect()
        if evidence.verification is None or evidence.expected_ledger is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "verification is required first")
        verification_payload = cast("Mapping[str, Any]", evidence.verification["payload"])
        if verification_payload["status"] != "pass":
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "a passing verification is required before sealing the ledger",
            )
        destination = self._run_root / _LEDGER
        if destination.exists() or destination.is_symlink():
            return self._status_from_evidence(evidence).as_dict()
        expected = evidence.expected_ledger

        def writer(staged: Path) -> None:
            write_new_bytes(staged / "ledger.json", expected.json_bytes)
            write_new_bytes(staged / "ledger.html", expected.html_bytes)

        _publish_directory(destination, purpose="ledger", writer=writer)
        return self.status()

    def create_bundle(
        self,
        *,
        content_classification: ContentClassification = "local_private",
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a delivery directory and verified audit ZIP from current evidence."""

        evidence = self._inspect()
        if (
            evidence.verification is None
            or cast("Mapping[str, Any]", evidence.verification["payload"])["status"] != "pass"
        ):
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "a passing verification is required before sealing delivery",
            )
        if evidence.run_manifest is None:
            if evidence.expected_ledger is None or not evidence.ledger_exists:
                raise ContractError(ErrorCode.SCHEMA_INVALID, "ledger is required first")
            self._publish_delivery(evidence, generated_at=generated_at)
            evidence = self._inspect()
        if evidence.audit_exists:
            return self._status_from_evidence(evidence).as_dict()
        if evidence.run_manifest is None or evidence.verification is None:
            raise ContractError(ErrorCode.INTERNAL_INVARIANT, "delivery evidence is incomplete")
        items = self._bundle_items(evidence.run_manifest, evidence.verification)
        create_audit_bundle(
            self._run_root / _DELIVERY,
            self._run_root / _AUDIT_BUNDLE,
            allowlist=items,
            run_manifest=evidence.run_manifest,
            verification_report=evidence.verification,
            content_classification=content_classification,
            generated_at=generated_at or utc_now(),
        )
        return self.status()

    def generate_results(
        self,
        *,
        patch_plan_sha256: str,
        latexmk_executable: str | Path = "latexmk",
        latexdiff_executable: str | Path = "latexdiff",
        policy: VerificationPolicy | None = None,
        content_classification: ContentClassification = "local_private",
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Run apply/verify/ledger/bundle after the explicit plan-hash gate."""

        self.apply_confirmed(patch_plan_sha256=patch_plan_sha256)
        status = self.verify_results(
            latexmk_executable=latexmk_executable,
            latexdiff_executable=latexdiff_executable,
            policy=policy,
            generated_at=generated_at,
        )
        if status["phase"] == "partially_completed":
            return status
        self.build_ledger()
        return self.create_bundle(
            content_classification=content_classification,
            generated_at=generated_at,
        )

    def _inspect(self) -> _Evidence:
        # A load-time evidence handoff is valid for one immediate read-only
        # status call only. Every action enters through _inspect(), which
        # discards it before revalidating disk and therefore never carries
        # cached authorization evidence across a mutation.
        self._loaded_status_evidence = None
        workflow = workflow_status(self._run_root)
        source_manifest = read_contract_file(
            self._run_root / _SOURCE_MANIFEST,
            expected_schema="SourceManifest",
            max_bytes=_MAX_CONTRACT_BYTES,
        )
        phase = cast("str", workflow["phase"])
        changeset: dict[str, Any] | None = None
        if phase == "ingested":
            changeset = read_contract_file(
                self._run_root / _CHANGESET,
                expected_schema="ChangeSet",
                max_bytes=_MAX_CONTRACT_BYTES,
            )
        approvals = self._load_approvals(changeset)
        plans = self._load_plans(source_manifest, changeset, approvals)
        latest = approvals[-1] if approvals else None
        current_plan = None
        if latest is not None:
            current_plan = next(
                (item for item in reversed(plans) if item.approval.revision == latest.revision),
                None,
            )

        revised_exists = self._path_exists_as_directory(self._run_root / _REVISED, "revised tree")
        if revised_exists:
            if changeset is None or latest is None or current_plan is None:
                raise ContractError(
                    ErrorCode.HASH_PATCHPLAN_MISMATCH,
                    "revised tree exists without current authorization evidence",
                )
            apply_patch_plan(
                self._run_root / _SNAPSHOT,
                self._run_root / _REVISED,
                patch_plan=current_plan.document,
                unified_diff=current_plan.unified_diff,
                changeset=changeset,
                approval=latest.document,
            )

        verifications = self._load_verifications(
            changeset,
            latest,
            current_plan,
            revised_exists,
        )
        active_verification = verifications[-1] if verifications else None
        verification = active_verification.document if active_verification is not None else None
        verification_directory = (
            active_verification.directory if active_verification is not None else None
        )
        expected_ledger: LedgerOutput | None = None
        if verification is not None and changeset is not None and latest and current_plan:
            expected_ledger = build_ledger(
                changeset,
                latest.document,
                current_plan.document,
                verification,
            )
        ledger_exists = self._validate_ledger(expected_ledger)
        run_manifest = self._load_delivery(
            source_manifest,
            changeset,
            latest,
            current_plan,
            verification,
            verification_directory,
            expected_ledger if ledger_exists else None,
        )
        audit_exists = self._validate_audit(run_manifest, verification)
        return _Evidence(
            workflow=workflow,
            source_manifest=source_manifest,
            changeset=changeset,
            approvals=approvals,
            plans=plans,
            current_plan=current_plan,
            revised_exists=revised_exists,
            verifications=verifications,
            verification=verification,
            verification_directory=verification_directory,
            expected_ledger=expected_ledger,
            ledger_exists=ledger_exists,
            run_manifest=run_manifest,
            audit_exists=audit_exists,
        )

    def _load_approvals(self, changeset: dict[str, Any] | None) -> tuple[_ApprovalVersion, ...]:
        directory = self._run_root / _APPROVALS
        if not directory.exists() and not directory.is_symlink():
            return ()
        if changeset is None:
            raise ContractError(
                ErrorCode.HASH_CHANGESET_MISMATCH,
                "approval directory exists without a ChangeSet",
            )
        root = _require_real_directory(directory, "approval directory")
        versions: list[_ApprovalVersion] = []
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "approvals cannot be listed") from exc
        numbered: list[tuple[int, Path]] = []
        for child in children:
            match = _APPROVAL_RE.fullmatch(child.name)
            if match is None or not child.is_file() or _is_link_or_junction(child):
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "approval directory contains an unexpected entry",
                )
            numbered.append((int(match.group(1)), child))
        numbered.sort(key=lambda item: item[0])
        if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
            raise ContractError(
                ErrorCode.HASH_APPROVAL_MISMATCH,
                "approval revisions are not contiguous",
            )

        previous_hash: str | None = None
        for number, path in numbered:
            document = read_contract_file(
                path,
                expected_schema="ApprovalSet",
                max_bytes=_MAX_CONTRACT_BYTES,
            )
            payload = cast("Mapping[str, Any]", document["payload"])
            if cast("int", payload["revision"]) != number:
                raise ContractError(
                    ErrorCode.HASH_APPROVAL_MISMATCH,
                    "approval filename and payload revision differ",
                )
            supersedes = cast("str | None", payload["supersedes_payload_sha256"])
            if supersedes != previous_hash:
                raise ContractError(
                    ErrorCode.HASH_APPROVAL_MISMATCH,
                    "approval revision chain differs from disk order",
                )
            try:
                finalize_approval_set(
                    changeset,
                    document,
                    generated_at=cast("str", document["generated_at"]),
                )
            except ContractError as exc:
                if exc.code is not ErrorCode.APPROVAL_NOT_FINAL:
                    raise
            payload_sha256 = compute_payload_sha256(document)
            versions.append(
                _ApprovalVersion(
                    revision=number,
                    path=f"{_APPROVALS}/{path.name}",
                    document=document,
                    payload_sha256=payload_sha256,
                )
            )
            previous_hash = payload_sha256
        return tuple(versions)

    def _load_plans(
        self,
        source_manifest: dict[str, Any],
        changeset: dict[str, Any] | None,
        approvals: tuple[_ApprovalVersion, ...],
    ) -> tuple[_PlanVersion, ...]:
        directory = self._run_root / _PLANS
        if not directory.exists() and not directory.is_symlink():
            return ()
        if changeset is None or not approvals:
            raise ContractError(
                ErrorCode.HASH_PATCHPLAN_MISMATCH,
                "plan directory exists without approval evidence",
            )
        root = _require_real_directory(directory, "plan directory")
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "plans cannot be listed") from exc
        numbered: list[tuple[int, Path]] = []
        for child in children:
            match = _PLAN_RE.fullmatch(child.name)
            if match is None or not child.is_dir() or _is_link_or_junction(child):
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "plan directory contains an unexpected entry",
                )
            numbered.append((int(match.group(1)), child))
        numbered.sort(key=lambda item: item[0])
        if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
            raise ContractError(
                ErrorCode.HASH_PATCHPLAN_MISMATCH,
                "plan versions are not contiguous",
            )

        approval_by_hash = {item.payload_sha256: item for item in approvals}
        source_payload = cast("Mapping[str, Any]", source_manifest["payload"])
        changeset_sha256 = compute_payload_sha256(changeset)
        versions: list[_PlanVersion] = []
        self._validate_bound_main(source_manifest)
        for number, plan_root in numbered:
            entries = {item.name for item in plan_root.iterdir()}
            if entries != {"changes.patch", "patch-plan.json"}:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "plan version must contain only patch-plan.json and changes.patch",
                )
            document = read_contract_file(
                plan_root / "patch-plan.json",
                expected_schema="PatchPlan",
                max_bytes=_MAX_CONTRACT_BYTES,
            )
            unified_diff = read_stable_bytes(
                plan_root / "changes.patch",
                max_bytes=_MAX_CONTRACT_BYTES,
            )
            verify_plan_identity(document)
            payload = cast("Mapping[str, Any]", document["payload"])
            approval = approval_by_hash.get(cast("str", payload["approval_set_sha256"]))
            if approval is None:
                raise ContractError(
                    ErrorCode.HASH_APPROVAL_MISMATCH,
                    "PatchPlan approval revision is absent",
                )
            if (
                document["run_id"] != changeset["run_id"]
                or payload["source_manifest_sha256"] != compute_payload_sha256(source_manifest)
                or payload["source_tree_sha256"] != source_payload["source_tree_sha256"]
                or payload["changeset_sha256"] != changeset_sha256
            ):
                raise ContractError(
                    ErrorCode.HASH_PATCHPLAN_MISMATCH,
                    "PatchPlan workflow binding differs",
                )
            diff_ref = cast("Mapping[str, Any] | None", payload["unified_diff"])
            if diff_ref is None:
                if unified_diff:
                    raise ContractError(
                        ErrorCode.HASH_PATCHPLAN_MISMATCH,
                        "noop PatchPlan has unexpected diff bytes",
                    )
                diff_path = f"{_PLANS}/plan-r{number}/changes.patch"
                confidentiality = "derived_private"
            else:
                if digest_bytes(unified_diff) != FileDigest(
                    cast("int", diff_ref["size_bytes"]),
                    cast("str", diff_ref["sha256"]),
                ):
                    raise ContractError(
                        ErrorCode.HASH_PATCHPLAN_MISMATCH,
                        "PatchPlan diff binding differs",
                    )
                diff_path = cast("str", diff_ref["path"])
                confidentiality = cast("str", diff_ref["confidentiality"])
            expected = plan_patch(
                self._run_root / _SNAPSHOT,
                source_tree_sha256=cast("str", source_payload["source_tree_sha256"]),
                changeset=changeset,
                approval=approval.document,
                generated_at=cast("str", document["generated_at"]),
                diff_path=diff_path,
                confidentiality=confidentiality,
            )
            if expected.document != document or expected.unified_diff != unified_diff:
                raise ContractError(
                    ErrorCode.HASH_PATCHPLAN_MISMATCH,
                    "PatchPlan is not reproducible from sealed inputs",
                )
            versions.append(
                _PlanVersion(
                    number=number,
                    directory=f"{_PLANS}/plan-r{number}",
                    document=document,
                    unified_diff=unified_diff,
                    approval=approval,
                    payload_sha256=compute_payload_sha256(document),
                )
            )
        return tuple(versions)

    def _load_verifications(
        self,
        changeset: dict[str, Any] | None,
        approval: _ApprovalVersion | None,
        plan: _PlanVersion | None,
        revised_exists: bool,
    ) -> tuple[_VerificationVersion, ...]:
        base = self._run_root / _VERIFICATION
        retries = self._run_root / _VERIFICATION_RETRIES
        if not base.exists() and not base.is_symlink():
            if retries.exists() or retries.is_symlink():
                raise ContractError(
                    ErrorCode.HASH_INTEGRITY_MISMATCH,
                    "verification retries exist without an initial attempt",
                )
            return ()

        versions = [
            _VerificationVersion(
                attempt=1,
                directory=_VERIFICATION,
                document=self._load_verification_at(
                    base,
                    changeset,
                    approval,
                    plan,
                    revised_exists,
                ),
            )
        ]
        if not retries.exists() and not retries.is_symlink():
            return tuple(versions)
        retry_root = _require_real_directory(retries, "verification retry directory")
        try:
            children = sorted(retry_root.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "verification retries cannot be listed",
            ) from exc
        numbered: list[tuple[int, Path]] = []
        for child in children:
            match = _VERIFICATION_RETRY_RE.fullmatch(child.name)
            if match is None or not child.is_dir() or _is_link_or_junction(child):
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "verification retry directory contains an unexpected entry",
                )
            numbered.append((int(match.group(1)), child))
        numbered.sort(key=lambda item: item[0])
        if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
            raise ContractError(
                ErrorCode.HASH_INTEGRITY_MISMATCH,
                "verification retry revisions are not contiguous",
            )
        for number, root in numbered:
            versions.append(
                _VerificationVersion(
                    attempt=number + 1,
                    directory=f"{_VERIFICATION_RETRIES}/retry-r{number}",
                    document=self._load_verification_at(
                        root,
                        changeset,
                        approval,
                        plan,
                        revised_exists,
                    ),
                )
            )
        return tuple(versions)

    def _load_verification_at(
        self,
        root: Path,
        changeset: dict[str, Any] | None,
        approval: _ApprovalVersion | None,
        plan: _PlanVersion | None,
        revised_exists: bool,
    ) -> dict[str, Any]:
        if not revised_exists or changeset is None or approval is None or plan is None:
            raise ContractError(
                ErrorCode.HASH_PATCHPLAN_MISMATCH,
                "verification exists without current applied evidence",
            )
        verification_root = _require_real_directory(root, "verification directory")
        report = read_contract_file(
            verification_root / "verification-report.json",
            expected_schema="VerificationReport",
            max_bytes=_MAX_CONTRACT_BYTES,
        )
        # build_ledger is also the public full binding validator for a
        # VerificationReport; the returned ledger is discarded at this point.
        build_ledger(changeset, approval.document, plan.document, report)
        for artifact in _collect_artifact_refs(report.get("payload")):
            if artifact.get("path_base") != "run_root" or artifact.get("immutable") is not True:
                raise ContractError(
                    ErrorCode.HASH_INTEGRITY_MISMATCH,
                    "verification artifact policy differs",
                )
            relative = validate_relative_path(cast("str", artifact["path"]))
            target = resolve_within(verification_root, relative)
            _require_regular_file(target, "verification artifact")
            observed = digest_file(target, max_bytes=_MAX_ARTIFACT_BYTES)
            if observed != FileDigest(
                cast("int", artifact["size_bytes"]), cast("str", artifact["sha256"])
            ):
                raise ContractError(
                    ErrorCode.HASH_INTEGRITY_MISMATCH,
                    "verification artifact digest differs",
                )
        return report

    def _validate_ledger(self, expected: LedgerOutput | None) -> bool:
        root = self._run_root / _LEDGER
        if not root.exists() and not root.is_symlink():
            return False
        if expected is None:
            raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "ledger lacks verification")
        directory = _require_real_directory(root, "ledger directory")
        entries = {item.name for item in directory.iterdir()}
        if entries != {"ledger.html", "ledger.json"}:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "ledger directory shape differs")
        if read_stable_bytes(directory / "ledger.json", max_bytes=_MAX_CONTRACT_BYTES) != (
            expected.json_bytes
        ):
            raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "ledger JSON differs")
        if read_stable_bytes(directory / "ledger.html", max_bytes=_MAX_CONTRACT_BYTES) != (
            expected.html_bytes
        ):
            raise ContractError(ErrorCode.HASH_INTEGRITY_MISMATCH, "ledger HTML differs")
        return True

    def _load_delivery(
        self,
        source_manifest: dict[str, Any],
        changeset: dict[str, Any] | None,
        approval: _ApprovalVersion | None,
        plan: _PlanVersion | None,
        verification: dict[str, Any] | None,
        verification_directory: str | None,
        ledger: LedgerOutput | None,
    ) -> dict[str, Any] | None:
        root = self._run_root / _DELIVERY
        if not root.exists() and not root.is_symlink():
            return None
        if None in (
            changeset,
            approval,
            plan,
            verification,
            verification_directory,
            ledger,
        ):
            raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "delivery lacks bound evidence")
        assert changeset is not None
        assert approval is not None
        assert plan is not None
        assert verification is not None
        assert verification_directory is not None
        assert ledger is not None
        verification_root = resolve_within(
            self._run_root,
            validate_relative_path(verification_directory),
        )
        directory = _require_real_directory(root, "delivery directory")
        run_manifest = read_contract_file(
            directory / "run-manifest.json",
            expected_schema="RunManifest",
            max_bytes=_MAX_CONTRACT_BYTES,
        )
        if run_manifest["run_id"] != source_manifest["run_id"]:
            raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "delivery run binding differs")
        payload = cast("Mapping[str, Any]", run_manifest["payload"])
        bindings = {
            (
                cast("str", item["schema_name"]),
                cast("str", item["object_id"]),
                cast("str", item["payload_sha256"]),
            )
            for item in cast("Sequence[Mapping[str, Any]]", payload["object_bindings"])
        }
        required = (source_manifest, changeset, approval.document, plan.document, verification)
        for document in required:
            receipt = validate_contract(document)
            key = (receipt.schema_name, receipt.object_id, receipt.payload_sha256)
            if key not in bindings:
                raise ContractError(
                    ErrorCode.BUNDLE_HASH_MISMATCH,
                    "delivery RunManifest omits current evidence",
                )
        expected_files: dict[str, bytes] = {
            "verification-report.json": canonical_json(verification) + b"\n",
            "ledger.json": ledger.json_bytes,
            "ledger.html": ledger.html_bytes,
        }
        verification_payload = cast("Mapping[str, Any]", verification["payload"])
        for artifact in cast("Sequence[Mapping[str, Any]]", verification_payload["deliverables"]):
            relative = validate_relative_path(cast("str", artifact["path"]))
            expected_files[relative] = read_stable_bytes(
                resolve_within(verification_root, relative),
                max_bytes=_MAX_ARTIFACT_BYTES,
            )
        expected_names = {*expected_files, "run-manifest.json"}
        observed_names = {
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file()
        }
        if observed_names != expected_names:
            raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "delivery file set differs")
        for relative, data in expected_files.items():
            if (
                read_stable_bytes(
                    resolve_within(directory, relative), max_bytes=_MAX_ARTIFACT_BYTES
                )
                != data
            ):
                raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "delivery artifact differs")
        return run_manifest

    def _validate_audit(
        self,
        run_manifest: dict[str, Any] | None,
        verification: dict[str, Any] | None,
    ) -> bool:
        path = self._run_root / _AUDIT_BUNDLE
        if not path.exists() and not path.is_symlink():
            return False
        if run_manifest is None or verification is None:
            raise ContractError(ErrorCode.BUNDLE_HASH_MISMATCH, "audit ZIP lacks delivery evidence")
        _require_regular_file(path, "audit bundle")
        verify_audit_bundle(
            path,
            expected_run_manifest=run_manifest,
            expected_verification_report=verification,
        )
        return True

    def _status_from_evidence(self, evidence: _Evidence) -> SessionStatus:
        source_payload = cast("Mapping[str, Any]", evidence.source_manifest["payload"])
        main_document = cast("str", source_payload["main_document"])
        artifacts: dict[str, str] = {
            "source_manifest": _SOURCE_MANIFEST,
            "source_snapshot": _SNAPSHOT,
            "main_document": f"{_SNAPSHOT}/{main_document}",
        }
        workflow_phase = cast("str", evidence.workflow["phase"])
        if workflow_phase in {"exported", "ingested"}:
            artifacts["review_docx"] = _REVIEW_DOCX
        if workflow_phase == "ingested":
            artifacts["changeset"] = _CHANGESET
            artifacts["returned_original"] = _RETURNED_ORIGINAL

        approval_summary: dict[str, Any] | None = None
        approval = evidence.latest_approval
        if approval is not None:
            payload = cast("Mapping[str, Any]", approval.document["payload"])
            approval_summary = {
                "path": approval.path,
                "revision": approval.revision,
                "status": payload["status"],
                "summary": dict(cast("Mapping[str, Any]", payload["decision_summary"])),
            }
            artifacts["approval"] = approval.path

        plan_summary: dict[str, Any] | None = None
        plan = evidence.current_plan
        if plan is not None:
            payload = cast("Mapping[str, Any]", plan.document["payload"])
            plan_summary = {
                "path": f"{plan.directory}/patch-plan.json",
                "diff_path": f"{plan.directory}/changes.patch",
                "number": plan.number,
                "status": payload["status"],
                "summary": dict(cast("Mapping[str, Any]", payload["summary"])),
                "approval_revision": plan.approval.revision,
                "payload_sha256": plan.payload_sha256,
            }
            artifacts["patch_plan"] = cast("str", plan_summary["path"])
            artifacts["patch_diff"] = cast("str", plan_summary["diff_path"])

        blockers: list[dict[str, Any]] = []
        confirmation: dict[str, Any] | None = None
        if workflow_phase == "snapshotted":
            phase = "ready_to_export"
            step: Literal[1, 2, 3, 4] = 1
            next_action = "generate_review_word"
        elif workflow_phase == "exported":
            phase = "waiting_for_return"
            step = 2
            next_action = "import_returned_word"
        elif approval is None:
            phase = "approval_required"
            step = 3
            next_action = "start_approval"
        else:
            approval_payload = cast("Mapping[str, Any]", approval.document["payload"])
            decision_summary = cast("Mapping[str, Any]", approval_payload["decision_summary"])
            pending = cast("int", decision_summary["pending"])
            if approval_payload["status"] != "final":
                step = 3
                if pending:
                    phase = "approval_in_progress"
                    next_action = "decide_remaining_changes"
                    blockers.append({"code": "approval_pending", "count": pending})
                else:
                    phase = "approval_ready_to_finalize"
                    next_action = "finalize_approval"
                    blockers.append({"code": ErrorCode.APPROVAL_NOT_FINAL.value})
            elif plan is None:
                phase = "ready_to_plan"
                step = 3
                next_action = "prepare_patch_preview"
            else:
                plan_payload = cast("Mapping[str, Any]", plan.document["payload"])
                if plan_payload["status"] == "blocked":
                    phase = "plan_blocked"
                    step = 3
                    next_action = "revise_approval"
                    for item in cast(
                        "Sequence[Mapping[str, Any]]", plan_payload["accepted_but_blocked"]
                    ):
                        blockers.append(dict(item))
                elif not evidence.revised_exists:
                    phase = "awaiting_apply_confirmation"
                    step = 4
                    next_action = "confirm_patch_preview"
                    confirmation = {
                        "required": True,
                        "patch_plan_sha256": plan.payload_sha256,
                    }
                elif evidence.verification is None:
                    phase = "applied"
                    step = 4
                    next_action = "verify_results"
                else:
                    verification_directory = evidence.verification_directory
                    if verification_directory is None:
                        raise ContractError(
                            ErrorCode.INTERNAL_INVARIANT,
                            "verification directory is unavailable",
                        )
                    artifacts["revised_source"] = _REVISED
                    artifacts["verification_report"] = (
                        f"{verification_directory}/verification-report.json"
                    )
                    verification_payload = cast(
                        "Mapping[str, Any]", evidence.verification["payload"]
                    )
                    for item in cast(
                        "Sequence[Mapping[str, Any]]", verification_payload["deliverables"]
                    ):
                        role = cast("str", item["role"])
                        artifacts[role] = f"{verification_directory}/{item['path']}"
                    verification_status = cast("str", verification_payload["status"])
                    if verification_status != "pass":
                        phase = "partially_completed"
                        step = 4
                        retry_available = not (
                            evidence.ledger_exists
                            or evidence.run_manifest is not None
                            or evidence.audit_exists
                        )
                        next_action = (
                            "retry_verification" if retry_available else "inspect_partial_results"
                        )
                        blockers.append(
                            {
                                "code": f"verification_{verification_status}",
                                "attempt": len(evidence.verifications),
                                "retry_available": retry_available,
                            }
                        )
                    elif not evidence.ledger_exists:
                        phase = "verified"
                        step = 4
                        next_action = "build_ledger"
                    elif not evidence.audit_exists:
                        phase = "ready_to_bundle"
                        step = 4
                        next_action = "create_audit_bundle"
                    else:
                        phase = "completed"
                        step = 4
                        next_action = "open_results"

        if evidence.revised_exists:
            artifacts["revised_source"] = _REVISED
        if evidence.ledger_exists:
            artifacts["ledger_json"] = f"{_LEDGER}/ledger.json"
            artifacts["ledger_html"] = f"{_LEDGER}/ledger.html"
        if evidence.run_manifest is not None:
            artifacts["run_manifest"] = f"{_DELIVERY}/run-manifest.json"
        if evidence.audit_exists:
            artifacts["audit_bundle"] = _AUDIT_BUNDLE
        return SessionStatus(
            run_id=cast("str", evidence.source_manifest["run_id"]),
            main_document=main_document,
            phase=cast("Any", phase),
            step=step,
            next_action=next_action,
            artifacts=artifacts,
            approval=approval_summary,
            plan=plan_summary,
            blockers=tuple(blockers),
            apply_confirmation=confirmation,
        )

    def _publish_next_approval(
        self,
        changeset: dict[str, Any],
        current: _ApprovalVersion,
        updated: dict[str, Any],
    ) -> None:
        revision = cast("int", cast("Mapping[str, Any]", updated["payload"])["revision"])
        if revision == current.revision:
            if updated != current.document:
                raise ContractError(
                    ErrorCode.HASH_APPROVAL_MISMATCH,
                    "idempotent approval result differs",
                )
            return
        if revision != current.revision + 1:
            raise ContractError(
                ErrorCode.HASH_APPROVAL_MISMATCH,
                "approval core returned a non-contiguous revision",
            )
        directory = _require_real_directory(self._run_root / _APPROVALS, "approval directory")
        write_approval_json(changeset, updated, directory / f"approval-r{revision}.json")

        published = read_contract_file(
            directory / f"approval-r{revision}.json",
            expected_schema="ApprovalSet",
            max_bytes=_MAX_CONTRACT_BYTES,
        )
        if published != updated:
            raise ContractError(
                ErrorCode.HASH_APPROVAL_MISMATCH,
                "published approval revision differs from the validated decision",
            )

    def _publish_plan(self, destination: Path, plan: PatchPlanResult) -> None:
        def writer(staged: Path) -> None:
            write_new_json(staged / "patch-plan.json", plan.document, contract=True)
            write_new_bytes(staged / "changes.patch", plan.unified_diff)

        _publish_directory(destination, purpose="plan", writer=writer)

    def _publish_delivery(self, evidence: _Evidence, *, generated_at: str | None) -> None:
        changeset = self._require_changeset(evidence)
        approval = self._require_latest_approval(evidence)
        plan = evidence.current_plan
        verification = evidence.verification
        verification_directory = evidence.verification_directory
        ledger = evidence.expected_ledger
        if plan is None or verification is None or verification_directory is None or ledger is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "delivery evidence is incomplete")
        verification_root = resolve_within(
            self._run_root,
            validate_relative_path(verification_directory),
        )
        export_capabilities = read_contract_file(
            self._run_root / "export/objects/backend-capabilities.json",
            expected_schema="BackendCapabilities",
        )
        reader_capabilities = read_contract_file(
            self._run_root / "receive/revision-reader.json",
            expected_schema="BackendCapabilities",
        )
        objects = [
            read_contract_file(
                self._run_root / "export/objects/review-ir.json", expected_schema="ReviewIR"
            ),
            read_contract_file(
                self._run_root / "export/objects/source-map.json", expected_schema="SourceMap"
            ),
            read_contract_file(
                self._run_root / "export/objects/export-report.json",
                expected_schema="ExportReport",
            ),
            changeset,
            approval.document,
            plan.document,
            verification,
        ]
        ledger_artifacts = (
            _artifact_ref(
                "ledger.json",
                "review_ledger_json",
                ledger.json_bytes,
                "application/json",
            ),
            _artifact_ref("ledger.html", "review_ledger_html", ledger.html_bytes, "text/html"),
        )
        verification_payload = cast("Mapping[str, Any]", verification["payload"])
        verification_status = cast("str", verification_payload["status"])
        manifest_status: Literal["blocked", "completed"] = (
            "completed" if verification_status == "pass" else "blocked"
        )
        run_manifest = build_run_manifest_document(
            evidence.source_manifest,
            objects=objects,
            backend_capabilities=[export_capabilities, reader_capabilities],
            artifacts=ledger_artifacts,
            current_phase="verified",
            status=manifest_status,
            generated_at=generated_at,
        )
        files: dict[str, bytes] = {
            "verification-report.json": canonical_json(verification) + b"\n",
            "ledger.json": ledger.json_bytes,
            "ledger.html": ledger.html_bytes,
            "run-manifest.json": canonical_json(run_manifest) + b"\n",
        }
        for artifact in cast("Sequence[Mapping[str, Any]]", verification_payload["deliverables"]):
            relative = validate_relative_path(cast("str", artifact["path"]))
            files[relative] = read_stable_bytes(
                resolve_within(verification_root, relative),
                max_bytes=_MAX_ARTIFACT_BYTES,
            )

        def writer(staged: Path) -> None:
            for relative, data in sorted(files.items()):
                target = staged.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                write_new_bytes(target, data)

        _publish_directory(self._run_root / _DELIVERY, purpose="delivery", writer=writer)

    def _bundle_items(
        self,
        run_manifest: dict[str, Any],
        verification: dict[str, Any],
    ) -> list[BundleItem]:
        items = [
            BundleItem(
                "verification-report.json",
                "verification_report",
                verification,
                "$document",
            )
        ]
        verification_payload = cast("Mapping[str, Any]", verification["payload"])
        for artifact in cast("Sequence[Mapping[str, Any]]", verification_payload["deliverables"]):
            items.append(
                BundleItem(
                    cast("str", artifact["path"]),
                    cast("str", artifact["role"]),
                    verification,
                    cast("str", artifact["artifact_id"]),
                )
            )
        manifest_payload = cast("Mapping[str, Any]", run_manifest["payload"])
        artifacts = cast("Sequence[Mapping[str, Any]]", manifest_payload["artifacts"])
        for path, role in (
            ("ledger.json", "review_ledger_json"),
            ("ledger.html", "review_ledger_html"),
        ):
            artifact = next(
                item for item in artifacts if item["path"] == path and item["role"] == role
            )
            items.append(BundleItem(path, role, run_manifest, cast("str", artifact["artifact_id"])))
        items.append(BundleItem("run-manifest.json", "run_manifest", run_manifest, "$document"))
        return items

    def _validate_bound_main(self, source_manifest: dict[str, Any]) -> None:
        payload = cast("Mapping[str, Any]", source_manifest["payload"])
        main_document = cast("str", payload["main_document"])
        discovery = discover_project(
            self._run_root / _SNAPSHOT,
            main_document=main_document,
        )
        if (
            discovery.main_document != main_document
            or discovery.source_tree_sha256 != payload["source_tree_sha256"]
        ):
            raise ContractError(
                ErrorCode.HASH_SOURCE_MISMATCH,
                "selected main document no longer matches the sealed snapshot",
            )

    @staticmethod
    def _require_changeset(evidence: _Evidence) -> dict[str, Any]:
        if evidence.changeset is None:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "returned Word must be ingested first")
        return evidence.changeset

    @staticmethod
    def _require_latest_approval(evidence: _Evidence) -> _ApprovalVersion:
        approval = evidence.latest_approval
        if approval is None:
            raise ContractError(ErrorCode.APPROVAL_NOT_FINAL, "approval has not started")
        return approval

    @staticmethod
    def _require_not_applied(evidence: _Evidence) -> None:
        if evidence.revised_exists:
            raise ContractError(ErrorCode.SCHEMA_INVALID, "applied sessions are immutable")

    @staticmethod
    def _path_exists_as_directory(path: Path, label: str) -> bool:
        if not path.exists() and not path.is_symlink():
            return False
        _require_real_directory(path, label)
        return True


__all__ = ["ApplicationSession", "ContentClassification"]
