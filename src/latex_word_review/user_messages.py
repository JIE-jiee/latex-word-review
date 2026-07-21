"""Chinese product messages for stable machine-readable errors.

The core keeps terse technical messages. This module adds a non-authoritative
presentation layer for the Windows app without changing error or exit-code
semantics.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Final, Literal

from latex_word_review.errors import ContractError, ErrorCode

RecoveryAction = Literal[
    "choose_project",
    "choose_returned_word",
    "review_changes",
    "install_tool",
    "retry",
    "start_new_round",
    "open_results",
    "create_support_bundle",
]

SafeDiagnosticValue = str | int

_SAFE_DIAGNOSTIC_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("provider", "提供程序"),
    ("stage", "诊断阶段"),
    ("name", "Windows 错误"),
    ("hex", "Windows 错误码"),
    ("code", "Windows 数值代码"),
    ("exception_type", "异常类型"),
    ("winerror", "Windows winerror"),
    ("export_status", "导出状态"),
    ("backend_status", "后端状态"),
    ("backend_error_code", "后端错误码"),
    ("failure_kind", "失败类型"),
    ("timed_out", "是否超时"),
    ("returncode", "后端返回码"),
    ("duration_ms", "后端耗时（毫秒）"),
    ("error_count", "后端错误数"),
    ("warning_count", "后端警告数"),
    ("output_truncated", "诊断输出是否超限"),
    ("finding_count", "诊断项数"),
    ("image_issue_summary", "问题摘要"),
    ("image_issue_1", "问题位置 1"),
    ("image_issue_2", "问题位置 2"),
    ("image_issue_3", "问题位置 3"),
)
_SAFE_DIAGNOSTIC_LABELS: Final = dict(_SAFE_DIAGNOSTIC_FIELDS)
_MAX_IMAGE_DIAGNOSTICS: Final = 1_000
_MAX_VISIBLE_IMAGE_DIAGNOSTICS: Final = 3
_IMAGE_DIAGNOSTIC_ID_RE: Final = re.compile(r"image_diag_[0-9a-f]{64}")
_STABLE_DIAGNOSTIC_CODE_RE: Final = re.compile(
    r"(?:E_[A-Z0-9_]{1,94}|W_[A-Z0-9_]{1,94}|IMAGE_[A-Z0-9_]{1,90})"
)
_IMAGE_ISSUE_SUMMARY_RE: Final = re.compile(r"[1-9][0-9]{0,3}处图片写法当前无法处理")
_IMAGE_ISSUE_LOCATION_RE: Final = re.compile(
    r"第[1-9][0-9]{0,6}行，第[1-9][0-9]{0,6}列（"
    r"(?:E_[A-Z0-9_]{1,94}|W_[A-Z0-9_]{1,94}|IMAGE_[A-Z0-9_]{1,90})）"
)


def _validated_image_diagnostics(value: object) -> tuple[tuple[int, int, str], ...]:
    """Validate image diagnostics while deliberately discarding private text and paths."""

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return ()
    if not 1 <= len(value) <= _MAX_IMAGE_DIAGNOSTICS:
        return ()
    validated: list[tuple[int, int, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return ()
        diagnostic_id = item.get("diagnostic_id")
        code = item.get("code")
        line = item.get("line")
        column = item.get("column")
        if (
            not isinstance(diagnostic_id, str)
            or _IMAGE_DIAGNOSTIC_ID_RE.fullmatch(diagnostic_id) is None
            or not isinstance(code, str)
            or _STABLE_DIAGNOSTIC_CODE_RE.fullmatch(code) is None
            or isinstance(line, bool)
            or not isinstance(line, int)
            or not 1 <= line <= 9_999_999
            or isinstance(column, bool)
            or not isinstance(column, int)
            or not 1 <= column <= 9_999_999
            or item.get("disposition") != "manual"
        ):
            return ()
        validated.append((line, column, code))
    return tuple(validated)


def _copy_projected_image_details(
    details: Mapping[str, object],
    safe: dict[str, SafeDiagnosticValue],
) -> None:
    """Accept only the exact bounded form produced by this module on a second pass."""

    summary = details.get("image_issue_summary")
    count = safe.get("finding_count")
    if (
        not isinstance(summary, str)
        or _IMAGE_ISSUE_SUMMARY_RE.fullmatch(summary) is None
        or isinstance(count, bool)
        or not isinstance(count, int)
        or summary != f"{count}处图片写法当前无法处理"
    ):
        return
    locations: dict[str, str] = {}
    for index in range(1, min(count, _MAX_VISIBLE_IMAGE_DIAGNOSTICS) + 1):
        key = f"image_issue_{index}"
        value = details.get(key)
        if not isinstance(value, str) or _IMAGE_ISSUE_LOCATION_RE.fullmatch(value) is None:
            return
        locations[key] = value
    safe["image_issue_summary"] = summary
    safe.update(locations)


def safe_diagnostic_details(details: Mapping[str, object] | None) -> dict[str, SafeDiagnosticValue]:
    """Return a bounded, path-free subset suitable for the local UI.

    ContractError details are intentionally open-ended for machine callers.  A
    background job must not copy that mapping verbatim because exception
    details can contain private paths.  This projection accepts only named
    scalar fields and rejects path separators and control characters.
    """

    if details is None:
        return {}
    safe: dict[str, SafeDiagnosticValue] = {}
    for key, _label in _SAFE_DIAGNOSTIC_FIELDS:
        value = details.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            safe[key] = value
            continue
        if not isinstance(value, str) or not value or len(value) > 96:
            continue
        if any(
            not character.isascii() or not (character.isalnum() or character in {".", "_", "-"})
            for character in value
        ):
            continue
        safe[key] = value
    image_diagnostics = _validated_image_diagnostics(details.get("diagnostics"))
    if image_diagnostics:
        count = len(image_diagnostics)
        safe.update(
            {
                "provider": "image_overlay",
                "stage": "image_overlay",
                "failure_kind": "manual_image_syntax",
                "finding_count": count,
                "image_issue_summary": f"{count}处图片写法当前无法处理",
            }
        )
        for index, (line, column, code) in enumerate(
            image_diagnostics[:_MAX_VISIBLE_IMAGE_DIAGNOSTICS],
            start=1,
        ):
            safe[f"image_issue_{index}"] = f"第{line}行，第{column}列（{code}）"
    elif "diagnostics" not in details:
        _copy_projected_image_details(details, safe)
    return safe


def format_safe_technical_details(
    code: ErrorCode | str,
    details: Mapping[str, object] | None = None,
) -> str:
    """Format a stable error code and an already allowlisted diagnostic subset."""

    code_value = code.value if isinstance(code, ErrorCode) else code
    lines = [f"稳定错误代码：{code_value}"]
    for key, value in safe_diagnostic_details(details).items():
        lines.append(f"{_SAFE_DIAGNOSTIC_LABELS[key]}：{value}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class UserErrorMessage:
    """One safe, actionable error card for the local product UI."""

    code: str
    title: str
    explanation: str
    safety: str
    action: RecoveryAction
    action_label: str
    technical_message: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Template:
    title: str
    explanation: str
    safety: str
    action: RecoveryAction
    action_label: str


_TEMPLATES: dict[str, _Template] = {
    "input": _Template(
        "输入内容无法使用",
        "所选文件或参数不符合当前步骤的要求。",
        "程序已停止，没有修改原始 LaTeX 或返回的 Word 文件。",
        "retry",
        "返回并重新选择",
    ),
    "path": _Template(
        "路径安全检查未通过",
        "项目包含越界路径、链接或无法证明安全的位置，因此不能继续。",
        "程序没有跟随可疑路径，也没有修改原始文件。",
        "choose_project",
        "重新选择论文",
    ),
    "integrity": _Template(
        "完整性检查未通过",
        "本轮证据与创建时记录的哈希不一致，程序不会猜测或绕过校验。",
        "自动流程已停止，没有继续写入新的 LaTeX 副本。",
        "start_new_round",
        "重新选择论文并新建一轮",
    ),
    "tool": _Template(
        "缺少所需工具",
        "当前电脑缺少此步骤需要的外部工具，或工具版本不受支持。",
        "已经完成并通过校验的产物仍然保留；未完成的步骤不会伪装成功。",
        "install_tool",
        "查看安装与重试方法",
    ),
    "export": _Template(
        "审阅 Word 未能安全生成",
        "转换后内容不完整、后端失败或所需能力不可用，因此没有交付可疑的审阅稿。",
        "原始 LaTeX 保持不变；不完整的 Word 不会作为本轮基线。",
        "retry",
        "查看问题并重试",
    ),
    "tracking": _Template(
        "返回稿无法安全核验",
        "Word 修订可能被关闭、接受，或返回稿已发生无法追踪的文字变化。",
        "该文件不会自动回填到 LaTeX，原稿和返回原件保持不变。",
        "choose_returned_word",
        "选择保留修订的 Word",
    ),
    "docx": _Template(
        "Word 文件无法安全读取",
        "返回文件不是受支持的 DOCX，或其中包含不安全、损坏的关系。",
        "程序没有执行文档中的外部关系，也没有修改原始文件。",
        "choose_returned_word",
        "重新选择 Word 文件",
    ),
    "mapping": _Template(
        "部分修改无法准确定位",
        "程序不能证明这些 Word 修改对应 LaTeX 中的唯一安全位置。",
        "无法定位的项目只会进入审阅账本，不会自动写入。",
        "review_changes",
        "查看并转为人工处理",
    ),
    "approval": _Template(
        "审批尚未满足应用条件",
        "仍有未决定、未知或缺少最终文字的修改，暂时不能生成补丁。",
        "审批操作本身没有修改 LaTeX。",
        "review_changes",
        "返回审批",
    ),
    "patch": _Template(
        "有采用项需要人工处理",
        "至少一项已采用修改涉及不安全类型、重叠范围或无法证明的源位置。",
        "程序没有应用被阻断的修改，也没有覆盖原稿。",
        "review_changes",
        "查看受影响修改",
    ),
    "source_drift": _Template(
        "论文源文件已经变化",
        "当前 LaTeX 与创建本轮审阅时的快照不同，旧补丁不能安全套用。",
        "程序已停止，现有原稿和审阅证据都不会被覆盖。",
        "start_new_round",
        "重新选择论文并新建一轮",
    ),
    "apply": _Template(
        "修订副本没有完整生成",
        "应用过程中未能完成原子发布，因此不能把当前目录当作有效结果。",
        "原始 LaTeX 没有被覆盖；程序不会交付部分写入的副本。",
        "retry",
        "清理临时项并重试",
    ),
    "verify_tool": _Template(
        "LaTeX 已生成，但 PDF 未完成",
        "编译或 latexdiff 没有成功，修订副本需要在工具就绪后重新核验。",
        "已生成的 LaTeX 副本会保留；不会虚报 PDF 核验通过。",
        "install_tool",
        "修复工具后重试 PDF",
    ),
    "verify": _Template(
        "结果核验未通过",
        "实际差异、原件完整性或预期补丁之间存在不一致。",
        "程序不会把当前结果标记为完成或继续生成可信审计包。",
        "create_support_bundle",
        "保存支持信息",
    ),
    "bundle": _Template(
        "审计包没有生成",
        "待打包产物的哈希或内容分类不满足发布规则。",
        "已有论文结果不会被删除；失败的包不能作为审计证据。",
        "open_results",
        "查看已有结果",
    ),
    "internal": _Template(
        "程序遇到内部错误",
        "发生了不应出现的内部状态，程序已安全停止。",
        "请保留当前任务目录；不要手工修改密封 JSON 或覆盖原稿。",
        "create_support_bundle",
        "保存支持信息",
    ),
}

_CODE_TEMPLATES: Final[dict[ErrorCode, _Template]] = {
    ErrorCode.BACKEND_CAPABILITY_MISSING: _Template(
        "论文中有内容当前无法自动处理",
        "发现当前转换流程无法可靠处理的内容；常见原因是特殊图片写法或格式。",
        "程序已安全停止，原始 LaTeX 保持不变；问题位置会在下方列出。",
        "retry",
        "按提示修改后重试",
    )
}

_CATEGORY_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.SCHEMA_INVALID: "input",
    ErrorCode.SCHEMA_MAJOR_UNSUPPORTED: "input",
    ErrorCode.SCHEMA_UNKNOWN_SECURITY_FIELD: "input",
    ErrorCode.PATH_ABSOLUTE: "path",
    ErrorCode.PATH_TRAVERSAL: "path",
    ErrorCode.PATH_LINK_ESCAPE: "path",
    ErrorCode.HASH_INTEGRITY_MISMATCH: "integrity",
    ErrorCode.HASH_SOURCE_MISMATCH: "integrity",
    ErrorCode.HASH_CHANGESET_MISMATCH: "integrity",
    ErrorCode.HASH_APPROVAL_MISMATCH: "integrity",
    ErrorCode.HASH_PATCHPLAN_MISMATCH: "integrity",
    ErrorCode.HASH_RETURNED_ORIGINAL_MISMATCH: "integrity",
    ErrorCode.TOOL_MISSING: "tool",
    ErrorCode.TOOL_VERSION_UNSUPPORTED: "tool",
    ErrorCode.BACKEND_CAPABILITY_MISSING: "export",
    ErrorCode.BACKEND_FAILED: "export",
    ErrorCode.EXPORT_SILENT_LOSS: "export",
    ErrorCode.EXPORT_DEGRADED: "export",
    ErrorCode.REVIEW_TRACKING_DISABLED: "tracking",
    ErrorCode.DOCX_INVALID_PACKAGE: "docx",
    ErrorCode.DOCX_UNSAFE_RELATIONSHIP: "docx",
    ErrorCode.REVISION_DELETE_TEXT_MISSING: "tracking",
    ErrorCode.REVISION_AUTHOR_MISSING: "mapping",
    ErrorCode.REVISION_TIMESTAMP_MISSING: "mapping",
    ErrorCode.REVISION_BASELINE_DRIFT: "tracking",
    ErrorCode.REVISION_VIEW_UNSUPPORTED: "tracking",
    ErrorCode.REVISION_RECONCILIATION: "tracking",
    ErrorCode.REVISION_STRUCTURED_TEXT: "mapping",
    ErrorCode.MAP_UNMATCHED: "mapping",
    ErrorCode.MAP_AMBIGUOUS: "mapping",
    ErrorCode.MAP_CONFIDENCE_LOW: "mapping",
    ErrorCode.APPROVAL_NOT_FINAL: "approval",
    ErrorCode.APPROVAL_CHANGE_UNKNOWN: "approval",
    ErrorCode.APPROVAL_FINAL_TEXT_REQUIRED: "approval",
    ErrorCode.PATCH_UNSAFE_KIND: "patch",
    ErrorCode.PATCH_SOURCE_DRIFT: "source_drift",
    ErrorCode.PATCH_OVERLAP: "patch",
    ErrorCode.PATCH_ACCEPTED_BUT_BLOCKED: "patch",
    ErrorCode.PATCH_UNAPPROVED_CHANGE: "patch",
    ErrorCode.APPLY_PARTIAL_WRITE: "apply",
    ErrorCode.VERIFY_COMPILE_FAILED: "verify_tool",
    ErrorCode.VERIFY_DIFF_MISMATCH: "verify",
    ErrorCode.VERIFY_ORIGINAL_MUTATED: "verify",
    ErrorCode.BUNDLE_HASH_MISMATCH: "bundle",
    ErrorCode.BUNDLE_PRIVATE_RELEASE: "bundle",
    ErrorCode.INTERNAL_INVARIANT: "internal",
}


def user_message_for_error(
    error: ContractError | ErrorCode | str,
    *,
    technical_message: str | None = None,
) -> UserErrorMessage:
    """Map a stable error to Chinese recovery guidance without weakening it."""

    detail: str | None
    if isinstance(error, ContractError):
        code = error.code
        detail = str(error)
    else:
        try:
            code = error if isinstance(error, ErrorCode) else ErrorCode(error)
        except ValueError:
            code = ErrorCode.INTERNAL_INVARIANT
        detail = technical_message
    template = _CODE_TEMPLATES.get(code, _TEMPLATES[_CATEGORY_BY_CODE[code]])
    return UserErrorMessage(
        code=code.value,
        title=template.title,
        explanation=template.explanation,
        safety=template.safety,
        action=template.action,
        action_label=template.action_label,
        technical_message=detail,
    )


__all__ = [
    "RecoveryAction",
    "SafeDiagnosticValue",
    "UserErrorMessage",
    "format_safe_technical_details",
    "safe_diagnostic_details",
    "user_message_for_error",
]
