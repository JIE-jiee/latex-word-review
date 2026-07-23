"""Build strict local-application view models from sealed session evidence.

The presenter is deliberately read-only.  It rebuilds the session status, reads
only validated contract artifacts below the run root, and returns plain mappings
accepted by :func:`latex_word_review.app_views.render_app_page`.  It never uses a
UI cache and never exposes an absolute filesystem path.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast
from urllib.parse import urlencode, urlsplit

from latex_word_review.application import ApplicationSession
from latex_word_review.contracts import compute_payload_sha256
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.hashing import digest_bytes, read_stable_bytes
from latex_word_review.jsonio import read_contract_file
from latex_word_review.paths import resolve_within, validate_relative_path
from latex_word_review.user_messages import (
    RecoveryAction,
    format_safe_technical_details,
    safe_diagnostic_details,
    user_message_for_error,
)

_ROUTE_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SUPPORT_TOKEN_RE: Final = re.compile(
    r"support1\.E_[A-Z0-9_]{1,94}\.[a-z][a-z0-9_-]{0,63}\.[0-9a-f]{64}"
)
_MAX_DIFF_BYTES: Final = 16 * 1024 * 1024
_APPROVAL_FILTERS: Final = ("all", "pending", "safe", "manual", "conflict")

_APPROVAL_PAGE_SIZE: Final = 25
_KIND_LABELS: Final[dict[str, str]] = {
    "insertion": "插入文字",
    "deletion": "删除文字",
    "replacement": "替换文字",
    "move": "移动内容",
    "comment": "Word 批注",
    "format": "格式修改",
    "unknown": "未知修改",
}
_FILTER_LABELS: Final[dict[str, str]] = {
    "all": "全部",
    "pending": "待决定",
    "safe": "安全正文",
    "manual": "人工处理",
    "conflict": "冲突",
}
_ARTIFACT_PRESENTATION: Final[dict[str, tuple[str, str]]] = {
    "revised_source": ("修订后的 LaTeX 副本", "只包含已批准自动修改的新工作副本。"),
    "revised_clean_pdf": ("修订后 PDF", "由修订副本编译并通过当前核验的 PDF。"),
    "latexdiff_pdf": ("带修订标记的 PDF", "使用 latexdiff 生成的可视化修改版本。"),
    "latexdiff_tex": ("带修订标记的 LaTeX", "可复现修订标记 PDF 的 latexdiff 源文件。"),
    "verification_report": ("核验报告", "记录编译、差异和原件完整性检查。"),
    "ledger_html": ("审阅账本", "适合直接阅读的作者、时间、决定和证据记录。"),
    "ledger_json": ("机器可读账本", "用于审计和后续工具处理的结构化记录。"),
    "audit_bundle": ("审计包", "绑定本轮密封证据和交付文件的 ZIP。"),
    "run_manifest": ("运行清单", "记录交付物、工具和对象哈希绑定。"),
    "actual_diff": ("实际差异", "核验阶段重新计算的源文件差异。"),
    "compile_log": ("编译日志", "用于诊断 LaTeX 编译问题的脱敏日志。"),
}
_ARTIFACT_ORDER: Final = tuple(_ARTIFACT_PRESENTATION)
_COMPATIBILITY_OUTPUT_METRICS: Final[tuple[tuple[str, str], ...]] = (
    ("omml_objects", "Word 原生公式"),
    ("image_instances", "图片实例"),
    ("tables", "表格"),
    ("live_fields", "编号/引用等特殊字段"),
)


class PresenterModelError(ValueError):
    """Raised when an alleged product status has an invalid shape."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PresenterModelError(f"{label} must be a string-keyed mapping")
    return cast("Mapping[str, Any]", value)


def _sequence(value: object, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise PresenterModelError(f"{label} must be a sequence")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PresenterModelError(f"{label} must be a non-empty string")
    return value


def _route_token(value: str, label: str) -> str:
    if _ROUTE_TOKEN_RE.fullmatch(value) is None:
        raise PresenterModelError(f"{label} is not a safe local route token")
    return value


def _common_fields(csrf_token: str, session_key: str) -> dict[str, object]:
    if not isinstance(csrf_token, str) or not csrf_token:
        raise PresenterModelError("csrf_token must be a non-empty string")
    return {"csrf": csrf_token, "session": session_key}


def _safe_recovery_href(value: str) -> str:
    """Validate an internal controller-provided recovery destination."""

    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or chr(92) in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise PresenterModelError("recovery href must be a same-origin local URL")
    return value


def build_error_recovery_control(
    action: RecoveryAction,
    action_label: str,
    *,
    csrf_token: str | None = None,
    session_key: str | None = None,
    retry_href: str | None = None,
    support_token: str | None = None,
    allow_session_actions: bool = True,
) -> dict[str, object] | None:
    """Bind a logical recovery action to one real local HTTP operation."""

    if not isinstance(action_label, str) or not action_label:
        raise PresenterModelError("recovery action label must be non-empty")
    safe_key = _route_token(session_key, "session_key") if session_key is not None else None

    def post(href: str, fields: Mapping[str, object]) -> dict[str, object]:
        return {
            "kind": "post",
            "label": action_label,
            "href": _safe_recovery_href(href),
            "form_fields": dict(fields),
        }

    def link(href: str) -> dict[str, object]:
        return {
            "kind": "link",
            "label": action_label,
            "href": _safe_recovery_href(href),
        }

    if action in {"choose_project", "start_new_round"}:
        if csrf_token is None:
            return None
        if not isinstance(csrf_token, str) or not csrf_token:
            raise PresenterModelError("csrf_token must be a non-empty string")
        return post("/new", {"csrf": csrf_token})
    if action == "retry":
        return link(retry_href) if retry_href is not None else None
    if safe_key is None:
        return None
    if not allow_session_actions and action != "create_support_bundle":
        return None
    if action == "review_changes":
        return link(f"/session/{safe_key}?filter=all")
    if action == "install_tool":
        return link(f"/session/{safe_key}")
    if csrf_token is None:
        return None
    fields = _common_fields(csrf_token, safe_key)
    if action == "choose_returned_word":
        return post("/session/receive", fields)
    if action == "open_results":
        return post("/result/open-revised", fields)
    if action == "create_support_bundle":
        if support_token is None:
            return None
        if _SUPPORT_TOKEN_RE.fullmatch(support_token) is None:
            raise PresenterModelError("support token is not a bounded signed token")
        return post(
            "/support/export",
            fields | {"support_token": support_token},
        )
    raise PresenterModelError(f"unsupported recovery action: {action}")


def _artifacts(status: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(status.get("artifacts"), "status.artifacts")


def _artifact_relative(status: Mapping[str, Any], key: str) -> str:
    value = _artifacts(status).get(key)
    return validate_relative_path(_string(value, f"status.artifacts.{key}"))


def _artifact_path(run_root: Path, status: Mapping[str, Any], key: str) -> Path:
    return resolve_within(run_root, _artifact_relative(status, key))


def _contract_artifact(
    run_root: Path,
    status: Mapping[str, Any],
    key: str,
    schema_name: str,
) -> dict[str, Any]:
    return read_contract_file(
        _artifact_path(run_root, status, key),
        expected_schema=schema_name,
    )


def _project_name(status: Mapping[str, Any]) -> str:
    main_document = validate_relative_path(
        _string(status.get("main_document"), "status.main_document")
    )
    name = PurePosixPath(main_document).stem.strip()
    return name or main_document


def _base_view(
    status: Mapping[str, Any],
    *,
    csrf_token: str,
    session_key: str,
) -> dict[str, object]:
    step = status.get("step")
    if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= 4:
        raise PresenterModelError("status.step must be an integer from 1 to 4")
    return {
        "step": step,
        "project_name": _project_name(status),
        "form_fields": _common_fields(csrf_token, session_key),
        "exit_form_fields": {"csrf": csrf_token},
    }


def _preflight_view(
    run_root: Path,
    status: Mapping[str, Any],
    *,
    csrf_token: str,
    session_key: str,
) -> dict[str, object]:
    main_document = validate_relative_path(
        _string(status.get("main_document"), "status.main_document")
    )
    main_path = _artifact_path(run_root, status, "main_document")
    if not main_path.is_file():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "sealed main document is not a file")
    return _base_view(status, csrf_token=csrf_token, session_key=session_key) | {
        "page": "preflight",
        "title": "确认论文并生成审阅 Word",
        "main_file": main_document,
        "source_location": "本机只读任务快照（不会覆盖原稿）",
        "checks": [
            {"status": "ok", "label": "主文件", "message": "已从密封快照确认。"},
            {"status": "ok", "label": "保存方式", "message": "后续结果写入新的工作副本。"},
            {"status": "info", "label": "隐私", "message": "论文内容不会上传到云端。"},
        ],
        "can_continue": True,
        "choose_form_fields": {"csrf": csrf_token},
        "choose_action": "/new",
        "continue_action": "/session/export-existing",
    }


def _waiting_view(
    run_root: Path,
    status: Mapping[str, Any],
    *,
    csrf_token: str,
    session_key: str,
) -> dict[str, object]:
    review_relative = _artifact_relative(status, "review_docx")
    review_path = resolve_within(run_root, review_relative)
    if not review_path.is_file():
        raise ContractError(ErrorCode.SCHEMA_INVALID, "sealed review Word is not a file")
    export_report = read_contract_file(
        resolve_within(run_root, validate_relative_path("export/objects/export-report.json")),
        expected_schema="ExportReport",
    )
    exported_at = _string(export_report.get("generated_at"), "ExportReport.generated_at")
    report_payload = _mapping(export_report.get("payload"), "ExportReport.payload")
    export_status = _string(report_payload.get("status"), "ExportReport.payload.status")
    findings = _sequence(report_payload.get("findings"), "ExportReport.payload.findings")
    metrics = _mapping(report_payload.get("metrics"), "ExportReport.payload.metrics")
    output_metrics = _mapping(metrics.get("output"), "ExportReport.payload.metrics.output")
    compatibility_counts: list[dict[str, object]] = []
    for metric_name, label in _COMPATIBILITY_OUTPUT_METRICS:
        value = output_metrics.get(metric_name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PresenterModelError(
                f"ExportReport.payload.metrics.output.{metric_name} must be a non-negative integer"
            )
        compatibility_counts.append({"label": label, "count": value})
    body_text_summary: tuple[int, int, str] | None = None
    for index, item in enumerate(
        _sequence(report_payload.get("feature_results"), "ExportReport.payload.feature_results")
    ):
        feature = _mapping(item, f"ExportReport.payload.feature_results[{index}]")
        if feature.get("feature") != "body_text":
            continue
        if body_text_summary is not None:
            raise PresenterModelError("ExportReport contains duplicate body_text feature results")
        source_count = feature.get("source_count")
        output_count = feature.get("output_count")
        if (
            isinstance(source_count, bool)
            or not isinstance(source_count, int)
            or source_count < 0
            or isinstance(output_count, bool)
            or not isinstance(output_count, int)
            or output_count < 0
            or output_count > source_count
        ):
            raise PresenterModelError(
                "body_text feature counts must be valid non-negative integers"
            )
        body_text_summary = (
            output_count,
            source_count,
            _string(feature.get("status"), "body_text feature status"),
        )
    word_paragraphs = output_metrics.get("paragraphs")
    if word_paragraphs is not None and (
        isinstance(word_paragraphs, bool)
        or not isinstance(word_paragraphs, int)
        or word_paragraphs < 0
    ):
        raise PresenterModelError("ExportReport output paragraphs must be a non-negative integer")
    warning_count = sum(
        1
        for index, item in enumerate(findings)
        if _mapping(item, f"ExportReport.payload.findings[{index}]").get("severity") == "warning"
    )
    notices: list[dict[str, str]] = []
    if export_status == "partial":
        notices.append(
            {
                "tone": "warning",
                "title": "审阅稿已生成，但有转换提示",
                "message": (
                    f"共 {warning_count} 项可恢复提示。图片完整性和 Word 结构已通过校验；"
                    "期刊专用格式或无法精确映射的内容不会被自动回填，请在发送前浏览审阅稿。"
                ),
            }
        )
    if body_text_summary is not None:
        exact_units, source_units, mapping_status = body_text_summary
        paragraph_context = (
            f"审阅 Word 共 {word_paragraphs} 个段落；"
            if isinstance(word_paragraphs, int) and not isinstance(word_paragraphs, bool)
            else ""
        )
        notices.append(
            {
                "tone": "warning" if mapping_status != "preserved" else "info",
                "title": "请先确认可自动回填的正文范围",
                "message": (
                    f"{paragraph_context}程序只为 {exact_units}/{source_units} 个可安全提取的"
                    "普通正文单元建立了精确定位锚点。只有锚点内的普通正文修订可能自动回填；"
                    "这个数字不是整篇论文覆盖率，公式、引用、标题、表格和其他结构内容仍需"
                    "人工处理。"
                ),
            }
        )
    return _base_view(status, csrf_token=csrf_token, session_key=session_key) | {
        "page": "waiting_word",
        "title": "Word 审阅交接与修改稿导入",
        "review_docx_name": PurePosixPath(review_relative).name,
        "exported_at": exported_at,
        "export_status": export_status,
        "export_warning_count": warning_count,
        "compatibility_counts": compatibility_counts,
        "notices": notices,
        "open_action": "/session/open-review-docx",
        "save_copy_action": "/session/save-review-copy",
        "receive_action": "/session/receive",
        "reviewer_message": "请在 Word 中保留修订和批注；不要接受全部修订。",
        "return_flow_steps": [
            "选择导师或合作者返回的 .docx，原件会只读归档。",
            "进入逐项审批，决定采用、不采用或留待人工处理。",
            "预览回填内容并再次确认后，生成新的 LaTeX 副本。",
        ],
    }


def _display_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decision_map(approval: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if approval is None:
        return {}
    payload = _mapping(approval.get("payload"), "ApprovalSet.payload")
    decisions: dict[str, Mapping[str, Any]] = {}
    for item in _sequence(payload.get("decisions"), "ApprovalSet.payload.decisions"):
        decision = _mapping(item, "ApprovalSet decision")
        change_id = _string(decision.get("change_id"), "decision.change_id")
        decisions[change_id] = decision
    return decisions


def _is_bulk_safe(change: Mapping[str, Any]) -> bool:
    """Mirror ``record_bulk_decision(operation='accept_all_safe')`` exactly."""

    resolution = _mapping(change.get("resolution"), "change.resolution")
    return (
        change.get("safety_class") == "plain_text_candidate" and resolution.get("status") == "exact"
    )


def _card_safety(change: Mapping[str, Any], decision: str) -> str:
    resolution = _mapping(change.get("resolution"), "change.resolution")
    if decision == "conflict" or resolution.get("status") in {
        "conflict",
        "unmatched",
        "unsupported",
    }:
        return "conflict"
    return "safe" if _is_bulk_safe(change) else "manual"


def _source_context(change: Mapping[str, Any]) -> tuple[str | None, list[dict[str, str]]]:
    details: list[dict[str, str]] = []
    comment = change.get("comment")
    context_parts = [comment] if isinstance(comment, str) and comment else []
    location_value = change.get("source_location")
    if location_value is not None:
        location = _mapping(location_value, "change.source_location")
        path = validate_relative_path(_string(location.get("path"), "source_location.path"))
        start_line = location.get("start_line")
        end_line = location.get("end_line")
        line_text = ""
        if isinstance(start_line, int) and not isinstance(start_line, bool):
            line_text = f"，第 {start_line} 行"
            if isinstance(end_line, int) and end_line != start_line:
                line_text = f"，第 {start_line}–{end_line} 行"
        context_parts.append(f"{path}{line_text}")
        details.extend(
            [
                {"label": "源文件", "value": path},
                {
                    "label": "UTF-8 字节范围",
                    "value": f"{location.get('start_byte')}..{location.get('end_byte')}",
                },
                {"label": "源切片哈希", "value": _display_value(location.get("slice_sha256"))},
            ]
        )
    else:
        details.append({"label": "源位置", "value": "未精确定位"})
    return ("；".join(context_parts) if context_parts else None), details


def _change_card(
    change: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
) -> dict[str, object]:
    change_id = _string(change.get("change_id"), "change.change_id")
    current = (
        _string(decision.get("decision"), "decision.decision")
        if decision is not None
        else _string(change.get("initial_decision"), "change.initial_decision")
    )
    context, location_details = _source_context(change)
    resolution = _mapping(change.get("resolution"), "change.resolution")
    safety = _card_safety(change, current)
    safety_labels = {
        "safe": "精确定位的普通正文",
        "manual": "公式、引用、结构或高风险项：仅人工处理",
        "conflict": "定位冲突：不会自动应用",
    }
    details = [
        {"label": "原始类型", "value": _display_value(change.get("native_kind")) or "未记录"},
        {"label": "定位状态", "value": _display_value(resolution.get("status"))},
        {"label": "定位方法", "value": _display_value(resolution.get("method"))},
        {"label": "置信度", "value": _display_value(resolution.get("confidence"))},
        {"label": "安全类别", "value": _display_value(change.get("safety_class"))},
        {"label": "变更指纹", "value": _display_value(change.get("change_fingerprint"))},
        *location_details,
    ]
    edited_text = _display_value(change.get("after"))
    reason = ""
    risk = ""
    if decision is not None:
        if decision.get("decision") == "accepted_with_edit":
            edited_text = _display_value(decision.get("final_text"))
        reason = _display_value(decision.get("reason"))
        risk = _display_value(decision.get("risk_acknowledgement"))
    card: dict[str, object] = {
        "change_id": change_id,
        "kind_label": _KIND_LABELS.get(_display_value(change.get("kind")), "其他修改"),
        "before": _display_value(change.get("before")),
        "after": _display_value(change.get("after")),
        "author": _display_value(change.get("author")) or "未记录",
        "timestamp": _display_value(change.get("timestamp")) or "未记录",
        "safety": safety,
        "safety_label": safety_labels[safety],
        "decision": current,
        "edited_text": edited_text,
        "reason": reason,
        "risk_acknowledgement": risk,
        "details": details,
    }
    if context is not None:
        card["context"] = context
    return card


def _approval_view(
    run_root: Path,
    status: Mapping[str, Any],
    *,
    csrf_token: str,
    session_key: str,
    selected_filter: str,
    selected_page: int,
) -> dict[str, object]:
    if isinstance(selected_page, bool) or selected_page < 1:
        raise ContractError(ErrorCode.SCHEMA_INVALID, "approval page must be a positive integer")
    changeset = _contract_artifact(run_root, status, "changeset", "ChangeSet")
    changeset_payload = _mapping(changeset.get("payload"), "ChangeSet.payload")
    changes = [
        _mapping(item, "ChangeSet change")
        for item in _sequence(changeset_payload.get("changes"), "ChangeSet.payload.changes")
    ]
    approval_summary_value = status.get("approval")
    approval: Mapping[str, Any] | None = None
    if approval_summary_value is not None:
        approval_summary = _mapping(approval_summary_value, "status.approval")
        approval_path = validate_relative_path(
            _string(approval_summary.get("path"), "status.approval.path")
        )
        if approval_path != _artifact_relative(status, "approval"):
            raise ContractError(ErrorCode.HASH_APPROVAL_MISMATCH, "approval paths disagree")
        approval = read_contract_file(
            resolve_within(run_root, approval_path), expected_schema="ApprovalSet"
        )
    decisions = _decision_map(approval)
    safety_by_id = {
        _string(change.get("change_id"), "change_id"): _card_safety(
            change,
            _string(decisions[cast("str", change["change_id"])].get("decision"), "decision")
            if cast("str", change["change_id"]) in decisions
            else _string(change.get("initial_decision"), "initial_decision"),
        )
        for change in changes
    }
    filter_name = selected_filter if selected_filter in _APPROVAL_FILTERS else "all"
    if filter_name == "pending":
        shown_changes = [
            change
            for change in changes
            if _string(change.get("change_id"), "change_id") not in decisions
        ]
    elif filter_name in {"safe", "manual", "conflict"}:
        shown_changes = [
            change
            for change in changes
            if safety_by_id[_string(change.get("change_id"), "change_id")] == filter_name
        ]
    else:
        shown_changes = changes
    counts = {
        "all": len(changes),
        "pending": len(changes) - len(decisions),
        "safe": sum(1 for change in changes if _is_bulk_safe(change)),
        "manual": sum(1 for value in safety_by_id.values() if value == "manual"),
        "conflict": sum(1 for value in safety_by_id.values() if value == "conflict"),
    }
    filtered_total = len(shown_changes)
    page_count = max(1, (filtered_total + _APPROVAL_PAGE_SIZE - 1) // _APPROVAL_PAGE_SIZE)
    page = min(selected_page, page_count)
    page_offset = (page - 1) * _APPROVAL_PAGE_SIZE
    page_changes = shown_changes[page_offset : page_offset + _APPROVAL_PAGE_SIZE]
    page_cards = [
        _change_card(
            change,
            decisions.get(_string(change.get("change_id"), "change_id")),
        )
        for change in page_changes
    ]
    page_start = page_offset + 1 if page_cards else 0
    page_end = page_offset + len(page_cards)

    def page_href(page_number: int, *, filter_value: str = filter_name) -> str:
        return f"/session/{session_key}?" + urlencode({"filter": filter_value, "page": page_number})

    pagination: dict[str, object] = {
        "page": page,
        "pages": page_count,
        "total": filtered_total,
        "start": page_start,
        "end": page_end,
    }
    if page > 1:
        pagination["previous_href"] = page_href(page - 1)
    if page < page_count:
        pagination["next_href"] = page_href(page + 1)
    filters = [
        {
            "label": _FILTER_LABELS[name],
            "count": counts[name],
            "href": page_href(1, filter_value=name),
            "current": name == filter_name,
        }
        for name in _APPROVAL_FILTERS
    ]
    notices: list[dict[str, str]] = []
    if changes and counts["safe"] == 0:
        notices.append(
            {
                "tone": "warning",
                "title": "已识别修改，但没有可安全自动回填项",
                "message": (
                    f"共识别 {len(changes)} 项修改，但没有一项同时具备精确 LaTeX 源位置和"
                    "安全普通正文证据。未精确定位、结构化或冲突项不会被强行匹配，也不会"
                    "自动写入 LaTeX；请逐项选择不采用或转人工处理。"
                ),
            }
        )
    approval_status = (
        _string(_mapping(approval.get("payload"), "ApprovalSet.payload").get("status"), "status")
        if approval is not None
        else "draft"
    )
    start_required = approval is None
    can_finalize = (
        approval is not None and approval_status == "draft" and len(decisions) == len(changes)
    )
    safe_pending_count = sum(
        1
        for change in changes
        if _is_bulk_safe(change) and cast("str", change["change_id"]) not in decisions
    )
    manual_pending_count = sum(
        1
        for change in changes
        if not _is_bulk_safe(change) and cast("str", change["change_id"]) not in decisions
    )
    view = _base_view(status, csrf_token=csrf_token, session_key=session_key) | {
        "page": "approval",
        "title": "逐项审批 Word 修改",
        "total": len(changes),
        "decided": len(decisions),
        # Mirror the core's no-overwrite bulk selector: only undecided exact
        # plain-text candidates can be accepted by the one-click action.
        "safe_pending_count": safe_pending_count,
        "manual_pending_count": manual_pending_count,
        "read_only": start_required or approval_status == "final",
        "start_required": start_required,
        "can_finalize": can_finalize,
        "start_action": "/approval/start",
        "decision_action": "/approval/decision",
        "bulk_action": "/approval/accept-safe",
        "manual_bulk_action": "/approval/mark-manual",
        "finalize_action": "/approval/finalize",
        "filters": filters,
        "selected_filter": filter_name,
        "selected_page": page,
        "page_start": page_start,
        "pagination": pagination,
        "changes": page_cards,
    }
    if approval is None:
        notices.append(
            {
                "tone": "info",
                "title": "审批尚未开始",
                "message": "请显式恢复审批；这一步只建立本机密封账本，不会记录决定或修改 LaTeX。",
            }
        )
    if notices:
        view["notices"] = notices
    return view


def _split_unified_diff(data: bytes) -> list[tuple[str, str]]:
    try:
        lines = data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "patch diff is not UTF-8") from exc
    if not lines:
        return []
    files: list[tuple[str, str]] = []
    current_path: str | None = None
    current_lines: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- a/"):
            if current_path is not None:
                files.append((current_path, "".join(current_lines)))
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ b/"):
                raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "diff header is incomplete")
            old_path = validate_relative_path(line[6:].rstrip("\r\n"))
            new_path = validate_relative_path(lines[index + 1][6:].rstrip("\r\n"))
            if old_path != new_path:
                raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "diff paths disagree")
            current_path = old_path
            current_lines = [line, lines[index + 1]]
            index += 2
            continue
        if current_path is None:
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "diff has content before header")
        current_lines.append(line)
        index += 1
    if current_path is not None:
        files.append((current_path, "".join(current_lines)))
    if len({path for path, _ in files}) != len(files):
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "diff repeats a file section")
    return files


def _patch_view(
    run_root: Path,
    status: Mapping[str, Any],
    *,
    csrf_token: str,
    session_key: str,
) -> dict[str, object]:
    plan_summary_value = status.get("plan")
    if plan_summary_value is None:
        return _base_view(status, csrf_token=csrf_token, session_key=session_key) | {
            "page": "patch",
            "title": "正在准备新 LaTeX 副本的回填预览",
            "counts": {"automatic": 0, "manual": 0, "rejected": 0, "conflict": 0},
            "files": [],
            "ready": False,
            "prepare_required": True,
            "prepare_action": "/patch/prepare",
            "back_action": f"/session/{session_key}?filter=all",
            "confirm_action": "/result/generate",
            "notices": [
                {
                    "tone": "info",
                    "title": "审批已经密封",
                    "message": "请显式恢复补丁预览；系统将从密封审批重建计划，尚不会写入 LaTeX。",
                }
            ],
        }
    plan_summary = _mapping(plan_summary_value, "status.plan")
    plan_path = validate_relative_path(_string(plan_summary.get("path"), "status.plan.path"))
    if plan_path != _artifact_relative(status, "patch_plan"):
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "PatchPlan paths disagree")
    plan = read_contract_file(resolve_within(run_root, plan_path), expected_schema="PatchPlan")
    payload = _mapping(plan.get("payload"), "PatchPlan.payload")
    plan_sha256 = compute_payload_sha256(plan)
    summary_sha = plan_summary.get("payload_sha256")
    if summary_sha != plan_sha256:
        raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "PatchPlan summary hash differs")
    diff_path = _artifact_path(run_root, status, "patch_diff")
    diff_data = read_stable_bytes(diff_path, max_bytes=_MAX_DIFF_BYTES)
    diff_ref_value = payload.get("unified_diff")
    if diff_ref_value is None:
        if diff_data:
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "noop plan has diff bytes")
    else:
        diff_ref = _mapping(diff_ref_value, "PatchPlan.payload.unified_diff")
        observed = digest_bytes(diff_data)
        if observed.sha256 != diff_ref.get("sha256") or observed.size_bytes != diff_ref.get(
            "size_bytes"
        ):
            raise ContractError(ErrorCode.HASH_PATCHPLAN_MISMATCH, "patch diff hash differs")
    operation_counts: Counter[str] = Counter()
    for item in _sequence(payload.get("operations"), "PatchPlan.payload.operations"):
        operation = _mapping(item, "PatchPlan operation")
        target = _mapping(operation.get("target"), "PatchPlan operation target")
        operation_counts[validate_relative_path(_string(target.get("path"), "target.path"))] += 1
    split_diff = _split_unified_diff(diff_data)
    if set(operation_counts) != {path for path, _ in split_diff} and (
        operation_counts or split_diff
    ):
        raise ContractError(
            ErrorCode.HASH_PATCHPLAN_MISMATCH,
            "operation and diff files differ",
        )
    files = [
        {"path": path, "change_count": operation_counts[path], "preview": preview}
        for path, preview in split_diff
    ]
    excluded = [
        _mapping(item, "PatchPlan excluded change")
        for item in _sequence(payload.get("excluded_changes"), "PatchPlan.payload.excluded_changes")
    ]
    excluded_reasons = Counter(_string(item.get("reason"), "excluded reason") for item in excluded)
    blocked = len(
        _sequence(payload.get("accepted_but_blocked"), "PatchPlan.payload.accepted_but_blocked")
    )
    summary = _mapping(payload.get("summary"), "PatchPlan.payload.summary")
    counts = {
        "automatic": int(summary.get("planned", 0)),
        "manual": excluded_reasons["manual"] + excluded_reasons["pending"] + blocked,
        "rejected": excluded_reasons["rejected"],
        "conflict": excluded_reasons["conflict"] + int(summary.get("overlaps", 0)),
    }
    phase = _string(status.get("phase"), "status.phase")
    plan_status = _string(payload.get("status"), "PatchPlan.payload.status")
    ready = phase == "awaiting_apply_confirmation" and plan_status in {"ready", "noop"}
    revise_required = phase == "plan_blocked" and plan_status == "blocked"
    confirmation_value = status.get("apply_confirmation")
    if ready:
        confirmation = _mapping(confirmation_value, "status.apply_confirmation")
        if confirmation.get("patch_plan_sha256") != plan_sha256:
            raise ContractError(
                ErrorCode.HASH_PATCHPLAN_MISMATCH,
                "apply confirmation is not bound to the current PatchPlan",
            )
    common = _common_fields(csrf_token, session_key)
    common["patch_plan_sha256"] = plan_sha256
    return {
        "page": "patch",
        "title": "确认回填到新 LaTeX 副本",
        "step": 4,
        "project_name": _project_name(status),
        "form_fields": common,
        "counts": counts,
        "files": files,
        "ready": ready,
        "prepare_required": False,
        "revise_required": revise_required,
        "prepare_action": "/patch/prepare",
        "revise_action": "/approval/revise",
        "back_action": f"/session/{session_key}?filter=all",
        "confirm_action": "/result/generate",
    }


def _warning_messages(status: Mapping[str, Any], phase: str) -> list[str]:
    phase_messages = {
        "applied": "修订副本已生成，仍需完成编译和差异核验。",
        "verified": "核心核验已完成，仍需生成审阅账本。",
        "ready_to_bundle": "账本已生成，仍需创建最终审计包。",
        "partially_completed": "部分核验工具或交付步骤尚未完成，可定向重试。",
    }
    warnings = [phase_messages[phase]] if phase in phase_messages else []
    for item in _sequence(status.get("blockers", ()), "status.blockers"):
        blocker = _mapping(item, "status blocker")
        code = blocker.get("code")
        if isinstance(code, str):
            if code.startswith("verification_"):
                warnings.append("LaTeX 或修订标记 PDF 尚未通过完整核验。")
                attempt = blocker.get("attempt")
                if isinstance(attempt, int) and not isinstance(attempt, bool):
                    warnings.append(f"已保留 {attempt} 次不可变核验记录，重试不会覆盖它们。")
                if blocker.get("retry_available") is False:
                    warnings.append(
                        "当前已有下游密封证据，不能覆盖式重试；请保留任务并导出支持包。"
                    )
            elif code == "approval_pending":
                warnings.append("仍有审批项尚未决定。")
            else:
                warnings.append(f"待处理项：{code}")
    return list(dict.fromkeys(warnings))


def _result_view(
    run_root: Path,
    status: Mapping[str, Any],
    *,
    csrf_token: str,
    session_key: str,
) -> dict[str, object]:
    status_artifacts = _artifacts(status)
    artifacts: list[dict[str, object]] = []
    for key in _ARTIFACT_ORDER:
        if key not in status_artifacts:
            continue
        relative = validate_relative_path(
            _string(status_artifacts.get(key), f"status.artifacts.{key}")
        )
        resolve_within(run_root, relative)
        label, description = _ARTIFACT_PRESENTATION[key]
        card: dict[str, object] = {
            "label": label,
            "description": description,
            "available": True,
        }
        if key == "revised_source":
            # This artifact is a directory, not a downloadable file. Keep it
            # visible without emitting an invalid /artifact URL.
            card["open_in_folder"] = True
        else:
            card["href"] = f"/artifact/{session_key}/{key}"
        artifacts.append(card)
    phase = _string(status.get("phase"), "status.phase")
    complete = phase == "completed"
    next_action = _string(status.get("next_action"), "status.next_action")
    retry_labels = {
        "verify_results": "继续核验结果",
        "retry_verification": "重新运行核验工具",
        "build_ledger": "继续生成审阅账本",
        "create_audit_bundle": "继续生成审计包",
    }
    retry_label = retry_labels.get(next_action)
    return _base_view(status, csrf_token=csrf_token, session_key=session_key) | {
        "page": "result",
        "title": "新 LaTeX 副本与审阅结果",
        "status": "complete" if complete else "partial",
        "artifacts": artifacts,
        "warnings": [] if complete else _warning_messages(status, phase),
        "revised_source_available": "revised_source" in status_artifacts,
        "open_revised_action": "/result/open-revised",
        "delivery_available": "run_manifest" in status_artifacts,
        "open_delivery_action": "/result/open-delivery",
        "retry_action": "/result/retry",
        "can_retry": retry_label is not None,
        "retry_label": retry_label or "重试缺失结果",
        "new_form_fields": {"csrf": csrf_token},
        "new_action": "/new",
        "exit_action": "/app/exit",
    }


def render_error_view(
    error: ContractError | ErrorCode | str,
    *,
    session_key: str,
    csrf_token: str,
    step: int = 1,
) -> dict[str, object]:
    """Return an actionable, path-free error page for a stable core error."""

    safe_key = _route_token(session_key, "session_key")
    message = user_message_for_error(error.code if isinstance(error, ContractError) else error)
    diagnostics = safe_diagnostic_details(
        error.violation.details if isinstance(error, ContractError) else None
    )
    primary_action = build_error_recovery_control(
        message.action,
        message.action_label,
        csrf_token=csrf_token,
        session_key=safe_key,
        allow_session_actions=False,
    )
    card: dict[str, object] = {
        "code": message.code,
        "title": message.title,
        "message": message.explanation,
        "guidance": [message.safety, message.action_label],
        "technical_details": format_safe_technical_details(message.code, diagnostics),
        "back_href": "/",
    }
    if primary_action is not None:
        card["primary_action"] = primary_action
    summary = diagnostics.get("image_issue_summary")
    if isinstance(summary, str):
        card["summary"] = summary
    return {
        "page": "error",
        "title": message.title,
        "step": step if 1 <= step <= 4 else 1,
        "form_fields": _common_fields(csrf_token, safe_key),
        "error": card,
    }


def render_session_view(
    session: ApplicationSession,
    *,
    session_key: str,
    csrf_token: str,
    selected_filter: str = "all",
    selected_page: int = 1,
) -> dict[str, object]:
    """Build the current render-ready view without mutating ``session``.

    The controller should call this again after every state transition.  A
    :class:`ContractError` raised while reconstructing or reading sealed evidence
    becomes a safe Chinese error view; malformed controller tokens still raise.
    """

    safe_key = _route_token(session_key, "session_key")
    _common_fields(csrf_token, safe_key)
    step = 1
    try:
        status = _mapping(session.status(), "session status")
        step_value = status.get("step")
        if isinstance(step_value, int) and not isinstance(step_value, bool):
            step = step_value
        phase = _string(status.get("phase"), "status.phase")
        if phase == "ready_to_export":
            return _preflight_view(
                session.run_root,
                status,
                csrf_token=csrf_token,
                session_key=safe_key,
            )
        if phase == "waiting_for_return":
            return _waiting_view(
                session.run_root,
                status,
                csrf_token=csrf_token,
                session_key=safe_key,
            )
        if phase in {
            "approval_required",
            "approval_in_progress",
            "approval_ready_to_finalize",
        }:
            return _approval_view(
                session.run_root,
                status,
                csrf_token=csrf_token,
                session_key=safe_key,
                selected_filter=selected_filter,
                selected_page=selected_page,
            )
        if phase in {"ready_to_plan", "plan_blocked", "awaiting_apply_confirmation"}:
            return _patch_view(
                session.run_root,
                status,
                csrf_token=csrf_token,
                session_key=safe_key,
            )
        if phase in {
            "applied",
            "verified",
            "partially_completed",
            "ready_to_bundle",
            "completed",
        }:
            return _result_view(
                session.run_root,
                status,
                csrf_token=csrf_token,
                session_key=safe_key,
            )
        raise ContractError(ErrorCode.INTERNAL_INVARIANT, "unknown application phase")
    except ContractError as exc:
        return render_error_view(
            exc,
            session_key=safe_key,
            csrf_token=csrf_token,
            step=step,
        )


__all__ = [
    "PresenterModelError",
    "build_error_recovery_control",
    "render_error_view",
    "render_session_view",
]
