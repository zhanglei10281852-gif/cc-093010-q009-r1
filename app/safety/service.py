from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any, Callable, Iterable

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.safety.repository import SafetyRepository

NotificationSender = Callable[[dict[str, Any]], None]
MAX_NOTIFICATION_ATTEMPTS = 3


def _fingerprint(product_code: str, session_id: int | None, site_code: str, symptoms: list[str], severity: str, occurred_at: str) -> str:
    payload = {"product": product_code, "session": session_id, "site": site_code, "symptoms": sorted(symptoms), "severity": severity, "occurred_at": occurred_at}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class SafetyService:
    """跨场地不良事件报告、分诊、调查、产品级暂停与双审解除。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None, sender: NotificationSender | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = SafetyRepository(self.connection)
        self.sender: NotificationSender = sender or _default_sender

    # ---- 上报 ----
    def create_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        product_code = payload["product_code"]
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            product = repository.product(product_code)
            if product is None:
                raise NotFoundError("关联的健康创新产品不存在")
            self._validate_occurred_at(payload["event_occurred_at"], now_value)
            session_id = payload["session_id"]
            site_code = payload["site_code"]
            if session_id is not None:
                session = repository.session(session_id)
                if session is None:
                    raise NotFoundError("关联的体验场次不存在")
                if session["product_code"] != product_code:
                    raise ConflictError("体验场次与上报产品不一致")
                if not site_code:
                    site_code = session["lease_owner"] or ""
            fingerprint = _fingerprint(product_code, session_id, site_code, payload["symptoms"], payload["severity"], payload["event_occurred_at"])

            target = self._find_merge_target(repository, payload, fingerprint)
            if target is not None:
                return self._merge_source(repository, int(target["id"]), payload, now)

            report_id = repository.create_report(
                product_code=product_code, session_id=session_id, site_code=site_code,
                symptoms=payload["symptoms"], severity=payload["severity"],
                event_occurred_at=payload["event_occurred_at"], description=payload["description"],
                fingerprint=fingerprint, created_by=payload["source"]["reporter"] or payload["source"]["channel"], now=now,
            )
            self._insert_source(repository, report_id, payload["source"], payload["event_occurred_at"], now)
            repository.add_timeline(
                report_id, "report", actor=payload["source"]["reporter"] or payload["source"]["channel"],
                summary=f"收到{payload['severity']}级不良事件上报",
                detail={"symptoms": payload["symptoms"], "severity": payload["severity"], "session_id": session_id, "site_code": site_code, "event_occurred_at": payload["event_occurred_at"]},
                now=now,
            )
            report = self._serialize_report(dict(repository.report_by_id(report_id)), repository)
            self._evaluate_rules(connection, repository, product_code, triggering_report_id=report_id, now_value=now_value)
            report = self._serialize_report(dict(repository.report_by_id(report_id)), repository)
            report["merged"] = False
            return report

    def add_supplement(self, report_no: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            report = repository.report_by_no(report_no)
            if report is None:
                raise NotFoundError("不良事件报告不存在")
            if report["status"] == "excluded":
                raise ConflictError("已排除关联的报告不再接收补充材料")
            source = {"channel": payload["channel"], "external_ref": payload.get("external_ref", ""), "reporter": payload["actor"]}
            repository.add_source(int(report["id"]), channel=source["channel"], external_ref=source["external_ref"], reporter=source["reporter"], received_at=now, now=now)
            repository.add_timeline(
                int(report["id"]), "source_merge", actor=payload["actor"],
                summary="补充材料已追加到原时间线",
                detail={"channel": payload["channel"], "content": payload["content"]},
                now=now,
            )
            return self.report_detail(connection, int(report["id"]))

    def _find_merge_target(self, repository: SafetyRepository, payload: dict[str, Any], fingerprint: str) -> sqlite3.Row | None:
        explicit = payload.get("duplicate_of_report_no")
        if explicit:
            target = repository.report_by_no(explicit)
            if target is None:
                raise NotFoundError("指定合并的原报告不存在")
            if target["status"] == "excluded":
                raise ConflictError("不能合并到已排除产品关联的报告")
            return target
        source = payload["source"]
        if source["external_ref"]:
            target = repository.report_by_external_ref(source["channel"], source["external_ref"])
            if target is not None:
                return target
        return repository.report_by_fingerprint(payload["product_code"], fingerprint)

    def _merge_source(self, repository: SafetyRepository, report_id: int, payload: dict[str, Any], now: str) -> dict[str, Any]:
        added = self._insert_source(repository, report_id, payload["source"], payload["event_occurred_at"], now)
        if added:
            repository.add_timeline(
                report_id, "source_merge", actor=payload["source"]["reporter"] or payload["source"]["channel"],
                summary="不同渠道的重复报告已合并来源，未重复计数",
                detail={"channel": payload["source"]["channel"], "external_ref": payload["source"]["external_ref"]},
                now=now,
            )
        report = self._serialize_report(dict(repository.report_by_id(report_id)), repository)
        report["merged"] = True
        return report

    @staticmethod
    def _insert_source(repository: SafetyRepository, report_id: int, source: dict[str, Any], received_at: str, now: str) -> bool:
        return repository.add_source(
            report_id, channel=source["channel"], external_ref=source["external_ref"],
            reporter=source["reporter"], received_at=received_at, now=now,
        )

    # ---- 医学分诊 ----
    def triage(self, report_no: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            report = repository.report_by_no(report_no)
            if report is None:
                raise NotFoundError("不良事件报告不存在")
            if report["status"] in {"excluded", "closed"}:
                raise ConflictError("报告已作出终局判断，不能再次分诊")
            actor, action, note = payload["actor"], payload["action"], payload["note"]
            report_id = int(report["id"])
            if action == "request_info":
                repository.set_report_status(report_id, "info_requested", now)
                repository.add_timeline(report_id, "triage", actor=actor, summary="医学分诊要求补充材料", detail={"action": action, "note": note}, now=now)
            elif action == "exclude":
                repository.set_report_status(report_id, "excluded", now)
                repository.add_timeline(report_id, "triage", actor=actor, summary="医学分诊排除产品关联", detail={"action": action, "note": note}, now=now)
            elif action == "investigate":
                investigation = repository.open_investigation_for_product(report["product_code"])
                if investigation is None:
                    investigation_id = repository.open_investigation(report["product_code"], opened_by=actor, now=now)
                else:
                    investigation_id = int(investigation["id"])
                repository.attach_report_signals(investigation_id, [report["report_no"]])
                repository.set_report_status(report_id, "investigating", now)
                repository.add_timeline(
                    report_id, "triage", actor=actor, summary="医学分诊升级为产品级调查",
                    detail={"action": action, "note": note, "investigation_no": f"INV-{investigation_id:06d}"}, now=now,
                )
            elif action == "suspend":
                if len(note.strip()) < 4:
                    raise ValidationError("触发产品级暂停必须说明依据")
                investigation = repository.open_investigation_for_product(report["product_code"])
                if investigation is None:
                    investigation_id = repository.open_investigation(report["product_code"], opened_by=actor, now=now)
                else:
                    investigation_id = int(investigation["id"])
                repository.attach_report_signals(investigation_id, [report["report_no"]])
                self._issue_suspension(
                    connection, repository, report["product_code"], reason=note,
                    trigger_rule="manual_triage", report_id=report_id, investigation_id=investigation_id,
                    issued_by=actor, now=now,
                )
            return self.report_detail(connection, report_id)

    def conclude_investigation(self, investigation_no: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            investigation = connection.execute("SELECT * FROM safety_investigations WHERE investigation_no=?", (investigation_no,)).fetchone()
            if investigation is None:
                raise NotFoundError("调查不存在")
            if investigation["status"] != "open":
                raise ConflictError("调查已经结论，结论不得改写")
            repository.conclude_investigation(int(investigation["id"]), actor=payload["actor"], conclusion=payload["conclusion"], now=now)
            linked = json.loads(investigation["related_signal_json"])
            for report_no in linked:
                row = repository.report_by_no(report_no)
                if row is not None and row["status"] not in {"excluded"}:
                    repository.set_report_status(int(row["id"]), "closed", now)
                    repository.add_timeline(
                        int(row["id"]), "closure", actor=payload["actor"],
                        summary=f"调查 {investigation_no} 已结论，报告关闭",
                        detail={"investigation_no": investigation_no}, now=now,
                    )
            result = dict(repository.investigation_by_id(int(investigation["id"])))
            result["related_signal"] = json.loads(result.pop("related_signal_json"))
            return result

    # ---- 规则评估 ----
    def _evaluate_rules(self, connection: sqlite3.Connection, repository: SafetyRepository, product_code: str, *, triggering_report_id: int, now_value) -> None:
        if repository.active_suspension(product_code) is not None:
            return
        now_storage = to_storage(now_value)
        for rule in repository.active_rules():
            severities = json.loads(rule["severity_in_json"])
            since = to_storage(now_value - timedelta(seconds=int(rule["window_seconds"])))
            rows = repository.reports_for_rule(product_code, severities, since)
            if not rows:
                continue
            matched: list[sqlite3.Row] = []
            if int(rule["same_symptom"]) == 1:
                buckets: dict[str, list[sqlite3.Row]] = {}
                for row in rows:
                    for symptom in json.loads(row["symptoms_json"]):
                        buckets.setdefault(symptom, []).append(row)
                group = max(buckets.values(), key=len, default=[])
                if len(group) >= int(rule["min_count"]):
                    matched = list(dict.fromkeys(group))
            elif len(rows) >= int(rule["min_count"]):
                matched = list(rows)
            if matched:
                self._trigger_rule_suspension(connection, repository, rule, matched, triggering_report_id, product_code, now_storage)
                return

    def _trigger_rule_suspension(self, connection: sqlite3.Connection, repository: SafetyRepository, rule: sqlite3.Row, matched: list[sqlite3.Row], triggering_report_id: int, product_code: str, now: str) -> None:
        investigation = repository.open_investigation_for_product(product_code)
        if investigation is None:
            investigation_id = repository.open_investigation(product_code, opened_by="safety-rule-engine", now=now)
        else:
            investigation_id = int(investigation["id"])
        report_nos = [row["report_no"] for row in matched]
        repository.attach_report_signals(investigation_id, report_nos)
        reason = f"规则 {rule['code']} 命中：{rule['name']}"
        self._issue_suspension(
            connection, repository, product_code, reason=reason, trigger_rule=rule["code"],
            report_id=triggering_report_id, investigation_id=investigation_id, issued_by="safety-rule-engine", now=now,
            linked_report_ids=[int(row["id"]) for row in matched],
        )

    # ---- 暂停决定 ----
    def _issue_suspension(self, connection: sqlite3.Connection, repository: SafetyRepository, product_code: str, *, reason: str, trigger_rule: str, report_id: int | None, investigation_id: int | None, issued_by: str, now: str, linked_report_ids: list[int] | None = None) -> dict[str, Any]:
        if repository.active_suspension(product_code) is not None:
            raise ConflictError("该产品已经处于暂停状态")
        try:
            decision_id = repository.create_decision(
                product_code=product_code, action="product_suspension", reason=reason, trigger_rule=trigger_rule,
                report_id=report_id, investigation_id=investigation_id, resumes_decision_id=None, issued_by=issued_by, now=now,
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该产品已经存在生效中的暂停决定") from exc
        decision = repository.decision_by_no(f"SUS-{decision_id:06d}")
        sessions = repository.unfinished_sessions(product_code)
        effects = repository.apply_suspension_effect(sessions, decision["decision_no"], now)
        repository.record_decision_sessions(decision_id, effects, now)
        recipients = self._collect_recipients(repository, product_code, sessions)
        payload = {
            "decision_no": decision["decision_no"], "product_code": product_code, "action": "product_suspension",
            "reason": reason, "issued_at": now, "affected_session_ids": [item["session_id"] for item in effects],
        }
        for recipient in recipients:
            repository.enqueue_notification(decision_id, recipient=recipient, channel="site", payload=payload, now=now)
        target_ids = list(dict.fromkeys([*(linked_report_ids or []), *([report_id] if report_id is not None else [])]))
        for target_id in target_ids:
            repository.set_report_status(target_id, "investigating", now)
            repository.add_timeline(
                target_id, "decision", actor=issued_by,
                summary=f"产品级暂停决定 {decision['decision_no']} 已签发",
                detail={"decision_no": decision["decision_no"], "reason": reason, "trigger_rule": trigger_rule, "effects": effects, "recipients": sorted(recipients)},
                now=now,
            )
            repository.add_timeline(
                target_id, "decision_effect", actor=issued_by,
                summary="同一份决定已记录到所有未完成场次",
                detail={"effects": effects}, now=now,
            )
        return dict(decision)

    @staticmethod
    def _collect_recipients(repository: SafetyRepository, product_code: str, sessions: Iterable[sqlite3.Row]) -> set[str]:
        recipients = {row["site_code"] for row in repository.list_reports(product_code=product_code, limit=500) if row["site_code"]}
        for session in sessions:
            if session["status"] == "running" and session["lease_owner"]:
                recipients.add(session["lease_owner"])
            recipients.add(f"submitter:{session['requested_by']}")
        return recipients

    # ---- 双审阅人解除 ----
    def approve_lift(self, decision_no: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            decision = repository.decision_by_no(decision_no)
            if decision is None or decision["action"] != "product_suspension":
                raise NotFoundError("产品暂停决定不存在")
            if decision["status"] != "active":
                raise ConflictError("暂停决定已经解除")
            decision_id = int(decision["id"])
            if not repository.add_approval(decision_id, reviewer=payload["reviewer"], note=payload["note"], now=now):
                raise ConflictError("同一审阅人不能重复签发解除意见")
            approvals = repository.approvals(decision_id)
            if len(approvals) < 2:
                return {"decision_no": decision_no, "status": "active", "approvals": approvals, "lifted": False}
            # 两名不同审阅人齐备：解除暂停，但不改动任何调查结论。
            return self._lift_suspension(connection, repository, decision, approvals, payload["note"], now)

    def _lift_suspension(self, connection: sqlite3.Connection, repository: SafetyRepository, decision: sqlite3.Row, approvals: list[dict[str, Any]], lift_reason: str, now: str) -> dict[str, Any]:
        decision_id = int(decision["id"])
        product_code = decision["product_code"]
        lift_summary = "两名不同审阅人批准：" + "；".join(f"{item['reviewer']}" for item in approvals)
        repository.lift_decision(decision_id, lift_reason=lift_reason, lift_summary=lift_summary, now=now)
        resumption_id = repository.create_decision(
            product_code=product_code, action="resumption", reason=lift_reason, trigger_rule="dual_review_lift",
            report_id=decision["report_id"], investigation_id=decision["investigation_id"],
            resumes_decision_id=decision_id, issued_by=approvals[-1]["reviewer"], now=now,
        )
        resumption = repository.decision_by_no(f"RES-{resumption_id:06d}")
        released = repository.release_sessions(decision_id, resumption_id, now)
        recipients = repository.notification_recipients(decision_id)
        recipients.update(item["reviewer"] for item in approvals)
        payload = {
            "decision_no": resumption["decision_no"], "product_code": product_code, "action": "resumption",
            "resumes": decision["decision_no"], "reason": lift_reason, "issued_at": now,
            "released_session_ids": [item["session_id"] for item in released],
        }
        for recipient in recipients:
            repository.enqueue_notification(resumption_id, recipient=recipient, channel="site", payload=payload, now=now)
        if decision["report_id"] is not None:
            repository.add_timeline(
                int(decision["report_id"]), "decision", actor=approvals[-1]["reviewer"],
                summary=f"暂停决定 {decision['decision_no']} 经双审解除，恢复决定 {resumption['decision_no']}",
                detail={"approvals": approvals, "lift_reason": lift_reason, "released": released}, now=now,
            )
        return {"decision_no": decision["decision_no"], "status": "lifted", "approvals": approvals, "lifted": True,
                "resumption_decision_no": resumption["decision_no"], "released": released}

    # ---- 通知外箱（持久状态，崩溃可续） ----
    def deliver_pending(self, *, limit: int = 100) -> dict[str, Any]:
        delivered: list[int] = []
        terminal_failed: list[int] = []
        retried: list[int] = []
        for row in self.repository.pending_notifications(limit=limit):
            notification_id = int(row["id"])
            try:
                with transaction(immediate=True) as connection:
                    self.sender(json.loads(row["payload_json"]))
                    SafetyRepository(connection).mark_notification_delivered(notification_id, to_storage(self.clock.now()))
                delivered.append(notification_id)
            except Exception as exc:  # noqa: BLE001 - 发送失败必须落库，恢复后继续投递
                with transaction(immediate=True) as connection:
                    repository = SafetyRepository(connection)
                    attempts = int(repository.connection.execute("SELECT attempts FROM safety_notifications WHERE id=?", (notification_id,)).fetchone()[0]) + 1
                    terminal = attempts >= MAX_NOTIFICATION_ATTEMPTS
                    repository.mark_notification_failed(notification_id, str(exc), terminal=terminal, now=to_storage(self.clock.now()))
                (terminal_failed if terminal else retried).append(notification_id)
        return {"delivered": delivered, "retry_pending": retried, "terminal_failed": terminal_failed}

    def requeue_failed_notifications(self) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE safety_notifications SET status='pending',last_error='',updated_at=? WHERE status='failed'",
                (now,),
            )
            return {"requeued": cursor.rowcount}

    def acknowledge(self, decision_no: str, payload: dict[str, Any]) -> dict[str, Any]:
        """迟到的回执只能追加到原时间线，不改写任何既有记录。"""
        now = to_storage(self.clock.now())
        occurred_at = payload.get("occurred_at") or now
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            decision = repository.decision_by_no(decision_no)
            if decision is None:
                raise NotFoundError("决定不存在")
            notification = repository.notification_for(int(decision["id"]), payload["recipient"], payload["channel"])
            if notification is None:
                raise NotFoundError("该对象没有对应的通知记录")
            if notification["status"] == "acknowledged":
                raise ConflictError("该通知已经回执，不能重复登记")
            if notification["status"] == "failed":
                raise ConflictError("该通知此前投递失败，请先重新排队再等待回执")
            changed = repository.acknowledge_notification(int(notification["id"]), now)
            if changed != 1:
                raise ConflictError("回执登记失败")
            report_ids = self._decision_report_ids(repository, decision)
            for report_id in report_ids:
                repository.add_timeline(
                    report_id, "acknowledgement", actor=payload["actor"] or payload["recipient"],
                    summary=f"{payload['recipient']} 对决定 {decision_no} 的回执（迟到可追溯）",
                    detail={"recipient": payload["recipient"], "channel": payload["channel"], "note": payload["note"], "acknowledged_at": now, "occurred_at": occurred_at},
                    now=now,
                )
            return {"decision_no": decision_no, "recipient": payload["recipient"], "acknowledged": True}

    @staticmethod
    def _decision_report_ids(repository: SafetyRepository, decision: sqlite3.Row) -> list[int]:
        ids: list[int] = []
        if decision["report_id"] is not None:
            ids.append(int(decision["report_id"]))
        if decision["investigation_id"] is not None:
            investigation = repository.investigation_by_id(int(decision["investigation_id"]))
            if investigation is not None:
                for report_no in json.loads(investigation["related_signal_json"]):
                    row = repository.report_by_no(report_no)
                    if row is not None and int(row["id"]) not in ids:
                        ids.append(int(row["id"]))
        return ids

    # ---- 查询 ----
    def list_reports(self, *, product_code: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = self.repository.list_reports(product_code=product_code, status=status, limit=max(1, min(limit, 500)))
        for item in items:
            item["symptoms"] = json.loads(item["symptoms_json"])
        return items

    def report_detail(self, connection: sqlite3.Connection | None = None, report_id: int | None = None, *, report_no: str | None = None) -> dict[str, Any]:
        repository = SafetyRepository(connection) if connection is not None else self.repository
        row = repository.report_by_id(report_id) if report_id is not None else repository.report_by_no(report_no or "")
        if row is None:
            raise NotFoundError("不良事件报告不存在")
        return self._serialize_report(dict(row), repository)

    @staticmethod
    def _serialize_report(result: dict[str, Any], repository: SafetyRepository) -> dict[str, Any]:
        result["symptoms"] = json.loads(result.pop("symptoms_json"))
        result["sources"] = repository.sources(int(result["id"]))
        timeline = repository.timeline(int(result["id"]))
        for item in timeline:
            item["detail"] = json.loads(item["detail_json"])
        result["timeline"] = timeline
        return result

    def product_safety_overview(self, product_code: str) -> dict[str, Any]:
        product = self.repository.product(product_code)
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        reports = self.repository.list_reports(product_code=product_code, limit=500)
        reports = [self._serialize_report(item, self.repository) for item in reports]
        investigations = self.repository.investigations_for_product(product_code)
        for item in investigations:
            item["related_signal"] = json.loads(item.pop("related_signal_json"))
        decisions = self.repository.decisions_for_product(product_code)
        for decision in decisions:
            decision_id = int(decision["id"])
            decision["approvals"] = self.repository.approvals(decision_id)
            decision["notifications"] = self.repository.notifications_for_decision(decision_id)
            decision["affected_sessions"] = [dict(row) for row in self.connection.execute(
                "SELECT session_id,previous_status,effect FROM safety_decision_sessions WHERE decision_id=? ORDER BY session_id",
                (decision_id,),
            ).fetchall()]
        active = self.repository.active_suspension(product_code)
        return {
            "product_code": product_code,
            "first_signal_at": self.repository.first_signal_at(product_code),
            "active_suspension": dict(active) if active else None,
            "reports": sorted(reports, key=lambda item: item["id"]),
            "investigations": investigations,
            "decisions": decisions,
        }

    def list_rules(self) -> list[dict[str, Any]]:
        rules = [dict(row) for row in self.connection.execute(
            "SELECT * FROM safety_signal_rules ORDER BY active DESC,id"
        ).fetchall()]
        for item in rules:
            item["severity_in"] = json.loads(item.pop("severity_in_json"))
        return rules

    def update_rule(self, code: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        changes = {key: value for key, value in payload.items() if value is not None}
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            rule = repository.rule_by_code(code)
            if rule is None:
                raise NotFoundError("信号规则不存在")
            if not changes:
                raise ValidationError("没有需要更新的规则字段")
            if "active" in changes:
                changes["active"] = 1 if changes["active"] else 0
            repository.update_rule(int(rule["id"]), changes, now)
            return dict(repository.rule_by_code(code))

    @staticmethod
    def _validate_occurred_at(value: str, now_value) -> None:
        try:
            parsed = from_storage(value)
        except (ValueError, TypeError) as exc:
            raise ValidationError("事件发生时间格式不正确") from exc
        if parsed is None or parsed > now_value + timedelta(minutes=1):
            raise ValidationError("事件发生时间不能晚于当前时间")
        if parsed < now_value - timedelta(days=3650):
            raise ValidationError("事件发生时间超出允许范围")


def _default_sender(payload: dict[str, Any]) -> None:
    """占位投递：与外部通知渠道对接前，通知进入外箱即视为可追踪送达。"""
    return None
