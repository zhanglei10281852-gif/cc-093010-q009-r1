from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.safety.repository import SafetyRepository


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


# 运输层默认把决定写入场地收件箱；测试可注入会失败的运输层验证中断恢复。
NoticeTransport = Callable[[dict[str, Any]], None]


def default_transport(notice: dict[str, Any]) -> None:
    """默认通知运输：决定已持久化，交付即视为送达场地收件箱。"""
    if not notice.get("decision_uid"):
        raise RuntimeError("通知缺少可追踪决定标识")


class SafetyService:
    """跨场地不良事件分诊、产品级暂停和场次通知处置链。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None, transport: NoticeTransport | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.transport = transport or default_transport

    # -- 触发规则 -----------------------------------------------------------
    def get_rule(self) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            repository.seed_default_rule(to_storage(self.clock.now()))
            return self._rule_view(repository.active_rule())

    def configure_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            repository.seed_default_rule(now)
            return self._rule_view(repository.update_rule(
                severe_severities=list(payload["severe_severities"]),
                cluster_signal_count=payload["cluster_signal_count"],
                cluster_window_hours=payload["cluster_window_hours"],
                actor=payload["actor"], now=now,
            ))

    @staticmethod
    def _rule_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "code": row["code"],
            "severe_severities": json.loads(row["severe_severities_json"]),
            "cluster_signal_count": row["cluster_signal_count"],
            "cluster_window_hours": row["cluster_window_hours"],
            "active": bool(row["active"]),
        }

    # -- 报告上报与跨渠道合并 -----------------------------------------------
    def submit_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        occurrence = self._parse_occurrence(payload["occurrence_at"])
        occurrence_at = to_storage(occurrence)
        symptoms = payload["symptoms"]
        symptoms_digest = digest(sorted(symptoms))
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            product = repository.product_by_code(payload["product_code"].strip().lower())
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            site_code = (payload.get("site_code") or "").strip().lower()
            site_id: int | None = None
            if site_code:
                site = repository.site_by_code(site_code)
                if site is None:
                    raise NotFoundError("试点场地不存在")
                site_id, site_code = int(site["id"]), site["code"]
            session_id = payload.get("session_id")
            if session_id is not None:
                session = repository.session_by_id(session_id)
                if session is None:
                    raise NotFoundError("关联体验场次不存在")
                session_product = session["product_code"] or session["protocol_product_code"]
                if session_product and product["code"] != session_product:
                    raise ConflictError("体验场次与上报产品不一致")
                site_code = site_code or session["lease_owner"] or ""
            signal_key = (payload.get("signal_key") or "").strip() or symptoms_digest
            fingerprint = digest([
                product["code"], site_code, session_id, occurrence_at[:16], symptoms_digest,
            ])
            channel = payload["channel"].strip()
            reporter_ref = (payload.get("reporter_ref") or "").strip()

            existing_key = repository.report_by_key(payload["report_key"])
            if existing_key is not None:
                return {"report_id": existing_key["id"], "merged": False, "idempotent": True, "suspension": None}

            duplicate = repository.report_by_fingerprint(int(product["id"]), fingerprint)
            if duplicate is not None:
                return self._merge_duplicate(
                    repository, duplicate, channel=channel, reporter_ref=reporter_ref,
                    severity=payload["severity"], site_code=site_code, session_id=session_id,
                    description=payload["description"], now=now,
                )

            report_id = repository.create_report(
                report_key=payload["report_key"], product_id=int(product["id"]), product_code=product["code"],
                site_id=site_id, site_code=site_code, session_id=session_id, severity=payload["severity"],
                symptoms=symptoms, symptoms_digest=symptoms_digest, signal_key=signal_key,
                dedup_fingerprint=fingerprint, occurrence_at=occurrence_at,
                description=payload["description"], channel=channel, reporter_ref=reporter_ref, now=now,
            )
            repository.add_timeline_event(
                report_id, "report", channel,
                f"首个信号：{payload['severity']} 级不良事件上报",
                {"channel": channel, "reporter_ref": reporter_ref, "symptoms": symptoms,
                 "site_code": site_code, "session_id": session_id, "occurrence_at": occurrence_at},
                now,
            )
            suspension = self._evaluate_auto_suspension(
                repository, product=product, report_id=report_id, signal_key=signal_key,
                severity=payload["severity"], occurrence=occurrence, actor=channel, now=now,
            )
            return {"report_id": report_id, "merged": False, "idempotent": False, "suspension": suspension}

    def _merge_duplicate(self, repository: SafetyRepository, duplicate: sqlite3.Row, *, channel: str, reporter_ref: str, severity: str, site_code: str, session_id: int | None, description: str, now: str) -> dict[str, Any]:
        # 跨渠道重复：合并来源而不是重复计数；同一渠道同一报告人视为纯重放。
        already = repository.source_exists(int(duplicate["id"]), channel, reporter_ref)
        repository.add_source(int(duplicate["id"]), channel=channel, reporter_ref=reporter_ref, received_at=now, detail="跨渠道重复上报", now=now)
        suspension = None
        if not already:
            repository.merge_source(int(duplicate["id"]), severity=severity, site_code=site_code, session_id=session_id, description=description, now=now)
            repository.add_timeline_event(
                int(duplicate["id"]), "report", channel,
                f"合并来自 {channel} 渠道的重复上报，不重复计数",
                {"reporter_ref": reporter_ref, "severity": severity, "site_code": site_code, "session_id": session_id},
                now,
            )
            merged = repository.report_by_id(int(duplicate["id"]))
            occurrence = from_storage(merged["occurrence_at"])
            suspension = self._evaluate_auto_suspension(
                repository, product=repository.product_by_id(merged["product_id"]),
                report_id=int(merged["id"]), signal_key=merged["signal_key"],
                severity=merged["severity"], occurrence=occurrence, actor=channel, now=now,
            )
        return {"report_id": int(duplicate["id"]), "merged": True, "idempotent": already, "suspension": suspension}

    def add_supplement(self, report_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            report = repository.report_by_id(report_id)
            if report is None:
                raise NotFoundError("安全报告不存在")
            event_type = "late_receipt" if payload["kind"] == "late_receipt" else "supplement"
            detail = dict(payload["detail"])
            if payload.get("occurred_at"):
                detail["occurred_at"] = payload["occurred_at"]
            sequence = repository.add_timeline_event(
                report_id, event_type, payload["actor"], payload["summary"], detail, now,
            )
            return {"report_id": report_id, "sequence": sequence, "event_type": event_type}

    # -- 分诊决定 -----------------------------------------------------------
    def triage(self, report_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            report = repository.report_by_id(report_id)
            if report is None:
                raise NotFoundError("安全报告不存在")
            decision = payload["decision"]
            status_map = {
                "request_information": "information_requested",
                "exclude_relation": "related_excluded",
                "escalate_investigation": "investigating",
                "trigger_suspension": "escalated",
            }
            if decision == "exclude_relation" and not payload.get("related_finding", "").strip():
                raise ValidationError("排除关联必须给出医学依据")
            if report["status"] == "escalated" and decision == "exclude_relation":
                raise ConflictError("报告已触发产品级暂停，翻案必须通过调查结论与双人解除流程")
            decision_id = repository.add_decision(
                report_id, decision=decision, reason=payload["reason"],
                reviewer=payload["reviewer"], related_finding=payload.get("related_finding", ""), now=now,
            )
            repository.add_timeline_event(
                report_id, "triage", payload["reviewer"],
                f"分诊决定：{self._decision_label(decision)}",
                {"decision_id": decision_id, "decision": decision, "reason": payload["reason"],
                 "related_finding": payload.get("related_finding", "")},
                now,
            )
            # 已升级为产品暂停的报告，后续判断只追加留痕，状态不倒退。
            escalated_before = report["status"] == "escalated"
            if not escalated_before:
                repository.set_report_status(report_id, status_map[decision], now)
            suspension = None
            if decision == "trigger_suspension":
                product = repository.product_by_code(report["product_code"])
                suspension = self._suspend(
                    repository, product=product, reason=payload["reason"], actor=payload["reviewer"],
                    triggered_report_ids=[report_id], rule={"trigger": "medical_triage"},
                    investigation_summary="", now=now,
                )
            resulting_status = "escalated" if escalated_before or decision == "trigger_suspension" else status_map[decision]
            return {"report_id": report_id, "decision_id": decision_id, "status": resulting_status, "suspension": suspension}

    @staticmethod
    def _decision_label(decision: str) -> str:
        return {
            "request_information": "要求补充信息",
            "exclude_relation": "排除产品关联",
            "escalate_investigation": "升级调查",
            "trigger_suspension": "触发产品级暂停",
        }[decision]

    def finalize_investigation(self, action_id: int, reviewer: str, summary: str) -> dict[str, Any]:
        summary = summary.strip()
        if len(summary) < 4:
            raise ValidationError("调查结论需要完整记录后才能写定")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            action = repository.action_by_id(action_id)
            if action is None or action["action_type"] != "suspension":
                raise NotFoundError("产品暂停决定不存在")
            if action["investigation_summary"]:
                raise ConflictError("调查结论已经写定，不得改写")
            repository.set_investigation_summary(action_id, summary)
            for report_id in json.loads(action["triggered_report_ids_json"]):
                repository.add_timeline_event(
                    report_id, "investigation", reviewer, "产品暂停调查结论已写定",
                    {"action_id": action_id, "summary": summary}, now,
                )
            return dict(repository.action_by_id(action_id))

    # -- 规则引擎 -----------------------------------------------------------
    def _evaluate_auto_suspension(self, repository: SafetyRepository, *, product: sqlite3.Row, report_id: int, signal_key: str, severity: str, occurrence: datetime, actor: str, now: str) -> dict[str, Any] | None:
        repository.seed_default_rule(now)
        rule_row = repository.active_rule()
        rule = self._rule_view(rule_row)
        if repository.active_suspension(int(product["id"])) is not None:
            return None
        severe_hit = severity in rule["severe_severities"]
        window_start = to_storage(occurrence - timedelta(hours=rule["cluster_window_hours"]))
        cluster = repository.count_cluster(
            product_id=int(product["id"]), signal_key=signal_key,
            since=window_start, until=to_storage(occurrence),
        )
        cluster_hit = cluster >= rule["cluster_signal_count"]
        if not (severe_hit or cluster_hit):
            return None
        matched: list[str] = []
        if severe_hit:
            matched.append("severe_event")
        if cluster_hit:
            matched.append(f"cluster:{cluster}")
        trigger_ids = self._cluster_report_ids(repository, product_id=int(product["id"]), signal_key=signal_key, since=window_start, until=to_storage(occurrence), include_severe=report_id)
        reason = f"触发规则自动暂停：{'、'.join(matched)}"
        return self._suspend(
            repository, product=product, reason=reason, actor=f"rule-engine:{actor}",
            triggered_report_ids=trigger_ids, rule={"trigger": matched, **rule},
            investigation_summary="", now=now,
        )

    @staticmethod
    def _cluster_report_ids(repository: SafetyRepository, *, product_id: int, signal_key: str, since: str, until: str, include_severe: int) -> list[int]:
        rows = repository.connection.execute(
            "SELECT id FROM safety_reports WHERE product_id=? AND signal_key=? AND status<>'related_excluded' AND occurrence_at>=? AND occurrence_at<=? ORDER BY id",
            (product_id, signal_key, since, until),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if include_severe not in ids:
            ids.append(include_severe)
        return ids

    # -- 产品级暂停 ---------------------------------------------------------
    def _suspend(self, repository: SafetyRepository, *, product: sqlite3.Row, reason: str, actor: str, triggered_report_ids: list[int], rule: dict[str, Any], investigation_summary: str, now: str) -> dict[str, Any]:
        existing = repository.active_suspension(int(product["id"]))
        if existing is not None:
            return self._action_view(repository, existing)
        sessions = repository.unfinished_sessions(product["code"])
        scope_ids = [int(row["id"]) for row in sessions]
        decision_uid = "SAF-" + uuid.uuid4().hex[:16].upper()
        action_id = repository.create_action(
            product_id=int(product["id"]), product_code=product["code"], action_type="suspension",
            related_action_id=None, reason=reason, rule=rule, triggered_by=actor,
            triggered_report_ids=triggered_report_ids, investigation_summary=investigation_summary,
            scope_session_ids=scope_ids, product_active_before=bool(product["active"]),
            decision_uid=decision_uid, status="active", now=now,
        )
        repository.set_product_active(int(product["id"]), False, now)
        for row in sessions:
            repository.create_notice(
                action_id=action_id, decision_uid=decision_uid, product_code=product["code"],
                session_id=int(row["id"]), site_code=row["lease_owner"] or "",
                recipient=row["lease_owner"] or row["requested_by"], kind="suspension", now=now,
            )
        for report_id in triggered_report_ids:
            repository.set_report_status(report_id, "escalated", now)
            repository.add_timeline_event(
                report_id, "triage", actor, f"产品级暂停决定 {decision_uid} 已生成",
                {"action_id": action_id, "decision_uid": decision_uid, "scope_session_ids": scope_ids, "rule": rule},
                now,
            )
        return self._action_view(repository, repository.action_by_id(action_id))

    # -- 双人解除 -----------------------------------------------------------
    def lift_signature(self, action_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            action = repository.action_by_id(action_id)
            if action is None or action["action_type"] != "suspension":
                raise NotFoundError("产品暂停决定不存在")
            if action["status"] != "active":
                raise ConflictError("暂停决定已经解除")
            if not action["investigation_summary"]:
                raise ConflictError("调查结论尚未写定，不能开始解除会签")
            reviewer = payload["reviewer"].strip()
            if not action["lift_reviewer1"]:
                reason = payload.get("reason", "").strip()
                if len(reason) < 4:
                    raise ValidationError("第一名审阅人必须登记解除依据")
                repository.add_lift_signature(action_id, "lift_reviewer1", "lift_reviewer1_at", reviewer, now)
                # 解除依据随第一签写定，第二签及之后都无法改写。
                connection.execute("UPDATE product_safety_actions SET lift_reason=? WHERE id=? AND lift_reason=''", (reason, action_id))
                for report_id in json.loads(action["triggered_report_ids_json"]):
                    repository.add_timeline_event(
                        report_id, "closure_note", reviewer,
                        "第一名审阅人签署暂停解除", {"action_id": action_id, "reason": reason}, now,
                    )
                return self._action_view(repository, repository.action_by_id(action_id))
            if action["lift_reviewer1"] == reviewer:
                raise ConflictError("两名解除审阅人必须不同")
            if action["lift_reviewer2"]:
                raise ConflictError("暂停解除已完成双人会签")
            repository.add_lift_signature(action_id, "lift_reviewer2", "lift_reviewer2_at", reviewer, now)
            result = self._complete_lift(repository, action, reviewer, now)
            return result

    def _complete_lift(self, repository: SafetyRepository, suspension: sqlite3.Row, reviewer: str, now: str) -> dict[str, Any]:
        if not suspension["investigation_summary"]:
            raise ConflictError("调查结论尚未写定，不能解除暂停")
        action_id = int(suspension["id"])
        repository.complete_lift(action_id, lift_reason=suspension["lift_reason"], now=now)
        repository.set_product_active(int(suspension["product_id"]), bool(suspension["product_active_before"]), now)
        sessions = repository.unfinished_sessions(suspension["product_code"])
        scope_ids = [int(row["id"]) for row in sessions]
        resume_uid = "SAF-" + uuid.uuid4().hex[:16].upper()
        resumption_id = repository.create_action(
            product_id=int(suspension["product_id"]), product_code=suspension["product_code"],
            action_type="resumption", related_action_id=action_id,
            reason=suspension["lift_reason"], rule={}, triggered_by=reviewer,
            triggered_report_ids=json.loads(suspension["triggered_report_ids_json"]),
            investigation_summary=suspension["investigation_summary"],
            scope_session_ids=scope_ids, decision_uid=resume_uid, status="lifted", now=now,
            product_active_before=bool(suspension["product_active_before"]),
        )
        for row in sessions:
            repository.create_notice(
                action_id=resumption_id, decision_uid=resume_uid, product_code=suspension["product_code"],
                session_id=int(row["id"]), site_code=row["lease_owner"] or "",
                recipient=row["lease_owner"] or row["requested_by"], kind="resumption", now=now,
            )
        # 解除后，原暂停决定下尚未投递的通知不再滞后投出，统一由解除决定取代并留痕。
        repository.supersede_pending_for_action(action_id, resumption_id, now)
        for report_id in json.loads(suspension["triggered_report_ids_json"]):
            repository.add_timeline_event(
                report_id, "closure_note", reviewer,
                f"第二名审阅人会签，暂停解除决定 {resume_uid} 已生成",
                {"suspension_action_id": action_id, "resumption_action_id": resumption_id,
                 "lift_reason": suspension["lift_reason"],
                 "reviewers": [suspension["lift_reviewer1"], reviewer]},
                now,
            )
        return self._action_view(repository, repository.action_by_id(action_id))

    # -- 通知 outbox：崩溃恢复后从持久状态继续 -------------------------------
    def deliver_pending_notices(self, *, limit: int = 100) -> dict[str, Any]:
        bound = max(1, limit)
        with transaction(immediate=True) as connection:
            pending = [dict(row) for row in SafetyRepository(connection).pending_notices(bound)]
        delivered: list[int] = []
        failed: list[dict[str, Any]] = []
        for notice in pending:
            notice_id = int(notice["id"])
            now = to_storage(self.clock.now())
            try:
                self.transport(notice)
            except Exception as exc:  # 运输失败不阻塞其他决定，状态保持 pending 供下轮恢复重试。
                with transaction(immediate=True) as connection:
                    SafetyRepository(connection).mark_delivery_failed(notice_id, str(exc), now)
                failed.append({"notice_id": notice_id, "error": str(exc)[:200]})
                continue
            with transaction(immediate=True) as connection:
                if SafetyRepository(connection).mark_delivered(notice_id, now):
                    delivered.append(notice_id)
        return {"delivered": delivered, "failed": failed, "remaining": self._pending_count()}

    def _pending_count(self) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM safety_session_notices WHERE delivery_status='pending'"
        ).fetchone()[0])

    def acknowledge_notice(self, notice_id: int, site_code: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            notice = repository.notice_by_id(notice_id)
            if notice is None:
                raise NotFoundError("安全通知不存在")
            if notice["site_code"] and notice["site_code"] != site_code:
                raise ConflictError("只有通知指向的场地可以回执")
            if notice["delivery_status"] == "pending":
                raise ConflictError("通知尚未送达，不能回执")
            if not repository.acknowledge_notice(notice_id, now):
                raise ConflictError("通知状态不允许回执")
            return dict(repository.notice_by_id(notice_id))

    # -- 查询 ---------------------------------------------------------------
    def list_reports(self, *, product_code: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return SafetyRepository(self.connection).list_reports(
            product_code=product_code, status=status, limit=max(1, min(limit, 500)),
        )

    def report_detail(self, report_id: int) -> dict[str, Any]:
        detail = SafetyRepository(self.connection).report_detail(report_id)
        if detail is None:
            raise NotFoundError("安全报告不存在")
        return detail

    def action_detail(self, action_id: int) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            action = repository.action_by_id(action_id)
            if action is None:
                raise NotFoundError("产品安全决定不存在")
            view = self._action_view(repository, action)
            view["notices"] = repository.notices_for_action(action_id)
            return view

    def list_actions(self, product_code: str | None = None) -> list[dict[str, Any]]:
        return [self._action_view_from_dict(row) for row in SafetyRepository(self.connection).list_actions(product_code)]

    def list_notices(self, **filters: Any) -> list[dict[str, Any]]:
        filters.setdefault("limit", 100)
        return SafetyRepository(self.connection).list_notices(**filters)

    def product_dossier(self, product_code: str) -> dict[str, Any]:
        """产品重新开放前的完整处置链：首个信号、每次判断、通知对象、影响范围、解除依据。"""
        with transaction(immediate=True) as connection:
            repository = SafetyRepository(connection)
            product = repository.product_by_code(product_code.strip().lower())
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            reports = repository.list_reports(product_code=product["code"], status=None, limit=500)
            report_views = []
            for row in reports:
                detail = repository.report_detail(int(row["id"]))
                report_views.append(detail)
            actions = []
            for row in repository.list_actions(product["code"]):
                view = self._action_view_from_dict(row)
                view["notices"] = repository.notices_for_action(int(row["id"]))
                actions.append(view)
            recipients = sorted({
                notice["recipient"]
                for action in actions for notice in action["notices"] if notice["recipient"]
            })
            active_suspension = next((action for action in actions if action["action_type"] == "suspension" and action["status"] == "active"), None)
            return {
                "product_code": product["code"],
                "product_name": product["name"],
                "product_active": bool(product["active"]),
                "open_for_new_sessions": product["active"] == 1 and active_suspension is None,
                "first_signal_at": report_views[0]["occurrence_at"] if report_views else None,
                "report_count": len([r for r in report_views if r["status"] != "related_excluded"]),
                "reports": report_views,
                "actions": actions,
                "notice_recipients": recipients,
                "active_suspension": active_suspension,
            }

    @staticmethod
    def _action_view(repository: SafetyRepository, row: sqlite3.Row) -> dict[str, Any]:
        return SafetyService._action_view_from_dict(dict(row))

    @staticmethod
    def _action_view_from_dict(row: dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        for key in ("rule_json", "triggered_report_ids_json", "scope_session_ids_json"):
            view[key.replace("_json", "")] = json.loads(view.pop(key))
        return view

    @staticmethod
    def _parse_occurrence(value: str) -> datetime:
        try:
            parsed = from_storage(value)
        except (ValueError, TypeError) as exc:
            raise ValidationError("事件发生时间需要是 ISO 8601 格式") from exc
        if parsed is None:
            raise ValidationError("事件发生时间不能为空")
        return parsed
