from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError, ValidationError
from app.database import get_connection, init_db
from app.pilots.service import PilotOperationsService
from app.safety.service import SafetyService

CLOCK_START = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

PRODUCT = {
    "code": "dt-insomnia",
    "name": "失眠数字疗法应用",
    "organization": "示例数字医疗",
    "origin_country": "中国",
    "category": "数字疗法",
    "intended_use": "用于成人失眠的认知行为数字干预与随访问卷推送",
    "risk_level": "medium",
    "regulatory_status": "已注册",
}

PROTOCOL = {
    "code": "dt-cbt-i",
    "name": "失眠数字疗法体验方案",
    "capability": "dt-cbt",
    "product_code": "dt-insomnia",
    "parameter_schema": {"weeks": {"type": "integer", "required": True, "minimum": 1, "maximum": 12}},
    "default_parameters": {},
    "max_runtime_seconds": 1800,
    "max_attempts": 3,
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_INNOVATION_DATABASE_PATH", str(tmp_path / "safety.db"))
    from app.database import close_connection
    close_connection()
    init_db()
    connection = get_connection()
    clock = FrozenClock(CLOCK_START)
    catalog = _CatalogHelper(connection)
    catalog.create_product(PRODUCT)
    catalog.create_site({"code": "site-bj", "name": "北京临床点", "site_type": "医院", "region": "北京", "capabilities": ["dt-cbt"], "max_concurrent": 2})
    catalog.create_site({"code": "site-sh", "name": "上海体验点", "site_type": "展会体验点", "region": "上海", "capabilities": ["dt-cbt"], "max_concurrent": 2})
    pilots = PilotOperationsService(connection, clock)
    pilots.create_protocol(PROTOCOL, "operator")
    safety = SafetyService(connection, clock)
    yield type("Env", (), {
        "connection": connection, "clock": clock, "catalog": catalog,
        "pilots": pilots, "safety": safety,
    })()
    close_connection()


class _CatalogHelper:
    def __init__(self, connection):
        from app.catalog.service import CatalogService
        from app.core.clock import FrozenClock
        self.service = CatalogService(connection, FrozenClock(CLOCK_START))

    def create_product(self, data):
        return self.service.create_product(data)

    def create_site(self, data):
        return self.service.create_site(data)


def report_payload(key, *, site="site-bj", session_id=None, severity="moderate", symptoms=("头晕",), channel="现场系统", reporter_ref="r-1", occurrence="2026-10-01T09:00:00+00:00", signal_key=""):
    payload = {
        "report_key": key,
        "product_code": "dt-insomnia",
        "site_code": site,
        "session_id": session_id,
        "severity": severity,
        "symptoms": list(symptoms),
        "occurrence_at": occurrence,
        "description": "受试者体验后出现不适",
        "channel": channel,
        "reporter_ref": reporter_ref,
    }
    if signal_key:
        payload["signal_key"] = signal_key
    return payload


def _submit_session(pilots, key, *, user="operator-1", priority=50):
    return pilots.submit({
        "protocol_code": "dt-cbt-i",
        "project_code": "pilot-2026",
        "requested_by": user,
        "parameters": {"weeks": 4},
        "priority": priority,
        "idempotency_key": key,
    })


# -- 报告内容持久化 ----------------------------------------------------------

def test_report_persists_symptoms_severity_timeline_and_links(env):
    session = _submit_session(env.pilots, "session-001")
    result = env.safety.submit_report(report_payload("report-0001", session_id=session["id"], symptoms=("头晕", "恶心")))
    assert result["report_id"] > 0 and result["suspension"] is None
    detail = env.safety.report_detail(result["report_id"])
    assert detail["severity"] == "moderate"
    assert detail["symptoms_json"]
    assert detail["product_code"] == "dt-insomnia"
    assert detail["session_id"] == session["id"]
    assert detail["status"] == "submitted"
    assert len(detail["timeline"]) == 1
    assert detail["timeline"][0]["event_type"] == "report"
    assert detail["sources"][0]["channel"] == "现场系统"


def test_session_product_mismatch_is_rejected(env):
    env.catalog.create_product({**PRODUCT, "code": "dt-other"})
    session = _submit_session(env.pilots, "session-002")
    payload = report_payload("report-0002", session_id=session["id"])
    payload["product_code"] = "dt-other"
    with pytest.raises(ConflictError):
        env.safety.submit_report(payload)


# -- 严重事件自动暂停、阻止新场次、通知全部未完成场次 --------------------------

def test_severe_event_suspends_product_blocks_new_sessions_and_notices_all_sites(env):
    queued_bj = _submit_session(env.pilots, "session-bj", user="op-bj")
    queued_sh = _submit_session(env.pilots, "session-sh", user="op-sh")
    running = _submit_session(env.pilots, "session-run", user="op-run", priority=90)
    claimed = env.pilots.claim("site-bj", ["dt-cbt"], 120)
    assert claimed["id"] == running["id"]
    finished = _submit_session(env.pilots, "session-done", user="op-done", priority=100)
    done_claim = env.pilots.claim("site-sh", ["dt-cbt"], 120)
    assert done_claim["id"] == finished["id"]
    env.pilots.complete(finished["id"], "site-sh", {"ok": True}, {})

    result = env.safety.submit_report(report_payload(
        "report-severe", severity="severe", symptoms=("惊恐发作",), channel="热线",
    ))
    action = result["suspension"]
    assert action is not None and action["action_type"] == "suspension" and action["status"] == "active"
    assert set(action["scope_session_ids"]) == {queued_bj["id"], queued_sh["id"], running["id"]}

    # 新场次提交被阻止。
    with pytest.raises(ConflictError):
        _submit_session(env.pilots, "session-blocked")
    # 排队场次无法再被领取，即使其他场地来领。
    assert env.pilots.claim("site-sh", ["dt-cbt"], 120) is None

    notices = env.safety.list_notices(status="pending")
    assert {n["session_id"] for n in notices} == set(action["scope_session_ids"])
    assert len({n["decision_uid"] for n in notices}) == 1
    recipients = {n["recipient"] for n in notices}
    assert "site-bj" in recipients and "op-sh" in recipients


def test_notice_delivery_is_resumable_after_transport_failure(env):
    _submit_session(env.pilots, "session-a")
    _submit_session(env.pilots, "session-b")
    env.safety.submit_report(report_payload("r-severe", severity="life_threatening", symptoms=("晕厥",)))
    notices = env.safety.list_notices(status="pending")
    assert len(notices) == 2

    failures = iter([RuntimeError("通知通道中断"), None])

    def flaky_transport(_notice):
        error = next(failures)
        if error is not None:
            raise error

    env.safety.transport = flaky_transport
    first_run = env.safety.deliver_pending_notices()
    assert len(first_run["delivered"]) == 1 and len(first_run["failed"]) == 1

    # 模拟进程重启：全新服务实例从持久状态恢复，只投递剩余的一份。
    resumed = SafetyService(get_connection(), env.clock)
    second_run = resumed.deliver_pending_notices()
    assert len(second_run["delivered"]) == 1 and second_run["failed"] == []
    third_run = resumed.deliver_pending_notices()
    assert third_run["delivered"] == []
    statuses = {n["delivery_status"] for n in resumed.list_notices()}
    assert statuses == {"delivered"}
    # 中断过的那份通知经历一次失败重试后才送达。
    recovered = [n for n in resumed.list_notices() if n["attempts"] == 2]
    first_shot = [n for n in resumed.list_notices() if n["attempts"] == 1]
    assert len(recovered) == 1 and len(first_shot) == 1


# -- 跨渠道合并与幂等 --------------------------------------------------------

def test_cross_channel_duplicates_merge_sources_without_double_counting(env):
    first = env.safety.submit_report(report_payload("key-channel-1", channel="现场系统", reporter_ref="nurse-7"))
    second = env.safety.submit_report(report_payload(
        "key-channel-2", channel="市民热线", reporter_ref="call-555", severity="moderate",
    ))
    assert second["merged"] is True and second["report_id"] == first["report_id"]
    detail = env.safety.report_detail(first["report_id"])
    assert len(detail["sources"]) == 2
    assert {s["channel"] for s in detail["sources"]} == {"现场系统", "市民热线"}
    assert len(detail["timeline"]) == 2
    # 列表只有一份报告，不重复计数。
    assert len(env.safety.list_reports()) == 1

    # 同渠道同报告人再次发送是纯重放：来源唯一、时间线不增加。
    replay = env.safety.submit_report(report_payload("key-channel-3", channel="市民热线", reporter_ref="call-555"))
    assert replay["idempotent"] is True
    detail = env.safety.report_detail(first["report_id"])
    assert len(detail["sources"]) == 2 and len(detail["timeline"]) == 2

    # 同一 report_key 重放也直接返回原报告。
    same_key = env.safety.submit_report(report_payload("key-channel-1", channel="现场系统", reporter_ref="nurse-7"))
    assert same_key["idempotent"] is True and same_key["report_id"] == first["report_id"]


def test_merged_higher_severity_can_trigger_suspension(env):
    first = env.safety.submit_report(report_payload("dup-sev-1", severity="moderate", channel="现场系统", reporter_ref="a"))
    assert first["suspension"] is None
    merged = env.safety.submit_report(report_payload("dup-sev-2", severity="death", channel="监管通报", reporter_ref="b"))
    assert merged["merged"] is True
    assert merged["suspension"] is not None
    detail = env.safety.report_detail(first["report_id"])
    assert detail["severity"] == "death"
    assert detail["status"] == "escalated"


# -- 迟到回执只能追加 --------------------------------------------------------

def test_late_receipt_only_appends_to_timeline(env):
    created = env.safety.submit_report(report_payload("late-1"))
    before = env.safety.report_detail(created["report_id"])
    env.safety.add_supplement(created["report_id"], {
        "actor": "site-bj",
        "kind": "late_receipt",
        "summary": "三天后才收到的设备日志回执",
        "detail": {"log_id": "L-99"},
        "occurred_at": "2026-10-01T09:05:00+00:00",
    })
    after = env.safety.report_detail(created["report_id"])
    assert len(after["timeline"]) == len(before["timeline"]) + 1
    event = after["timeline"][-1]
    assert event["event_type"] == "late_receipt"
    assert event["detail_json"]["occurred_at"] == "2026-10-01T09:05:00+00:00"
    # 原报告关键字段不被改写。
    assert after["severity"] == before["severity"]
    assert after["first_signal_at"] == before["first_signal_at"]


# -- 分诊四种决定 ------------------------------------------------------------

def test_triage_decisions_flow(env):
    report = env.safety.submit_report(report_payload("tri-1"))
    result = env.safety.triage(report["report_id"], {
        "reviewer": "dr-zhang", "decision": "request_information", "reason": "需要设备使用时长日志",
    })
    assert result["status"] == "information_requested"

    env.safety.add_supplement(report["report_id"], {"actor": "site-bj", "kind": "supplement", "summary": "已补交日志", "detail": {}})
    escalated = env.safety.triage(report["report_id"], {
        "reviewer": "dr-zhang", "decision": "escalate_investigation", "reason": "不能排除产品关联",
    })
    assert escalated["status"] == "investigating"

    detail = env.safety.report_detail(report["report_id"])
    assert [d["decision"] for d in detail["decisions"]] == ["request_information", "escalate_investigation"]
    assert [t["event_type"] for t in detail["timeline"]].count("triage") == 2


def test_exclude_relation_requires_finding_and_drops_from_cluster(env):
    report = env.safety.submit_report(report_payload("tri-excl", signal_key="sig-x"))
    with pytest.raises(ValidationError):
        env.safety.triage(report["report_id"], {"reviewer": "dr-li", "decision": "exclude_relation", "reason": "无关", "related_finding": ""})
    env.safety.triage(report["report_id"], {
        "reviewer": "dr-li", "decision": "exclude_relation",
        "reason": "症状由受试者基础疾病导致", "related_finding": "既往偏头痛史，用药记录吻合",
    })
    detail = env.safety.report_detail(report["report_id"])
    assert detail["status"] == "related_excluded"


def test_manual_suspension_via_triage(env):
    _submit_session(env.pilots, "manual-session")
    report = env.safety.submit_report(report_payload("tri-manual", severity="moderate"))
    result = env.safety.triage(report["report_id"], {
        "reviewer": "dr-wang", "decision": "trigger_suspension", "reason": "两家场地出现相似信号，先暂停",
    })
    assert result["suspension"]["status"] == "active"
    assert env.safety.list_notices(status="pending")


# -- 同类信号聚集规则 --------------------------------------------------------

def test_cluster_of_similar_signals_triggers_suspension(env):
    base = {"severity": "moderate", "symptoms": ("心悸",), "signal_key": "palpitation"}
    times = ["2026-10-01T09:00:00+00:00", "2026-10-02T09:00:00+00:00", "2026-10-03T08:00:00+00:00"]
    r1 = env.safety.submit_report(report_payload("cl-1", site="site-bj", channel="现场系统", reporter_ref="a", occurrence=times[0], **base))
    assert r1["suspension"] is None
    r2 = env.safety.submit_report(report_payload("cl-2", site="site-sh", channel="现场系统", reporter_ref="b", occurrence=times[1], **base))
    assert r2["suspension"] is None
    r3 = env.safety.submit_report(report_payload("cl-3", site="site-sh", channel="热线", reporter_ref="c", occurrence=times[2], **base))
    action = r3["suspension"]
    assert action is not None
    assert set(action["triggered_report_ids"]) == {r1["report_id"], r2["report_id"], r3["report_id"]}


def test_cluster_outside_window_does_not_trigger(env):
    base = {"severity": "moderate", "symptoms": ("心悸",), "signal_key": "palpitation-wide"}
    env.safety.submit_report(report_payload("cw-1", occurrence="2026-10-01T09:00:00+00:00", **base))
    env.clock.advance(days=2)
    env.safety.submit_report(report_payload("cw-2", occurrence="2026-10-03T09:00:00+00:00", **base))
    env.clock.advance(days=2)
    third = env.safety.submit_report(report_payload("cw-3", occurrence="2026-10-05T09:00:00+00:00", **base))
    assert third["suspension"] is None


# -- 双人解除与调查结论不可改写 ----------------------------------------------

def _suspend(env):
    _submit_session(env.pilots, "lift-session")
    result = env.safety.submit_report(report_payload("lift-1", severity="severe", symptoms=("抽搐",)))
    return result["suspension"]["id"]


def test_lift_requires_finalized_investigation_and_two_distinct_reviewers(env):
    action_id = _suspend(env)
    with pytest.raises(ConflictError):
        env.safety.lift_signature(action_id, {"reviewer": "dr-a", "reason": "复核通过可以恢复"})

    with pytest.raises(ValidationError):
        env.safety.finalize_investigation(action_id, "dr-a", "已恢复")
    env.safety.finalize_investigation(action_id, "dr-a", "三轮复测未复现，厂家完成参数修复，证据充分")

    # 结论写定后不得改写。
    with pytest.raises(ConflictError):
        env.safety.finalize_investigation(action_id, "dr-b", "试图改写调查结论为其他内容")

    first = env.safety.lift_signature(action_id, {"reviewer": "dr-a", "reason": "调查闭环，同意恢复"})
    assert first["status"] == "active"
    with pytest.raises(ConflictError):
        env.safety.lift_signature(action_id, {"reviewer": "dr-a", "reason": "重复签署"})
    second = env.safety.lift_signature(action_id, {"reviewer": "dr-b", "reason": ""})
    assert second["status"] == "lifted"
    assert second["lift_reviewer1"] == "dr-a" and second["lift_reviewer2"] == "dr-b"
    assert second["investigation_summary"].startswith("三轮复测")

    # 解除依据不能被第二签改写。
    assert second["lift_reason"] == "调查闭环，同意恢复"
    # 产品重新开放，新场次可提交。
    assert _submit_session(env.pilots, "after-lift")["status"] == "queued"
    resumes = [a for a in env.safety.list_actions("dt-insomnia") if a["action_type"] == "resumption"]
    assert len(resumes) == 1 and resumes[0]["related_action_id"] == action_id


def test_lifted_sessions_receive_same_trackable_resumption_decision(env):
    action_id = _suspend(env)
    env.safety.finalize_investigation(action_id, "dr-a", "调查完成，根因明确，修复有效")
    env.safety.lift_signature(action_id, {"reviewer": "dr-a", "reason": "同意恢复"})
    env.safety.lift_signature(action_id, {"reviewer": "dr-b", "reason": ""})
    pending = env.safety.list_notices(status="pending")
    resumes = [n for n in pending if n["kind"] == "resumption"]
    assert resumes and len({n["decision_uid"] for n in resumes}) == 1
    # 解除动作通过 related_action_id 可回溯到原暂停决定。
    resumption_action = env.safety.list_actions("dt-insomnia")[-1]
    assert resumption_action["related_action_id"] == action_id


# -- 完整产品档案 ------------------------------------------------------------

def test_product_dossier_shows_full_chain_before_reopening(env):
    _submit_session(env.pilots, "dossier-session")
    report = env.safety.submit_report(report_payload("dossier-1", severity="severe", symptoms=("胸闷",)))
    action_id = report["suspension"]["id"]
    env.safety.add_supplement(report["report_id"], {"actor": "site-bj", "kind": "supplement", "summary": "补充心电记录", "detail": {}})
    env.safety.finalize_investigation(action_id, "dr-a", "确认为佩戴指导不足，已更新引导流程并复测通过")
    env.safety.lift_signature(action_id, {"reviewer": "dr-a", "reason": "整改验证通过"})

    dossier = env.safety.product_dossier("DT-INSOMNIA")  # 大小写归一
    assert dossier["open_for_new_sessions"] is False
    assert dossier["first_signal_at"]
    chain = dossier["reports"][0]
    types = [t["event_type"] for t in chain["timeline"]]
    assert "report" in types and "supplement" in types
    assert dossier["notice_recipients"]
    suspension = dossier["active_suspension"]
    assert suspension["lift_reviewer1"] == "dr-a" and not suspension["lift_reviewer2"]

    env.safety.lift_signature(action_id, {"reviewer": "dr-b", "reason": ""})
    reopened = env.safety.product_dossier("dt-insomnia")
    assert reopened["open_for_new_sessions"] is True
    assert reopened["active_suspension"] is None
    lift_basis = [a for a in reopened["actions"] if a["action_type"] == "suspension"][0]
    assert lift_basis["status"] == "lifted" and lift_basis["lift_reason"] == "整改验证通过"
    assert lift_basis["investigation_summary"].startswith("确认")
