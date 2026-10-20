from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_relpath TEXT NOT NULL,
    filename TEXT NOT NULL,
    normalized_filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    route TEXT NOT NULL,
    stat_device INTEGER NOT NULL,
    stat_inode INTEGER NOT NULL,
    stat_mtime_ns INTEGER NOT NULL,
    duplicate_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    inspection_id TEXT NOT NULL REFERENCES inspections(inspection_id),
    idempotency_key TEXT,
    status TEXT NOT NULL,
    duplicate_action TEXT NOT NULL,
    reused_from_task_id TEXT,
    requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    message_zh TEXT NOT NULL,
    progress_percent INTEGER,
    current_stage TEXT,
    started_at TEXT,
    finished_at TEXT,
    worker_id TEXT,
    heartbeat_at TEXT,
    batch_relpath TEXT,
    error_code TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    quality_json TEXT,
    document_language TEXT NOT NULL DEFAULT 'ch',
    document_profile TEXT NOT NULL DEFAULT 'general_text',
    parser_backend TEXT NOT NULL DEFAULT 'pipeline',
    parse_method TEXT NOT NULL DEFAULT 'auto'
);

CREATE TABLE IF NOT EXISTS documents (
    task_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    normalized_filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_location_label TEXT NOT NULL,
    archive_location_label TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_normalized_filename ON documents(normalized_filename);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    relative_path TEXT NOT NULL,
    display_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    purpose TEXT NOT NULL,
    feishu_sendable INTEGER NOT NULL,
    UNIQUE(task_id, relative_path)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_task_id ON artifacts(task_id);
"""


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            self._migrate(db)

    def _migrate(self, db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
        additions = {
            "idempotency_key": "TEXT",
            "progress_percent": "INTEGER",
            "current_stage": "TEXT",
            "started_at": "TEXT",
            "finished_at": "TEXT",
            "worker_id": "TEXT",
            "heartbeat_at": "TEXT",
            "batch_relpath": "TEXT",
            "error_code": "TEXT",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "quality_json": "TEXT",
            "document_language": "TEXT NOT NULL DEFAULT 'ch'",
            "document_profile": "TEXT NOT NULL DEFAULT 'general_text'",
            "parser_backend": "TEXT NOT NULL DEFAULT 'pipeline'",
            "parse_method": "TEXT NOT NULL DEFAULT 'auto'",
        }
        for name, declaration in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")  # noqa: S608
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_requester_idempotency
            ON tasks(requested_by, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            # The SQLite context commits or rolls back; it does not close.
            with db:
                yield db
        finally:
            db.close()

    def find_documents(self, *, normalized_filename: str, sha256: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT task_id, filename, normalized_filename, sha256, status,
                       candidate_location_label, archive_location_label, updated_at
                FROM documents
                WHERE sha256 = ? OR normalized_filename = ?
                ORDER BY updated_at DESC, task_id DESC
                """,
                (sha256, normalized_filename),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_inspection(self, record: dict[str, Any]) -> None:
        values = dict(record)
        values["duplicate_json"] = json.dumps(values.pop("duplicate"), ensure_ascii=False, sort_keys=True)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self._connect() as db:
            db.execute(
                f"INSERT INTO inspections ({columns}) VALUES ({placeholders})",  # noqa: S608
                tuple(values.values()),
            )

    def get_inspection(self, inspection_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM inspections WHERE inspection_id = ?", (inspection_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["duplicate"] = json.loads(record.pop("duplicate_json"))
        return record

    def create_task(self, record: dict[str, Any]) -> None:
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        with self._connect() as db:
            db.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",  # noqa: S608
                tuple(record.values()),
            )

    def get_task_by_idempotency(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE requested_by = ? AND idempotency_key = ?",
                (requested_by, idempotency_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def create_task_idempotent(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Atomically create a task or return a concurrent identical-key task."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM tasks WHERE requested_by = ? AND idempotency_key = ?",
                (record["requested_by"], record["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                return dict(existing), True
            columns = ", ".join(record)
            placeholders = ", ".join("?" for _ in record)
            db.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",  # noqa: S608
                tuple(record.values()),
            )
            created = db.execute("SELECT * FROM tasks WHERE task_id = ?", (record["task_id"],)).fetchone()
            assert created is not None
            return dict(created), False

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def update_task_status(self, task_id: str, *, status: str, message_zh: str, updated_at: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE tasks SET status = ?, message_zh = ?, updated_at = ? WHERE task_id = ?",
                (status, message_zh, updated_at, task_id),
            )

    def claim_next_task(self, *, worker_id: str, updated_at: str, task_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tasks WHERE status = 'queued' AND (? IS NULL OR task_id=?) ORDER BY created_at, task_id LIMIT 1",
                (task_id, task_id),
            ).fetchone()
            if row is None:
                return None
            cursor = db.execute(
                """
                UPDATE tasks
                SET status = 'preparing', progress_percent = 5, current_stage = 'preparing',
                    started_at = COALESCE(started_at, ?), worker_id = ?, heartbeat_at = ?,
                    updated_at = ?, message_zh = ?
                WHERE task_id = ? AND status = 'queued'
                """,
                (updated_at, worker_id, updated_at, updated_at, "正在准备本机隔离转换批次", row["task_id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = db.execute("SELECT * FROM tasks WHERE task_id = ?", (row["task_id"],)).fetchone()
            return dict(claimed) if claimed is not None else None

    def update_task_progress(
        self,
        task_id: str,
        *,
        status: str,
        progress_percent: int | None,
        current_stage: str,
        message_zh: str,
        updated_at: str,
        batch_relpath: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE tasks
                SET status = ?, progress_percent = ?, current_stage = ?, message_zh = ?,
                    updated_at = ?, heartbeat_at = ?, batch_relpath = COALESCE(?, batch_relpath)
                WHERE task_id = ?
                """,
                (
                    status,
                    progress_percent,
                    current_stage,
                    message_zh,
                    updated_at,
                    updated_at,
                    batch_relpath,
                    task_id,
                ),
            )

    def finish_task(
        self,
        task_id: str,
        *,
        status: str,
        message_zh: str,
        updated_at: str,
        quality: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        quality_json = json.dumps(quality, ensure_ascii=False, sort_keys=True) if quality is not None else None
        with self._connect() as db:
            db.execute(
                """
                UPDATE tasks
                SET status = ?, progress_percent = 100, current_stage = ?, message_zh = ?,
                    updated_at = ?, heartbeat_at = ?, finished_at = ?, quality_json = ?, error_code = ?
                WHERE task_id = ?
                """,
                (status, status, message_zh, updated_at, updated_at, updated_at, quality_json, error_code, task_id),
            )

    def request_cancel(self, task_id: str, *, message_zh: str, updated_at: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE tasks
                SET cancel_requested = 1, status = 'cancelling', current_stage = 'cancelling',
                    message_zh = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (message_zh, updated_at, task_id),
            )

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT cancel_requested FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def count_queued_before(self, task_id: str) -> int:
        with self._connect() as db:
            task = db.execute("SELECT created_at FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if task is None:
                return 0
            row = db.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE status = 'queued' AND created_at < ?",
                (task["created_at"],),
            ).fetchone()
        return int(row["count"])

    def replace_artifacts(self, task_id: str, artifacts: list[dict[str, Any]]) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM artifacts WHERE task_id = ?", (task_id,))
            for record in artifacts:
                columns = ", ".join(record)
                placeholders = ", ".join("?" for _ in record)
                db.execute(
                    f"INSERT INTO artifacts ({columns}) VALUES ({placeholders})",  # noqa: S608
                    tuple(record.values()),
                )

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY purpose, relative_path",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_interrupted_tasks(self, *, updated_at: str) -> int:
        interrupted = (
            "preparing",
            "preprocessing",
            "converting",
            "quality_checking",
            "delivering",
            "cancelling",
            "running",
        )
        placeholders = ", ".join("?" for _ in interrupted)
        with self._connect() as db:
            cursor = db.execute(
                f"""
                UPDATE tasks
                SET status = 'needs_review',
                    message_zh = ?,
                    updated_at = ?
                WHERE status IN ({placeholders})
                """,  # noqa: S608
                (
                    "服务重启后发现任务曾在执行中，未继续标记为运行；请检查本机工件后决定是否重试",
                    updated_at,
                    *interrupted,
                ),
            )
        return cursor.rowcount

    def record_document(self, record: dict[str, Any]) -> None:
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        updates = ", ".join(f"{column}=excluded.{column}" for column in record if column != "task_id")
        with self._connect() as db:
            db.execute(
                f"""INSERT INTO documents ({columns}) VALUES ({placeholders})
                ON CONFLICT(task_id) DO UPDATE SET {updates}""",  # noqa: S608
                tuple(record.values()),
            )
