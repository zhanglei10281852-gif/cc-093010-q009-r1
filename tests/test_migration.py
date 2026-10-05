from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


LEGACY_SCHEMA = """
CREATE TABLE pilot_protocols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    capability TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    parameter_schema_json TEXT NOT NULL,
    default_parameters_json TEXT NOT NULL DEFAULT '{}',
    max_runtime_seconds INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE pilot_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id INTEGER NOT NULL REFERENCES pilot_protocols(id) ON DELETE RESTRICT,
    project_code TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','cancel_requested','cancelled','succeeded','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    available_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    current_observation_version INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requested_by, idempotency_key)
);
CREATE TABLE pilot_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    observation_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    observation_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, version)
);
CREATE TABLE pilot_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    actor TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
    before_json TEXT NOT NULL, after_json TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
"""


def test_legacy_v2_database_upgrades_and_preserves_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(LEGACY_SCHEMA)
    raw.execute("PRAGMA user_version=2")
    raw.execute(
        "INSERT INTO pilot_protocols(code,name,capability,parameter_schema_json,max_runtime_seconds,max_attempts,created_by,created_at,updated_at) "
        "VALUES('p1','旧方案','cap','{}',60,2,'tester','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
    )
    raw.execute(
        "INSERT INTO pilot_sessions(protocol_id,project_code,requested_by,parameters_json,parameter_digest,idempotency_key,status,max_attempts,available_at,created_at,updated_at) "
        "VALUES(1,'proj','user-1','{}','dig','idem-1','running',2,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
    )
    raw.execute("INSERT INTO pilot_observations(session_id,version,observation_json,observation_digest,created_by,created_at) VALUES(1,1,'{}','d','site','2026-01-02T00:00:00+00:00')")
    raw.commit()
    raw.close()

    monkeypatch.setenv("HEALTH_INNOVATION_DATABASE_PATH", str(db_path))
    from app.database import close_connection, get_connection, init_db
    close_connection()
    init_db()

    connection = get_connection()
    assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
    # 旧数据保留
    session = connection.execute("SELECT * FROM pilot_sessions WHERE id=1").fetchone()
    assert session["status"] == "running" and session["product_code"] == "" and session["blocked_by_decision_no"] == ""
    # 新状态允许写入
    connection.execute("UPDATE pilot_sessions SET status='blocked' WHERE id=1")
    connection.commit()
    assert connection.execute("SELECT status FROM pilot_sessions WHERE id=1").fetchone()[0] == "blocked"
    # 子表外键仍指向 pilot_sessions（legacy_alter_table 未污染引用）
    fk = connection.execute("PRAGMA foreign_key_list(pilot_observations)").fetchall()
    assert any(row["table"] == "pilot_sessions" for row in fk)
    assert connection.execute("SELECT COUNT(*) FROM safety_signal_rules").fetchone()[0] >= 2
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    close_connection()
