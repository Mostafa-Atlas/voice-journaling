from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from voicebot.database import Database
from voicebot.models import MemoStatus, SummaryData


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "transcripts.db"
        self.database = Database(self.path)
        self.database.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def register(self, memo_id="discord-1-2"):
        return self.database.register_memo(
            memo_id=memo_id,
            message_id=memo_id + "-message",
            attachment_id=memo_id + "-attachment",
            discord_id="123",
            username="user",
            original_filename="voice.ogg",
            content_type="audio/ogg",
            size_bytes=20,
            received_at="2026-09-17T12:00:00+00:00",
        )

    def test_registration_is_idempotent(self):
        first, created_first = self.register()
        second, created_second = self.register()
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.memo_id, second.memo_id)

    def test_search_is_owner_scoped(self):
        memo, _ = self.register()
        summary = SummaryData("Plan the satellite launch")
        self.database.update_memo(
            memo.memo_id,
            transcript="Discuss the orbital checklist",
            summary_json=summary.to_json(),
            summary_text=summary.summary,
        )
        self.assertEqual(len(self.database.search_for_user("123", "satellite")), 1)
        self.assertEqual(self.database.search_for_user("999", "satellite"), [])

    def test_outbox_is_unique_and_tracks_delivery(self):
        memo, _ = self.register()
        self.database.enqueue_outbox(memo.memo_id)
        self.database.enqueue_outbox(memo.memo_id)
        rows = self.database.pending_outbox()
        self.assertEqual(len(rows), 1)
        self.database.mark_outbox_delivered(rows[0]["id"])
        self.assertEqual(self.database.outbox_status(memo.memo_id), "delivered")

    def test_legacy_table_is_migrated_without_removal(self):
        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "legacy.db"
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE transcripts (id INTEGER PRIMARY KEY, username TEXT, "
                "discord_id TEXT, timestamp TEXT, transcript TEXT, markdown_path TEXT, "
                "summary_path TEXT, audio_file TEXT)"
            )
            connection.execute(
                "INSERT INTO transcripts VALUES (1, 'user', '123', '2026-01-01 10:00:00', "
                "'hello', 'voice_logs/a/transcript.md', 'voice_logs/a/summary.md', 'a.ogg')"
            )
        database = Database(self.path)
        database.initialize()
        migrated = database.get_memo("legacy-1")
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.status, MemoStatus.COMPLETED.value)
        with database.connect() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='transcripts'"
                ).fetchone()
            )

    def test_integrity_and_wal(self):
        self.assertEqual(self.database.integrity_check(), "ok")
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_outbox_claim_is_exclusive(self):
        memo, _ = self.register()
        self.database.enqueue_outbox(memo.memo_id)
        first = self.database.claim_outbox("worker-a")
        second = self.database.claim_outbox("worker-b")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.database.mark_outbox_delivered(first["id"], "worker-a")

    def test_user_scoped_status_does_not_leak_other_users(self):
        self.register()
        self.database.register_memo(
            memo_id="other",
            message_id="other-message",
            attachment_id="other-attachment",
            discord_id="999",
            username="other",
            original_filename="voice.ogg",
            content_type="audio/ogg",
            size_bytes=1,
            received_at="2026-09-17T12:00:00+00:00",
        )
        self.assertEqual(self.database.status_counts("123").get("received"), 1)
        self.assertEqual(self.database.status_counts("999").get("received"), 1)

    def test_newer_schema_is_rejected(self):
        with self.database.connect() as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(RuntimeError, "newer"):
            self.database.initialize()


if __name__ == "__main__":
    unittest.main()
