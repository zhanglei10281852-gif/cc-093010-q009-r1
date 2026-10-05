from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.clock import to_storage, utc_now


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "health-innovation.db"
_local = threading.local()


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    manager TEXT NOT NULL,
    phone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','locked')),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(role_id, permission_id)
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(user_id, role_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    client_label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failure')),
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);
CREATE TABLE IF NOT EXISTS department_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    title TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, department_id, starts_at)
);
CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    locked_by TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON background_jobs(status,available_at);

CREATE TABLE IF NOT EXISTS health_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    organization TEXT NOT NULL,
    origin_country TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('康复设备','辅助诊断','数字疗法','慢病管理','数字中医','健康消费')),
    intended_use TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high')),
    regulatory_status TEXT NOT NULL DEFAULT '展示' CHECK(regulatory_status IN ('展示','研究','已注册','暂停')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    site_type TEXT NOT NULL CHECK(site_type IN ('展会体验点','医院','康复机构','研究机构','产业伙伴')),
    region TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrent > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('临床','性能','安全','合规','体验')),
    title TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_region TEXT NOT NULL,
    version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','accepted','rejected','superseded')),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    UNIQUE(product_id, evidence_type, version, content_digest)
);
CREATE TABLE IF NOT EXISTS public_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,
    site_id INTEGER NOT NULL REFERENCES pilot_sites(id) ON DELETE CASCADE,
    session_reference TEXT NOT NULL,
    audience_type TEXT NOT NULL CHECK(audience_type IN ('公众','临床人员','采购商','产业伙伴')),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    tags_json TEXT NOT NULL DEFAULT '[]',
    comment TEXT NOT NULL DEFAULT '',
    contact_digest TEXT NOT NULL DEFAULT '',
    consent_to_follow_up INTEGER NOT NULL DEFAULT 0 CHECK(consent_to_follow_up IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, session_reference, audience_type, contact_digest)
);

CREATE TABLE IF NOT EXISTS pilot_protocols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    capability TEXT NOT NULL,
    product_code TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    parameter_schema_json TEXT NOT NULL,
    default_parameters_json TEXT NOT NULL DEFAULT '{}',
    max_runtime_seconds INTEGER NOT NULL CHECK(max_runtime_seconds > 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('user','role','project')),
    subject_key TEXT NOT NULL,
    max_queued INTEGER NOT NULL CHECK(max_queued >= 0),
    max_running INTEGER NOT NULL CHECK(max_running >= 0),
    daily_submissions INTEGER NOT NULL CHECK(daily_submissions >= 0),
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_type, subject_key)
);
CREATE TABLE IF NOT EXISTS pilot_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id INTEGER NOT NULL REFERENCES pilot_protocols(id) ON DELETE RESTRICT,
    project_code TEXT NOT NULL,
    product_code TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','cancel_requested','cancelled','succeeded','failed','blocked','safety_hold')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    blocked_by_decision_no TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_pilot_queue ON pilot_sessions(status,priority DESC,available_at,created_at);
CREATE INDEX IF NOT EXISTS idx_pilot_sessions_product ON pilot_sessions(product_code,status);
CREATE TABLE IF NOT EXISTS pilot_observations (
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
CREATE TABLE IF NOT EXISTS pilot_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pilot_interventions ON pilot_interventions(session_id,id);

-- 跨场地安全处置链：不良事件报告、医学分诊、调查、产品级暂停决定、通知外箱、双审阅解除
CREATE TABLE IF NOT EXISTS safety_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_no TEXT NOT NULL UNIQUE,
    product_code TEXT NOT NULL,
    session_id INTEGER REFERENCES pilot_sessions(id),
    site_code TEXT NOT NULL DEFAULT '',
    symptoms_json TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('mild','moderate','serious','critical')),
    event_occurred_at TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','info_requested','investigating','excluded','closed')),
    fingerprint TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_safety_reports_product ON safety_reports(product_code,status,severity);
CREATE INDEX IF NOT EXISTS idx_safety_reports_session ON safety_reports(session_id);
CREATE TABLE IF NOT EXISTS safety_report_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES safety_reports(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    external_ref TEXT NOT NULL DEFAULT '',
    reporter TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(report_id, channel, external_ref)
);
CREATE INDEX IF NOT EXISTS idx_safety_sources_lookup ON safety_report_sources(channel,external_ref);
CREATE TABLE IF NOT EXISTS safety_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES safety_reports(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('report','source_merge','triage','investigation','decision','decision_effect','acknowledgement','closure')),
    actor TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(report_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_safety_timeline_report ON safety_timeline(report_id,id);
CREATE TABLE IF NOT EXISTS safety_investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER REFERENCES safety_reports(id),
    investigation_no TEXT NOT NULL UNIQUE,
    product_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','concluded','superseded')),
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    concluded_by TEXT NOT NULL DEFAULT '',
    concluded_at TEXT,
    conclusion TEXT NOT NULL DEFAULT '',
    related_signal_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_safety_investigations_product ON safety_investigations(product_code,status);
CREATE TABLE IF NOT EXISTS safety_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_no TEXT NOT NULL UNIQUE,
    product_code TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('product_suspension','resumption')),
    scope TEXT NOT NULL DEFAULT 'product' CHECK(scope IN ('product')),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','lifted')),
    trigger_rule TEXT NOT NULL DEFAULT '',
    report_id INTEGER REFERENCES safety_reports(id),
    investigation_id INTEGER REFERENCES safety_investigations(id),
    resumes_decision_id INTEGER REFERENCES safety_decisions(id),
    issued_by TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    lifted_at TEXT,
    lift_reason TEXT NOT NULL DEFAULT '',
    lift_summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_safety_decisions_product ON safety_decisions(product_code,status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_safety_active_suspension ON safety_decisions(product_code) WHERE action='product_suspension' AND status='active';
CREATE TABLE IF NOT EXISTS safety_decision_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES safety_decisions(id) ON DELETE CASCADE,
    reviewer TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL,
    UNIQUE(decision_id, reviewer)
);
CREATE TABLE IF NOT EXISTS safety_decision_sessions (
    decision_id INTEGER NOT NULL REFERENCES safety_decisions(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id),
    previous_status TEXT NOT NULL,
    effect TEXT NOT NULL CHECK(effect IN ('blocked','held','released')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(decision_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_safety_decision_sessions_session ON safety_decision_sessions(session_id);
CREATE TABLE IF NOT EXISTS safety_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES safety_decisions(id) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'site',
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered','acknowledged','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    dedup_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(decision_id, recipient, channel)
);
CREATE INDEX IF NOT EXISTS idx_safety_notifications_status ON safety_notifications(status,id);
CREATE TABLE IF NOT EXISTS safety_signal_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    severity_in_json TEXT NOT NULL DEFAULT '[]',
    min_count INTEGER NOT NULL CHECK(min_count > 0),
    window_seconds INTEGER NOT NULL CHECK(window_seconds > 0),
    same_symptom INTEGER NOT NULL DEFAULT 0 CHECK(same_symptom IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
'''


PERMISSIONS = [
    ("users.read", "查看用户", "users", "read"),
    ("users.write", "维护用户", "users", "write"),
    ("roles.read", "查看角色", "roles", "read"),
    ("roles.write", "维护角色", "roles", "write"),
    ("departments.read", "查看协作团队", "departments", "read"),
    ("departments.write", "维护协作团队", "departments", "write"),
    ("catalog.read", "查看健康创新目录", "catalog", "read"),
    ("catalog.write", "维护健康创新目录", "catalog", "write"),
    ("evidence.review", "审阅产品证据", "evidence", "review"),
    ("feedback.read", "查看体验反馈", "feedback", "read"),
    ("audit.read", "查看审计", "audit", "read"),
    ("jobs.run", "执行后台任务", "jobs", "run"),
]


def database_path() -> Path:
    raw = os.getenv("HEALTH_INNOVATION_DATABASE_PATH", str(DEFAULT_DB_PATH))
    return Path(raw).expanduser().resolve()


def _create_connection() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def get_connection() -> sqlite3.Connection:
    connection = getattr(_local, "connection", None)
    if connection is None:
        connection = _create_connection()
        _local.connection = connection
    return connection


def close_connection() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


DEFAULT_SIGNAL_RULES = [
    ("serious-any", "任一严重或危急事件即暂停", ["serious", "critical"], 1, 90 * 24 * 3600, 0),
    ("cluster-same-symptom", "30天内同类症状信号聚集", ["moderate", "serious", "critical"], 3, 30 * 24 * 3600, 1),
]


def _seed_signal_rules(connection: sqlite3.Connection, now: str) -> None:
    import json

    for code, name, severities, min_count, window_seconds, same_symptom in DEFAULT_SIGNAL_RULES:
        connection.execute(
            "INSERT OR IGNORE INTO safety_signal_rules(code,name,severity_in_json,min_count,window_seconds,same_symptom,active,created_at,updated_at) VALUES(?,?,?,?,?,?,'1',?,?)",
            (code, name, json.dumps(severities, ensure_ascii=False), min_count, window_seconds, same_symptom, now, now),
        )


def _upgrade_to_version_3(connection: sqlite3.Connection) -> None:
    session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(pilot_sessions)").fetchall()}
    protocol_columns = {row["name"] for row in connection.execute("PRAGMA table_info(pilot_protocols)").fetchall()}
    table_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='pilot_sessions'").fetchone()
    rebuild_needed = table_sql is not None and "'blocked'" not in (table_sql[0] or "")
    # 必须在事务外设置：foreign_keys=OFF + legacy_alter_table=ON，
    # RENAME 既不触发级联也不改写子表外键引用，新表沿用原名后引用继续有效。
    if rebuild_needed:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if "product_code" not in protocol_columns:
            connection.execute("ALTER TABLE pilot_protocols ADD COLUMN product_code TEXT NOT NULL DEFAULT ''")
        if "product_code" not in session_columns:
            connection.execute("ALTER TABLE pilot_sessions ADD COLUMN product_code TEXT NOT NULL DEFAULT ''")
        if "blocked_by_decision_no" not in session_columns:
            connection.execute("ALTER TABLE pilot_sessions ADD COLUMN blocked_by_decision_no TEXT NOT NULL DEFAULT ''")
        if rebuild_needed:
            connection.execute("ALTER TABLE pilot_sessions RENAME TO pilot_sessions_v2")
            connection.execute(
                """
CREATE TABLE pilot_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id INTEGER NOT NULL REFERENCES pilot_protocols(id) ON DELETE RESTRICT,
    project_code TEXT NOT NULL,
    product_code TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','cancel_requested','cancelled','succeeded','failed','blocked','safety_hold')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    blocked_by_decision_no TEXT NOT NULL DEFAULT '',
    current_observation_version INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requested_by, idempotency_key)
)
"""
            )
            connection.execute(
                """
INSERT INTO pilot_sessions(id,protocol_id,project_code,product_code,requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,lease_owner,lease_expires_at,current_observation_version,last_error_code,last_error_message,version,started_at,finished_at,created_at,updated_at)
SELECT id,protocol_id,project_code,'',requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,lease_owner,lease_expires_at,current_observation_version,last_error_code,last_error_message,version,started_at,finished_at,created_at,updated_at FROM pilot_sessions_v2
"""
            )
            connection.execute("DROP TABLE pilot_sessions_v2")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_pilot_queue ON pilot_sessions(status,priority DESC,available_at,created_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_pilot_sessions_product ON pilot_sessions(product_code,status)")
    except Exception:
        connection.rollback()
        if rebuild_needed:
            connection.execute("PRAGMA legacy_alter_table=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
        raise
    else:
        connection.commit()
        if rebuild_needed:
            connection.execute("PRAGMA legacy_alter_table=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"升级后外键检查失败：{[tuple(row) for row in violations][:5]}")


def _seed_access_control(connection: sqlite3.Connection, now: str) -> None:
    for code, name, resource, action in PERMISSIONS:
        connection.execute(
            "INSERT OR IGNORE INTO permissions(code,name,resource,action) VALUES(?,?,?,?)",
            (code, name, resource, action),
        )
    roles = [
        ("administrator", "系统管理员", "拥有全部系统权限"),
        ("operator", "试点运营员", "维护目录、场地和体验场次"),
        ("reviewer", "证据审阅员", "审阅产品证据与体验反馈"),
        ("auditor", "审计查看员", "只读查看运行与审计记录"),
    ]
    for code, name, description in roles:
        connection.execute(
            "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES(?,?,?,1,?,?)",
            (code, name, description, now, now),
        )
    administrator = connection.execute("SELECT id FROM roles WHERE code='administrator'").fetchone()[0]
    connection.execute(
        "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) SELECT ?,id,? FROM permissions",
        (administrator, now),
    )


def init_db() -> None:
    connection = get_connection()
    version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
    legacy_sessions = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_sessions'"
    ).fetchone()
    # 旧库先补齐列并重建场次表，随后 SCHEMA 中依赖新列的索引才能创建。
    if version < 3 and legacy_sessions is not None:
        _upgrade_to_version_3(connection)
    # executescript 会自行提交；CREATE TABLE IF NOT EXISTS 不会覆盖既有表。
    connection.executescript(SCHEMA)
    now = to_storage(utc_now())
    connection.execute("BEGIN IMMEDIATE")
    try:
        _seed_access_control(connection, now)
        _seed_signal_rules(connection, now)
        if version < 3:
            connection.execute("PRAGMA user_version=3")
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def migrate_db() -> None:
    init_db()
