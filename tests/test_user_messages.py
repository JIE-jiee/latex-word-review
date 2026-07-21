"""Tests for stable machine-error to Chinese product-message mapping."""

from __future__ import annotations

from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.user_messages import (
    format_safe_technical_details,
    safe_diagnostic_details,
    user_message_for_error,
)


def test_every_public_error_code_has_a_product_message() -> None:
    messages = {code: user_message_for_error(code) for code in ErrorCode}

    assert set(messages) == set(ErrorCode)
    assert all(message.code == code.value for code, message in messages.items())
    assert all(message.title for message in messages.values())
    assert all(message.explanation for message in messages.values())
    assert all(message.safety for message in messages.values())
    assert all(message.action_label for message in messages.values())


def test_returned_word_drift_gives_actionable_fail_closed_message() -> None:
    message = user_message_for_error(
        ContractError(
            ErrorCode.REVISION_BASELINE_DRIFT,
            "reject view differs from export baseline",
        )
    )

    assert message.title == "返回稿无法安全核验"
    assert message.action == "choose_returned_word"
    assert "不会自动回填" in message.safety
    assert message.technical_message is not None
    assert ErrorCode.REVISION_BASELINE_DRIFT.value in message.technical_message


def test_missing_tex_preserves_partial_result_language() -> None:
    message = user_message_for_error(ErrorCode.VERIFY_COMPILE_FAILED)

    assert message.title == "LaTeX 已生成，但 PDF 未完成"
    assert message.action == "install_tool"
    assert "LaTeX 副本" in message.safety


def test_unknown_code_falls_back_to_internal_support_message() -> None:
    message = user_message_for_error("E_FUTURE_UNKNOWN", technical_message="future")

    assert message.code == ErrorCode.INTERNAL_INVARIANT.value
    assert message.action == "create_support_bundle"
    assert message.technical_message == "future"


def test_safe_diagnostics_are_bounded_allowlisted_and_path_free() -> None:
    details = {
        "provider": "tex2word",
        "failure_kind": "timeout",
        "returncode": -9,
        "timed_out": "yes",
        "path": r"C:\private\paper.tex",
        "exception_type": r"bad\private",
        "name": "C:private",
        "unknown": "not rendered",
        "output_truncated": False,
    }

    safe = safe_diagnostic_details(details)
    rendered = format_safe_technical_details(ErrorCode.BACKEND_FAILED, details)

    assert safe == {
        "provider": "tex2word",
        "failure_kind": "timeout",
        "timed_out": "yes",
        "returncode": -9,
    }
    assert ErrorCode.BACKEND_FAILED.value in rendered
    assert "timeout" in rendered
    assert "private" not in rendered
    assert "not rendered" not in rendered


def test_image_diagnostics_project_locations_without_paths_or_messages() -> None:
    details = {
        "manifest_path": r"C:\private\overlay.json",
        "diagnostics": [
            {
                "diagnostic_id": "image_diag_" + "a" * 64,
                "code": "E_PATH_TRAVERSAL",
                "message": r"dynamic target C:\private\first.pdf is not resolvable",
                "source_path": "chapters/private-first.tex",
                "line": 464,
                "column": 5,
                "disposition": "manual",
            },
            {
                "diagnostic_id": "image_diag_" + "b" * 64,
                "code": "E_PATH_TRAVERSAL",
                "message": "private second image message",
                "source_path": "chapters/private-second.tex",
                "line": 490,
                "column": 9,
                "disposition": "manual",
            },
        ],
    }

    safe = safe_diagnostic_details(details)
    rendered = format_safe_technical_details(ErrorCode.BACKEND_CAPABILITY_MISSING, safe)

    assert safe == {
        "provider": "image_overlay",
        "stage": "image_overlay",
        "failure_kind": "manual_image_syntax",
        "finding_count": 2,
        "image_issue_summary": "2处图片写法当前无法处理",
        "image_issue_1": "第464行，第5列（E_PATH_TRAVERSAL）",
        "image_issue_2": "第490行，第9列（E_PATH_TRAVERSAL）",
    }
    assert safe_diagnostic_details(safe) == safe
    assert "问题摘要：2处图片写法当前无法处理" in rendered
    assert "问题位置 1：第464行，第5列（E_PATH_TRAVERSAL）" in rendered
    assert "问题位置 2：第490行，第9列（E_PATH_TRAVERSAL）" in rendered
    assert "private" not in rendered


def test_malformed_nested_image_diagnostics_fail_closed() -> None:
    details = {
        "diagnostics": [
            {
                "diagnostic_id": "not-a-real-id",
                "code": "E_PATH_TRAVERSAL",
                "line": 1,
                "column": 1,
                "disposition": "manual",
                "source_path": "private.tex",
            }
        ]
    }

    assert safe_diagnostic_details(details) == {}


def test_backend_capability_message_explains_unsupported_content() -> None:
    message = user_message_for_error(ErrorCode.BACKEND_CAPABILITY_MISSING)

    assert message.title == "论文中有内容当前无法自动处理"
    assert "特殊图片写法" in message.explanation
    assert "原始 LaTeX 保持不变" in message.safety
