from __future__ import annotations


PRODUCT = {
    "code": "dt-anxiety",
    "name": "焦虑干预数字疗法",
    "organization": "示例数字疗法公司",
    "origin_country": "中国",
    "category": "数字疗法",
    "intended_use": "用于成人焦虑情绪的数字化认知行为干预和随访问卷评估",
    "risk_level": "high",
    "regulatory_status": "已注册",
}


def _prepare(client) -> None:
    assert client.post("/api/catalog/products", json=PRODUCT).status_code == 201
    site = {
        "code": "clinic-cd", "name": "成都临床合作点", "site_type": "医院",
        "region": "四川", "capabilities": ["dt-anx"], "max_concurrent": 2,
    }
    assert client.post("/api/catalog/sites", json=site).status_code == 201
    protocol = {
        "code": "dt-anx-v1", "name": "焦虑数字疗法方案", "capability": "dt-anx",
        "product_code": "dt-anxiety",
        "parameter_schema": {"level": {"type": "integer", "required": True, "minimum": 1, "maximum": 5}},
        "max_runtime_seconds": 600, "max_attempts": 2,
    }
    assert client.post("/api/pilots/protocols?actor=ops", json=protocol).status_code == 201


def _session(client, key):
    response = client.post("/api/pilots/sessions", json={
        "protocol_code": "dt-anx-v1", "project_code": "west-pilot", "requested_by": "ops-cd",
        "parameters": {"level": 2}, "priority": 50, "idempotency_key": key,
    })
    assert response.status_code == 202, response.text
    return response.json()


def test_safety_chain_over_http(client):
    _prepare(client)
    session = _session(client, "http-session-1")

    # 规则查询返回默认值。
    rules = client.get("/api/safety/rules")
    assert rules.status_code == 200 and rules.json()["cluster_signal_count"] == 3

    # 严重事件上报，自动暂停并生成同一份决定的场次通知。
    report = client.post("/api/safety/reports", json={
        "report_key": "http-report-severe", "product_code": "dt-anxiety", "site_code": "clinic-cd",
        "session_id": session["id"], "severity": "severe", "symptoms": ["急性焦虑发作"],
        "occurrence_at": "2026-10-05T03:00:00+00:00", "description": "体验后出现急性发作",
        "channel": "现场系统", "reporter_ref": "nurse-2",
    })
    assert report.status_code == 201, report.text
    suspension = report.json()["suspension"]
    assert suspension and suspension["status"] == "active"

    # 新场次经 HTTP 被阻止。
    blocked = client.post("/api/pilots/sessions", json={
        "protocol_code": "dt-anx-v1", "project_code": "west-pilot", "requested_by": "ops-cd",
        "parameters": {"level": 2}, "priority": 50, "idempotency_key": "http-session-blocked",
    })
    assert blocked.status_code == 409

    # 同一份决定投递给所有未完成场次。
    notices = client.get("/api/safety/notices", params={"status": "pending"})
    assert notices.status_code == 200
    items = notices.json()["items"]
    assert len(items) == 1 and items[0]["decision_uid"] == suspension["decision_uid"]

    delivery = client.post("/api/safety/notices/deliver-pending")
    assert delivery.status_code == 200 and len(delivery.json()["delivered"]) == 1

    # 调查结论写定前不能开始解除会签。
    action_id = suspension["id"]
    early = client.post(f"/api/safety/actions/{action_id}/lift-signatures", json={"reviewer": "dr-one", "reason": "同意恢复"})
    assert early.status_code == 409

    assert client.post(f"/api/safety/actions/{action_id}/investigation", json={
        "reviewer": "dr-one", "reason": "根因为引导语缺失，已修复并在两地复测通过",
    }).status_code == 200

    # 同一审阅人不能完成双人解除。
    first = client.post(f"/api/safety/actions/{action_id}/lift-signatures", json={"reviewer": "dr-one", "reason": "证据闭环同意恢复"})
    assert first.status_code == 201 and first.json()["status"] == "active"
    same = client.post(f"/api/safety/actions/{action_id}/lift-signatures", json={"reviewer": "dr-one", "reason": "再次签署"})
    assert same.status_code == 409
    second = client.post(f"/api/safety/actions/{action_id}/lift-signatures", json={"reviewer": "dr-two", "reason": ""})
    assert second.status_code == 201 and second.json()["status"] == "lifted"

    # 解除后场次恢复可提交。
    assert _session(client, "http-session-reopened")["status"] == "queued"

    # 产品档案在重新开放后仍可追溯首个信号、判断、通知对象和解除依据。
    dossier = client.get("/api/safety/products/dt-anxiety/dossier")
    assert dossier.status_code == 200
    body = dossier.json()
    assert body["open_for_new_sessions"] is True
    assert body["first_signal_at"]
    assert body["reports"][0]["timeline"][0]["event_type"] == "report"
    assert body["notice_recipients"]
    lifted = [a for a in body["actions"] if a["action_type"] == "suspension"][0]
    assert lifted["lift_reviewer1"] == "dr-one" and lifted["lift_reviewer2"] == "dr-two"
    assert lifted["investigation_summary"]


def test_duplicate_channels_merge_via_http(client):
    _prepare(client)
    first = client.post("/api/safety/reports", json={
        "report_key": "dup-http-1", "product_code": "dt-anxiety", "site_code": "clinic-cd",
        "severity": "mild", "symptoms": ["头晕"],
        "occurrence_at": "2026-10-05T03:00:00+00:00", "channel": "现场系统", "reporter_ref": "n-1",
    })
    second = client.post("/api/safety/reports", json={
        "report_key": "dup-http-2", "product_code": "dt-anxiety", "site_code": "clinic-cd",
        "severity": "mild", "symptoms": ["头晕"],
        "occurrence_at": "2026-10-05T03:00:00+00:00", "channel": "市民热线", "reporter_ref": "call-9",
    })
    assert first.status_code == second.status_code == 201
    assert second.json()["merged"] is True
    assert second.json()["report_id"] == first.json()["report_id"]
    listing = client.get("/api/safety/reports")
    assert len(listing.json()["items"]) == 1
