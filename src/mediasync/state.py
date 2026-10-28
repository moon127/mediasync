from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from .models import TitleFingerprint
from .util import ensure_dir, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    name TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    last_source_titles INTEGER NOT NULL DEFAULT 0,
    last_target_titles INTEGER NOT NULL DEFAULT 0,
    last_run TEXT,
    last_status TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    library TEXT NOT NULL,
    mode TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    status TEXT NOT NULL,
    plan_id TEXT,
    summary TEXT,
    files_total INTEGER NOT NULL DEFAULT 0,
    files_done INTEGER NOT NULL DEFAULT 0,
    bytes_total INTEGER NOT NULL DEFAULT 0,
    bytes_done INTEGER NOT NULL DEFAULT 0,
    current TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT,
    library TEXT,
    action TEXT NOT NULL,
    path TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS trash (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library TEXT NOT NULL,
    run_id TEXT,
    original_path TEXT NOT NULL,
    trash_path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    entry_type TEXT NOT NULL DEFAULT 'file',
    reason TEXT,
    deleted_at TEXT NOT NULL,
    restored_at TEXT,
    purged_at TEXT
);

CREATE TABLE IF NOT EXISTS title_cache (
    library TEXT NOT NULL,
    title TEXT NOT NULL,
    source_mtime_ns INTEGER,
    target_mtime_ns INTEGER,
    source_count INTEGER NOT NULL DEFAULT 0,
    target_count INTEGER NOT NULL DEFAULT 0,
    source_digest TEXT,
    target_digest TEXT,
    clean INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    last_scan TEXT,
    PRIMARY KEY (library, title)
);

CREATE INDEX IF NOT EXISTS idx_trash_library ON trash(library);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit(run_id);
"""


class State:
    def __init__(self, state_dir: str):
        ensure_dir(state_dir)
        self.path = os.path.join(state_dir, "mediasync.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(title_cache)")
        }
        for name, declaration in (
            ("source_digest", "TEXT"),
            ("target_digest", "TEXT"),
        ):
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE title_cache ADD COLUMN {name} {declaration}"
                )
        run_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(runs)")
        }
        for name, declaration in (
            ("files_total", "INTEGER NOT NULL DEFAULT 0"),
            ("files_done", "INTEGER NOT NULL DEFAULT 0"),
            ("bytes_total", "INTEGER NOT NULL DEFAULT 0"),
            ("bytes_done", "INTEGER NOT NULL DEFAULT 0"),
            ("current", "TEXT"),
        ):
            if name not in run_columns:
                self.conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> State:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def upsert_library(
        self,
        name: str,
        source: str,
        target: str,
        source_titles: int | None = None,
        target_titles: int | None = None,
        last_run: str | None = None,
        last_status: str | None = None,
    ) -> None:
        existing = self.get_library(name)
        if existing is None:
            self.conn.execute(
                "INSERT INTO libraries(name, source, target, last_source_titles, "
                "last_target_titles, last_run, last_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    source,
                    target,
                    source_titles or 0,
                    target_titles or 0,
                    last_run,
                    last_status,
                ),
            )
        else:
            self.conn.execute(
                "UPDATE libraries SET source=?, target=?, "
                "last_source_titles=COALESCE(?, last_source_titles), "
                "last_target_titles=COALESCE(?, last_target_titles), "
                "last_run=COALESCE(?, last_run), "
                "last_status=COALESCE(?, last_status) WHERE name=?",
                (
                    source,
                    target,
                    source_titles,
                    target_titles,
                    last_run,
                    last_status,
                    name,
                ),
            )
        self.conn.commit()

    def get_library(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM libraries WHERE name=?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def start_run(self, run_id: str, library: str, mode: str, plan_id: str | None) -> None:
        self.conn.execute(
            "INSERT INTO runs(id, library, mode, started, status, plan_id, files_total, files_done, bytes_total, bytes_done) "
            "VALUES (?, ?, ?, ?, 'running', ?, 0, 0, 0, 0)",
            (run_id, library, mode, now_iso(), plan_id),
        )
        self.conn.commit()

    def finish_run(
        self,
        run_id: str,
        status: str,
        summary: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET finished=?, status=?, summary=? WHERE id=?",
            (now_iso(), status, json.dumps(summary or {}, default=str), run_id),
        )
        self.conn.commit()

    def audit(
        self,
        run_id: str | None,
        library: str | None,
        action: str,
        path: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO audit(ts, run_id, library, action, path, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), run_id, library, action, path, detail),
        )
        self.conn.commit()

    def add_trash(
        self,
        library: str,
        run_id: str,
        original_path: str,
        trash_path: str,
        size: int,
        entry_type: str,
        reason: str,
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO trash(library, run_id, original_path, trash_path, size, "
            "entry_type, reason, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                library,
                run_id,
                original_path,
                trash_path,
                size,
                entry_type,
                reason,
                now_iso(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_trash(
        self, library: str | None = None, include_done: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM trash"
        clauses: list[str] = []
        params: list[Any] = []
        if library:
            clauses.append("library=?")
            params.append(library)
        if not include_done:
            clauses.append("restored_at IS NULL AND purged_at IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY deleted_at DESC, id DESC"
        return [dict(row) for row in self.conn.execute(query, params)]

    def get_trash(self, trash_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM trash WHERE id=?", (trash_id,)
        ).fetchone()
        return dict(row) if row else None

    def mark_trash_restored(self, trash_id: int) -> None:
        self.conn.execute(
            "UPDATE trash SET restored_at=? WHERE id=?", (now_iso(), trash_id)
        )
        self.conn.commit()

    def mark_trash_purged(self, trash_id: int) -> None:
        self.conn.execute(
            "UPDATE trash SET purged_at=? WHERE id=?", (now_iso(), trash_id)
        )
        self.conn.commit()

    def get_title_cache(self, library: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM title_cache WHERE library=?", (library,)
        ).fetchall()
        return {row["title"]: dict(row) for row in rows}

    def set_title_cache(
        self,
        library: str,
        title: str,
        fingerprint: TitleFingerprint,
        clean: bool,
        size: int = 0,
        file_count: int = 0,
    ) -> None:
        self.conn.execute(
            "INSERT INTO title_cache(library, title, source_mtime_ns, target_mtime_ns, "
            "source_count, target_count, source_digest, target_digest, clean, size, "
            "file_count, last_scan) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(library, title) DO UPDATE SET "
            "source_mtime_ns=excluded.source_mtime_ns, "
            "target_mtime_ns=excluded.target_mtime_ns, "
            "source_count=excluded.source_count, "
            "target_count=excluded.target_count, "
            "source_digest=excluded.source_digest, "
            "target_digest=excluded.target_digest, "
            "clean=excluded.clean, size=excluded.size, "
            "file_count=excluded.file_count, last_scan=excluded.last_scan",
            (
                library,
                title,
                fingerprint.source_mtime_ns,
                fingerprint.target_mtime_ns,
                fingerprint.source_count,
                fingerprint.target_count,
                fingerprint.source_digest,
                fingerprint.target_digest,
                1 if clean else 0,
                size,
                file_count,
                now_iso(),
            ),
        )
        self.conn.commit()

    def set_title_clean(self, library: str, title: str, clean: bool) -> None:
        self.conn.execute(
            "UPDATE title_cache SET clean=? WHERE library=? AND title=?",
            (1 if clean else 0, library, title),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 10, library: str | None = None) -> list[dict[str, Any]]:
        if library:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE library=? ORDER BY started DESC LIMIT ?",
                (library, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def trash_summary(self, library: str | None = None) -> dict[str, int]:
        query = (
            "SELECT COUNT(*) AS entries, COALESCE(SUM(size), 0) AS size "
            "FROM trash WHERE restored_at IS NULL AND purged_at IS NULL"
        )
        params: list[Any] = []
        if library:
            query += " AND library=?"
            params.append(library)
        row = self.conn.execute(query, params).fetchone()
        return {"entries": int(row["entries"]), "size": int(row["size"])}


    def update_run_progress(
        self,
        run_id: str,
        *,
        library: str = "",
        plan_id: str = "",
        files_total: int = 0,
        files_done: int = 0,
        bytes_total: int = 0,
        bytes_done: int = 0,
        current: str = "",
        status: str = "running",
        mode: str = "apply",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO runs(id, library, mode, started, finished, status, plan_id, summary, files_total, files_done, bytes_total, bytes_done, current)
            VALUES(?, ?, ?, datetime('now'), NULL, ?, ?, '', ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                files_total=excluded.files_total,
                files_done=excluded.files_done,
                bytes_total=excluded.bytes_total,
                bytes_done=excluded.bytes_done,
                current=excluded.current,
                plan_id=COALESCE(NULLIF(excluded.plan_id,''), runs.plan_id)
            """,
            (
                run_id,
                library,
                mode,
                status,
                plan_id,
                files_total,
                files_done,
                bytes_total,
                bytes_done,
                current,
            ),
        )
        self.conn.commit()

    def get_run_progress(self, run_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT id, library, mode, started, finished, status, plan_id, summary, files_total, files_done, bytes_total, bytes_done, current FROM runs WHERE id=?",
            (run_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)

    def active_run_progress(self, library: str | None = None) -> dict[str, Any] | None:
        if library:
            cur = self.conn.execute(
                "SELECT id, library, mode, started, finished, status, plan_id, summary, files_total, files_done, bytes_total, bytes_done, current FROM runs WHERE library=? AND finished IS NULL AND status IN ('running','apply','run') ORDER BY started DESC LIMIT 1",
                (library,),
            )
        else:
            cur = self.conn.execute(
                "SELECT id, library, mode, started, finished, status, plan_id, summary, files_total, files_done, bytes_total, bytes_done, current FROM runs WHERE finished IS NULL AND status IN ('running','apply','run') ORDER BY started DESC LIMIT 1",
            )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)

    def clear_stale_runs(self, max_age_minutes: int = 60) -> int:
        try:
            from datetime import datetime, timedelta

            cutoff = (datetime.utcnow() - timedelta(minutes=max_age_minutes)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
        except Exception:
            return 0
        cur = self.conn.execute(
            "UPDATE runs SET status='cancelled', finished=? WHERE finished IS NULL AND status IN ('running','apply','run') AND started < ?",
            (cutoff, cutoff),
        )
        self.conn.commit()
        return cur.rowcount
