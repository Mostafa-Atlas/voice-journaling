from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .models import Memo, MemoStatus, SummaryData


SCHEMA_VERSION = 2


class Database:
    def __init__(self, path: Path):
        self.path = path.resolve()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            self.path.chmod(0o600)
            self.path.parent.chmod(0o700)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists():
                    sidecar.chmod(0o600)
        except OSError:
            pass
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current_version} is newer than supported {SCHEMA_VERSION}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memos (
                    memo_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    discord_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    content_type TEXT,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    audio_path TEXT,
                    transcript TEXT,
                    summary_json TEXT,
                    summary_text TEXT,
                    transcript_path TEXT,
                    summary_path TEXT,
                    daily_index_path TEXT,
                    error_stage TEXT,
                    error_message TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    processing_ms INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(message_id, attachment_id)
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memo_id TEXT NOT NULL REFERENCES memos(memo_id) ON DELETE CASCADE,
                    destination TEXT NOT NULL DEFAULT 'obsidian',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    last_error TEXT,
                    delivered_at TEXT,
                    lease_owner TEXT,
                    lease_until TEXT,
                    UNIQUE(memo_id, destination)
                );

                CREATE INDEX IF NOT EXISTS idx_memos_user_recent
                    ON memos(discord_id, received_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memos_status
                    ON memos(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                    ON outbox(status, available_at, id);
                """
            )
            self._ensure_column(connection, "outbox", "lease_owner", "TEXT")
            self._ensure_column(connection, "outbox", "lease_until", "TEXT")
            required_memo_columns = set(Memo.__dataclass_fields__)
            actual_memo_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(memos)")
            }
            missing = required_memo_columns - actual_memo_columns
            if missing:
                raise RuntimeError(f"unsupported incomplete memos schema; missing {sorted(missing)}")
            self._create_fts(connection)
            self._migrate_legacy_transcripts(connection)
            self._migrate_legacy_queue(connection)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _create_fts(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memo_fts "
                "USING fts5(memo_id UNINDEXED, transcript, summary)"
            )
        except sqlite3.OperationalError:
            # Some minimal SQLite builds omit FTS5. Search falls back to LIKE.
            pass

    def _migrate_legacy_transcripts(self, connection: sqlite3.Connection) -> None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transcripts'"
        ).fetchone()
        if not exists:
            return
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transcripts)")
        }
        required = {"id", "username", "discord_id", "timestamp", "transcript"}
        if not required.issubset(columns):
            return
        rows = connection.execute("SELECT * FROM transcripts ORDER BY id").fetchall()
        for row in rows:
            values = dict(row)
            legacy_id = f"legacy-{values['id']}"
            markdown_path = values.get("markdown_path")
            audio_file = values.get("audio_file") or "legacy-audio"
            audio_path = None
            if markdown_path:
                audio_path = str(Path(markdown_path).parent / audio_file)
            received_at = values.get("timestamp") or _utc_now()
            connection.execute(
                """
                INSERT OR IGNORE INTO memos (
                    memo_id, message_id, attachment_id, discord_id, username,
                    original_filename, size_bytes, received_at, status, audio_path,
                    transcript, transcript_path, summary_path, completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_id,
                    legacy_id,
                    legacy_id,
                    str(values.get("discord_id") or "unknown"),
                    values.get("username") or "unknown",
                    audio_file,
                    received_at,
                    MemoStatus.COMPLETED.value,
                    audio_path,
                    values.get("transcript"),
                    markdown_path,
                    values.get("summary_path"),
                    received_at,
                    received_at,
                ),
            )
            self._sync_fts(connection, legacy_id)

    def _migrate_legacy_queue(self, connection: sqlite3.Connection) -> None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='obsidian_queue'"
        ).fetchone()
        if not exists:
            return
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(obsidian_queue)")
        }
        required = {"id", "date_str", "time_str", "audio_filename", "summary", "transcript"}
        if not required.issubset(columns):
            return
        for row in connection.execute("SELECT * FROM obsidian_queue ORDER BY id"):
            values = dict(row)
            memo_id = f"legacy-queue-{values['id']}"
            received_at = f"{values['date_str']}T{values['time_str']}"
            raw_summary = " ".join((values.get("summary") or "Legacy voice memo").split())
            summary = SummaryData(
                summary=(raw_summary[:4_000] or "Legacy voice memo queued for Obsidian")
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO memos (
                    memo_id, message_id, attachment_id, discord_id, username,
                    original_filename, size_bytes, received_at, status, transcript,
                    summary_json, summary_text, completed_at, updated_at
                ) VALUES (?, ?, ?, 'legacy', 'legacy', ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memo_id,
                    memo_id,
                    memo_id,
                    values.get("audio_filename") or "legacy-audio",
                    received_at,
                    MemoStatus.COMPLETED.value,
                    values.get("transcript") or "Legacy transcript unavailable.",
                    summary.to_json(),
                    summary.summary,
                    values.get("queued_at") or received_at,
                    values.get("queued_at") or received_at,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox (
                    memo_id, destination, status, available_at
                ) VALUES (?, 'obsidian', 'pending', ?)
                """,
                (memo_id, _utc_now()),
            )
            self._sync_fts(connection, memo_id)

    def register_memo(
        self,
        *,
        memo_id: str,
        message_id: str,
        attachment_id: str,
        discord_id: str,
        username: str,
        original_filename: str,
        content_type: str | None,
        size_bytes: int,
        received_at: str,
    ) -> tuple[Memo, bool]:
        now = _utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO memos (
                    memo_id, message_id, attachment_id, discord_id, username,
                    original_filename, content_type, size_bytes, received_at,
                    status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memo_id,
                    message_id,
                    attachment_id,
                    discord_id,
                    username,
                    original_filename,
                    content_type,
                    size_bytes,
                    received_at,
                    MemoStatus.RECEIVED.value,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM memos WHERE message_id=? AND attachment_id=?",
                (message_id, attachment_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("memo registration failed")
        return _memo(row), created

    def get_memo(self, memo_id: str) -> Memo | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memos WHERE memo_id=?", (memo_id,)
            ).fetchone()
        return _memo(row) if row else None

    def update_memo(self, memo_id: str, **fields: Any) -> Memo:
        allowed = {
            "status",
            "audio_path",
            "transcript",
            "summary_json",
            "summary_text",
            "transcript_path",
            "summary_path",
            "daily_index_path",
            "error_stage",
            "error_message",
            "attempt_count",
            "completed_at",
            "processing_ms",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported memo fields: {sorted(unknown)}")
        fields["updated_at"] = _utc_now()
        assignments = ", ".join(f"{name}=?" for name in fields)
        values = list(fields.values()) + [memo_id]
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE memos SET {assignments} WHERE memo_id=?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(memo_id)
            if "transcript" in fields or "summary_text" in fields:
                self._sync_fts(connection, memo_id)
            row = connection.execute(
                "SELECT * FROM memos WHERE memo_id=?", (memo_id,)
            ).fetchone()
        if row is None:
            raise KeyError(memo_id)
        return _memo(row)

    def increment_attempt(self, memo_id: str) -> Memo:
        with self.connect() as connection:
            connection.execute(
                "UPDATE memos SET attempt_count=attempt_count+1, updated_at=? WHERE memo_id=?",
                (_utc_now(), memo_id),
            )
            row = connection.execute(
                "SELECT * FROM memos WHERE memo_id=?", (memo_id,)
            ).fetchone()
        if row is None:
            raise KeyError(memo_id)
        return _memo(row)

    def record_failure(self, memo_id: str, stage: str, message: str) -> Memo:
        return self.update_memo(
            memo_id,
            status=MemoStatus.FAILED.value,
            error_stage=stage[:100],
            error_message=_sanitize_error(message),
        )

    def recent_for_user(self, discord_id: str, limit: int = 5) -> list[Memo]:
        limit = max(1, min(int(limit), 20))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memos WHERE discord_id=? "
                "ORDER BY received_at DESC, memo_id DESC LIMIT ?",
                (discord_id, limit),
            ).fetchall()
        return [_memo(row) for row in rows]

    def search_for_user(self, discord_id: str, query: str, limit: int = 10) -> list[Memo]:
        query = " ".join(query.split()).strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 20))
        with self.connect() as connection:
            try:
                tokens = [token.replace('"', '""') for token in query.split()]
                fts_query = " AND ".join(f'"{token}"' for token in tokens)
                rows = connection.execute(
                    """
                    SELECT m.* FROM memo_fts f
                    JOIN memos m ON m.memo_id=f.memo_id
                    WHERE memo_fts MATCH ? AND m.discord_id=?
                    ORDER BY m.received_at DESC LIMIT ?
                    """,
                    (fts_query, discord_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                pattern = f"%{query}%"
                rows = connection.execute(
                    "SELECT * FROM memos WHERE discord_id=? "
                    "AND (transcript LIKE ? OR summary_text LIKE ?) "
                    "ORDER BY received_at DESC LIMIT ?",
                    (discord_id, pattern, pattern, limit),
                ).fetchall()
        return [_memo(row) for row in rows]

    def memos_for_date(self, date_text: str) -> list[Memo]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memos WHERE substr(received_at, 1, 10)=? "
                "AND transcript_path IS NOT NULL AND summary_path IS NOT NULL "
                "ORDER BY received_at, memo_id",
                (date_text,),
            ).fetchall()
        return [_memo(row) for row in rows]

    def incomplete_memos(self) -> list[Memo]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memos WHERE status NOT IN (?, ?) AND audio_path IS NOT NULL "
                "ORDER BY updated_at",
                (MemoStatus.COMPLETED.value, MemoStatus.FAILED.value),
            ).fetchall()
        return [_memo(row) for row in rows]

    def mark_orphaned_received(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memos
                SET status=?, error_stage='audio_save',
                    error_message='Original attachment must be resent', updated_at=?
                WHERE status=? AND audio_path IS NULL
                """,
                (MemoStatus.FAILED.value, _utc_now(), MemoStatus.RECEIVED.value),
            )
        return cursor.rowcount

    def enqueue_outbox(self, memo_id: str, destination: str = "obsidian") -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO outbox (memo_id, destination, status, available_at)
                VALUES (?, ?, 'pending', ?)
                ON CONFLICT(memo_id, destination) DO UPDATE SET
                    status=CASE WHEN outbox.status='delivered' THEN 'delivered' ELSE 'pending' END,
                    available_at=CASE WHEN outbox.status='delivered' THEN outbox.available_at ELSE excluded.available_at END,
                    lease_owner=CASE WHEN outbox.status='delivered' THEN outbox.lease_owner ELSE NULL END,
                    lease_until=CASE WHEN outbox.status='delivered' THEN outbox.lease_until ELSE NULL END
                """,
                (memo_id, destination, _utc_now()),
            )

    def pending_outbox(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT o.*, m.* FROM outbox o JOIN memos m USING(memo_id) "
                "WHERE o.status='pending' AND o.available_at<=? "
                "ORDER BY o.id LIMIT ?",
                (_utc_now(), limit),
            ).fetchall()

    def claim_outbox(self, worker_id: str, lease_seconds: int = 120) -> sqlite3.Row | None:
        now = datetime.now(UTC)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE outbox SET status='pending', lease_owner=NULL, lease_until=NULL "
                "WHERE status='delivering' AND lease_until<?",
                (now.isoformat(),),
            )
            row = connection.execute(
                "SELECT id FROM outbox WHERE status='pending' AND available_at<=? "
                "ORDER BY id LIMIT 1",
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            item_id = int(row["id"])
            updated = connection.execute(
                "UPDATE outbox SET status='delivering', lease_owner=?, lease_until=? "
                "WHERE id=? AND status='pending'",
                (worker_id, lease_until, item_id),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                "SELECT o.*, m.discord_id FROM outbox o "
                "JOIN memos m USING(memo_id) WHERE o.id=?",
                (item_id,),
            ).fetchone()
            connection.commit()
            return claimed

    def mark_outbox_delivered(self, item_id: int, worker_id: str | None = None) -> None:
        with self.connect() as connection:
            if worker_id is None:
                connection.execute(
                    "UPDATE outbox SET status='delivered', delivered_at=?, last_error=NULL, "
                    "lease_owner=NULL, lease_until=NULL WHERE id=?",
                    (_utc_now(), item_id),
                )
            else:
                connection.execute(
                    "UPDATE outbox SET status='delivered', delivered_at=?, last_error=NULL, "
                    "lease_owner=NULL, lease_until=NULL "
                    "WHERE id=? AND status='delivering' AND lease_owner=?",
                    (_utc_now(), item_id, worker_id),
                )

    def mark_outbox_error(
        self, item_id: int, attempts: int, error: str, worker_id: str | None = None
    ) -> None:
        delay = min(60 * (2 ** min(attempts, 6)), 3_600)
        available = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        with self.connect() as connection:
            condition = "id=?" if worker_id is None else "id=? AND lease_owner=?"
            parameters: list[Any] = [
                attempts,
                available,
                _sanitize_error(error),
                item_id,
            ]
            if worker_id is not None:
                parameters.append(worker_id)
            connection.execute(
                "UPDATE outbox SET status='pending', attempts=?, available_at=?, last_error=?, "
                f"lease_owner=NULL, lease_until=NULL WHERE {condition}",
                parameters,
            )

    def mark_outbox_failed(
        self, item_id: int, attempts: int, error: str, worker_id: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE outbox SET status='failed', attempts=?, last_error=?, "
                "lease_owner=NULL, lease_until=NULL "
                "WHERE id=? AND lease_owner=?",
                (attempts, _sanitize_error(error), item_id, worker_id),
            )

    def outbox_counts(self, discord_id: str | None = None) -> dict[str, int]:
        with self.connect() as connection:
            if discord_id is None:
                rows = connection.execute(
                    "SELECT status, count(*) AS count FROM outbox GROUP BY status"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT o.status, count(*) AS count FROM outbox o "
                    "JOIN memos m USING(memo_id) WHERE m.discord_id=? GROUP BY o.status",
                    (discord_id,),
                ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def outbox_status(self, memo_id: str, destination: str = "obsidian") -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM outbox WHERE memo_id=? AND destination=?",
                (memo_id, destination),
            ).fetchone()
        return str(row["status"]) if row else None

    def status_counts(self, discord_id: str | None = None) -> dict[str, int]:
        with self.connect() as connection:
            if discord_id is None:
                rows = connection.execute(
                    "SELECT status, count(*) AS count FROM memos GROUP BY status"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT status, count(*) AS count FROM memos "
                    "WHERE discord_id=? GROUP BY status",
                    (discord_id,),
                ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def last_completed(self, discord_id: str | None = None) -> Memo | None:
        with self.connect() as connection:
            if discord_id is None:
                row = connection.execute(
                    "SELECT * FROM memos WHERE status=? ORDER BY completed_at DESC LIMIT 1",
                    (MemoStatus.COMPLETED.value,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM memos WHERE status=? AND discord_id=? "
                    "ORDER BY completed_at DESC LIMIT 1",
                    (MemoStatus.COMPLETED.value, discord_id),
                ).fetchone()
        return _memo(row) if row else None

    def retention_candidates(self, cutoff_iso: str) -> list[Memo]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.* FROM memos m
                WHERE m.status=? AND m.completed_at<?
                  AND NOT EXISTS (
                    SELECT 1 FROM outbox o
                    WHERE o.memo_id=m.memo_id AND o.status<>'delivered'
                  )
                ORDER BY m.completed_at
                """,
                (MemoStatus.COMPLETED.value, cutoff_iso),
            ).fetchall()
        return [_memo(row) for row in rows]

    def delete_memo(self, memo_id: str) -> None:
        with self.connect() as connection:
            try:
                connection.execute("DELETE FROM memo_fts WHERE memo_id=?", (memo_id,))
            except sqlite3.OperationalError:
                pass
            connection.execute("DELETE FROM memos WHERE memo_id=?", (memo_id,))

    def all_memos(self) -> Iterable[Memo]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM memos ORDER BY received_at").fetchall()
        return [_memo(row) for row in rows]

    def integrity_check(self) -> str:
        with self.connect() as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    def _sync_fts(self, connection: sqlite3.Connection, memo_id: str) -> None:
        try:
            connection.execute("DELETE FROM memo_fts WHERE memo_id=?", (memo_id,))
            row = connection.execute(
                "SELECT memo_id, transcript, summary_text FROM memos WHERE memo_id=?",
                (memo_id,),
            ).fetchone()
            if row:
                connection.execute(
                    "INSERT INTO memo_fts (memo_id, transcript, summary) VALUES (?, ?, ?)",
                    (row["memo_id"], row["transcript"] or "", row["summary_text"] or ""),
                )
        except sqlite3.OperationalError:
            pass


def _memo(row: sqlite3.Row) -> Memo:
    values = dict(row)
    fields = Memo.__dataclass_fields__
    return Memo(**{name: values.get(name) for name in fields})


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sanitize_error(value: str) -> str:
    text = " ".join(str(value).split())[:500]
    import re

    text = re.sub(r"\b(?:gsk|sk)_[A-Za-z0-9_-]{16,}\b", "<redacted>", text)
    text = re.sub(
        r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b",
        "<redacted>",
        text,
    )
    lowered = text.lower()
    for marker in ("bearer ", "api_key=", "token="):
        index = lowered.find(marker)
        if index >= 0:
            text = text[: index + len(marker)] + "<redacted>"
            break
    return text
