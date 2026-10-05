from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

SEVERE_LEVELS = ("mild", "moderate", "severe", "life_threatening", "death")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SafetyRepository:
    """跨场地不良事件、分诊决定、产品暂停和场次通知的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # -- 目录与场次引用 -----------------------------------------------------
    def product_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM health_products WHERE code=?", (code,)).fetchone()

    def product_by_id(self, product_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM health_products WHERE id=?", (product_id,)).fetchone()

    def site_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM pilot_sites WHERE code=?", (code,)).fetchone()

    def session_by_id(self, session_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT t.*,tpl.product_code AS protocol_product_code FROM pilot_sessions t JOIN pilot_protocols tpl ON tpl.id=t.protocol_id WHERE t.id=?",
            (session_id,),
        ).fetchone()

    def set_product_active(self, product_id: int, active: bool, now: str) -> None:
        self.connection.execute(
            "UPDATE health_products SET active=?,updated_at=? WHERE id=?",
            (1 if active else 0, now, product_id),
        )

    # -- 触发规则 -----------------------------------------------------------
    def seed_default_rule(self, now: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO safety_trigger_rules(code,severe_severities_json,cluster_signal_count,cluster_window_hours,active,updated_by,created_at,updated_at) "
            "VALUES('default',?,3,72,1,'system',?,?)",
            (_dumps(["severe", "life_threatening", "death"]), now, now),
        )

    def active_rule(self) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM safety_trigger_rules WHERE active=1 ORDER BY id LIMIT 1"
        ).fetchone()

    def update_rule(self, *, severe_severities: list[str], cluster_signal_count: int, cluster_window_hours: int, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "UPDATE safety_trigger_rules SET severe_severities_json=?,cluster_signal_count=?,cluster_window_hours=?,updated_by=?,updated_at=? WHERE code='default'",
            (_dumps(severe_severities), cluster_signal_count, cluster_window_hours, actor, now),
        )
        return dict(self.active_rule())

    # -- 报告与来源 ---------------------------------------------------------
    def report_by_key(self, report_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_reports WHERE report_key=?", (report_key,)).fetchone()

    def report_by_fingerprint(self, product_id: int, fingerprint: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM safety_reports WHERE product_id=? AND dedup_fingerprint=? ORDER BY id LIMIT 1",
            (product_id, fingerprint),
        ).fetchone()

    def report_by_id(self, report_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_reports WHERE id=?", (report_id,)).fetchone()

    def create_report(self, *, report_key: str, product_id: int, product_code: str, site_id: int | None, site_code: str, session_id: int | None, severity: str, symptoms: list[str], symptoms_digest: str, signal_key: str, dedup_fingerprint: str, occurrence_at: str, description: str, channel: str, reporter_ref: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO safety_reports(report_key,product_id,product_code,site_id,site_code,session_id,severity,symptoms_json,symptoms_digest,signal_key,dedup_fingerprint,occurrence_at,description,reporter_channel,reporter_ref,status,first_signal_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'submitted',?,?,?)",
            (report_key, product_id, product_code, site_id, site_code, session_id, severity, _dumps(symptoms), symptoms_digest, signal_key, dedup_fingerprint, occurrence_at, description, channel, reporter_ref, occurrence_at, now, now),
        )
        report_id = int(cursor.lastrowid)
        self.add_source(report_id, channel=channel, reporter_ref=reporter_ref, received_at=now, detail="首次上报渠道", now=now)
        return report_id

    def merge_source(self, report_id: int, *, severity: str, site_code: str, session_id: int | None, description: str, now: str) -> None:
        # 合并渠道时只做最保守的修正：严重程度就高不就低，不覆盖任何既有判断。
        current = self.report_by_id(report_id)
        ranked = max(SEVERE_LEVELS.index(current["severity"]), SEVERE_LEVELS.index(severity))
        merged_severity = SEVERE_LEVELS[ranked]
        self.connection.execute(
            "UPDATE safety_reports SET severity=?,site_code=COALESCE(NULLIF(?,''),site_code),session_id=COALESCE(?,session_id),description=CASE WHEN ?='' THEN description ELSE description||CHAR(10)||? END,updated_at=? WHERE id=?",
            (merged_severity, site_code, session_id, description, description, now, report_id),
        )

    def add_source(self, report_id: int, *, channel: str, reporter_ref: str, received_at: str, detail: str, now: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO safety_report_sources(report_id,channel,reporter_ref,received_at,detail,created_at) VALUES(?,?,?,?,?,?)",
            (report_id, channel, reporter_ref, received_at, detail, now),
        )

    def source_exists(self, report_id: int, channel: str, reporter_ref: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM safety_report_sources WHERE report_id=? AND channel=? AND reporter_ref=?",
            (report_id, channel, reporter_ref),
        ).fetchone() is not None

    def set_report_status(self, report_id: int, status: str, now: str) -> None:
        self.connection.execute("UPDATE safety_reports SET status=?,updated_at=? WHERE id=?", (status, now, report_id))

    def list_reports(self, *, product_code: str | None, status: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if product_code:
            clauses.append("product_code=?")
            values.append(product_code)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT * FROM safety_reports" + where + " ORDER BY occurrence_at ASC,id ASC LIMIT ?", values
        ).fetchall()
        return [dict(row) for row in rows]

    def report_detail(self, report_id: int) -> dict[str, Any] | None:
        report = self.report_by_id(report_id)
        if report is None:
            return None
        detail = dict(report)
        detail["sources"] = [dict(row) for row in self.connection.execute(
            "SELECT id,channel,reporter_ref,received_at,detail,created_at FROM safety_report_sources WHERE report_id=? ORDER BY id",
            (report_id,),
        ).fetchall()]
        detail["timeline"] = [
            {**dict(row), "detail_json": json.loads(row["detail_json"])}
            for row in self.connection.execute(
            "SELECT sequence,event_type,actor,summary,detail_json,created_at FROM safety_timeline_events WHERE report_id=? ORDER BY sequence,id",
            (report_id,),
        ).fetchall()
        ]
        detail["decisions"] = [dict(row) for row in self.connection.execute(
            "SELECT id,decision,reason,reviewer,related_finding,created_at FROM safety_decisions WHERE report_id=? ORDER BY id",
            (report_id,),
        ).fetchall()]
        return detail

    # -- 时间线与决定（只追加）---------------------------------------------
    def add_timeline_event(self, report_id: int, event_type: str, actor: str, summary: str, detail: dict[str, Any], now: str) -> int:
        sequence = int(self.connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM safety_timeline_events WHERE report_id=?", (report_id,)
        ).fetchone()[0])
        self.connection.execute(
            "INSERT INTO safety_timeline_events(report_id,sequence,event_type,actor,summary,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (report_id, sequence, event_type, actor, summary, _dumps(detail), now),
        )
        return sequence

    def add_decision(self, report_id: int, *, decision: str, reason: str, reviewer: str, related_finding: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO safety_decisions(report_id,decision,reason,reviewer,related_finding,created_at) VALUES(?,?,?,?,?,?)",
            (report_id, decision, reason, reviewer, related_finding, now),
        )
        return int(cursor.lastrowid)

    # -- 规则判定数据 -------------------------------------------------------
    def count_cluster(self, *, product_id: int, signal_key: str, since: str, until: str) -> int:
        if not signal_key:
            return 0
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM safety_reports WHERE product_id=? AND signal_key=? AND status<>'related_excluded' AND occurrence_at>=? AND occurrence_at<=?",
            (product_id, signal_key, since, until),
        ).fetchone()[0])

    # -- 产品级暂停/解除 ----------------------------------------------------
    def active_suspension(self, product_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM product_safety_actions WHERE product_id=? AND action_type='suspension' AND status='active' ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()

    def action_by_id(self, action_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM product_safety_actions WHERE id=?", (action_id,)).fetchone()

    def create_action(self, *, product_id: int, product_code: str, action_type: str, related_action_id: int | None, reason: str, rule: dict[str, Any], triggered_by: str, triggered_report_ids: list[int], investigation_summary: str, scope_session_ids: list[int], decision_uid: str, status: str, now: str, product_active_before: bool = True) -> int:
        cursor = self.connection.execute(
            "INSERT INTO product_safety_actions(product_id,product_code,action_type,related_action_id,reason,rule_json,triggered_by,triggered_report_ids_json,investigation_summary,scope_session_ids_json,product_active_before,decision_uid,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (product_id, product_code, action_type, related_action_id, reason, _dumps(rule), triggered_by, _dumps(triggered_report_ids), investigation_summary, _dumps(scope_session_ids), 1 if product_active_before else 0, decision_uid, status, now),
        )
        return int(cursor.lastrowid)

    def list_actions(self, product_code: str | None) -> list[dict[str, Any]]:
        if product_code:
            rows = self.connection.execute("SELECT * FROM product_safety_actions WHERE product_code=? ORDER BY id", (product_code,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM product_safety_actions ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def add_lift_signature(self, action_id: int, column_reviewer: str, column_at: str, reviewer: str, now: str) -> None:
        self.connection.execute(
            f"UPDATE product_safety_actions SET {column_reviewer}=?,{column_at}=? WHERE id=?",
            (reviewer, now, action_id),
        )

    def complete_lift(self, action_id: int, *, lift_reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE product_safety_actions SET status='lifted',lifted_at=?,lift_reason=? WHERE id=?",
            (now, lift_reason, action_id),
        )

    def set_investigation_summary(self, action_id: int, summary: str) -> None:
        # 仅在尚无调查结论时写入，写定后任何调用都无法再改。
        self.connection.execute(
            "UPDATE product_safety_actions SET investigation_summary=? WHERE id=? AND investigation_summary=''",
            (summary, action_id),
        )

    # -- 受影响场次与通知 outbox --------------------------------------------
    def unfinished_sessions(self, product_code: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM pilot_sessions WHERE product_code=? AND status IN ('queued','running','cancel_requested') ORDER BY id",
            (product_code,),
        ).fetchall()

    def create_notice(self, *, action_id: int, decision_uid: str, product_code: str, session_id: int, site_code: str, recipient: str, kind: str, now: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO safety_session_notices(action_id,decision_uid,product_code,session_id,site_code,recipient,kind,delivery_status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?, 'pending',?,?)",
            (action_id, decision_uid, product_code, session_id, site_code, recipient, kind, now, now),
        )

    def pending_notices(self, limit: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM safety_session_notices WHERE delivery_status='pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def notice_by_id(self, notice_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_session_notices WHERE id=?", (notice_id,)).fetchone()

    def mark_delivered(self, notice_id: int, now: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE safety_session_notices SET delivery_status='delivered',attempts=attempts+1,delivered_at=?,updated_at=? WHERE id=? AND delivery_status='pending'",
            (now, now, notice_id),
        )
        return cursor.rowcount == 1

    def mark_delivery_failed(self, notice_id: int, error: str, now: str) -> None:
        self.connection.execute(
            "UPDATE safety_session_notices SET attempts=attempts+1,last_error=?,updated_at=? WHERE id=? AND delivery_status='pending'",
            (error[:500], now, notice_id),
        )

    def supersede_pending_for_action(self, action_id: int, superseding_action_id: int, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE safety_session_notices SET delivery_status='superseded',last_error=?,updated_at=? "
            "WHERE action_id=? AND delivery_status='pending'",
            (f"由解除决定 action#{superseding_action_id} 取代", now, action_id),
        )
        return cursor.rowcount

    def acknowledge_notice(self, notice_id: int, now: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE safety_session_notices SET delivery_status='acknowledged',acknowledged_at=?,updated_at=? "
            "WHERE id=? AND delivery_status IN ('delivered','acknowledged')",
            (now, now, notice_id),
        )
        return cursor.rowcount == 1

    def list_notices(self, *, status: str | None = None, site_code: str | None = None, product_code: str | None = None, session_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("delivery_status=?")
            values.append(status)
        if site_code:
            clauses.append("site_code=?")
            values.append(site_code)
        if product_code:
            clauses.append("product_code=?")
            values.append(product_code)
        if session_id is not None:
            clauses.append("session_id=?")
            values.append(session_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT * FROM safety_session_notices" + where + " ORDER BY id LIMIT ?", values
        ).fetchall()
        return [dict(row) for row in rows]

    def notices_for_action(self, action_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT id,session_id,site_code,recipient,kind,delivery_status,attempts,delivered_at,acknowledged_at,last_error FROM safety_session_notices WHERE action_id=? ORDER BY id",
            (action_id,),
        ).fetchall()]
