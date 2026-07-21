"""Bounded in-process jobs for the local Windows product surface.

Jobs are a convenience layer only. Durable workflow state is always rebuilt
from sealed artifacts after a restart.
"""

from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, cast

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.user_messages import safe_diagnostic_details, user_message_for_error

JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobTask = Callable[["ProgressReporter"], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """JSON-ready snapshot of one non-authoritative background job."""

    job_id: str
    session_key: str
    operation: str
    status: JobStatus
    progress: int
    stage: str
    result: dict[str, Any] | None
    error: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "session_key": self.session_key,
            "operation": self.operation,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "result": copy.deepcopy(self.result),
            "error": copy.deepcopy(self.error),
        }


@dataclass(slots=True)
class _JobRecord:
    job_id: str
    session_key: str
    operation: str
    status: JobStatus
    progress: int
    stage: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    future: Future[None] | None = None


class ProgressReporter:
    """Narrow capability handed to a worker for bounded progress updates."""

    __slots__ = ("_job_id", "_manager")

    def __init__(self, manager: JobManager, job_id: str) -> None:
        self._manager = manager
        self._job_id = job_id

    def update(self, progress: int, stage: str) -> None:
        self._manager._update(self._job_id, progress, stage)


class JobManager:
    """Run bounded jobs while allowing at most one writer per session."""

    def __init__(self, *, max_workers: int = 2) -> None:
        if max_workers < 1 or max_workers > 8:
            raise ValueError("max_workers must be in [1, 8]")
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="latex-word-review",
        )
        self._jobs: dict[str, _JobRecord] = {}
        self._active_by_session: dict[str, str] = {}
        self._closed = False

    @staticmethod
    def _require_label(value: str, label: str, *, max_length: int) -> str:
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > max_length
            or any(ord(character) < 0x20 for character in normalized)
        ):
            raise ContractError(ErrorCode.SCHEMA_INVALID, f"invalid {label}")
        return normalized

    def submit(self, session_key: str, operation: str, task: JobTask) -> JobSnapshot:
        """Queue one task and reject concurrent writers for the same session."""

        key = self._require_label(session_key, "session key", max_length=256)
        name = self._require_label(operation, "operation", max_length=96)
        with self._lock:
            if self._closed:
                raise ContractError(ErrorCode.INTERNAL_INVARIANT, "job manager is closed")
            if key in self._active_by_session:
                raise ContractError(
                    ErrorCode.SCHEMA_INVALID,
                    "another operation is already active for this review session",
                )
            job_id = f"job_{uuid.uuid4().hex}"
            record = _JobRecord(job_id, key, name, "queued", 0, "等待开始")
            self._jobs[job_id] = record
            self._active_by_session[key] = job_id
            record.future = self._executor.submit(self._execute, job_id, task)
            return self._snapshot(record)

    def _execute(self, job_id: str, task: JobTask) -> None:
        with self._lock:
            record = self._jobs[job_id]
            record.status = "running"
            record.stage = "正在处理"
        reporter = ProgressReporter(self, job_id)
        try:
            result = dict(task(reporter))
        except ContractError as exc:
            # Job snapshots may be rendered by the local UI. Keep detailed
            # exception text (which can contain a private path) inside the
            # process and expose only the stable public code.
            message = user_message_for_error(
                exc.code,
                technical_message=exc.code.value,
            )
            with self._lock:
                record = self._jobs[job_id]
                record.status = "failed"
                diagnostics = safe_diagnostic_details(
                    {
                        "stage": record.operation,
                        **(exc.violation.details or {}),
                    }
                )
                error: dict[str, Any] = {
                    **message.as_dict(),
                    "diagnostics": diagnostics,
                }
                summary = diagnostics.get("image_issue_summary")
                if isinstance(summary, str):
                    error["summary"] = summary
                record.error = error
                record.stage = message.title
        except Exception as exc:  # pragma: no cover - exact exception types are task-defined
            message = user_message_for_error(
                ErrorCode.INTERNAL_INVARIANT,
                technical_message=type(exc).__name__,
            )
            with self._lock:
                record = self._jobs[job_id]
                record.status = "failed"
                diagnostic_source: dict[str, object] = {
                    "stage": record.operation,
                    "exception_type": type(exc).__name__,
                }
                winerror = getattr(exc, "winerror", None)
                if isinstance(winerror, int) and not isinstance(winerror, bool):
                    diagnostic_source["winerror"] = winerror
                record.error = {
                    **message.as_dict(),
                    "diagnostics": safe_diagnostic_details(diagnostic_source),
                }
                record.stage = message.title
        else:
            with self._lock:
                record = self._jobs[job_id]
                record.status = "succeeded"
                record.progress = 100
                record.stage = "已完成"
                record.result = copy.deepcopy(result)
        finally:
            with self._lock:
                record = self._jobs[job_id]
                if self._active_by_session.get(record.session_key) == job_id:
                    del self._active_by_session[record.session_key]

    def _update(self, job_id: str, progress: int, stage: str) -> None:
        if progress < 0 or progress > 99:
            raise ContractError(
                ErrorCode.SCHEMA_INVALID,
                "running job progress must be in [0, 99]",
            )
        normalized_stage = self._require_label(stage, "job stage", max_length=160)
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.status != "running":
                raise ContractError(ErrorCode.SCHEMA_INVALID, "job is not running")
            if progress < record.progress:
                raise ContractError(ErrorCode.SCHEMA_INVALID, "job progress cannot move backwards")
            record.progress = progress
            record.stage = normalized_stage

    @staticmethod
    def _snapshot(record: _JobRecord) -> JobSnapshot:
        return JobSnapshot(
            job_id=record.job_id,
            session_key=record.session_key,
            operation=record.operation,
            status=record.status,
            progress=record.progress,
            stage=record.stage,
            result=copy.deepcopy(record.result),
            error=copy.deepcopy(record.error),
        )

    def snapshot(self, job_id: str) -> JobSnapshot:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise ContractError(ErrorCode.SCHEMA_INVALID, "unknown application job")
            return self._snapshot(record)

    def active_for(self, session_key: str) -> JobSnapshot | None:
        with self._lock:
            job_id = self._active_by_session.get(session_key)
            if job_id is None:
                return None
            return self._snapshot(self._jobs[job_id])

    def has_active_jobs(self) -> bool:
        """Return whether shutdown must wait for any local writer."""

        with self._lock:
            return bool(self._active_by_session)

    def wait(self, job_id: str, *, timeout: float | None = None) -> JobSnapshot:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise ContractError(ErrorCode.SCHEMA_INVALID, "unknown application job")
            future = cast("Future[None]", record.future)
        future.result(timeout=timeout)
        return self.snapshot(job_id)

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def __enter__(self) -> JobManager:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["JobManager", "JobSnapshot", "JobStatus", "JobTask", "ProgressReporter"]
