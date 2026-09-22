from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import dispose_database, make_settings
from voicebot.database import Database
from voicebot.models import MemoStatus, SummaryData
from voicebot.obsidian import ObsidianSync


class ObsidianTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temporary.name)
        self.settings = make_settings(root, OBSIDIAN_ENABLED="true")
        self.database = Database(self.settings.database_path)
        self.database.initialize()
        memo, _ = self.database.register_memo(
            memo_id="discord-1-2",
            message_id="1",
            attachment_id="2",
            discord_id="123",
            username="user",
            original_filename="voice`name.ogg",
            content_type="audio/ogg",
            size_bytes=10,
            received_at="2026-09-17T14:05:00+03:00",
        )
        summary = SummaryData("A safe summary")
        self.memo = self.database.update_memo(
            memo.memo_id,
            status=MemoStatus.COMPLETED.value,
            transcript="hello ![beacon](https://example.test)",
            summary_json=summary.to_json(),
            summary_text=summary.summary,
        )
        self.sync = ObsidianSync(self.settings, self.database)

    async def asyncTearDown(self):
        dispose_database(self.database)
        self.temporary.cleanup()

    async def test_delivery_is_idempotent_even_after_acknowledgement_loss(self):
        self.sync.enqueue(self.memo.memo_id)
        self.assertEqual(await self.sync.flush(), 1)
        note = self.settings.obsidian_dir / "2026-09-17.md"
        first = note.read_text(encoding="utf-8")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE outbox SET status='pending', delivered_at=NULL WHERE memo_id=?",
                (self.memo.memo_id,),
            )
        self.assertEqual(await self.sync.flush(), 1)
        second = note.read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(second.count("<!-- voice-memo:discord-1-2 -->"), 1)
        self.assertNotIn("![beacon]", second)

    async def test_disabled_obsidian_skips_queue(self):
        from voicebot.obsidian import ObsidianSync

        disabled = make_settings(Path(self.temporary.name), OBSIDIAN_ENABLED="false")
        sync = ObsidianSync(disabled, self.database)
        self.assertFalse(sync.available())
        sync.enqueue(self.memo.memo_id)
        self.assertEqual(await sync.flush(), 0)

    async def test_unavailable_vault_leaves_item_pending(self):
        missing_settings = make_settings(
            Path(self.temporary.name),
            OBSIDIAN_ENABLED="true",
            OBSIDIAN_VAULT_PATH=str(Path(self.temporary.name) / "missing"),
        )
        sync = ObsidianSync(missing_settings, self.database)
        sync.enqueue(self.memo.memo_id)
        self.assertEqual(await sync.flush(), 0)
        self.assertEqual(self.database.outbox_status(self.memo.memo_id), "pending")


if __name__ == "__main__":
    unittest.main()
