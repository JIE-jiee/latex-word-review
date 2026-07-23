"""Pure, framework-free server-rendered views for the Windows local application.

The public integration surface is :func:`render_app_page`.  It accepts a plain
mapping with a ``page`` discriminator and returns a complete HTML document.  The
supported page names are exposed by :data:`SUPPORTED_PAGES`; every page-specific
collection is a sequence of plain mappings.  Controllers remain responsible for
authorization, CSRF validation, state transitions, and revalidating every action.

This module deliberately contains no filesystem, network, session, or business
logic.  Dynamic text is escaped at the final rendering boundary, dynamic links are
restricted to same-origin root-relative URLs, and the document contains no inline
style, inline script, event handler, CDN, or third-party resource.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from typing import Final, cast
from urllib.parse import urlsplit

__all__ = [
    "APP_STYLESHEET_PATH",
    "SUPPORTED_PAGES",
    "ViewModelError",
    "render_app_page",
    "render_error_card",
    "render_stepper",
]

APP_STYLESHEET_PATH: Final = "/assets/app.css"
SUPPORTED_PAGES: Final[tuple[str, ...]] = (
    "home",
    "delete_confirm",
    "preflight",
    "progress",
    "waiting_word",
    "approval",
    "patch",
    "result",
    "shutdown",
    "error",
)

_PAGE_DEFAULTS: Final[dict[str, tuple[str, int]]] = {
    "home": ("LaTeX–Word 审阅助手", 1),
    "delete_confirm": ("删除本机任务", 1),
    "preflight": ("检查论文", 1),
    "progress": ("正在处理", 1),
    "waiting_word": ("导入修改后的 Word", 2),
    "approval": ("审批修改", 3),
    "patch": ("预览并回填新副本", 4),
    "result": ("新 LaTeX 副本与审阅结果", 4),
    "shutdown": ("程序已退出", 4),
    "error": ("需要处理", 1),
}
_STEP_LABELS: Final[tuple[str, ...]] = (
    "选择论文",
    "生成 Word / 导入返回稿",
    "审批修改",
    "预览并回填新副本",
)
_TONES: Final = frozenset({"info", "success", "warning", "danger", "neutral"})
_CHECK_STATES: Final = frozenset({"ok", "warning", "error", "info"})
_SAFETY_STATES: Final = frozenset({"safe", "manual", "conflict"})
_DECISIONS: Final = frozenset(
    {"pending", "accepted", "accepted_with_edit", "rejected", "manual", "conflict"}
)
_APPROVAL_FILTERS: Final = frozenset({"all", "pending", "safe", "manual", "conflict"})
_RESULT_STATES: Final = frozenset({"complete", "partial"})
_FIELD_NAME_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")
_IMAGE_ISSUE_SUMMARY_RE: Final = re.compile(r"[1-9][0-9]{0,3}处图片写法当前无法处理")
_IMAGE_ISSUE_SUMMARY_PREFIX: Final = "问题摘要："

_CHECK_LABELS: Final[dict[str, str]] = {
    "ok": "通过",
    "warning": "请留意",
    "error": "需要处理",
    "info": "信息",
}
_SAFETY_LABELS: Final[dict[str, str]] = {
    "safe": "可安全自动应用",
    "manual": "需要人工处理",
    "conflict": "存在冲突",
}
_DECISION_LABELS: Final[dict[str, str]] = {
    "pending": "待决定",
    "accepted": "已采用",
    "accepted_with_edit": "修改后采用",
    "rejected": "不采用",
    "manual": "留待人工",
    "conflict": "无法判断",
}


class ViewModelError(ValueError):
    """Raised when a controller supplies an invalid or unsafe view mapping."""


def _escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _as_mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ViewModelError(f"{location} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ViewModelError(f"{location} keys must be strings")
    return cast("Mapping[str, object]", value)


def _mapping_items(model: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = model.get(key, ())
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ViewModelError(f"{key} must be a sequence of mappings")
    return tuple(_as_mapping(item, f"{key}[{index}]") for index, item in enumerate(value))


def _string_items(
    model: Mapping[str, object],
    key: str,
    *,
    default: Sequence[str] = (),
) -> tuple[str, ...]:
    value = model.get(key, default)
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ViewModelError(f"{key} must be a sequence of strings")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ViewModelError(f"{key}[{index}] must be a string")
        items.append(item)
    return tuple(items)


def _text(
    model: Mapping[str, object],
    key: str,
    *,
    default: str | None = None,
) -> str:
    value = model.get(key, default)
    if not isinstance(value, str):
        raise ViewModelError(f"{key} must be a string")
    return value


def _optional_text(model: Mapping[str, object], key: str) -> str | None:
    value = model.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ViewModelError(f"{key} must be a string or null")
    return value


def _integer(
    model: Mapping[str, object],
    key: str,
    *,
    default: int,
    minimum: int = 0,
) -> int:
    value = model.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ViewModelError(f"{key} must be an integer greater than or equal to {minimum}")
    return value


def _boolean(model: Mapping[str, object], key: str, *, default: bool) -> bool:
    value = model.get(key, default)
    if not isinstance(value, bool):
        raise ViewModelError(f"{key} must be a boolean")
    return value


def _choice(
    model: Mapping[str, object],
    key: str,
    choices: frozenset[str],
    *,
    default: str,
) -> str:
    value = model.get(key, default)
    if not isinstance(value, str) or value not in choices:
        raise ViewModelError(f"{key} must be one of {sorted(choices)}")
    return value


def _safe_local_url(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ViewModelError(f"{location} must be a non-empty local URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ViewModelError(f"{location} must be a same-origin root-relative URL")
    return value


def _local_url(
    model: Mapping[str, object],
    key: str,
    *,
    default: str,
) -> str:
    return _safe_local_url(model.get(key, default), key)


def _hidden_fields(
    model: Mapping[str, object],
    *,
    extra: Sequence[tuple[str, object]] = (),
) -> str:
    configured = model.get("form_fields", {})
    fields = _as_mapping(configured, "form_fields")
    names: set[str] = set()
    rendered: list[str] = []
    for name, raw_value in (*tuple(fields.items()), *tuple(extra)):
        if _FIELD_NAME_RE.fullmatch(name) is None or name in names:
            raise ViewModelError("form field names must be unique safe HTML names")
        if not isinstance(raw_value, (str, int)) or isinstance(raw_value, bool):
            raise ViewModelError(f"form field {name} must be a string or integer")
        names.add(name)
        rendered.append(
            '<input type="hidden" name="' + _escape(name) + '" value="' + _escape(raw_value) + '">'
        )
    return "".join(rendered)


def _status_badge(tone: str, label: str) -> str:
    if tone not in _TONES:
        raise ViewModelError(f"unknown status tone: {tone}")
    return f'<span class="badge badge--{tone}">{_escape(label)}</span>'


def render_stepper(current_step: int) -> str:
    """Render the four-step journey with explicit current and completed states."""

    if isinstance(current_step, bool) or current_step not in range(1, 5):
        raise ViewModelError("current_step must be an integer from 1 through 4")
    items: list[str] = []
    for number, label in enumerate(_STEP_LABELS, start=1):
        if number < current_step:
            state = "done"
            marker = "✓"
            current = ""
            assistive = '<span class="sr-only">已完成：</span>'
        elif number == current_step:
            state = "current"
            marker = str(number)
            current = ' aria-current="step"'
            assistive = '<span class="sr-only">当前步骤：</span>'
        else:
            state = "upcoming"
            marker = str(number)
            current = ""
            assistive = '<span class="sr-only">尚未开始：</span>'
        items.append(
            f'<li class="stepper__item stepper__item--{state}"{current}>'
            f'<span class="stepper__marker" aria-hidden="true">{marker}</span>'
            f'<span class="stepper__label">{assistive}{label}</span></li>'
        )
    return (
        '<nav class="stepper" aria-label="审阅进度"><ol class="stepper__list">'
        + "".join(items)
        + "</ol></nav>"
    )


def _render_notices(model: Mapping[str, object]) -> str:
    notices = _mapping_items(model, "notices")
    rendered: list[str] = []
    for notice in notices:
        tone = _choice(notice, "tone", _TONES, default="info")
        title = _text(notice, "title")
        message = _text(notice, "message")
        role = "alert" if tone == "danger" else "status"
        rendered.append(
            f'<section class="notice notice--{tone}" role="{role}">'
            f"<h2>{_escape(title)}</h2><p>{_escape(message)}</p></section>"
        )
    return "".join(rendered)


def _render_home(view: Mapping[str, object]) -> str:
    new_action = _local_url(view, "new_action", default="/new")
    exit_action = _local_url(view, "exit_action", default="/app/exit")
    exit_fields = {"form_fields": _as_mapping(view.get("exit_form_fields", {}), "exit_form_fields")}
    recent_sessions = _mapping_items(view, "recent_sessions")
    recent: list[str] = []
    for session in recent_sessions:
        name = _text(session, "name")
        status = _text(session, "status")
        updated_at = _text(session, "updated_at")
        href = _safe_local_url(session.get("href"), "recent_sessions.href")
        delete_href = _safe_local_url(
            session.get("delete_href"),
            "recent_sessions.delete_href",
        )
        tone = _choice(session, "tone", _TONES, default="neutral")
        action_label = _optional_text(session, "action_label") or "继续任务"
        next_step = _text(
            session,
            "next_step",
            default="打开任务并查看已验证的下一步。",
        )
        recent.append(
            '<li class="task-card"><div class="task-card__body">'
            f"<h3>{_escape(name)}</h3>"
            f"<p>{_status_badge(tone, status)} <span>最近更新：{_escape(updated_at)}</span></p>"
            f'<p class="task-card__next"><strong>下一步：</strong>{_escape(next_step)}</p>'
            "</div>"
            '<div class="task-card__actions">'
            f'<a class="button button--secondary" href="{_escape(href)}">'
            f"{_escape(action_label)}</a>"
            f'<a class="button button--danger" href="{_escape(delete_href)}">删除</a>'
            "</div></li>"
        )
    recent_content = (
        '<ul class="task-list">' + "".join(recent) + "</ul>"
        if recent
        else '<div class="empty-state"><p>还没有审阅任务。</p>'
        "<p>选择一篇论文即可开始，原稿不会被修改。</p></div>"
    )
    return (
        '<section class="hero" aria-labelledby="welcome-title">'
        '<div><p class="eyebrow">Windows 本地审阅工作流</p>'
        '<h1 id="welcome-title">让合作者只用 Word，也能安全审阅 LaTeX</h1>'
        "<p>点击后会打开 Windows 文件选择窗口，请选择论文的主 .tex 文件（文件名不必是 main.tex）。"
        "随后生成 Word；"
        "收到修订稿后逐项审批，再生成新的 LaTeX 与 PDF。整个过程仅在本机运行。"
        "已经收到修改后的 Word 时，请从下方“最近任务”进入原任务，不要重新新建审阅。</p></div>"
        f'<form method="post" action="{_escape(new_action)}">{_hidden_fields(view)}'
        '<button class="button button--primary button--large" type="submit">'
        "新建审阅</button></form></section>"
        '<section aria-labelledby="recent-title"><div class="section-heading">'
        '<div><p class="eyebrow">继续工作</p><h2 id="recent-title">最近任务</h2></div>'
        '<p class="section-heading__hint">按最近更新排序；打开任务时会完整核验状态。</p>'
        "</div>"
        f"{recent_content}</section>"
        '<section class="privacy-panel" aria-labelledby="privacy-title">'
        '<h2 id="privacy-title">论文留在你的电脑上</h2>'
        "<p>程序不会上传论文，也不会覆盖原始 LaTeX 或导师返回的 Word。"
        "自动回填只针对你明确批准且能够精确定位的普通正文。</p></section>"
        '<section class="exit-panel" aria-labelledby="exit-title"><div>'
        '<h2 id="exit-title">结束本机程序</h2>'
        "<p>关闭浏览器标签页不会自动结束程序。完成工作后请使用此按钮；"
        "若后台仍在处理，程序会等待任务安全结束后退出。</p></div>"
        f'<form method="post" action="{_escape(exit_action)}">{_hidden_fields(exit_fields)}'
        '<button class="button button--secondary" type="submit">退出程序</button></form>'
        "</section>"
    )


def _render_delete_confirm(view: Mapping[str, object]) -> str:
    session_name = _text(view, "session_name")
    status = _text(view, "status")
    updated_at = _text(view, "updated_at")
    can_delete = _boolean(view, "can_delete", default=False)
    delete_action = _local_url(view, "delete_action", default="/session/delete")
    cancel_href = _local_url(view, "cancel_href", default="/")
    if can_delete:
        delete_control = (
            f'<form method="post" action="{_escape(delete_action)}">'
            f"{_hidden_fields(view)}"
            '<label class="confirmation-check">'
            '<input name="confirm_delete" type="checkbox" value="yes" required>'
            "<span>我明白此任务的本机数据将被永久删除，且无法撤销。</span></label>"
            '<button class="button button--danger" type="submit">永久删除此任务</button>'
            "</form>"
        )
        availability = ""
    else:
        delete_control = (
            '<button class="button button--danger" type="button" disabled>'
            "任务处理中，暂时不能删除</button>"
        )
        availability = (
            '<section class="notice notice--warning" role="status">'
            "<h2>请等待当前操作结束</h2>"
            "<p>后台任务完成后再返回此页面。程序不会在读写任务证据时删除目录。</p>"
            "</section>"
        )
    return (
        '<section class="page-heading" aria-labelledby="delete-title">'
        '<p class="eyebrow">管理最近任务</p>'
        '<h1 id="delete-title">确认删除本机任务</h1>'
        "<p>这是永久操作，只会作用于本程序管理的这一项任务。</p></section>"
        '<section class="notice notice--danger" role="alert">'
        "<h2>删除后无法恢复</h2>"
        "<p>本任务中的审阅 Word、返回稿归档、审批记录、LaTeX 新副本和审计结果"
        "都会从本机任务目录删除。原始 LaTeX 项目和另存到任务外的文件不会被删除。</p>"
        "</section>"
        '<section class="selection-card" aria-labelledby="delete-task-title">'
        '<h2 id="delete-task-title">即将删除</h2><dl class="definition-grid">'
        f"<dt>任务</dt><dd>{_escape(session_name)}</dd>"
        f"<dt>状态</dt><dd>{_escape(status)}</dd>"
        f"<dt>最近更新</dt><dd>{_escape(updated_at)}</dd>"
        "</dl></section>"
        f"{availability}"
        '<div class="actions">'
        f'<a class="button button--secondary" href="{_escape(cancel_href)}">取消并返回首页</a>'
        f"{delete_control}</div>"
    )


def _render_preflight(view: Mapping[str, object]) -> str:
    project_name = _text(view, "project_name")
    main_file = _text(view, "main_file")
    source_location = _text(view, "source_location")
    checks = _mapping_items(view, "checks")
    rendered_checks: list[str] = []
    has_error = False
    for check in checks:
        state = _choice(check, "status", _CHECK_STATES, default="info")
        has_error = has_error or state == "error"
        rendered_checks.append(
            f'<li class="check-list__item check-list__item--{state}">'
            f'<span class="check-list__state">{_CHECK_LABELS[state]}</span><div>'
            f"<h3>{_escape(_text(check, 'label'))}</h3>"
            f"<p>{_escape(_text(check, 'message'))}</p></div></li>"
        )
    can_continue = _boolean(view, "can_continue", default=not has_error)
    if can_continue and has_error:
        raise ViewModelError("can_continue cannot be true while a preflight check is an error")
    choose_action = _local_url(view, "choose_action", default="/new")
    choose_fields = {
        "form_fields": _as_mapping(
            view.get("choose_form_fields", view.get("form_fields", {})),
            "choose_form_fields",
        )
    }
    if can_continue:
        continue_action = _local_url(view, "continue_action", default="/session/export")
        next_control = (
            f'<form method="post" action="{_escape(continue_action)}">'
            f"{_hidden_fields(view)}"
            '<button class="button button--primary" type="submit">生成审阅 Word</button></form>'
        )
    else:
        next_control = (
            '<button class="button button--primary" type="button" disabled>'
            "请先处理检查问题</button>"
        )
    return (
        '<section class="page-heading"><p class="eyebrow">第 1 步</p>'
        "<h1>确认论文与环境</h1>"
        "<p>程序只读取所选论文，并为本轮审阅创建独立副本。</p></section>"
        '<section class="selection-card" aria-labelledby="selection-title">'
        '<h2 id="selection-title">已选择</h2><dl class="definition-grid">'
        f"<dt>任务名称</dt><dd>{_escape(project_name)}</dd>"
        f"<dt>主文件</dt><dd><code>{_escape(main_file)}</code></dd>"
        f"<dt>论文位置</dt><dd><code>{_escape(source_location)}</code></dd>"
        "</dl></section>"
        '<section aria-labelledby="checks-title"><h2 id="checks-title">开始前检查</h2>'
        f'<ul class="check-list">{"".join(rendered_checks)}</ul></section>'
        '<div class="actions">'
        f'<form method="post" action="{_escape(choose_action)}">{_hidden_fields(choose_fields)}'
        '<button class="button button--secondary" type="submit">重新选择</button></form>'
        f"{next_control}</div>"
    )


def _render_progress(view: Mapping[str, object]) -> str:
    progress = _integer(view, "progress", default=0)
    if progress > 100:
        raise ViewModelError("progress must be between 0 and 100")
    progress_indeterminate = _boolean(view, "progress_indeterminate", default=False)
    stage = _text(view, "stage", default="正在处理")
    message = _text(
        view,
        "message",
        default="可以保留此页面，程序完成后会自动进入下一步。",
    )
    duration_hint = _optional_text(view, "duration_hint")
    progress_note = _optional_text(view, "progress_note")
    refresh_href = _local_url(view, "refresh_href", default="/")
    if progress_indeterminate:
        progress_status = "<p>当前阶段正在进行</p>"
        progress_element = f'<progress max="100" aria-label="{_escape(stage)}">正在处理</progress>'
    else:
        progress_status = f"<p>当前进度：{_escape(progress)}%</p>"
        progress_element = (
            f'<progress value="{_escape(progress)}" max="100">{_escape(progress)}%</progress>'
        )
    timing = (
        '<section class="notice notice--info" role="status">'
        f"<h2>预计需要多久？</h2><p>{_escape(duration_hint)}</p></section>"
        if duration_hint is not None
        else ""
    )
    progress_explanation = (
        f'<p class="progress-explanation">{_escape(progress_note)}</p>'
        if progress_note is not None
        else ""
    )
    return (
        '<section class="page-heading"><p class="eyebrow">本机后台任务</p>'
        "<h1>正在安全处理</h1>"
        f"<p>{_escape(message)}</p></section>"
        '<section class="progress-card progress-card--job" aria-labelledby="job-stage-title">'
        f'<div><h2 id="job-stage-title">{_escape(stage)}</h2>'
        f"{progress_status}{progress_explanation}</div>"
        f"{progress_element}"
        "</section>"
        f"{timing}"
        '<p class="notice notice--info" role="status">'
        "后台任务不会覆盖原稿；即使关闭页面，已经密封的阶段仍可恢复。</p>"
        f'<p><a class="button button--secondary" href="{_escape(refresh_href)}">'
        "刷新状态</a></p>"
    )


def _render_waiting_word(view: Mapping[str, object]) -> str:
    review_docx_name = _text(view, "review_docx_name")
    exported_at = _text(view, "exported_at")
    open_action = _local_url(view, "open_action", default="/session/open-review-docx")
    save_copy_action = _local_url(view, "save_copy_action", default="/session/save-review-copy")
    receive_action = _local_url(view, "receive_action", default="/session/receive")
    reviewer_message = _optional_text(view, "reviewer_message")
    compatibility_counts = _mapping_items(view, "compatibility_counts")
    compatibility_tiles = "".join(
        '<li class="summary-tile summary-tile--neutral">'
        f"<strong>{_escape(_integer(item, 'count', default=-1))}</strong>"
        f"<span>{_escape(_text(item, 'label'))}</span></li>"
        for item in compatibility_counts
    )
    compatibility_summary = (
        '<section aria-labelledby="compatibility-summary-title">'
        '<h2 id="compatibility-summary-title">兼容性计数摘要（仅计数）</h2>'
        "<p>以下数字来自已校验的导出报告，只表示审阅副本中的对象数量，"
        "不代表 PDF 排版或视觉效果完全一致；报告未提供的类别不会按 0 推测。</p>"
        f'<ul class="summary-grid" aria-label="审阅 Word 兼容性计数">'
        f"{compatibility_tiles}</ul></section>"
        if compatibility_tiles
        else ""
    )
    return_flow_steps = _string_items(
        view,
        "return_flow_steps",
        default=(
            "选择返回的 .docx，原件会只读归档。",
            "进入逐项审批，决定采用、不采用或留待人工处理。",
            "再次确认后生成新的 LaTeX 副本。",
        ),
    )
    instructions = _string_items(
        view,
        "instructions",
        default=(
            "使用 Microsoft Word 打开另存的可编辑审阅副本。",
            "保持“修订”开启；讨论内容使用批注。",
            "不要接受全部修订，也不要删除定位标记。",
            "审阅完成后保存为 .docx，再点击下方按钮选择该文件。",
        ),
    )
    instruction_items = "".join(f"<li>{_escape(item)}</li>" for item in instructions)
    return_flow_items = "".join(f"<li>{_escape(item)}</li>" for item in return_flow_steps)
    message = (
        '<section class="copy-card" aria-labelledby="message-title">'
        '<h2 id="message-title">给审阅者的说明</h2>'
        f"<p>{_escape(reviewer_message)}</p></section>"
        if reviewer_message is not None
        else ""
    )
    return (
        '<section class="page-heading" aria-labelledby="word-handoff-title">'
        '<p class="eyebrow">第 2 步</p>'
        '<h1 id="word-handoff-title">Word 审阅交接与修改稿导入</h1>'
        "<p>先另存可编辑副本交给审阅者；收到修改稿后，仍在本页导入并开始逐项审批。</p>"
        "</section>"
        '<section class="handoff-card" aria-labelledby="handoff-title">'
        '<div><p class="eyebrow">第 2 步 · 2A</p>'
        '<h2 id="handoff-title">2A 另存可编辑 Word 并交给审阅者</h2>'
        f"<p><strong>{_escape(review_docx_name)}</strong></p>"
        f"<p>生成时间：{_escape(exported_at)}</p>"
        "<p>该 Word 是用于修订正文和添加批注的语义审阅副本，不是 PDF 排版复刻。"
        "程序默认采用内置的 A4 单栏学术审阅样式（西文 Times New Roman、中文 SimSun；"
        "LaTeX 中明确声明的字体仍可优先），并压缩表格间距、限制图片不越过正文宽度。"
        "分页和图片位置仍可能与 PDF 不同，公式、图片、表格及特殊字段仍应人工检查。</p>"
        "<p>可另存到你指定的位置；副本可正常编辑和发送，程序不会覆盖已有文件。</p></div>"
        '<div class="actions">'
        f'<form method="post" action="{_escape(save_copy_action)}">{_hidden_fields(view)}'
        '<button class="button button--primary" type="submit">另存可编辑 Word</button></form>'
        f'<form method="post" action="{_escape(open_action)}">{_hidden_fields(view)}'
        '<button class="button button--secondary" type="submit">'
        "打开只读基线（仅检查）</button></form>"
        "</div>"
        "</section>"
        f"{_render_notices(view)}"
        f"{compatibility_summary}"
        '<section class="hero" aria-labelledby="return-word-title"><div>'
        '<p class="eyebrow">第 2 步 · 2B</p>'
        '<h2 id="return-word-title">2B 收到修改稿后导入 Word</h2>'
        "<p>选择审阅者保存并返回的 .docx，随后逐项决定哪些修改可以进入新 LaTeX 副本。</p>"
        '<ol class="instruction-list">'
        f"{return_flow_items}</ol></div>"
        f'<form method="post" action="{_escape(receive_action)}">'
        f"{_hidden_fields(view)}"
        '<button class="button button--primary button--large" type="submit">'
        "选择修改后的 Word，开始逐项审批</button></form></section>"
        '<section aria-labelledby="review-rules-title"><h2 id="review-rules-title">审阅方式</h2>'
        f'<ol class="instruction-list">{instruction_items}</ol></section>'
        f"{message}"
        '<section class="receive-card" aria-labelledby="receive-safety-title">'
        '<h2 id="receive-safety-title">导入时不会改动原件</h2>'
        "<p>只接受 .docx。按钮会打开 Windows 文件选择器；程序先只读归档并核验返回稿，"
        "不会修改所选 Word，也不会立即写入 LaTeX。</p></section>"
    )


def _render_filters(view: Mapping[str, object]) -> str:
    filters = _mapping_items(view, "filters")
    if not filters:
        return ""
    items: list[str] = []
    for item in filters:
        label = _text(item, "label")
        count = _integer(item, "count", default=0)
        href = _safe_local_url(item.get("href"), "filters.href")
        current = _boolean(item, "current", default=False)
        aria_current = ' aria-current="page"' if current else ""
        items.append(
            f'<li><a class="filter-link" href="{_escape(href)}"{aria_current}>'
            f"{_escape(label)} <span>{_escape(count)}</span></a></li>"
        )
    return '<nav class="filters" aria-label="筛选修改"><ul>' + "".join(items) + "</ul></nav>"


def _render_pagination(view: Mapping[str, object]) -> str:
    pagination = _as_mapping(view.get("pagination", {}), "pagination")
    page = _integer(pagination, "page", default=1, minimum=1)
    pages = _integer(pagination, "pages", default=1, minimum=1)
    total = _integer(pagination, "total", default=0)
    start = _integer(pagination, "start", default=0)
    end = _integer(pagination, "end", default=0)
    if page > pages:
        raise ViewModelError("pagination page cannot exceed total pages")
    if total == 0:
        if start != 0 or end != 0:
            raise ViewModelError("empty pagination must use a zero item range")
        range_label = "当前筛选下共 0 项"
    else:
        if not (1 <= start <= end <= total):
            raise ViewModelError("pagination item range is invalid")
        range_label = f"显示第 {start}–{end} 项，共 {total} 项"

    items: list[str] = []
    previous = pagination.get("previous_href")
    if previous is not None:
        previous_href = _safe_local_url(previous, "pagination.previous_href")
        items.append(
            f'<li><a class="filter-link" href="{_escape(previous_href)}">← 上一页</a></li>'
        )
    items.append(
        '<li><span class="filter-link" aria-current="page">'
        f"第 {_escape(page)} / {_escape(pages)} 页 · {_escape(range_label)}</span></li>"
    )
    following = pagination.get("next_href")
    if following is not None:
        next_href = _safe_local_url(following, "pagination.next_href")
        items.append(f'<li><a class="filter-link" href="{_escape(next_href)}">下一页 →</a></li>')
    return (
        '<nav class="filters pagination" aria-label="修改列表分页"><ul>'
        + "".join(items)
        + "</ul></nav>"
    )


def _render_change_card(
    card: Mapping[str, object],
    *,
    index: int,
    action: str,
    common_fields: Mapping[str, object],
    read_only: bool,
    return_filter: str,
    return_page: int,
) -> str:
    change_id = _text(card, "change_id")
    kind_label = _text(card, "kind_label")
    before = _text(card, "before", default="")
    after = _text(card, "after", default="")
    author = _text(card, "author", default="未记录")
    timestamp = _text(card, "timestamp", default="未记录")
    context = _optional_text(card, "context")
    safety = _choice(card, "safety", _SAFETY_STATES, default="manual")
    safety_label = _text(card, "safety_label", default=_SAFETY_LABELS[safety])
    decision = _choice(card, "decision", _DECISIONS, default="pending")
    edited_text = _text(card, "edited_text", default=after)
    reason = _text(card, "reason", default="")
    risk = _text(card, "risk_acknowledgement", default="")
    details = _mapping_items(card, "details")
    detail_rows = "".join(
        f"<dt>{_escape(_text(detail, 'label'))}</dt>"
        f"<dd><code>{_escape(_text(detail, 'value'))}</code></dd>"
        for detail in details
    )
    context_html = (
        f'<p class="change-card__context"><strong>上下文：</strong>{_escape(context)}</p>'
        if context is not None
        else ""
    )
    decision_form = ""
    if not read_only:
        fields_model = dict(common_fields)
        hidden = _hidden_fields(
            fields_model,
            extra=(
                ("change_id", change_id),
                ("return_filter", return_filter),
                ("return_page", return_page),
            ),
        )
        decision_form = (
            f'<form class="decision-form" method="post" action="{_escape(action)}">'
            f"{hidden}"
            f'<label for="edited-text-{index}">修改后采用的文字（仅用于“修改后采用”）</label>'
            f'<textarea id="edited-text-{index}" name="final_text" rows="3" '
            f'aria-describedby="edited-text-help-{index}">'
            f"{_escape(edited_text)}</textarea>"
            f'<p id="edited-text-help-{index}">此框中的文字只有点击“修改后采用”时才会'
            "作为该项决定保存；点击其他决定按钮不会采用此框内容。</p>"
            f'<label for="reason-{index}">决定说明（可选）</label>'
            f'<textarea id="reason-{index}" name="reason" rows="2">{_escape(reason)}</textarea>'
            f'<label for="risk-{index}">风险说明（高风险修改时填写）</label>'
            f'<textarea id="risk-{index}" name="risk_acknowledgement" rows="2">'
            f"{_escape(risk)}</textarea>"
            '<div class="decision-buttons">'
            '<button name="decision" value="accepted" type="submit">采用</button>'
            '<button name="decision" value="accepted_with_edit" type="submit">修改后采用</button>'
            '<button class="button--secondary" name="decision" value="rejected" '
            'type="submit">不采用</button>'
            '<button class="button--secondary" name="decision" value="manual" '
            'type="submit">留待人工</button>'
            '<button class="button--secondary" name="decision" value="conflict" '
            'type="submit">无法判断</button></div></form>'
        )
    card_title_id = f"change-{index}-title"
    safety_tone = "success" if safety == "safe" else "warning" if safety == "manual" else "danger"
    return (
        f'<article class="change-card change-card--{safety}" '
        f'aria-labelledby="{card_title_id}">'
        '<header class="change-card__header"><div>'
        f'<p class="eyebrow">修改 {index}</p><h2 id="{card_title_id}">'
        f"{_escape(kind_label)}</h2></div>"
        '<div class="change-card__badges">'
        f"{_status_badge(safety_tone, safety_label)}"
        f"{_status_badge('neutral', _DECISION_LABELS[decision])}</div></header>"
        f"{context_html}"
        '<div class="diff-grid"><section aria-labelledby="before-title-'
        f'{index}"><h3 id="before-title-{index}">修改前</h3>'
        f"<pre><del>{_escape(before)}</del></pre></section>"
        f'<section aria-labelledby="after-title-{index}"><h3 id="after-title-{index}">修改后</h3>'
        f"<pre><ins>{_escape(after)}</ins></pre></section></div>"
        '<dl class="change-meta">'
        f"<dt>审阅者</dt><dd>{_escape(author)}</dd>"
        f"<dt>时间</dt><dd>{_escape(timestamp)}</dd></dl>"
        '<details class="technical"><summary>技术详情</summary><dl>'
        f"<dt>修改 ID</dt><dd><code>{_escape(change_id)}</code></dd>"
        f"{detail_rows}</dl></details>{decision_form}</article>"
    )


def _render_approval(view: Mapping[str, object]) -> str:
    cards = _mapping_items(view, "changes")
    total = _integer(view, "total", default=len(cards))
    decided = _integer(view, "decided", default=0)
    if decided > total:
        raise ViewModelError("decided cannot exceed total")
    safe_count = _integer(view, "safe_pending_count", default=0)
    manual_count = _integer(view, "manual_pending_count", default=0)
    selected_filter = _choice(view, "selected_filter", _APPROVAL_FILTERS, default="all")
    selected_page = _integer(view, "selected_page", default=1, minimum=1)
    page_start = _integer(view, "page_start", default=1)
    if cards and page_start < 1:
        raise ViewModelError("non-empty approval page must start at item 1 or later")
    read_only = _boolean(view, "read_only", default=False)
    start_required = _boolean(view, "start_required", default=False)
    if start_required and not read_only:
        raise ViewModelError("start_required approval must be read-only")
    can_finalize = _boolean(view, "can_finalize", default=decided == total)
    if can_finalize and decided != total:
        raise ViewModelError("can_finalize requires every change to be decided")
    decision_action = _local_url(view, "decision_action", default="/approval/decision")
    common_fields = {"form_fields": _as_mapping(view.get("form_fields", {}), "form_fields")}
    pagination_html = _render_pagination(view)
    rendered_cards = "".join(
        _render_change_card(
            card,
            index=index,
            action=decision_action,
            common_fields=common_fields,
            read_only=read_only,
            return_filter=selected_filter,
            return_page=selected_page,
        )
        for index, card in enumerate(cards, start=page_start if page_start else 1)
    )
    if not rendered_cards:
        rendered_cards = '<div class="empty-state"><p>当前筛选下没有修改。</p></div>'
    bulk_forms: list[str] = []
    if safe_count and not read_only:
        bulk_action = _local_url(view, "bulk_action", default="/approval/accept-safe")
        bulk_forms.append(
            '<section class="bulk-card" aria-labelledby="bulk-title"><div>'
            '<h2 id="bulk-title">批量处理低风险正文</h2>'
            f"<p>仅采用 {_escape(safe_count)} 项精确映射的普通文字修改；"
            "公式、引用、结构、移动和冲突项不会包含在内。</p></div>"
            f'<form method="post" action="{_escape(bulk_action)}">{_hidden_fields(view)}'
            '<button class="button button--secondary" type="submit">'
            f"采用全部安全正文修改（{_escape(safe_count)} 项）</button></form></section>"
        )
    if manual_count and not read_only:
        manual_action = _local_url(view, "manual_bulk_action", default="/approval/mark-manual")
        bulk_forms.append(
            '<section class="bulk-card" aria-labelledby="manual-bulk-title"><div>'
            '<h2 id="manual-bulk-title">批量归入人工处理</h2>'
            f"<p>将剩余 {_escape(manual_count)} 项不可安全自动回填的修改标为“留待人工”；"
            "这些内容不会写入 LaTeX，完成审批前仍可逐项更改决定。</p></div>"
            f'<form method="post" action="{_escape(manual_action)}">{_hidden_fields(view)}'
            '<button class="button button--secondary" type="submit">'
            f"将全部不可自动回填项标为人工（{_escape(manual_count)} 项）</button></form></section>"
        )
    finalize = ""
    if can_finalize and not read_only:
        finalize_action = _local_url(view, "finalize_action", default="/approval/finalize")
        finalize = (
            '<section class="gate-card" aria-labelledby="first-gate-title">'
            '<div><p class="eyebrow">第一道闸门</p>'
            '<h2 id="first-gate-title">完成审批</h2>'
            "<p>这一步只保存你的决定，不会修改 LaTeX。下一页将单独预览补丁。</p></div>"
            f'<form method="post" action="{_escape(finalize_action)}">{_hidden_fields(view)}'
            '<button class="button button--primary" type="submit">完成审批并预览补丁</button>'
            "</form></section>"
        )
    readonly_notice = (
        '<p class="notice notice--info" role="status">审批已锁定，只能查看。</p>'
        if read_only and not start_required
        else ""
    )
    start_control = ""
    if start_required:
        start_action = _local_url(view, "start_action", default="/approval/start")
        start_control = (
            '<section class="gate-card" aria-labelledby="resume-approval-title">'
            '<div><p class="eyebrow">崩溃恢复</p>'
            '<h2 id="resume-approval-title">恢复审批账本</h2>'
            "<p>系统已核验返回 Word，但审批账本尚未建立。点击后只创建第一版密封账本，"
            "不会替你决定任何修改。</p></div>"
            f'<form method="post" action="{_escape(start_action)}">{_hidden_fields(view)}'
            '<button class="button button--primary" type="submit">恢复并开始逐项审批</button>'
            "</form></section>"
        )
    progress_max = total if total else 1
    return (
        '<section class="page-heading"><p class="eyebrow">第 3 步</p>'
        "<h1>逐项审批 Word 修改</h1>"
        "<p>先确认每一项是否采用。审批不会直接修改任何 LaTeX 文件。</p></section>"
        '<section class="progress-card" aria-labelledby="approval-progress-title">'
        '<div><h2 id="approval-progress-title">审批进度</h2>'
        f"<p>已决定 {_escape(decided)} / {_escape(total)} 项</p></div>"
        f'<progress value="{_escape(decided)}" max="{_escape(progress_max)}">'
        f"{_escape(decided)}/{_escape(total)}</progress></section>"
        f"{readonly_notice}{start_control}{_render_filters(view)}{''.join(bulk_forms)}"
        f'{pagination_html}<section id="approval-change-list" class="change-list" '
        f'aria-label="修改列表">{rendered_cards}</section>{pagination_html}{finalize}'
    )


def _render_patch(view: Mapping[str, object]) -> str:
    counts = _as_mapping(view.get("counts", {}), "counts")
    count_rows = (
        ("将自动应用", _integer(counts, "automatic", default=0), "success"),
        ("留待人工", _integer(counts, "manual", default=0), "warning"),
        ("不采用", _integer(counts, "rejected", default=0), "neutral"),
        ("存在冲突", _integer(counts, "conflict", default=0), "danger"),
    )
    summaries = "".join(
        f'<li class="summary-tile summary-tile--{tone}"><strong>{_escape(value)}</strong>'
        f"<span>{label}</span></li>"
        for label, value, tone in count_rows
    )
    files = _mapping_items(view, "files")
    rendered_files: list[str] = []
    for file_item in files:
        path = _text(file_item, "path")
        change_count = _integer(file_item, "change_count", default=0)
        preview = _text(file_item, "preview", default="")
        rendered_files.append(
            '<article class="patch-file"><header>'
            f"<h3><code>{_escape(path)}</code></h3>"
            f"<p>{_escape(change_count)} 项自动修改</p></header>"
            f'<details><summary>查看差异预览</summary><pre aria-label="{_escape(path)} 的差异">'
            f"{_escape(preview)}</pre></details></article>"
        )
    file_content = (
        "".join(rendered_files)
        if rendered_files
        else '<div class="empty-state"><p>没有需要自动应用的修改。</p></div>'
    )
    ready = _boolean(view, "ready", default=True)
    prepare_required = _boolean(view, "prepare_required", default=False)
    revise_required = _boolean(view, "revise_required", default=False)
    if sum((ready, prepare_required, revise_required)) > 1:
        raise ViewModelError("patch actions must be mutually exclusive")
    back_action = _local_url(view, "back_action", default="/approval")
    confirm_action = _local_url(view, "confirm_action", default="/result/generate")
    if ready:
        confirmation = (
            f'<form class="confirmation-form" method="post" action="{_escape(confirm_action)}">'
            f"{_hidden_fields(view)}"
            '<label class="confirmation-check"><input name="confirm_apply" type="checkbox" '
            'value="yes" required><span>我已检查补丁摘要，并确认只回填到新的 LaTeX 工作副本。'
            "</span></label>"
            '<button class="button button--primary button--large" type="submit">'
            "确认回填到新 LaTeX 副本并生成结果</button></form>"
        )
    elif prepare_required:
        prepare_action = _local_url(view, "prepare_action", default="/patch/prepare")
        confirmation = (
            "<div><p>审批已经密封，但补丁预览尚未发布。显式恢复会从密封输入重新计算"
            "预览，不会应用补丁。</p>"
            f'<form method="post" action="{_escape(prepare_action)}">{_hidden_fields(view)}'
            '<button class="button button--primary" type="submit">恢复并生成补丁预览</button>'
            "</form></div>"
        )
    elif revise_required:
        revise_action = _local_url(view, "revise_action", default="/approval/revise")
        confirmation = (
            "<div><p>当前计划包含已采用但不能安全自动回填的修改。重新审批会保留旧审批和"
            "旧计划作为不可变历史，并建立一份可编辑的新审批草稿；此操作不会写入 LaTeX。</p>"
            f'<form method="post" action="{_escape(revise_action)}">{_hidden_fields(view)}'
            '<button class="button button--primary" type="submit">重新审批阻断项</button>'
            "</form></div>"
        )
    else:
        confirmation = (
            '<div class="notice notice--danger" role="alert"><h2>暂时不能生成</h2>'
            "<p>补丁计划仍有阻断项。返回审批，将相关修改改为人工处理后再试。</p></div>"
        )
    return (
        '<section class="page-heading"><p class="eyebrow">第 4 步 · 第二道闸门</p>'
        "<h1>预览并回填到新 LaTeX 副本</h1>"
        "<p>此页是独立的应用确认。原始 LaTeX 和返回 Word 始终保持不变。</p></section>"
        f'<ul class="summary-grid" aria-label="补丁摘要">{summaries}</ul>'
        '<section aria-labelledby="files-title"><h2 id="files-title">按文件查看</h2>'
        f'<div class="patch-files">{file_content}</div></section>'
        '<section class="gate-card gate-card--final" aria-labelledby="second-gate-title">'
        '<div><p class="eyebrow">第二道闸门</p><h2 id="second-gate-title">生成新 LaTeX 副本</h2>'
        "<p>程序会再次核验源文件哈希、精确字节范围、重叠和安全策略。"
        "任何漂移都会停止，不会猜测位置。</p></div>"
        f"{confirmation}</section>"
        f'<p><a class="text-link" href="{_escape(back_action)}">← 返回审批修改</a></p>'
    )


def _render_result(view: Mapping[str, object]) -> str:
    status = _choice(view, "status", _RESULT_STATES, default="complete")
    artifacts = _mapping_items(view, "artifacts")
    warnings = _string_items(view, "warnings")
    rendered_artifacts: list[str] = []
    for artifact in artifacts:
        label = _text(artifact, "label")
        description = _text(artifact, "description")
        available = _boolean(artifact, "available", default=True)
        open_in_folder = _boolean(artifact, "open_in_folder", default=False)
        if available:
            if open_in_folder:
                if artifact.get("href") is not None:
                    raise ViewModelError("folder-only artifact cannot also have an href")
                action = (
                    '<span class="artifact-card__missing">'
                    "请使用下方“打开修订后的 LaTeX 文件夹”查看</span>"
                )
            else:
                href = _safe_local_url(artifact.get("href"), "artifacts.href")
                action = f'<a class="button button--secondary" href="{_escape(href)}">打开</a>'
            state = _status_badge("success", "已生成")
        else:
            action = '<span class="artifact-card__missing">暂不可用</span>'
            state = _status_badge("warning", "未生成")
        rendered_artifacts.append(
            '<li class="artifact-card"><div>'
            f"<h3>{_escape(label)}</h3><p>{_escape(description)}</p>{state}</div>{action}</li>"
        )
    artifact_content = (
        '<ul class="artifact-list">' + "".join(rendered_artifacts) + "</ul>"
        if rendered_artifacts
        else '<div class="empty-state"><p>没有可展示的产物。</p></div>'
    )
    warning_content = ""
    if warnings:
        warning_content = (
            '<section class="notice notice--warning" role="status" '
            'aria-labelledby="partial-details-title"><h2 id="partial-details-title">'
            "尚未完成的内容</h2><ul>"
            + "".join(f"<li>{_escape(item)}</li>" for item in warnings)
            + "</ul></section>"
        )
    open_revised_action = _local_url(view, "open_revised_action", default="/result/open-revised")
    open_delivery_action = _local_url(view, "open_delivery_action", default="/result/open-delivery")
    revised_source_available = _boolean(view, "revised_source_available", default=False)
    delivery_available = _boolean(view, "delivery_available", default=False)
    open_actions = ""
    if revised_source_available:
        open_actions += (
            f'<form method="post" action="{_escape(open_revised_action)}">'
            f'{_hidden_fields(view)}<button class="button button--primary" type="submit">'
            "打开修订后的 LaTeX 文件夹</button></form>"
        )
    if delivery_available:
        open_actions += (
            f'<form method="post" action="{_escape(open_delivery_action)}">'
            f'{_hidden_fields(view)}<button class="button button--secondary" type="submit">'
            "打开 PDF 与账本交付文件夹</button></form>"
        )
    retry = ""
    can_retry = _boolean(view, "can_retry", default=status == "partial")
    if status == "partial" and can_retry:
        retry_action = _local_url(view, "retry_action", default="/result/retry")
        retry_label = _text(view, "retry_label", default="重试缺失结果")
        retry = (
            f'<form method="post" action="{_escape(retry_action)}">{_hidden_fields(view)}'
            '<button class="button button--secondary" type="submit">'
            f"{_escape(retry_label)}</button></form>"
        )
    new_action = _local_url(view, "new_action", default="/new")
    new_fields = {
        "form_fields": _as_mapping(
            view.get("new_form_fields", view.get("form_fields", {})),
            "new_form_fields",
        )
    }
    exit_action = _local_url(view, "exit_action", default="/app/exit")
    exit_fields = {"form_fields": _as_mapping(view.get("exit_form_fields", {}), "exit_form_fields")}
    heading = (
        "新 LaTeX 副本与全部结果已生成"
        if status == "complete"
        else "新 LaTeX 副本已生成，部分结果待补齐"
    )
    intro = (
        "新的 LaTeX 工作副本、核验结果和审计记录已经准备好，原稿未被覆盖。"
        if status == "complete"
        else "修订后的 LaTeX 已安全保留；缺失工具不会让整轮审批作废。"
    )
    tone = "success" if status == "complete" else "warning"
    result_icon = "✓" if status == "complete" else "!"
    return (
        f'<section class="result-hero result-hero--{tone}" aria-labelledby="result-title">'
        f'<div class="result-hero__icon" aria-hidden="true">{result_icon}</div>'
        f'<div><p class="eyebrow">第 4 步完成</p><h1 id="result-title">{heading}</h1>'
        f"<p>{intro}</p></div></section>{warning_content}"
        '<section aria-labelledby="artifacts-title"><h2 id="artifacts-title">交付结果</h2>'
        f"{artifact_content}</section>"
        '<div class="actions">'
        f"{open_actions}"
        f"{retry}"
        f'<form method="post" action="{_escape(new_action)}">{_hidden_fields(new_fields)}'
        '<button class="button button--secondary" type="submit">新建审阅</button></form>'
        f'<form method="post" action="{_escape(exit_action)}">{_hidden_fields(exit_fields)}'
        '<button class="button button--secondary" type="submit">退出程序</button></form></div>'
    )


def _render_shutdown(view: Mapping[str, object]) -> str:
    waiting_for_jobs = _boolean(view, "waiting_for_jobs", default=False)
    detail = (
        "本地服务器已停止接收新操作，正在等待后台任务安全结束。任务完成后程序会自动退出。"
        if waiting_for_jobs
        else "本地服务器已停止，程序可以安全关闭。"
    )
    return (
        '<section class="result-hero result-hero--success" aria-labelledby="shutdown-title">'
        '<div class="result-hero__icon" aria-hidden="true">✓</div><div>'
        '<p class="eyebrow">Windows 本机程序</p>'
        '<h1 id="shutdown-title">已请求退出</h1>'
        f"<p>{_escape(detail)}</p></div></section>"
        '<section class="notice notice--info" role="status"><h2>此页面无需继续操作</h2>'
        "<p>可以关闭浏览器标签页。论文原稿、返回 Word 和已经密封的任务证据不会被删除。</p>"
        "</section>"
    )


def _split_error_issue_summary(
    explicit_summary: str | None,
    technical_details: str | None,
) -> tuple[str | None, str | None]:
    """Elevate only the exact path-free image summary emitted by the diagnostic layer."""

    if explicit_summary is not None and _IMAGE_ISSUE_SUMMARY_RE.fullmatch(explicit_summary) is None:
        raise ViewModelError("error.summary is not a supported path-free summary")
    technical_summary: str | None = None
    remaining: list[str] = []
    for line in technical_details.splitlines() if technical_details is not None else ():
        if line.startswith(_IMAGE_ISSUE_SUMMARY_PREFIX):
            candidate = line.removeprefix(_IMAGE_ISSUE_SUMMARY_PREFIX)
            if _IMAGE_ISSUE_SUMMARY_RE.fullmatch(candidate) is not None:
                if technical_summary is not None:
                    raise ViewModelError("technical_details contains duplicate problem summaries")
                technical_summary = candidate
                continue
        remaining.append(line)
    if (
        explicit_summary is not None
        and technical_summary is not None
        and explicit_summary != technical_summary
    ):
        raise ViewModelError("error summary and technical details disagree")
    summary = explicit_summary or technical_summary
    kept_details = "\n".join(remaining) or None
    return summary, kept_details


def render_error_card(error: Mapping[str, object]) -> str:
    """Render one actionable Chinese error card from a strict plain mapping."""

    error = _as_mapping(error, "error")
    code = _text(error, "code")
    title = _text(error, "title")
    message = _text(error, "message")
    guidance = _string_items(error, "guidance")
    technical_details = _optional_text(error, "technical_details")
    summary, technical_details = _split_error_issue_summary(
        _optional_text(error, "summary"),
        technical_details,
    )
    guidance_html = (
        "<ol>" + "".join(f"<li>{_escape(item)}</li>" for item in guidance) + "</ol>"
        if guidance
        else "<p>请保存支持包，并在项目页面反馈此问题。</p>"
    )
    details = (
        '<details class="technical"><summary>技术详情</summary>'
        f"<pre>{_escape(technical_details)}</pre></details>"
        if technical_details is not None
        else ""
    )
    summary_html = (
        f'<p class="error-card__summary"><strong>{_escape(summary)}</strong></p>'
        if summary is not None
        else ""
    )
    actions: list[str] = []
    primary_action_value = error.get("primary_action")
    if primary_action_value is not None:
        primary_action = _as_mapping(primary_action_value, "error.primary_action")
        kind = _choice(
            primary_action,
            "kind",
            frozenset({"link", "post"}),
            default="link",
        )
        label = _text(primary_action, "label")
        href = _safe_local_url(primary_action.get("href"), "error.primary_action.href")
        if kind == "link":
            if "form_fields" in primary_action:
                raise ViewModelError("link recovery action must not contain form_fields")
            actions.append(
                f'<a class="button button--primary" href="{_escape(href)}">{_escape(label)}</a>'
            )
        else:
            hidden_fields = _hidden_fields(primary_action)
            actions.append(
                f'<form method="post" action="{_escape(href)}">{hidden_fields}'
                f'<button class="button button--primary" type="submit">'
                f"{_escape(label)}</button></form>"
            )
    back_href = error.get("back_href")
    if back_href is not None:
        back = _safe_local_url(back_href, "error.back_href")
        actions.append(f'<a class="button button--secondary" href="{_escape(back)}">返回</a>')
    return (
        '<section class="error-card" role="alert" aria-labelledby="error-title">'
        '<div class="error-card__icon" aria-hidden="true">!</div><div>'
        f'<p class="error-card__code">错误代码：<code>{_escape(code)}</code></p>'
        f'<h1 id="error-title">{_escape(title)}</h1>{summary_html}'
        f"<p>{_escape(message)}</p>"
        f'<section aria-labelledby="next-steps-title"><h2 id="next-steps-title">可以这样处理</h2>'
        f"{guidance_html}</section>{details}"
        f'<div class="actions">{"".join(actions)}</div></div></section>'
    )


def _render_error(view: Mapping[str, object]) -> str:
    error = _as_mapping(view.get("error"), "error")
    return render_error_card(error)


def _render_page_body(page: str, view: Mapping[str, object]) -> str:
    if page == "home":
        return _render_home(view)
    if page == "delete_confirm":
        return _render_delete_confirm(view)
    if page == "preflight":
        return _render_preflight(view)
    if page == "progress":
        return _render_progress(view)
    if page == "waiting_word":
        return _render_waiting_word(view)
    if page == "approval":
        return _render_approval(view)
    if page == "patch":
        return _render_patch(view)
    if page == "result":
        return _render_result(view)
    if page == "shutdown":
        return _render_shutdown(view)
    if page == "error":
        return _render_error(view)
    raise ViewModelError(f"unsupported page: {page}")


def render_app_page(view: Mapping[str, object]) -> str:
    """Render a complete Chinese local-application page from a plain mapping.

    Common optional keys are ``title``, ``step``, ``project_name``, ``notices``, and
    ``form_fields``.  Page-specific keys are intentionally simple strings, integers,
    booleans, sequences of strings, or sequences of mappings so the application layer
    can serialize and test its view model without importing a UI framework.
    """

    view = _as_mapping(view, "view")
    page_value = view.get("page")
    if not isinstance(page_value, str) or page_value not in SUPPORTED_PAGES:
        raise ViewModelError(f"page must be one of {SUPPORTED_PAGES}")
    default_title, default_step = _PAGE_DEFAULTS[page_value]
    title = _text(view, "title", default=default_title)
    step = _integer(view, "step", default=default_step, minimum=1)
    if step > 4:
        raise ViewModelError("step must be between 1 and 4")
    project_name = _optional_text(view, "project_name")
    project_context = (
        f'<p class="app-header__project">当前任务：<strong>{_escape(project_name)}</strong></p>'
        if project_name is not None
        else ""
    )
    page_body = _render_page_body(page_value, view)
    body = page_body if page_value == "waiting_word" else _render_notices(view) + page_body
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_escape(title)}</title>"
        f'<link rel="stylesheet" href="{APP_STYLESHEET_PATH}">'
        '</head><body><a class="skip-link" href="#main-content">跳到主要内容</a>'
        '<header class="app-header"><div class="app-header__inner">'
        '<a class="brand" href="/" aria-label="LaTeX–Word 审阅助手首页">'
        '<span class="brand__mark" aria-hidden="true">LW</span>'
        "<span>LaTeX–Word 审阅助手</span></a>"
        '<div class="app-header__status"><span class="privacy-badge">仅在本机运行</span>'
        f"{project_context}</div></div></header>"
        f'<div class="app-shell">{render_stepper(step)}'
        f'<main id="main-content" class="page page--{page_value}" tabindex="-1">{body}</main>'
        '<footer class="app-footer"><p>原稿与返回 Word 保持不变；只有明确批准的安全正文'
        "才可能写入新副本。</p></footer></div></body></html>"
    )
