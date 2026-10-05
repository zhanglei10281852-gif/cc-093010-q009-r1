from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


class SafetyRepository:
    """跨场地安全处置链的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 基础查询 ----
    def product(self, product_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM health_products WHERE code=?", (product_code,)).fetchone()

    def session(self, session_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM pilot_sessions WHERE id=?", (session_id,)).fetchone()

    def report_by_no(self, report_no: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_reports WHERE report_no=?", (report_no,)).fetchone()

    def report_by_id(self, report_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_reports WHERE id=?", (report_id,)).fetchone()

    def report_by_external_ref(self, channel: str, external_ref: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT r.* FROM safety_reports r JOIN safety_report_sources s ON s.report_id=r.id "
            "WHERE s.channel=? AND s.external_ref=? AND r.status<>'excluded' ORDER BY r.id LIMIT 1",
            (channel, external_ref),
        ).fetchone()

    def report_by_fingerprint(self, product_code: str, fingerprint: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM safety_reports WHERE product_code=? AND fingerprint=? AND status<>'excluded' ORDER BY id LIMIT 1",
            (product_code, fingerprint),
        ).fetchone()

    def create_report(self, *, product_code: str, session_id: int | None, site_code: str, symptoms: list[str], severity: str, event_occurred_at: str, description: str, fingerprint: str, created_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO safety_reports(product_code,session_id,site_code,symptoms_json,severity,event_occurred_at,description,status,fingerprint,created_by,created_at,updated_at,report_no) "
            "VALUES(?,?,?,?,?,? ,?, 'open', ?,?,?,?, '')",
            (product_code, session_id, site_code, json.dumps(symptoms, ensure_ascii=False), severity, event_occurred_at, description, fingerprint, created_by, now, now),
        )
        report_id = int(cursor.lastrowid)
        report_no = f"SER-{report_id:06d}"
        self.connection.execute("UPDATE safety_reports SET report_no=? WHERE id=?", (report_no, report_id))
        return report_id

    def set_report_status(self, report_id: int, status: str, now: str) -> None:
        self.connection.execute("UPDATE safety_reports SET status=?,updated_at=? WHERE id=?", (status, now, report_id))

    def list_reports(self, *, product_code: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
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
            "SELECT * FROM safety_reports" + where + " ORDER BY id DESC LIMIT ?", values,
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 来源与时间线 ----
    def add_source(self, report_id: int, *, channel: str, external_ref: str, reporter: str, received_at: str, now: str) -> bool:
        """返回 False 表示该来源已经登记过（不重复计数）。"""
        try:
            self.connection.execute(
                "INSERT INTO safety_report_sources(report_id,channel,external_ref,reporter,received_at,created_at) VALUES(?,?,?,?,?,?)",
                (report_id, channel, external_ref, reporter, received_at, now),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def sources(self, report_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM safety_report_sources WHERE report_id=? ORDER BY id", (report_id,),
        ).fetchall()]

    def add_timeline(self, report_id: int, entry_type: str, *, actor: str, summary: str, detail: dict[str, Any], now: str) -> int:
        seq = int(self.connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM safety_timeline WHERE report_id=?", (report_id,)).fetchone()[0])
        self.connection.execute(
            "INSERT INTO safety_timeline(report_id,seq,entry_type,actor,summary,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (report_id, seq, entry_type, actor, summary, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )
        return seq

    def timeline(self, report_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM safety_timeline WHERE report_id=? ORDER BY seq,id", (report_id,),
        ).fetchall()]

    # ---- 调查 ----
    def open_investigation(self, product_code: str, *, opened_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO safety_investigations(report_id,investigation_no,product_code,status,opened_by,opened_at,updated_at) VALUES(NULL,'',?,'open',?,?,?)",
            (product_code, opened_by, now, now),
        )
        investigation_id = int(cursor.lastrowid)
        self.connection.execute(
            "UPDATE safety_investigations SET investigation_no=? WHERE id=?",
            (f"INV-{investigation_id:06d}", investigation_id),
        )
        return investigation_id

    def open_investigation_for_product(self, product_code: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM safety_investigations WHERE product_code=? AND status='open' ORDER BY id LIMIT 1",
            (product_code,),
        ).fetchone()

    def investigation_by_id(self, investigation_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_investigations WHERE id=?", (investigation_id,)).fetchone()

    def attach_report_signals(self, investigation_id: int, report_nos: Iterable[str]) -> None:
        row = self.connection.execute("SELECT related_signal_json FROM safety_investigations WHERE id=?", (investigation_id,)).fetchone()
        signals: list[str] = json.loads(row["related_signal_json"]) if row else []
        merged = list(dict.fromkeys([*signals, *report_nos]))
        self.connection.execute(
            "UPDATE safety_investigations SET related_signal_json=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), investigation_id),
        )

    def conclude_investigation(self, investigation_id: int, *, actor: str, conclusion: str, now: str) -> None:
        self.connection.execute(
            "UPDATE safety_investigations SET status='concluded',concluded_by=?,concluded_at=?,conclusion=?,updated_at=? WHERE id=?",
            (actor, now, conclusion, now, investigation_id),
        )

    def investigations_for_product(self, product_code: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM safety_investigations WHERE product_code=? ORDER BY id", (product_code,),
        ).fetchall()]

    # ---- 规则评估 ----
    def reports_for_rule(self, product_code: str, severities: list[str], since: str) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in severities)
        return self.connection.execute(
            f"SELECT * FROM safety_reports WHERE product_code=? AND status<>'excluded' AND severity IN ({placeholders}) AND created_at>=? ORDER BY id",
            (product_code, *severities, since),
        ).fetchall()

    def active_rules(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM safety_signal_rules WHERE active=1 ORDER BY id").fetchall()

    def rule_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_signal_rules WHERE code=?", (code,)).fetchone()

    def update_rule(self, rule_id: int, changes: dict[str, Any], now: str) -> None:
        assignments = [f"{key}=?" for key in changes]
        self.connection.execute(
            f"UPDATE safety_signal_rules SET {','.join(assignments)},updated_at=? WHERE id=?",
            (*changes.values(), now, rule_id),
        )

    # ---- 暂停决定 ----
    def active_suspension(self, product_code: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM safety_decisions WHERE product_code=? AND action='product_suspension' AND status='active' ORDER BY id DESC LIMIT 1",
            (product_code,),
        ).fetchone()

    def decision_by_no(self, decision_no: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM safety_decisions WHERE decision_no=?", (decision_no,)).fetchone()

    def create_decision(self, *, product_code: str, action: str, reason: str, trigger_rule: str, report_id: int | None, investigation_id: int | None, resumes_decision_id: int | None, issued_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO safety_decisions(decision_no,product_code,action,scope,reason,status,trigger_rule,report_id,investigation_id,resumes_decision_id,issued_by,issued_at) "
            "VALUES('',?,?,'product',?,'active',?,?,?,?,?,?)",
            (product_code, action, reason, trigger_rule, report_id, investigation_id, resumes_decision_id, issued_by, now),
        )
        decision_id = int(cursor.lastrowid)
        prefix = "SUS" if action == "product_suspension" else "RES"
        self.connection.execute("UPDATE safety_decisions SET decision_no=? WHERE id=?", (f"{prefix}-{decision_id:06d}", decision_id))
        return decision_id

    def add_approval(self, decision_id: int, *, reviewer: str, note: str, now: str) -> bool:
        """返回 False 表示该审阅人已经签过。"""
        try:
            self.connection.execute(
                "INSERT INTO safety_decision_approvals(decision_id,reviewer,note,approved_at) VALUES(?,?,?,?)",
                (decision_id, reviewer, note, now),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def approvals(self, decision_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM safety_decision_approvals WHERE decision_id=? ORDER BY id", (decision_id,),
        ).fetchall()]

    def lift_decision(self, decision_id: int, *, lift_reason: str, lift_summary: str, now: str) -> None:
        self.connection.execute(
            "UPDATE safety_decisions SET status='lifted',lifted_at=?,lift_reason=?,lift_summary=? WHERE id=?",
            (now, lift_reason, lift_summary, decision_id),
        )

    def decisions_for_product(self, product_code: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM safety_decisions WHERE product_code=? ORDER BY id", (product_code,),
        ).fetchall()]

    # ---- 场次影响 ----
    def unfinished_sessions(self, product_code: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM pilot_sessions WHERE product_code=? AND status IN ('queued','running') ORDER BY id",
            (product_code,),
        ).fetchall()

    def apply_suspension_effect(self, sessions: Iterable[sqlite3.Row], decision_no: str, now: str) -> list[dict[str, Any]]:
        effects: list[dict[str, Any]] = []
        for session in sessions:
            if session["status"] == "queued":
                effect = "blocked"
                self.connection.execute(
                    "UPDATE pilot_sessions SET status='blocked',blocked_by_decision_no=?,updated_at=?,version=version+1 WHERE id=?",
                    (decision_no, now, session["id"]),
                )
            else:
                effect = "held"
                self.connection.execute(
                    "UPDATE pilot_sessions SET status='safety_hold',blocked_by_decision_no=?,updated_at=?,version=version+1 WHERE id=?",
                    (decision_no, now, session["id"]),
                )
            effects.append({"session_id": session["id"], "previous_status": session["status"], "effect": effect})
        return effects

    def record_decision_sessions(self, decision_id: int, effects: list[dict[str, Any]], now: str) -> None:
        for item in effects:
            self.connection.execute(
                "INSERT INTO safety_decision_sessions(decision_id,session_id,previous_status,effect,created_at) VALUES(?,?,?,?,?)",
                (decision_id, item["session_id"], item["previous_status"], item["effect"], now),
            )

    def release_sessions(self, decision_id: int, resumption_decision_id: int, now: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM safety_decision_sessions WHERE decision_id=? ORDER BY session_id", (decision_id,),
        ).fetchall()
        released: list[dict[str, Any]] = []
        for row in rows:
            session = self.session(int(row["session_id"]))
            if session is None or session["status"] not in {"blocked", "safety_hold"}:
                continue
            self.connection.execute(
                "UPDATE pilot_sessions SET status='queued',available_at=?,blocked_by_decision_no='',lease_owner='',lease_expires_at='',updated_at=?,version=version+1 WHERE id=?",
                (now, now, session["id"]),
            )
            # 暂停决定保留暂停时的原始效果；恢复决定记录 released，解除依据可双向追溯。
            self.connection.execute(
                "INSERT INTO safety_decision_sessions(decision_id,session_id,previous_status,effect,created_at) VALUES(?,?,?,?,?)",
                (resumption_decision_id, session["id"], session["status"], "released", now),
            )
            released.append({"session_id": session["id"], "previous_status": session["status"], "suspension_effect": row["effect"]})
        return released

    # ---- 通知外箱 ----
    def enqueue_notification(self, decision_id: int, *, recipient: str, channel: str, payload: dict[str, Any], now: str) -> bool:
        dedup_key = f"{decision_id}:{channel}:{recipient}"
        try:
            self.connection.execute(
                "INSERT INTO safety_notifications(decision_id,recipient,channel,payload_json,status,dedup_key,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?,?)",
                (decision_id, recipient, channel, json.dumps(payload, ensure_ascii=False, sort_keys=True), dedup_key, now, now),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def pending_notifications(self, *, limit: int = 100) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM safety_notifications WHERE status='pending' ORDER BY id LIMIT ?", (limit,),
        ).fetchall()

    def notification_for(self, decision_id: int, recipient: str, channel: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM safety_notifications WHERE decision_id=? AND recipient=? AND channel=?",
            (decision_id, recipient, channel),
        ).fetchone()

    def mark_notification_delivered(self, notification_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE safety_notifications SET status='delivered',attempts=attempts+1,last_error='',updated_at=? WHERE id=?",
            (now, notification_id),
        )

    def mark_notification_failed(self, notification_id: int, error: str, *, terminal: bool, now: str) -> None:
        status = "failed" if terminal else "pending"
        self.connection.execute(
            "UPDATE safety_notifications SET status=?,attempts=attempts+1,last_error=?,updated_at=? WHERE id=?",
            (status, error[:500], now, notification_id),
        )

    def acknowledge_notification(self, notification_id: int, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE safety_notifications SET status='acknowledged',updated_at=? WHERE id=? AND status<>'failed'",
            (now, notification_id),
        )
        return cursor.rowcount

    def notifications_for_decision(self, decision_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT id,recipient,channel,status,attempts,last_error,created_at,updated_at FROM safety_notifications WHERE decision_id=? ORDER BY id",
            (decision_id,),
        ).fetchall()]

    def notification_recipients(self, decision_id: int) -> set[str]:
        return {str(row[0]) for row in self.connection.execute(
            "SELECT DISTINCT recipient FROM safety_notifications WHERE decision_id=?", (decision_id,),
        ).fetchall()}

    # ---- 产品安全全景 ----
    def first_signal_at(self, product_code: str) -> str | None:
        return self.connection.execute(
            "SELECT MIN(created_at) FROM safety_reports WHERE product_code=?", (product_code,),
        ).fetchone()[0]
