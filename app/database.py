from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import CONFIG
import json
import os
import sqlite3
from datetime import datetime, timezone

_IS_SQLITE = CONFIG.DB_URL.startswith("sqlite")
_connect_args = {}
if _IS_SQLITE:
    _connect_args = {
        "check_same_thread": False,
        "timeout": max(float(CONFIG.SQLITE_BUSY_TIMEOUT_MS) / 1000.0, 1.0),
    }

engine = create_engine(
    CONFIG.DB_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    pool_size=max(int(CONFIG.DB_POOL_SIZE), 1),
    max_overflow=max(int(CONFIG.DB_MAX_OVERFLOW), 0),
    pool_timeout=max(int(CONFIG.DB_POOL_TIMEOUT_S), 1),
    pool_recycle=max(int(CONFIG.DB_POOL_RECYCLE_S), 30),
)


if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute(f"PRAGMA busy_timeout={max(int(CONFIG.SQLITE_BUSY_TIMEOUT_MS), 1000)};")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA temp_store=MEMORY;")
        finally:
            cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def is_sqlite_enabled() -> bool:
    return _IS_SQLITE


def get_sqlite_db_path() -> str | None:
    if not _IS_SQLITE:
        return None
    raw = CONFIG.DB_URL[len("sqlite:///") :]
    if raw.startswith("/"):
        return raw
    return os.path.abspath(raw)


def _sqlite_connect() -> sqlite3.Connection:
    path = get_sqlite_db_path()
    if not path:
        raise RuntimeError("sqlite_not_enabled")
    conn = sqlite3.connect(
        path,
        timeout=max(float(CONFIG.SQLITE_BUSY_TIMEOUT_MS) / 1000.0, 1.0),
    )
    conn.execute(f"PRAGMA busy_timeout={max(int(CONFIG.SQLITE_BUSY_TIMEOUT_MS), 1000)};")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def sqlite_health_check(mode: str = "quick_check") -> dict:
    path = get_sqlite_db_path()
    if not path:
        return {"enabled": False, "ok": True, "mode": mode, "result": "not_sqlite"}

    pragma = "integrity_check" if str(mode).lower() == "integrity_check" else "quick_check"
    conn = _sqlite_connect()
    try:
        row = conn.execute(f"PRAGMA {pragma};").fetchone()
        result = str(row[0] if row else "unknown")
        return {
            "enabled": True,
            "ok": result.lower() == "ok",
            "mode": pragma,
            "result": result,
            "path": path,
        }
    finally:
        conn.close()


def sqlite_checkpoint(mode: str = "PASSIVE") -> dict:
    path = get_sqlite_db_path()
    if not path:
        return {"enabled": False, "ok": True, "mode": mode, "result": "not_sqlite"}

    checkpoint_mode = str(mode or "PASSIVE").upper()
    conn = _sqlite_connect()
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({checkpoint_mode});").fetchone()
        result = tuple(row or ())
        return {
            "enabled": True,
            "ok": True,
            "mode": checkpoint_mode,
            "result": result,
            "path": path,
        }
    finally:
        conn.close()


def export_task_runtime_snapshot(tag: str = "manual") -> str | None:
    path = get_sqlite_db_path()
    if not path:
        return None

    snapshot_dir = os.path.join(os.path.dirname(path), "task_db_snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    file_path = os.path.join(snapshot_dir, f"task_runtime_{tag}_{timestamp}.json")

    conn = _sqlite_connect()
    conn.row_factory = sqlite3.Row
    try:
        tasks = []
        task_events = []
        if _sqlite_table_exists(conn, "tasks"):
            tasks = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY started_at DESC LIMIT 500")]
        if _sqlite_table_exists(conn, "task_events"):
            task_events = [dict(row) for row in conn.execute("SELECT * FROM task_events ORDER BY ts DESC LIMIT 1000")]
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tag": tag,
            "db_path": path,
            "tasks_count": len(tasks),
            "task_events_count": len(task_events),
            "tasks": tasks,
            "task_events": task_events,
        }
        with open(file_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        return file_path
    finally:
        conn.close()


def sqlite_ensure_task_runtime_indexes() -> dict:
    path = get_sqlite_db_path()
    if not path:
        return {"enabled": False, "ok": True, "indexes": []}

    statements = {
        "idx_tasks_status_started_at": (
            "tasks",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_started_at "
            "ON tasks(status, started_at DESC)"
        ),
        "idx_tasks_account_status_started_at": (
            "tasks",
            "CREATE INDEX IF NOT EXISTS idx_tasks_account_status_started_at "
            "ON tasks(account_name, status, started_at DESC)"
        ),
        "idx_tasks_status_heartbeat_at": (
            "tasks",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_heartbeat_at "
            "ON tasks(status, heartbeat_at)"
        ),
        "idx_tasks_request_id_started_at": (
            "tasks",
            "CREATE INDEX IF NOT EXISTS idx_tasks_request_id_started_at "
            "ON tasks(request_id, started_at DESC)"
        ),
        "idx_task_events_task_id_ts": (
            "task_events",
            "CREATE INDEX IF NOT EXISTS idx_task_events_task_id_ts "
            "ON task_events(task_id, ts DESC)"
        ),
        "idx_send_logs_task_id_created_at": (
            "send_logs",
            "CREATE INDEX IF NOT EXISTS idx_send_logs_task_id_created_at "
            "ON send_logs(task_id, created_at DESC)"
        ),
        "idx_send_logs_account_created_at": (
            "send_logs",
            "CREATE INDEX IF NOT EXISTS idx_send_logs_account_created_at "
            "ON send_logs(account_name, created_at DESC)"
        ),
    }
    conn = _sqlite_connect()
    try:
        applied: list[str] = []
        skipped: list[str] = []
        for index_name, (table_name, sql) in statements.items():
            if not _sqlite_table_exists(conn, table_name):
                skipped.append(index_name)
                continue
            conn.execute(sql)
            applied.append(index_name)
        conn.execute("PRAGMA optimize;")
        conn.commit()
        return {
            "enabled": True,
            "ok": True,
            "path": path,
            "indexes": applied,
            "skipped": skipped,
        }
    finally:
        conn.close()


def export_task_migration_manifest(tag: str = "manual") -> str | None:
    path = get_sqlite_db_path()
    if not path:
        return None

    snapshot_dir = os.path.join(os.path.dirname(path), "task_db_snapshots")
    os.makedirs(snapshot_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    file_path = os.path.join(snapshot_dir, f"task_migration_manifest_{tag}_{timestamp}.json")

    conn = _sqlite_connect()
    conn.row_factory = sqlite3.Row
    try:
        tables: dict[str, dict] = {}
        for table_name in ("tasks", "task_events", "send_logs"):
            if not _sqlite_table_exists(conn, table_name):
                continue
            columns = [
                {
                    "cid": row["cid"],
                    "name": row["name"],
                    "type": row["type"],
                    "notnull": row["notnull"],
                    "default": row["dflt_value"],
                    "pk": row["pk"],
                }
                for row in conn.execute(f"PRAGMA table_info('{table_name}')")
            ]
            indexes = [
                {
                    "seq": row["seq"],
                    "name": row["name"],
                    "unique": row["unique"],
                    "origin": row["origin"],
                    "partial": row["partial"],
                }
                for row in conn.execute(f"PRAGMA index_list('{table_name}')")
            ]
            row_count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            tables[table_name] = {
                "row_count": int(row_count or 0),
                "columns": columns,
                "indexes": indexes,
            }

        pragma_names = (
            "journal_mode",
            "synchronous",
            "busy_timeout",
            "page_count",
            "page_size",
            "freelist_count",
            "wal_autocheckpoint",
            "auto_vacuum",
        )
        pragmas = {}
        for name in pragma_names:
            row = conn.execute(f"PRAGMA {name};").fetchone()
            pragmas[name] = row[0] if row else None
        quick_check = conn.execute("PRAGMA quick_check;").fetchone()

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tag": tag,
            "db_path": path,
            "sqlite_version": sqlite3.sqlite_version,
            "quick_check": quick_check[0] if quick_check else "unknown",
            "pragmas": pragmas,
            "tables": tables,
        }
        with open(file_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        return file_path
    finally:
        conn.close()


def is_database_malformed_error(exc: Exception) -> bool:
    try:
        text = str(exc).lower()
    except Exception:
        return False
    return "database disk image is malformed" in text or "malformed" in text
