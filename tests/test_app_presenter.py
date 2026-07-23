"""Product presenter tests against real sealed application sessions."""

from __future__ import annotations

import copy
import shutil
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

import latex_word_review.app_presenter as app_presenter_module
from latex_word_review.app_presenter import (
    PresenterModelError,
    _is_bulk_safe,
    build_error_recovery_control,
    render_error_view,
    render_session_view,
)
from latex_word_review.app_views import render_app_page
from latex_word_review.application import ApplicationSession
from latex_word_review.approval import record_bulk_decision
from latex_word_review.contracts import compute_payload_sha256
from latex_word_review.errors import ContractError, ErrorCode
from latex_word_review.jsonio import read_contract_file
from latex_word_review.user_messages import RecoveryAction
from tests.test_application import TIME, _only_change_id, _review_session
from tests.test_workflow import FIXTURE


class _StatusSession:
    def __init__(self, run_root: Path, status: Mapping[str, Any]) -> None:
        self.run_root = run_root
        self._status = copy.deepcopy(dict(status))

    def status(self) -> dict[str, Any]:
        return copy.deepcopy(self._status)


def _as_application_session(value: _StatusSession) -> ApplicationSession:
    return cast("ApplicationSession", value)


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _strings(item)


def _assert_renderable_and_path_free(view: Mapping[str, object], run_root: Path) -> str:
    rendered = render_app_page(view)
    root = str(run_root)
    assert root not in rendered
    assert all(root not in value for value in _strings(view))
    return rendered


def _new_session(tmp_path: Path) -> ApplicationSession:
    source = tmp_path / "source"
    shutil.copytree(FIXTURE, source)
    return ApplicationSession.create(
        source,
        tmp_path / "run",
        main_document="main.tex",
        confidentiality="public_fixture",
        generated_at=TIME,
    )


def test_preflight_and_waiting_views_are_renderable_and_post_bound(tmp_path: Path) -> None:
    session = _new_session(tmp_path)

    preflight = render_session_view(session, session_key="paper-1", csrf_token="csrf-value")
    preflight_html = _assert_renderable_and_path_free(preflight, session.run_root)

    assert preflight["page"] == "preflight"
    assert preflight["continue_action"] == "/session/export-existing"
    assert 'name="csrf" value="csrf-value"' in preflight_html
    assert 'name="session" value="paper-1"' in preflight_html
    choose_form = preflight_html.split('action="/new"', 1)[1].split("</form>", 1)[0]
    assert 'name="csrf" value="csrf-value"' in choose_form
    assert 'name="session"' not in choose_form

    session.export_review(confidentiality="public_fixture", generated_at=TIME)
    waiting = render_session_view(session, session_key="paper-1", csrf_token="csrf-value")
    waiting_html = _assert_renderable_and_path_free(waiting, session.run_root)

    assert waiting["page"] == "waiting_word"
    assert waiting["review_docx_name"] == "review.docx"
    assert waiting["exported_at"] == TIME
    assert waiting["export_status"] == "partial"
    assert cast("int", waiting["export_warning_count"]) > 0
    notices = cast("Sequence[Mapping[str, str]]", waiting["notices"])
    assert [notice["title"] for notice in notices] == [
        "审阅稿已生成，但有转换提示",
        "请先确认可自动回填的正文范围",
    ]
    assert "可恢复提示" in notices[0]["message"]
    assert "不是整篇论文覆盖率" in notices[1]["message"]
    assert "审阅稿已生成，但有转换提示" in waiting_html
    assert "不是整篇论文覆盖率" in waiting_html
    assert waiting["title"] == "Word 审阅交接与修改稿导入"
    compatibility_counts = cast("Sequence[Mapping[str, object]]", waiting["compatibility_counts"])
    assert [item["label"] for item in compatibility_counts] == [
        "Word 原生公式",
        "图片实例",
        "表格",
        "编号/引用等特殊字段",
    ]
    assert all(
        isinstance(item["count"], int) and not isinstance(item["count"], bool)
        for item in compatibility_counts
    )
    assert waiting["return_flow_steps"] == [
        "选择导师或合作者返回的 .docx，原件会只读归档。",
        "进入逐项审批，决定采用、不采用或留待人工处理。",
        "预览回填内容并再次确认后，生成新的 LaTeX 副本。",
    ]
    assert 'method="post" action="/session/open-review-docx"' in waiting_html
    assert waiting["save_copy_action"] == "/session/save-review-copy"
    assert 'method="post" action="/session/save-review-copy"' in waiting_html
    assert 'method="post" action="/session/receive"' in waiting_html
    assert "Windows 文件选择器" in waiting_html
    assert "选择修改后的 Word" in waiting_html
    assert "2A 另存可编辑 Word 并交给审阅者" in waiting_html
    assert "2B 收到修改稿后导入 Word" in waiting_html
    assert "语义审阅副本" in waiting_html
    assert "不是 PDF 排版复刻" in waiting_html
    assert "打开只读基线（仅检查）" in waiting_html
    assert "打开审阅稿" not in waiting_html
    assert waiting_html.index("2A 另存可编辑 Word 并交给审阅者") < waiting_html.index(
        "2B 收到修改稿后导入 Word"
    )
    assert waiting_html.index("2B 收到修改稿后导入 Word") < waiting_html.index("选择修改后的 Word")
    assert "拖回" not in waiting_html
    assert waiting_html.count('name="csrf" value="csrf-value"') == 3
    assert waiting_html.count('name="session" value="paper-1"') == 3


def test_waiting_view_does_not_guess_missing_compatibility_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _new_session(tmp_path)
    session.export_review(confidentiality="public_fixture", generated_at=TIME)
    report_path = session.run_root / "export/objects/export-report.json"
    report = read_contract_file(report_path, expected_schema="ExportReport")
    payload = cast("dict[str, Any]", report["payload"])
    metrics = cast("dict[str, Any]", payload["metrics"])
    output = cast("dict[str, Any]", metrics["output"])
    table_count = cast("int", output["tables"])
    output.clear()
    output["tables"] = table_count

    def read_report(path: str | Path, *, expected_schema: str) -> dict[str, Any]:
        assert Path(path) == report_path
        assert expected_schema == "ExportReport"
        return copy.deepcopy(report)

    monkeypatch.setattr("latex_word_review.app_presenter.read_contract_file", read_report)

    view = render_session_view(session, session_key="metrics-only", csrf_token="csrf")
    html = _assert_renderable_and_path_free(view, session.run_root)

    assert view["compatibility_counts"] == [{"label": "表格", "count": table_count}]
    assert "表格" in html
    assert "Word 原生公式" not in html
    assert "图片实例" not in html
    assert "编号/引用等特殊字段" not in html


def test_approval_cards_and_bulk_count_match_the_core_policy(tmp_path: Path) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    view = render_session_view(
        session,
        session_key="paper-approval",
        csrf_token="csrf-approval",
    )
    html = _assert_renderable_and_path_free(view, session.run_root)

    changeset = read_contract_file(
        session.run_root / "receive/changeset.json",
        expected_schema="ChangeSet",
    )
    approval_path = cast("str", cast("Mapping[str, Any]", session.status()["approval"])["path"])
    approval = read_contract_file(session.run_root / approval_path, expected_schema="ApprovalSet")
    bulk = record_bulk_decision(
        changeset,
        approval,
        operation="accept_all_safe",
        decision_source="local_ui",
        decided_at=TIME,
        generated_at=TIME,
    )
    audit = cast("Mapping[str, Any]", cast("Mapping[str, Any]", bulk["payload"])["audit"])
    operations = cast("Sequence[Mapping[str, Any]]", audit["bulk_operations"])
    selected_by_core = cast("Sequence[str]", operations[-1]["change_ids"])

    assert view["page"] == "approval"
    assert view["safe_pending_count"] == len(selected_by_core)
    cards = cast("Sequence[Mapping[str, object]]", view["changes"])
    assert len(cards) == 1
    assert {
        "before",
        "after",
        "author",
        "timestamp",
        "context",
        "safety",
        "decision",
        "details",
    } <= cards[0].keys()
    assert '<details class="technical"><summary>技术详情</summary>' in html
    assert "公式、引用、结构、移动和冲突项不会包含在内" in html


def test_approval_presenter_pages_large_change_sets_without_rendering_every_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _review_session(tmp_path)
    changeset = read_contract_file(
        session.run_root / "receive/changeset.json",
        expected_schema="ChangeSet",
    )
    payload = cast("dict[str, Any]", changeset["payload"])
    template = copy.deepcopy(cast("list[dict[str, Any]]", payload["changes"])[0])
    changes: list[dict[str, Any]] = []
    for index in range(61):
        change = copy.deepcopy(template)
        change["change_id"] = f"chg_{index:03d}"
        changes.append(change)
    payload["changes"] = changes

    def contract_artifact(
        run_root: Path,
        status: Mapping[str, Any],
        key: str,
        schema_name: str,
    ) -> dict[str, Any]:
        assert run_root == session.run_root
        assert status["phase"] == "approval_required"
        assert key == "changeset"
        assert schema_name == "ChangeSet"
        return changeset

    monkeypatch.setattr(
        "latex_word_review.app_presenter._contract_artifact",
        contract_artifact,
    )
    built_change_ids: list[str] = []
    original_change_card = app_presenter_module._change_card

    def counted_change_card(
        change: Mapping[str, Any], decision: Mapping[str, Any] | None
    ) -> dict[str, object]:
        built_change_ids.append(cast("str", change["change_id"]))
        return original_change_card(change, decision)

    monkeypatch.setattr(app_presenter_module, "_change_card", counted_change_card)

    view = render_session_view(
        session,
        session_key="large-approval",
        csrf_token="csrf",
        selected_page=3,
    )
    html = _assert_renderable_and_path_free(view, session.run_root)
    cards = cast("Sequence[Mapping[str, object]]", view["changes"])
    pagination = cast("Mapping[str, object]", view["pagination"])

    assert len(cards) == 11
    assert built_change_ids == [f"chg_{index:03d}" for index in range(50, 61)]
    assert cards[0]["change_id"] == "chg_050"
    assert view["page_start"] == 51
    assert pagination == {
        "page": 3,
        "pages": 3,
        "total": 61,
        "start": 51,
        "end": 61,
        "previous_href": "/session/large-approval?filter=all&page=2",
    }
    assert "修改 51" in html
    assert "修改 1</p>" not in html


def test_bulk_count_excludes_safe_changes_already_decided(tmp_path: Path) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    changeset = read_contract_file(
        session.run_root / "receive/changeset.json",
        expected_schema="ChangeSet",
    )
    safe_change = next(
        change
        for change in cast("Sequence[Mapping[str, Any]]", changeset["payload"]["changes"])
        if change["safety_class"] == "plain_text_candidate"
    )
    session.decide(
        change_id=cast("str", safe_change["change_id"]),
        decision="rejected",
        decided_at=TIME,
        generated_at=TIME,
    )

    view = render_session_view(
        session,
        session_key="paper-approval",
        csrf_token="csrf-approval",
    )
    html = _assert_renderable_and_path_free(view, session.run_root)

    assert view["safe_pending_count"] == 0
    assert "采用全部安全正文修改" not in html


def test_interrupted_approval_and_plan_require_explicit_post_recovery(
    tmp_path: Path,
) -> None:
    session = _review_session(tmp_path)

    approval_view = render_session_view(
        session,
        session_key="recovery-flow",
        csrf_token="csrf-recovery",
    )
    approval_html = _assert_renderable_and_path_free(approval_view, session.run_root)

    assert approval_view["start_required"] is True
    assert approval_view["read_only"] is True
    assert 'method="post" action="/approval/start"' in approval_html
    assert 'action="/approval/decision"' not in approval_html
    assert "不会替你决定任何修改" in approval_html
    assert not (session.run_root / "approvals").exists()

    session.begin_approval(actor_id="author", actor_name="Author", generated_at=TIME)
    session.decide(
        change_id=_only_change_id(session),
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    session.finalize_approval(prepare_plan=False, generated_at=TIME)
    patch_view = render_session_view(
        session,
        session_key="recovery-flow",
        csrf_token="csrf-recovery",
    )
    patch_html = _assert_renderable_and_path_free(patch_view, session.run_root)

    assert patch_view["prepare_required"] is True
    assert patch_view["ready"] is False
    assert 'method="post" action="/patch/prepare"' in patch_html
    assert 'action="/result/generate"' not in patch_html
    assert "不会应用补丁" in patch_html
    assert patch_view["back_action"] == "/session/recovery-flow?filter=all"
    assert not (session.run_root / "plans").exists()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"safety_class": "plain_text_candidate", "resolution": {"status": "exact"}}, True),
        ({"safety_class": "manual_high_risk", "resolution": {"status": "exact"}}, False),
        ({"safety_class": "ledger_only", "resolution": {"status": "exact"}}, False),
        ({"safety_class": "denied_unknown", "resolution": {"status": "exact"}}, False),
        (
            {"safety_class": "plain_text_candidate", "resolution": {"status": "conflict"}},
            False,
        ),
    ],
    ids=("plain-text", "formula-or-structure", "reference-ledger", "unknown", "conflict"),
)
def test_bulk_safe_classification_exactly_matches_core_selector(
    change: Mapping[str, Any], expected: bool
) -> None:
    assert _is_bulk_safe(change) is expected


def test_patch_preview_uses_real_diff_and_exact_second_gate_hash(tmp_path: Path) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    session.decide(
        change_id=_only_change_id(session),
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    status = session.finalize_approval(generated_at=TIME)

    view = render_session_view(session, session_key="patch-1", csrf_token="csrf-patch")
    html = _assert_renderable_and_path_free(view, session.run_root)
    plan_path = cast("str", cast("Mapping[str, Any]", status["plan"])["path"])
    plan = read_contract_file(session.run_root / plan_path, expected_schema="PatchPlan")
    exact_hash = compute_payload_sha256(plan)
    fields = cast("Mapping[str, object]", view["form_fields"])
    files = cast("Sequence[Mapping[str, object]]", view["files"])
    actual_diff = (session.run_root / "plans/plan-r1/changes.patch").read_text(encoding="utf-8")

    assert view["page"] == "patch"
    assert view["ready"] is True
    assert fields["patch_plan_sha256"] == exact_hash
    assert cast("str", status["apply_confirmation"]["patch_plan_sha256"]) == exact_hash
    assert "".join(cast("str", item["preview"]) for item in files) == actual_diff
    assert all(not Path(cast("str", item["path"])).is_absolute() for item in files)
    assert f'name="patch_plan_sha256" value="{exact_hash}"' in html
    assert 'name="confirm_apply" type="checkbox" value="yes" required' in html


def test_blocked_patch_get_is_read_only_and_offers_exact_revise_post(
    tmp_path: Path,
) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(
        actor_id="paper-author",
        actor_name="Paper Author",
        generated_at=TIME,
    )
    session.decide(
        change_id=_only_change_id(session),
        decision="accepted_with_edit",
        final_text="50%",
        decided_at=TIME,
        generated_at=TIME,
    )
    status = session.finalize_approval(generated_at=TIME)
    old_hash = cast("str", status["plan"]["payload_sha256"])
    evidence_before = {
        path.relative_to(session.run_root): path.read_bytes()
        for root in ("approvals", "plans")
        for path in (session.run_root / root).rglob("*")
        if path.is_file()
    }

    first = render_session_view(
        session,
        session_key="blocked-plan",
        csrf_token="csrf-blocked",
    )
    second = render_session_view(
        session,
        session_key="blocked-plan",
        csrf_token="csrf-blocked",
    )
    html = _assert_renderable_and_path_free(first, session.run_root)

    assert first == second
    assert first["page"] == "patch"
    assert first["ready"] is False
    assert first["revise_required"] is True
    assert 'method="post" action="/approval/revise"' in html
    revise_form = html.split('action="/approval/revise"', 1)[1].split("</form>", 1)[0]
    assert 'name="csrf" value="csrf-blocked"' in revise_form
    assert 'name="session" value="blocked-plan"' in revise_form
    assert f'name="patch_plan_sha256" value="{old_hash}"' in revise_form
    assert 'action="/result/generate"' not in html
    assert session.status() == status
    assert {
        path.relative_to(session.run_root): path.read_bytes()
        for root in ("approvals", "plans")
        for path in (session.run_root / root).rglob("*")
        if path.is_file()
    } == evidence_before


def test_every_application_phase_has_a_nonempty_renderable_page(tmp_path: Path) -> None:
    session = _review_session(tmp_path)
    observed: dict[str, str] = {}

    observed[cast("str", session.status()["phase"])] = cast(
        "str", render_session_view(session, session_key="all-phases", csrf_token="csrf")["page"]
    )
    session.begin_approval(actor_id="author", actor_name="Author", generated_at=TIME)
    observed[cast("str", session.status()["phase"])] = cast(
        "str", render_session_view(session, session_key="all-phases", csrf_token="csrf")["page"]
    )
    session.decide(
        change_id=_only_change_id(session),
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    observed[cast("str", session.status()["phase"])] = cast(
        "str", render_session_view(session, session_key="all-phases", csrf_token="csrf")["page"]
    )
    session.finalize_approval(prepare_plan=False, generated_at=TIME)
    observed[cast("str", session.status()["phase"])] = cast(
        "str", render_session_view(session, session_key="all-phases", csrf_token="csrf")["page"]
    )
    session.prepare_plan(generated_at=TIME)
    awaiting = session.status()
    observed[cast("str", awaiting["phase"])] = cast(
        "str", render_session_view(session, session_key="all-phases", csrf_token="csrf")["page"]
    )

    blocked = copy.deepcopy(awaiting)
    blocked["phase"] = "plan_blocked"
    blocked["apply_confirmation"] = None
    blocked_view = render_session_view(
        _as_application_session(_StatusSession(session.run_root, blocked)),
        session_key="all-phases",
        csrf_token="csrf",
    )
    observed["plan_blocked"] = cast("str", blocked_view["page"])
    assert blocked_view["ready"] is False

    token = cast("str", awaiting["apply_confirmation"]["patch_plan_sha256"])
    session.apply_confirmed(patch_plan_sha256=token)
    applied = session.status()
    observed[cast("str", applied["phase"])] = cast(
        "str", render_session_view(session, session_key="all-phases", csrf_token="csrf")["page"]
    )
    for phase in ("verified", "partially_completed", "ready_to_bundle", "completed"):
        synthetic = copy.deepcopy(applied)
        synthetic["phase"] = phase
        view = render_session_view(
            _as_application_session(_StatusSession(session.run_root, synthetic)),
            session_key="all-phases",
            csrf_token="csrf",
        )
        observed[phase] = cast("str", view["page"])
        _assert_renderable_and_path_free(view, session.run_root)

    assert observed == {
        "approval_required": "approval",
        "approval_in_progress": "approval",
        "approval_ready_to_finalize": "approval",
        "ready_to_plan": "patch",
        "plan_blocked": "patch",
        "awaiting_apply_confirmation": "patch",
        "applied": "result",
        "verified": "result",
        "partially_completed": "result",
        "ready_to_bundle": "result",
        "completed": "result",
    }


def test_result_exposes_only_status_allowed_artifact_routes(tmp_path: Path) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(actor_id="author", actor_name="Author", generated_at=TIME)
    session.decide(
        change_id=_only_change_id(session),
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    ready = session.finalize_approval(generated_at=TIME)
    token = cast("str", ready["apply_confirmation"]["patch_plan_sha256"])
    session.apply_confirmed(patch_plan_sha256=token)
    status = session.status()
    (session.run_root / "ui-cache.json").write_text(
        '{"artifact":"C:/private/paper.tex"}', encoding="utf-8"
    )

    view = render_session_view(session, session_key="results-1", csrf_token="csrf-result")
    html = _assert_renderable_and_path_free(view, session.run_root)
    cards = cast("Sequence[Mapping[str, object]]", view["artifacts"])
    allowed = set(cast("Mapping[str, str]", status["artifacts"]))

    assert view["status"] == "partial"
    assert cards
    folder_cards = [card for card in cards if card.get("open_in_folder") is True]
    assert len(folder_cards) == 1
    assert folder_cards[0]["label"] == "修订后的 LaTeX 副本"
    assert "href" not in folder_cards[0]
    for card in cards:
        if card.get("open_in_folder") is True:
            continue
        href = cast("str", card["href"])
        assert href.startswith("/artifact/results-1/")
        assert href.rsplit("/", 1)[-1] in allowed
    assert "/artifact/results-1/revised_source" not in html
    assert "请使用下方“打开修订后的 LaTeX 文件夹”查看" in html
    assert view["revised_source_available"] is True
    assert view["open_revised_action"] == "/result/open-revised"
    assert view["delivery_available"] is False
    assert view["open_delivery_action"] == "/result/open-delivery"
    assert 'action="/result/open-revised"' in html
    assert 'action="/result/open-delivery"' not in html
    assert "/result/open-folder" not in html
    assert "source_manifest" not in html
    assert "C:/private" not in html
    exit_form = html.split('action="/app/exit"', 1)[1].split("</form>", 1)[0]
    assert 'name="csrf" value="csrf-result"' in exit_form
    assert 'name="session"' not in exit_form
    new_form = html.split('action="/new"', 1)[1].split("</form>", 1)[0]
    assert 'name="csrf" value="csrf-result"' in new_form
    assert 'name="session"' not in new_form


def test_partial_result_hides_retry_when_downstream_evidence_is_already_sealed(
    tmp_path: Path,
) -> None:
    session = _review_session(tmp_path)
    status = session.status()
    synthetic = copy.deepcopy(status)
    synthetic["phase"] = "partially_completed"
    synthetic["step"] = 4
    synthetic["next_action"] = "inspect_partial_results"
    synthetic["blockers"] = [
        {
            "code": "verification_blocked",
            "attempt": 2,
            "retry_available": False,
        }
    ]

    view = render_session_view(
        _as_application_session(_StatusSession(session.run_root, synthetic)),
        session_key="sealed-partial",
        csrf_token="csrf",
    )
    html = _assert_renderable_and_path_free(view, session.run_root)

    assert view["can_retry"] is False
    assert 'action="/result/retry"' not in html
    assert "已保留 2 次不可变核验记录" in html
    assert "不能覆盖式重试" in html


def test_tampered_sealed_diff_becomes_path_free_error_view(tmp_path: Path) -> None:
    session = _review_session(tmp_path)
    session.begin_approval(actor_id="author", actor_name="Author", generated_at=TIME)
    session.decide(
        change_id=_only_change_id(session),
        decision="accepted",
        decided_at=TIME,
        generated_at=TIME,
    )
    session.finalize_approval(generated_at=TIME)
    diff = session.run_root / "plans/plan-r1/changes.patch"
    original = diff.read_bytes()
    diff.write_bytes(original + b"tamper")

    view = render_session_view(session, session_key="error-1", csrf_token="csrf-error")
    html = _assert_renderable_and_path_free(view, session.run_root)

    assert view["page"] == "error"
    assert "E_HASH_PATCHPLAN_MISMATCH" in html
    assert str(diff) not in html


def test_image_diagnostics_become_a_visible_path_free_error_summary() -> None:
    error = ContractError(
        ErrorCode.BACKEND_CAPABILITY_MISSING,
        "private export message",
        details={
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
            ]
        },
    )

    view = render_error_view(
        error,
        session_key="error-images",
        csrf_token="csrf-images",
        step=2,
    )
    html = render_app_page(view)
    card = cast("Mapping[str, object]", view["error"])

    assert card["summary"] == "2处图片写法当前无法处理"
    assert html.count("2处图片写法当前无法处理") == 1
    assert html.index("2处图片写法当前无法处理") < html.index("技术详情")
    assert "第464行，第5列（E_PATH_TRAVERSAL）" in html
    assert "第490行，第9列（E_PATH_TRAVERSAL）" in html
    assert "private" not in html
    assert "primary_action" not in card


@pytest.mark.parametrize(
    ("action", "kind", "href"),
    (
        ("choose_project", "post", "/new"),
        ("choose_returned_word", "post", "/session/receive"),
        ("review_changes", "link", "/session/session_safe?filter=all"),
        ("install_tool", "link", "/session/session_safe"),
        ("retry", "link", "/preflight/selection_safe"),
        ("start_new_round", "post", "/new"),
        ("open_results", "post", "/result/open-revised"),
        ("create_support_bundle", "post", "/support/export"),
    ),
)
def test_each_recovery_action_binds_to_one_real_http_control(
    action: RecoveryAction,
    kind: str,
    href: str,
) -> None:
    control = build_error_recovery_control(
        action,
        "继续处理",
        csrf_token="csrf-safe",
        session_key="session_safe",
        retry_href="/preflight/selection_safe",
        support_token=("support1.E_INTERNAL_INVARIANT.session_view." + "a" * 64),
    )

    assert control is not None
    assert control["kind"] == kind
    assert control["href"] == href
    if kind == "post":
        fields = cast("Mapping[str, object]", control["form_fields"])
        assert fields["csrf"] == "csrf-safe"
    else:
        assert "form_fields" not in control


def test_retry_control_is_omitted_without_a_distinct_retry_destination() -> None:
    assert (
        build_error_recovery_control(
            "retry",
            "重试",
            csrf_token="csrf-safe",
            session_key="session_safe",
        )
        is None
    )


def test_session_render_failure_does_not_link_back_to_the_same_session() -> None:
    assert (
        build_error_recovery_control(
            "install_tool",
            "查看安装方法",
            csrf_token="csrf-safe",
            session_key="session_safe",
            allow_session_actions=False,
        )
        is None
    )


def test_choose_project_error_uses_post_instead_of_broken_get() -> None:
    view = render_error_view(
        ErrorCode.PATH_TRAVERSAL,
        session_key="session_safe",
        csrf_token="csrf-safe",
    )
    card = cast("Mapping[str, object]", view["error"])
    control = cast("Mapping[str, object]", card["primary_action"])

    assert control["kind"] == "post"
    assert control["href"] == "/new"
    assert cast("Mapping[str, object]", control["form_fields"]) == {"csrf": "csrf-safe"}


@pytest.mark.parametrize("session_key", ("../escape", "with/slash", "", "x y"))
def test_unsafe_session_route_tokens_are_rejected(session_key: str, tmp_path: Path) -> None:
    session = _new_session(tmp_path)

    with pytest.raises(PresenterModelError, match="route token"):
        render_session_view(session, session_key=session_key, csrf_token="csrf")
