from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.database import get_connection
from app.safety.service import SafetyService

PRODUCT = {
    "code": "dtx-calmbio",
    "name": "心境数字疗法程序",
    "organization": "示例数字医疗",
    "origin_country": "中国",
    "category": "数字疗法",
    "intended_use": "用于焦虑症状管理的处方数字疗法与跨场地试点观察",
    "risk_level": "high",
    "regulatory_status": "研究",
}

PROTOCOL = {
    "code": "dtx-calm",
    "name": "数字疗法定场次方案",
    "capability": "dtx-calm",
    "product_code": "dtx-calmbio",
    "parameter_schema": {"minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 60}},
    "default_parameters": {},
    "max_runtime_seconds": 600,
    "max_attempts": 3,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _setup_product(client) -> None:
    response = client.post("/api/catalog/products", json=PRODUCT)
    assert response.status_code == 201, response.text


def _setup_protocol(client) -> None:
    _setup_product(client)
    response = client.post("/api/pilots/protocols?actor=admin", json=PROTOCOL)
    assert response.status_code == 201, response.text


def _submit(client, key: str, *, user: str = "operator-a") -> dict:
    response = client.post("/api/pilots/sessions", json={
        "protocol_code": "dtx-calm",
        "product_code": "dtx-calmbio",
        "project_code": "pilot-cross-site",
        "requested_by": user,
        "parameters": {"minutes": 20},
        "idempotency_key": key,
    })
    assert response.status_code == 202, response.text
    return response.json()


def _report_payload(*, severity: str = "serious", symptoms=None, session_id=None, site_code: str = "site-a",
                    channel: str = "hotline", external_ref: str = "call-001", reporter: str = "护士甲",
                    occurred_at: str | None = None, duplicate_of=None) -> dict:
    payload = {
        "product_code": "dtx-calmbio",
        "session_id": session_id,
        "site_code": site_code,
        "symptoms": symptoms or ["心悸", "头晕"],
        "severity": severity,
        "event_occurred_at": occurred_at or _now_iso(),
        "description": "体验数字疗法程序后出现不适",
        "source": {"channel": channel, "external_ref": external_ref, "reporter": reporter},
    }
    if duplicate_of is not None:
        payload["duplicate_of_report_no"] = duplicate_of
    return payload


def test_serious_report_suspends_product_blocks_new_and_unfinished_sessions(client):
    _setup_protocol(client)
    first = _submit(client, "session-0001")
    second = _submit(client, "session-0002", user="operator-b")
    claimed = client.post("/api/pilots/sessions/claim", json={"site_code": "site-a", "capabilities": ["dtx-calm"], "lease_seconds": 120})
    assert claimed.status_code == 200 and claimed.json()["session"]["id"] == first["id"]

    response = client.post("/api/safety/reports", json=_report_payload(session_id=first["id"]))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "investigating"
    assert body["merged"] is False
    types = [item["entry_type"] for item in body["timeline"]]
    assert "report" in types and "decision" in types and "decision_effect" in types

    held = client.get(f"/api/pilots/session-details/{first['id']}").json()
    blocked = client.get(f"/api/pilots/session-details/{second['id']}").json()
    assert held["status"] == "safety_hold" and held["blocked_by_decision_no"].startswith("SUS-")
    assert blocked["status"] == "blocked" and blocked["blocked_by_decision_no"] == held["blocked_by_decision_no"]

    # 其他场地不能领取同一产品的新场次
    claim = client.post("/api/pilots/sessions/claim", json={"site_code": "site-b", "capabilities": ["dtx-calm"], "lease_seconds": 120})
    assert claim.status_code == 200 and claim.json()["session"] is None
    # 新场次提交被阻止
    new_submit = client.post("/api/pilots/sessions", json={
        "protocol_code": "dtx-calm", "product_code": "dtx-calmbio", "project_code": "pilot-cross-site",
        "requested_by": "operator-c", "parameters": {"minutes": 20}, "idempotency_key": "session-0003",
    })
    assert new_submit.status_code == 409
    assert new_submit.json()["error"]["context"]["decision_no"].startswith("SUS-")


def test_duplicate_reports_from_different_channels_merge_sources(client):
    _setup_protocol(client)
    occurred = _now_iso()
    first = client.post("/api/safety/reports", json=_report_payload(severity="mild", occurred_at=occurred, channel="hotline", external_ref="call-100"))
    assert first.status_code == 201
    second = client.post("/api/safety/reports", json=_report_payload(severity="mild", occurred_at=occurred, channel="email", external_ref="mail-200", reporter="医生乙"))
    assert second.status_code == 201 and second.json()["merged"] is True
    # 同渠道同外部编号的迟到重报不增加来源
    third = client.post("/api/safety/reports", json=_report_payload(severity="mild", occurred_at=occurred, channel="hotline", external_ref="call-100"))
    assert third.status_code == 201 and third.json()["merged"] is True

    reports = client.get("/api/safety/reports").json()["items"]
    assert len(reports) == 1
    detail = client.get(f"/api/safety/reports/{first.json()['report_no']}").json()
    assert {item["channel"] for item in detail["sources"]} == {"hotline", "email"}
    merge_entries = [item for item in detail["timeline"] if item["entry_type"] == "source_merge"]
    assert len(merge_entries) == 1


def test_triage_request_info_exclude_and_investigate(client):
    _setup_protocol(client)
    # 要求补充
    r1 = client.post("/api/safety/reports", json=_report_payload(severity="moderate", symptoms=["恶心"], external_ref="ref-1"))
    triaged = client.post(f"/api/safety/reports/{r1.json()['report_no']}/triage", json={"actor": "医师A", "action": "request_info", "note": "请补充用药记录"})
    assert triaged.status_code == 200 and triaged.json()["status"] == "info_requested"
    # 补充材料只追加时间线
    supplement = client.post(f"/api/safety/reports/{r1.json()['report_no']}/supplement", json={"actor": "护士甲", "channel": "hotline", "content": "患者已提供用药清单"})
    assert supplement.status_code == 201
    assert any(item["entry_type"] == "source_merge" for item in supplement.json()["timeline"])

    # 排除关联
    r2 = client.post("/api/safety/reports", json=_report_payload(severity="moderate", symptoms=["失眠"], external_ref="ref-2"))
    excluded = client.post(f"/api/safety/reports/{r2.json()['report_no']}/triage", json={"actor": "医师A", "action": "exclude", "note": "与产品无关"})
    assert excluded.status_code == 200 and excluded.json()["status"] == "excluded"
    assert client.post(f"/api/safety/reports/{r2.json()['report_no']}/triage", json={"actor": "医师A", "action": "investigate"}).status_code == 409

    # 升级调查
    r3 = client.post("/api/safety/reports", json=_report_payload(severity="moderate", symptoms=["头痛"], external_ref="ref-3"))
    investigated = client.post(f"/api/safety/reports/{r3.json()['report_no']}/triage", json={"actor": "医师B", "action": "investigate", "note": "需要产品级评估"})
    assert investigated.status_code == 200 and investigated.json()["status"] == "investigating"
    inv_no = next(item["detail"]["investigation_no"] for item in investigated.json()["timeline"] if item["entry_type"] == "triage" and item["detail"]["action"] == "investigate")

    # 调查结论一次定稿，不可改写
    concluded = client.post(f"/api/safety/investigations/{inv_no}/conclude", json={"actor": "医师B", "conclusion": "确认与产品提醒逻辑相关，已修复"})
    assert concluded.status_code == 200 and concluded.json()["status"] == "concluded"
    again = client.post(f"/api/safety/investigations/{inv_no}/conclude", json={"actor": "医师C", "conclusion": "试图改写结论"})
    assert again.status_code == 409
    assert client.get(f"/api/safety/reports/{r3.json()['report_no']}").json()["status"] == "closed"


def test_cluster_rule_requires_same_symptom_count_within_window(client):
    _setup_protocol(client)
    base = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    clock = FrozenClock(base)
    service = SafetyService(get_connection(), clock)
    common = dict(product_code="dtx-calmbio", severity="moderate", description="聚集信号测试",
                  source={"channel": "hotline", "external_ref": "", "reporter": "护士"})
    # 两条不同症状 + 一条同类，不满足 3 条同类
    service.create_report({**common, "session_id": None, "site_code": "s1", "symptoms": ["恶心"], "event_occurred_at": _storage(base)})
    service.create_report({**common, "session_id": None, "site_code": "s2", "symptoms": ["失眠"], "event_occurred_at": _storage(base)})
    service.create_report({**common, "session_id": None, "site_code": "s3", "symptoms": ["恶心"], "event_occurred_at": _storage(base)})
    overview = service.product_safety_overview("dtx-calmbio")
    assert overview["active_suspension"] is None
    # 第三条同类恶心出现 -> 聚集规则命中
    service.create_report({**common, "session_id": None, "site_code": "s4", "symptoms": ["恶心", "出汗"], "event_occurred_at": _storage(base)})
    overview = service.product_safety_overview("dtx-calmbio")
    assert overview["active_suspension"] is not None
    assert overview["active_suspension"]["trigger_rule"] == "cluster-same-symptom"


def _storage(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def test_cluster_rule_window_expiry_does_not_trigger(client):
    _setup_protocol(client)
    clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
    service = SafetyService(get_connection(), clock)
    common = dict(product_code="dtx-calmbio", severity="moderate", description="窗口测试",
                  source={"channel": "hotline", "external_ref": "", "reporter": "护士"})
    for day, ref in ((0, "a"), (10, "b"), (40, "c")):
        clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=UTC) + timedelta(days=day))
        service = SafetyService(get_connection(), clock)
        service.create_report({**common, "session_id": None, "site_code": f"site-{ref}", "symptoms": ["恶心"], "event_occurred_at": _storage(clock.current)})
    assert service.product_safety_overview("dtx-calmbio")["active_suspension"] is None


def test_dual_reviewer_lift_releases_sessions_without_altering_conclusion(client):
    _setup_protocol(client)
    session = _submit(client, "session-lift-1")
    report = client.post("/api/safety/reports", json=_report_payload(severity="serious", session_id=session["id"]))
    assert report.status_code == 201
    decision_no = report.json()["timeline"][-2]["detail"]["decision_no"]

    # 单一审阅人不能解除，同一人不能签两次
    first = client.post(f"/api/safety/decisions/{decision_no}/lift-approvals", json={"reviewer": "安全官甲", "note": "厂家已完成整改"})
    assert first.status_code == 200 and first.json()["lifted"] is False
    duplicate = client.post(f"/api/safety/decisions/{decision_no}/lift-approvals", json={"reviewer": "安全官甲", "note": "重复签发"})
    assert duplicate.status_code == 409
    second = client.post(f"/api/safety/decisions/{decision_no}/lift-approvals", json={"reviewer": "安全官乙", "note": "复核整改证据齐备，同意恢复"})
    assert second.status_code == 200 and second.json()["lifted"] is True

    held = client.get(f"/api/pilots/session-details/{session['id']}").json()
    assert held["status"] == "queued" and held["blocked_by_decision_no"] == ""
    # 产品重新开放，新场次可领取
    claim = client.post("/api/pilots/sessions/claim", json={"site_code": "site-a", "capabilities": ["dtx-calm"], "lease_seconds": 120})
    assert claim.json()["session"]["id"] == session["id"]

    overview = client.get("/api/safety/products/dtx-calmbio/overview").json()
    suspension = next(item for item in overview["decisions"] if item["decision_no"] == decision_no)
    assert suspension["status"] == "lifted"
    assert {item["reviewer"] for item in suspension["approvals"]} == {"安全官甲", "安全官乙"}
    resumptions = [item for item in overview["decisions"] if item["action"] == "resumption"]
    assert len(resumptions) == 1
    assert resumptions[0]["resumes_decision_id"] == suspension["id"]


def test_notification_outbox_resumes_after_crash_and_late_ack_appends_timeline(client):
    _setup_protocol(client)
    session = _submit(client, "session-notify-1")
    report = client.post("/api/safety/reports", json=_report_payload(severity="serious", session_id=session["id"]))
    decision_no = next(item["detail"]["decision_no"] for item in report.json()["timeline"] if item["entry_type"] == "decision")

    # 投递器连续失败：状态持久化，新实例恢复后继续
    def failing_sender(_payload):
        raise RuntimeError("通知渠道暂时不可用")

    clock = FrozenClock(datetime.now(UTC))
    broken = SafetyService(get_connection(), clock, sender=failing_sender)
    result = broken.deliver_pending()
    assert result["delivered"] == []
    pending = get_connection().execute("SELECT status,attempts FROM safety_notifications WHERE status='pending'").fetchall()
    assert pending and all(row["attempts"] == 1 for row in pending)

    recovered = SafetyService(get_connection(), clock)
    result = recovered.deliver_pending()
    assert len(result["delivered"]) >= 1

    # 迟到回执追加到原时间线，且不可重复
    ack = client.post(f"/api/safety/decisions/{decision_no}/acknowledge", json={"recipient": "site-a", "note": "场次已停止"})
    assert ack.status_code == 200
    detail = client.get(f"/api/safety/reports/{report.json()['report_no']}").json()
    assert any(item["entry_type"] == "acknowledgement" and item["detail"]["recipient"] == "site-a" for item in detail["timeline"])
    duplicate = client.post(f"/api/safety/decisions/{decision_no}/acknowledge", json={"recipient": "site-a", "note": "再次回执"})
    assert duplicate.status_code == 409


def test_terminal_failure_can_be_requeued_and_redelivered(client):
    _setup_protocol(client)
    session = _submit(client, "session-retry-1")
    client.post("/api/safety/reports", json=_report_payload(severity="serious", session_id=session["id"]))

    clock = FrozenClock(datetime.now(UTC))

    def failing_sender(_payload):
        raise RuntimeError("渠道宕机")

    broken = SafetyService(get_connection(), clock, sender=failing_sender)
    terminal: list[int] = []
    for _ in range(3):
        result = broken.deliver_pending()
        terminal = result["terminal_failed"]
    assert terminal, "连续三次失败后通知应转为终态失败"
    statuses = {row[0] for row in get_connection().execute("SELECT DISTINCT status FROM safety_notifications").fetchall()}
    assert statuses <= {"failed"}

    requeued = client.post("/api/safety/notifications/requeue-failed")
    assert requeued.status_code == 200 and requeued.json()["requeued"] >= 1
    recovered = SafetyService(get_connection(), clock)
    result = recovered.deliver_pending()
    assert len(result["delivered"]) >= 1


def test_overview_shows_first_signal_decisions_recipients_and_impact(client):
    _setup_protocol(client)
    s1 = _submit(client, "session-ov-1")
    s2 = _submit(client, "session-ov-2", user="operator-b")
    claimed = client.post("/api/pilots/sessions/claim", json={"site_code": "site-a", "capabilities": ["dtx-calm"], "lease_seconds": 120})
    assert claimed.json()["session"]["id"] == s1["id"]
    client.post("/api/safety/reports", json=_report_payload(severity="serious", session_id=s1["id"], site_code="site-a"))
    overview = client.get("/api/safety/products/dtx-calmbio/overview").json()
    assert overview["first_signal_at"]
    assert overview["active_suspension"]
    decision = overview["decisions"][-1]
    recipients = {item["recipient"] for item in decision["notifications"]}
    assert "site-a" in recipients
    affected = {item["session_id"] for item in decision["affected_sessions"]}
    assert affected == {s1["id"], s2["id"]}
    assert {item["effect"] for item in decision["affected_sessions"]} == {"held", "blocked"}
    # 每个判断、来源、时间线条目齐备
    assert overview["reports"][0]["sources"] and overview["reports"][0]["timeline"]


def test_report_validation_and_session_product_consistency(client):
    _setup_protocol(client)
    session = _submit(client, "session-val-1")
    # 场次必须属于上报产品
    bad = client.post("/api/safety/reports", json=_report_payload(product_code=None) if False else {
        **_report_payload(session_id=session["id"]), "product_code": "other-product"})
    assert bad.status_code == 404
    # 发生时间不能晚于当前时间
    future = client.post("/api/safety/reports", json=_report_payload(occurred_at=(datetime.now(UTC) + timedelta(days=2)).isoformat(timespec="seconds")))
    assert future.status_code == 422
    # 手动暂停需要依据
    mild = client.post("/api/safety/reports", json=_report_payload(severity="mild", symptoms=["口干"], external_ref="manual-1"))
    denied = client.post(f"/api/safety/reports/{mild.json()['report_no']}/triage", json={"actor": "医师A", "action": "suspend", "note": ""})
    assert denied.status_code == 422
    done = client.post(f"/api/safety/reports/{mild.json()['report_no']}/triage", json={"actor": "医师A", "action": "suspend", "note": "监管要求暂停同品类"})
    assert done.status_code == 200
    assert client.get("/api/safety/products/dtx-calmbio/overview").json()["active_suspension"]["trigger_rule"] == "manual_triage"
