"""Tests for non-authoritative background product jobs."""

from __future__ import annotations

import threading

import pytest

from latex_word_review.app_jobs import JobManager, ProgressReporter
from latex_word_review.errors import ContractError, ErrorCode


def test_job_reports_progress_and_returns_an_immutable_snapshot() -> None:
    def task(progress: ProgressReporter) -> dict[str, object]:
        progress.update(25, "正在读取论文")
        progress.update(80, "正在核验")
        return {"artifact": "export/review.docx", "nested": {"ready": True}}

    with JobManager(max_workers=1) as manager:
        queued = manager.submit("run_demo", "export", task)
        completed = manager.wait(queued.job_id, timeout=2)
        first = completed.as_dict()
        first["result"]["nested"]["ready"] = False
        second = manager.snapshot(queued.job_id).as_dict()

    assert completed.status == "succeeded"
    assert completed.progress == 100
    assert completed.stage == "已完成"
    assert second["result"]["nested"]["ready"] is True


def test_only_one_writer_is_allowed_for_each_session() -> None:
    started = threading.Event()
    release = threading.Event()

    def held_task(_: ProgressReporter) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=2)
        return {}

    with JobManager(max_workers=2) as manager:
        first = manager.submit("run_same", "export", held_task)
        assert started.wait(timeout=2)
        with pytest.raises(ContractError) as captured:
            manager.submit("run_same", "receive", held_task)
        assert captured.value.code is ErrorCode.SCHEMA_INVALID
        assert manager.has_active_jobs() is True
        assert manager.active_for("run_same") is not None
        release.set()
        assert manager.wait(first.job_id, timeout=2).status == "succeeded"
        assert manager.has_active_jobs() is False
        assert manager.active_for("run_same") is None


def test_contract_error_is_rendered_as_a_safe_chinese_error() -> None:
    def task(_: ProgressReporter) -> dict[str, object]:
        raise ContractError(
            ErrorCode.REVISION_BASELINE_DRIFT,
            r"returned document C:\private\paper.docx reject-view differs",
        )

    with JobManager(max_workers=1) as manager:
        job = manager.submit("run_bad_word", "receive", task)
        completed = manager.wait(job.job_id, timeout=2)

    assert completed.status == "failed"
    assert completed.error is not None
    assert completed.error["code"] == ErrorCode.REVISION_BASELINE_DRIFT.value
    assert completed.error["title"] == "返回稿无法安全核验"
    assert "private" not in str(completed.error)


def test_contract_error_carries_only_allowlisted_path_free_diagnostics() -> None:
    def task(_: ProgressReporter) -> dict[str, object]:
        raise ContractError(
            ErrorCode.BACKEND_FAILED,
            "backend details stay private",
            details={
                "provider": "tex2word",
                "stage": "backend_export",
                "timed_out": "yes",
                "returncode": -9,
                "duration_ms": 60_001,
                "path": r"C:\private\paper.tex",
                "failure_kind": r"unsafe\private",
            },
        )

    with JobManager(max_workers=1) as manager:
        job = manager.submit("run_backend", "export", task)
        completed = manager.wait(job.job_id, timeout=2)

    assert completed.error is not None
    assert completed.error["diagnostics"] == {
        "provider": "tex2word",
        "stage": "backend_export",
        "timed_out": "yes",
        "returncode": -9,
        "duration_ms": 60_001,
    }
    assert "private\\paper" not in str(completed.error)
    assert "unsafe\\private" not in str(completed.error)


def test_image_export_error_keeps_a_visible_path_free_summary() -> None:
    def task(_: ProgressReporter) -> dict[str, object]:
        raise ContractError(
            ErrorCode.BACKEND_CAPABILITY_MISSING,
            "private image diagnostic details",
            details={
                "manifest_path": r"C:\private\overlay.json",
                "diagnostics": [
                    {
                        "diagnostic_id": "image_diag_" + "a" * 64,
                        "code": "E_PATH_TRAVERSAL",
                        "message": "private first message",
                        "source_path": "chapters/private-first.tex",
                        "line": 464,
                        "column": 5,
                        "disposition": "manual",
                    },
                    {
                        "diagnostic_id": "image_diag_" + "b" * 64,
                        "code": "E_PATH_TRAVERSAL",
                        "message": "private second message",
                        "source_path": "chapters/private-second.tex",
                        "line": 490,
                        "column": 9,
                        "disposition": "manual",
                    },
                ],
            },
        )

    with JobManager(max_workers=1) as manager:
        job = manager.submit("run_images", "export_review", task)
        completed = manager.wait(job.job_id, timeout=2)

    assert completed.error is not None
    assert completed.error["title"] == "论文中有内容当前无法自动处理"
    assert completed.error["summary"] == "2处图片写法当前无法处理"
    assert completed.error["diagnostics"] == {
        "provider": "image_overlay",
        "stage": "image_overlay",
        "failure_kind": "manual_image_syntax",
        "finding_count": 2,
        "image_issue_summary": "2处图片写法当前无法处理",
        "image_issue_1": "第464行，第5列（E_PATH_TRAVERSAL）",
        "image_issue_2": "第490行，第9列（E_PATH_TRAVERSAL）",
    }
    assert "private" not in str(completed.error)


def test_unexpected_error_does_not_expose_its_message() -> None:
    def task(_: ProgressReporter) -> dict[str, object]:
        raise RuntimeError("private absolute path must not leave the process")

    with JobManager(max_workers=1) as manager:
        job = manager.submit("run_internal", "export", task)
        completed = manager.wait(job.job_id, timeout=2)

    assert completed.status == "failed"
    assert completed.error is not None
    assert completed.error["technical_message"] == "RuntimeError"
    assert completed.error["diagnostics"] == {
        "stage": "export",
        "exception_type": "RuntimeError",
    }
    assert "private absolute path" not in str(completed.error)


def test_progress_cannot_move_backwards_or_claim_completion() -> None:
    def task(progress: ProgressReporter) -> dict[str, object]:
        progress.update(50, "一半")
        with pytest.raises(ContractError):
            progress.update(49, "倒退")
        with pytest.raises(ContractError):
            progress.update(100, "伪造完成")
        return {}

    with JobManager(max_workers=1) as manager:
        job = manager.submit("run_progress", "verify", task)
        completed = manager.wait(job.job_id, timeout=2)

    assert completed.status == "succeeded"


def test_unknown_job_and_closed_manager_fail_closed() -> None:
    manager = JobManager(max_workers=1)
    with pytest.raises(ContractError):
        manager.snapshot("job_missing")
    manager.close()
    with pytest.raises(ContractError) as captured:
        manager.submit("run_closed", "export", lambda _: {})
    assert captured.value.code is ErrorCode.INTERNAL_INVARIANT


@pytest.mark.parametrize(
    ("session_key", "operation"),
    [
        ("", "export"),
        ("run", ""),
        ("run\nunsafe", "export"),
        ("run", "x" * 97),
    ],
)
def test_job_labels_are_bounded(session_key: str, operation: str) -> None:
    with JobManager(max_workers=1) as manager, pytest.raises(ContractError):
        manager.submit(session_key, operation, lambda _: {})
