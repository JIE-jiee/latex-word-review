from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest

from latex_word_review.app_views import (
    APP_STYLESHEET_PATH,
    ViewModelError,
    render_app_page,
    render_error_card,
    render_stepper,
)

PAYLOAD = '<script>alert("&")</script>'
ESCAPED_PAYLOAD = "&lt;script&gt;alert(&quot;&amp;&quot;)&lt;/script&gt;"


def _common(page: str) -> dict[str, object]:
    return {
        "page": page,
        "title": PAYLOAD,
        "project_name": PAYLOAD,
        "form_fields": {"csrf": PAYLOAD, "revision": 3},
        "exit_form_fields": {"csrf": PAYLOAD},
        "notices": [
            {"tone": "info", "title": PAYLOAD, "message": PAYLOAD},
        ],
    }


def _home() -> dict[str, object]:
    return _common("home") | {
        "new_action": "/new?from=home&name=<paper>",
        "recent_sessions": [
            {
                "name": PAYLOAD,
                "status": "等待返回稿",
                "updated_at": PAYLOAD,
                "href": "/session/one?tab=<summary>&mode=local",
                "delete_href": "/session/one/delete?return=<home>",
                "tone": "warning",
                "action_label": "导入返回 Word",
                "next_step": PAYLOAD,
            }
        ],
    }


def _preflight() -> dict[str, object]:
    return _common("preflight") | {
        "project_name": PAYLOAD,
        "main_file": PAYLOAD,
        "source_location": PAYLOAD,
        "checks": [
            {"status": "ok", "label": "主文件", "message": PAYLOAD},
            {"status": "warning", "label": "PDF 工具", "message": "可稍后安装"},
        ],
        "can_continue": True,
        "choose_action": "/new",
        "continue_action": "/session/export?name=<paper>&mode=safe",
    }


def _progress() -> dict[str, object]:
    return _common("progress") | {
        "step": 2,
        "progress": 48,
        "stage": PAYLOAD,
        "message": PAYLOAD,
        "refresh_href": "/jobs/job_one?from=<page>",
    }


def _waiting_word() -> dict[str, object]:
    return _common("waiting_word") | {
        "review_docx_name": PAYLOAD,
        "exported_at": PAYLOAD,
        "open_action": "/session/open-review-docx",
        "save_copy_action": "/session/save-review-copy?name=<paper>",
        "receive_action": "/session/receive?round=1&name=<paper>",
        "compatibility_counts": [{"label": PAYLOAD, "count": 7}],
        "reviewer_message": PAYLOAD,
        "return_flow_steps": [PAYLOAD, "逐项决定修改", "生成新的 LaTeX 副本"],
        "instructions": [PAYLOAD, "保持修订开启"],
    }


def _approval() -> dict[str, object]:
    return _common("approval") | {
        "step": 3,
        "total": 1,
        "decided": 1,
        "safe_pending_count": 1,
        "manual_pending_count": 2,
        "can_finalize": True,
        "decision_action": "/approval/decision",
        "bulk_action": "/approval/accept-safe",
        "manual_bulk_action": "/approval/mark-manual",
        "finalize_action": "/approval/finalize",
        "selected_filter": "all",
        "selected_page": 1,
        "page_start": 1,
        "pagination": {"page": 1, "pages": 1, "total": 1, "start": 1, "end": 1},
        "filters": [
            {"label": PAYLOAD, "count": 1, "href": "/approval?filter=<all>&x=1", "current": True}
        ],
        "changes": [
            {
                "change_id": PAYLOAD,
                "kind_label": PAYLOAD,
                "before": PAYLOAD,
                "after": PAYLOAD,
                "author": PAYLOAD,
                "timestamp": PAYLOAD,
                "context": PAYLOAD,
                "safety": "safe",
                "safety_label": PAYLOAD,
                "decision": "accepted",
                "edited_text": PAYLOAD,
                "reason": PAYLOAD,
                "risk_acknowledgement": PAYLOAD,
                "details": [{"label": PAYLOAD, "value": PAYLOAD}],
            }
        ],
    }


def _patch() -> dict[str, object]:
    return _common("patch") | {
        "step": 4,
        "counts": {"automatic": 2, "manual": 1, "rejected": 3, "conflict": 0},
        "files": [{"path": PAYLOAD, "change_count": 2, "preview": PAYLOAD}],
        "ready": True,
        "back_action": "/approval?name=<paper>&x=1",
        "confirm_action": "/result/generate",
    }


def _result() -> dict[str, object]:
    return _common("result") | {
        "status": "partial",
        "artifacts": [
            {
                "label": PAYLOAD,
                "description": PAYLOAD,
                "available": True,
                "href": "/artifact/revised?name=<paper>&x=1",
            },
            {
                "label": "带标记 PDF",
                "description": PAYLOAD,
                "available": False,
            },
        ],
        "warnings": [PAYLOAD],
        "revised_source_available": True,
        "open_revised_action": "/result/open-revised",
        "delivery_available": False,
        "open_delivery_action": "/result/open-delivery",
        "retry_action": "/result/retry",
        "new_action": "/new",
    }


def _error() -> dict[str, object]:
    return _common("error") | {
        "step": 2,
        "error": {
            "code": PAYLOAD,
            "title": PAYLOAD,
            "message": PAYLOAD,
            "guidance": [PAYLOAD],
            "technical_details": PAYLOAD,
            "primary_action": {
                "kind": "post",
                "label": PAYLOAD,
                "href": "/session/receive?reason=<bad>&x=1",
                "form_fields": {"csrf": PAYLOAD, "session": "session_safe"},
            },
            "back_href": "/",
        },
    }


def _shutdown() -> dict[str, object]:
    return {
        "page": "shutdown",
        "waiting_for_jobs": True,
    }


def test_home_renders_four_step_local_first_shell_and_escapes_values() -> None:
    page = render_app_page(_home())

    assert page.startswith('<!doctype html><html lang="zh-CN">')
    assert f'<link rel="stylesheet" href="{APP_STYLESHEET_PATH}">' in page
    assert '<a class="skip-link" href="#main-content">跳到主要内容</a>' in page
    assert 'aria-label="审阅进度"' in page
    assert page.count('class="stepper__item ') == 4
    assert 'aria-current="step"' in page
    assert "生成 Word / 导入返回稿" in page
    assert "预览并回填新副本" in page
    assert "仅在本机运行" in page
    assert "点击后会打开 Windows 文件选择窗口" in page
    assert "文件名不必是 main.tex" in page
    assert "已经收到修改后的 Word" in page
    assert "不要重新新建审阅" in page
    assert "导入返回 Word" in page
    assert ">继续任务</a>" not in page
    assert "原稿与返回 Word 保持不变" in page
    assert 'method="post" action="/new?from=home&amp;name=&lt;paper&gt;"' in page
    assert 'method="post" action="/app/exit"' in page
    assert "关闭浏览器标签页不会自动结束程序" in page
    assert PAYLOAD not in page
    assert page.count(ESCAPED_PAYLOAD) >= 4
    assert "/new?from=home&amp;name=&lt;paper&gt;" in page


def test_preflight_shows_checks_and_only_enables_safe_next_action() -> None:
    page = render_app_page(_preflight())

    assert "确认论文与环境" in page
    assert "程序只读取所选论文" in page
    assert "通过" in page
    assert "请留意" in page
    assert 'method="post" action="/session/export?name=&lt;paper&gt;&amp;mode=safe"' in page
    assert '<input type="hidden" name="csrf" value="' + ESCAPED_PAYLOAD in page
    assert "生成审阅 Word" in page


def test_preflight_with_error_cannot_claim_it_can_continue() -> None:
    model = _preflight()
    model["checks"] = [{"status": "error", "label": "主文件", "message": "找不到"}]

    with pytest.raises(ViewModelError, match="can_continue"):
        render_app_page(model)


def test_progress_page_is_accessible_bounded_and_has_a_manual_refresh() -> None:
    page = render_app_page(_progress())

    assert "正在安全处理" in page
    assert 'value="48" max="100"' in page
    assert "/jobs/job_one?from=&lt;page&gt;" in page
    assert "后台任务不会覆盖原稿" in page
    assert PAYLOAD not in page

    model = _progress()
    model["progress"] = 101
    with pytest.raises(ViewModelError, match="between 0 and 100"):
        render_app_page(model)


def test_long_export_uses_real_stage_and_indeterminate_progress() -> None:
    model = _progress() | {
        "stage": "正在处理论文图片并生成审阅 Word",
        "progress_indeterminate": True,
        "duration_hint": "包含大量 PDF 图片的论文通常约需 1–2 分钟。",
        "progress_note": "显示真实处理阶段，不按图片数量伪造百分比。",
    }

    page = render_app_page(model)

    assert "正在处理论文图片并生成审阅 Word" in page
    assert "通常约需 1–2 分钟" in page
    assert "不按图片数量伪造百分比" in page
    assert '<progress max="100" aria-label="正在处理论文图片并生成审阅 Word">' in page
    assert '<progress value="48"' not in page
    assert "当前进度：48%" not in page


def test_waiting_word_uses_windows_picker_post_without_browser_upload() -> None:
    page = render_app_page(_waiting_word())

    assert "Word 审阅交接与修改稿导入" in page
    assert "2A 另存可编辑 Word 并交给审阅者" in page
    assert "2B 收到修改稿后导入 Word" in page
    assert "逐项审批" in page
    assert "生成新的 LaTeX 副本" in page
    assert 'method="post" action="/session/receive?round=1&amp;name=&lt;paper&gt;"' in page
    assert 'type="file"' not in page
    assert 'enctype="multipart/form-data"' not in page
    assert "Windows 文件选择器" in page
    assert "选择修改后的 Word" in page
    assert "另存可编辑 Word" in page
    assert "打开只读基线（仅检查）" in page
    assert "打开审阅稿" not in page
    assert "语义审阅副本" in page
    assert "不是 PDF 排版复刻" in page
    assert "兼容性计数摘要（仅计数）" in page
    assert "报告未提供的类别不会按 0 推测" in page
    assert 'method="post" action="/session/save-review-copy?name=&lt;paper&gt;"' in page
    assert "不会修改所选 Word，也不会立即写入 LaTeX" in page
    assert "拖回" not in page
    assert PAYLOAD not in page
    assert page.index("2A 另存可编辑 Word 并交给审阅者") < page.index("2B 收到修改稿后导入 Word")
    assert page.index("2B 收到修改稿后导入 Word") < page.index("选择修改后的 Word")
    assert 'class="button button--primary button--large"' in page


def test_waiting_word_renders_existing_latex_changes_as_a_separate_display_only_card() -> None:
    model = _waiting_word()
    model["form_fields"] = {"csrf": "csrf-display", "session": "session-display"}
    model["existing_changes_display"] = {
        "docx_name": "existing-changes-display.docx",
        "open_action": "/session/open-existing-changes-display",
        "save_copy_action": "/session/save-existing-changes-copy",
    }

    page = render_app_page(model)

    assert "已有 LaTeX 批改展示稿（仅供对照）" in page
    assert "新增文字为蓝色" in page
    assert "删除文字为蓝色删除线" in page
    assert "旧文字蓝色删除线＋新文字蓝色" in page
    assert "不使用高亮" in page
    assert "这不是 Word 原生修订" in page
    assert "不要把它作为审阅者返回的 Word 导入" in page
    assert 'method="post" action="/session/open-existing-changes-display"' in page
    save_form = page.split('action="/session/save-existing-changes-copy"', 1)[1].split(
        "</form>",
        1,
    )[0]
    assert 'name="csrf"' in save_form
    assert 'name="session"' in save_form
    assert 'name="path"' not in save_form
    assert 'name="destination"' not in save_form
    assert 'type="file"' not in save_form
    assert (
        page.index("2A 另存可编辑 Word 并交给审阅者")
        < page.index("已有 LaTeX 批改展示稿（仅供对照）")
        < page.index("2B 收到修改稿后导入 Word")
    )


def test_approval_cards_keep_technical_evidence_collapsed_and_preserve_first_gate() -> None:
    page = render_app_page(_approval())

    assert "逐项审批 Word 修改" in page
    assert "审批不会直接修改任何 LaTeX 文件" in page
    assert "采用全部安全正文修改（1 项）" in page
    assert "将全部不可自动回填项标为人工（2 项）" in page
    assert 'method="post" action="/approval/mark-manual"' in page
    assert 'name="return_filter" value="all"' in page
    assert 'name="return_page" value="1"' in page
    assert 'aria-label="修改列表分页"' in page
    assert "第 1 / 1 页" in page
    assert 'id="approval-change-list"' in page
    assert '<details class="technical"><summary>技术详情</summary>' in page
    assert "修改前" in page and "修改后" in page
    assert "采用" in page and "修改后采用" in page and "不采用" in page
    assert "修改后采用的文字（仅用于“修改后采用”）" in page
    assert "只有点击“修改后采用”时才会作为该项决定保存" in page
    assert 'aria-describedby="edited-text-help-1"' in page
    assert "留待人工" in page and "无法判断" in page
    assert "第一道闸门" in page
    assert "这一步只保存你的决定，不会修改 LaTeX" in page
    assert PAYLOAD not in page
    assert page.count(ESCAPED_PAYLOAD) >= 10


def test_approval_pagination_validates_ranges_and_same_origin_links() -> None:
    model = _approval()
    model["pagination"] = {
        "page": 1,
        "pages": 2,
        "total": 26,
        "start": 1,
        "end": 25,
        "next_href": "/session/session_safe?filter=all&page=2",
    }

    page = render_app_page(model)

    assert "显示第 1–25 项，共 26 项" in page
    assert "/session/session_safe?filter=all&amp;page=2" in page

    model["pagination"] = {
        "page": 1,
        "pages": 1,
        "total": 1,
        "start": 0,
        "end": 1,
    }
    with pytest.raises(ViewModelError, match="item range"):
        render_app_page(model)

    model["pagination"] = {
        "page": 1,
        "pages": 2,
        "total": 26,
        "start": 1,
        "end": 25,
        "next_href": "https://example.com/",
    }
    with pytest.raises(ViewModelError, match="same-origin"):
        render_app_page(model)


def test_patch_summary_requires_explicit_second_gate_confirmation() -> None:
    page = render_app_page(_patch())

    assert "第二道闸门" in page
    assert "原始 LaTeX 和返回 Word 始终保持不变" in page
    assert "将自动应用" in page
    assert "留待人工" in page
    assert 'name="confirm_apply" type="checkbox" value="yes" required' in page
    assert "确认只回填到新的 LaTeX 工作副本" in page
    assert "确认回填到新 LaTeX 副本并生成结果" in page
    assert PAYLOAD not in page
    assert ESCAPED_PAYLOAD in page


def test_blocked_patch_never_renders_apply_form() -> None:
    model = _patch()
    model["ready"] = False
    page = render_app_page(model)

    assert "暂时不能生成" in page
    assert 'name="confirm_apply"' not in page
    assert "确认回填到新 LaTeX 副本并生成结果" not in page


def test_partial_result_is_honest_and_supports_targeted_retry() -> None:
    page = render_app_page(_result())

    assert "新 LaTeX 副本已生成，部分结果待补齐" in page
    assert "缺失工具不会让整轮审批作废" in page
    assert "尚未完成的内容" in page
    assert "未生成" in page
    assert "重试缺失结果" in page
    assert 'method="post" action="/result/open-revised"' in page
    assert 'method="post" action="/result/open-delivery"' not in page
    assert 'method="post" action="/app/exit"' in page
    assert PAYLOAD not in page
    assert "/artifact/revised?name=&lt;paper&gt;&amp;x=1" in page


def test_revised_source_artifact_uses_its_explicit_folder_action() -> None:
    model = _result()
    model["artifacts"] = [
        {
            "label": "修订后的 LaTeX 副本",
            "description": "新的工作副本目录",
            "available": True,
            "open_in_folder": True,
        }
    ]

    page = render_app_page(model)

    assert "修订后的 LaTeX 副本" in page
    assert "请使用下方“打开修订后的 LaTeX 文件夹”查看" in page
    assert "/artifact/" not in page
    assert 'method="post" action="/result/open-revised"' in page
    assert 'method="post" action="/result/open-delivery"' not in page
    assert "/result/open-folder" not in page

    folder_artifact = cast(list[dict[str, object]], model["artifacts"])[0]
    folder_artifact["href"] = "/artifact/invalid-directory"
    with pytest.raises(ViewModelError, match="folder-only artifact"):
        render_app_page(model)


def test_complete_result_does_not_offer_retry() -> None:
    model = _result()
    model["status"] = "complete"
    model["warnings"] = []
    model["delivery_available"] = True
    page = render_app_page(model)

    assert "新 LaTeX 副本与全部结果已生成" in page
    assert "原稿未被覆盖" in page
    assert "重试缺失结果" not in page
    assert 'method="post" action="/result/open-revised"' in page
    assert 'method="post" action="/result/open-delivery"' in page
    assert "打开修订后的 LaTeX 文件夹" in page
    assert "打开 PDF 与账本交付文件夹" in page


def test_shutdown_page_explains_active_job_drain_without_followup_actions() -> None:
    page = render_app_page(_shutdown())

    assert "已请求退出" in page
    assert "正在等待后台任务安全结束" in page
    assert "可以关闭浏览器标签页" in page
    assert "<form" not in page


def test_error_card_is_chinese_actionable_collapsed_and_fully_escaped() -> None:
    error = _error()["error"]
    assert isinstance(error, dict)
    card = render_error_card(error)
    page = render_app_page(_error())

    for rendered in (card, page):
        assert 'role="alert"' in rendered
        assert "错误代码" in rendered
        assert "可以这样处理" in rendered
        assert '<details class="technical"><summary>技术详情</summary>' in rendered
        assert 'method="post" action="/session/receive?reason=&lt;bad&gt;&amp;x=1"' in rendered
        assert 'name="session" value="session_safe"' in rendered
        assert "返回" in rendered
        assert PAYLOAD not in rendered
        assert ESCAPED_PAYLOAD in rendered


def test_error_link_action_is_a_real_get_without_hidden_fields() -> None:
    error = cast("dict[str, object]", _error()["error"])
    error["primary_action"] = {
        "kind": "link",
        "label": "返回审批",
        "href": "/session/session_safe?filter=all",
    }

    rendered = render_error_card(error)

    assert '<a class="button button--primary"' in rendered
    assert 'href="/session/session_safe?filter=all"' in rendered
    assert "返回审批" in rendered


def test_error_link_action_rejects_post_fields() -> None:
    error = cast("dict[str, object]", _error()["error"])
    error["primary_action"] = {
        "kind": "link",
        "label": "不安全动作",
        "href": "/",
        "form_fields": {"csrf": "must-not-be-used"},
    }

    with pytest.raises(ViewModelError, match="must not contain form_fields"):
        render_error_card(error)


def test_image_issue_summary_is_visible_once_and_details_stay_collapsed() -> None:
    error = cast("dict[str, object]", _error()["error"])
    error["summary"] = "2处图片写法当前无法处理"
    error["technical_details"] = (
        "稳定错误代码：E_BACKEND_CAPABILITY_MISSING\n"
        "问题摘要：2处图片写法当前无法处理\n"
        "问题位置 1：第464行，第5列（E_PATH_TRAVERSAL）"
    )

    rendered = render_error_card(error)

    assert rendered.count("2处图片写法当前无法处理") == 1
    assert rendered.index("2处图片写法当前无法处理") < rendered.index("技术详情")
    assert '<p class="error-card__summary"><strong>' in rendered
    assert '<details class="technical"><summary>技术详情</summary>' in rendered
    assert "第464行，第5列（E_PATH_TRAVERSAL）" in rendered


def test_error_summary_and_technical_details_must_agree() -> None:
    error = cast("dict[str, object]", _error()["error"])
    error["summary"] = "2处图片写法当前无法处理"
    error["technical_details"] = "问题摘要：3处图片写法当前无法处理"

    with pytest.raises(ViewModelError, match="disagree"):
        render_error_card(error)


@pytest.mark.parametrize(
    "model",
    [
        _home(),
        _preflight(),
        _progress(),
        _waiting_word(),
        _approval(),
        _patch(),
        _result(),
        _shutdown(),
        _error(),
    ],
)
def test_every_page_is_csp_friendly_and_has_no_inline_executable_content(
    model: dict[str, object],
) -> None:
    page = render_app_page(model)

    assert "<script" not in page.casefold()
    assert " style=" not in page.casefold()
    assert re.search(r"\son[a-z]+\s*=", page, flags=re.IGNORECASE) is None
    assert "http://" not in page and "https://" not in page
    assert APP_STYLESHEET_PATH in page


@pytest.mark.parametrize(
    "unsafe_url",
    ["javascript:alert(1)", "https://example.invalid/", "//example.invalid/", "\\server\\x", "x"],
)
def test_dynamic_urls_must_be_same_origin_root_relative(unsafe_url: str) -> None:
    model = _home()
    model["new_action"] = unsafe_url

    with pytest.raises(ViewModelError, match="same-origin"):
        render_app_page(model)


def test_view_mapping_and_form_field_types_fail_closed() -> None:
    with pytest.raises(ViewModelError, match="page"):
        render_app_page({"page": "unknown"})
    with pytest.raises(ViewModelError, match="step"):
        render_stepper(5)

    model = _preflight()
    model["form_fields"] = {"csrf onclick": "bad"}
    with pytest.raises(ViewModelError, match="safe HTML names"):
        render_app_page(model)


def test_stylesheet_supports_keyboard_forced_colors_and_200_percent_zoom() -> None:
    stylesheet = (
        Path(__file__).parents[1] / "src" / "latex_word_review" / "assets" / "app.css"
    ).read_text(encoding="utf-8")

    assert ":focus-visible" in stylesheet
    assert "min-block-size: 2.75rem" in stylesheet
    assert "overflow-wrap: anywhere" in stylesheet
    assert "minmax(min(100%, 20rem), 1fr)" in stylesheet
    assert "@media (max-width: 48rem)" in stylesheet
    assert "@media (forced-colors: active)" in stylesheet
    assert "@import" not in stylesheet
    assert "url(" not in stylesheet
