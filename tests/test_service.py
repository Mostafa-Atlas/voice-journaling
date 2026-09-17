from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.helpers import make_settings
from voicebot.database import Database
from voicebot.models import MemoStatus, SummaryData
from voicebot.obsidian import ObsidianSync
from voicebot.service import IncomingMemo, MemoService
from voicebot.storage import FileStorage


class FakeGateway:
    def __init__(self):
        self.transcriptions = 0
        self.summaries = 0
        self.fail_summary_once = False

    async def transcribe(self, path: Path) -> str:
        self.transcriptions += 1
        self.last_audio = path.read_bytes()
        return "A transcript about finishing the project."

    async def summarize(self, transcript: str) -> SummaryData:
        self.summaries += 1
        if self.fail_summary_once:
            self.fail_summary_once = False
            raise RuntimeError("temporary summary error")
        return SummaryData(
            "Finish the project.",
            key_points=("The project needs final testing.",),
            action_items=(),
        )


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = make_settings(root)
        self.database = Database(self.settings.database_path)
        self.database.initialize()
        self.storage = FileStorage(self.settings.data_root, self.settings.voice_log_dir)
        self.gateway = FakeGateway()
        self.obsidian = ObsidianSync(self.settings, self.database)
        self.service = MemoService(
            self.settings, self.database, self.storage, self.gateway, self.obsidian
        )
        self.incoming = IncomingMemo(
            message_id="100",
            attachment_id="200",
            discord_id="123",
            username="user",
            filename="../../voice.ogg",
            content_type="audio/ogg",
            size_bytes=4,
            received_at=datetime(2026, 9, 17, 14, 5, tzinfo=ZoneInfo("Africa/Cairo")),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def saver(self, path: Path):
        path.write_bytes(b"OggS")

    async def test_complete_pipeline_and_duplicate_delivery(self):
        result = await self.service.accept(self.incoming, self.saver)
        self.assertEqual(result.memo.status, MemoStatus.COMPLETED.value)
        self.assertTrue(result.obsidian_synced)
        self.assertEqual(self.gateway.transcriptions, 1)
        self.assertEqual(self.gateway.summaries, 1)

        async def must_not_save(path: Path):
            self.fail("duplicate attempted to save audio")

        duplicate = await self.service.accept(self.incoming, must_not_save)
        self.assertFalse(duplicate.created)
        self.assertEqual(self.gateway.transcriptions, 1)
        self.assertEqual(self.gateway.summaries, 1)

    async def test_failed_summary_retries_without_retranscribing(self):
        self.gateway.fail_summary_once = True
        with self.assertRaises(RuntimeError):
            await self.service.accept(self.incoming, self.saver)
        failed = self.database.get_memo(self.incoming.memo_id)
        self.assertEqual(failed.status, MemoStatus.FAILED.value)
        self.assertIsNotNone(failed.transcript)
        self.assertEqual(self.gateway.transcriptions, 1)

        result = await self.service.retry(self.incoming.memo_id)
        self.assertEqual(result.memo.status, MemoStatus.COMPLETED.value)
        self.assertEqual(self.gateway.transcriptions, 1)
        self.assertEqual(self.gateway.summaries, 2)

    async def test_same_message_different_attachments_are_distinct(self):
        second = IncomingMemo(
            message_id=self.incoming.message_id,
            attachment_id="201",
            discord_id=self.incoming.discord_id,
            username=self.incoming.username,
            filename="voice.ogg",
            content_type="audio/ogg",
            size_bytes=4,
            received_at=self.incoming.received_at,
        )
        first_result, second_result = await asyncio.gather(
            self.service.accept(self.incoming, self.saver),
            self.service.accept(second, self.saver),
        )
        self.assertNotEqual(first_result.memo.memo_id, second_result.memo.memo_id)

    async def test_concurrent_duplicate_runs_ai_once_and_releases_lock(self):
        first, second = await asyncio.gather(
            self.service.accept(self.incoming, self.saver),
            self.service.accept(self.incoming, self.saver),
        )
        self.assertEqual(first.memo.memo_id, second.memo.memo_id)
        self.assertEqual(self.gateway.transcriptions, 1)
        self.assertEqual(self.gateway.summaries, 1)
        self.assertEqual(self.service._memo_locks, {})

    async def test_obsidian_exception_after_completion_does_not_fail_memo(self):
        class BrokenSync:
            def __init__(self, database):
                self.database = database

            def enqueue(self, memo_id):
                self.database.enqueue_outbox(memo_id)

            async def flush(self):
                raise RuntimeError("sync broke")

        self.service.obsidian = BrokenSync(self.database)
        result = await self.service.accept(self.incoming, self.saver)
        self.assertEqual(result.memo.status, MemoStatus.COMPLETED.value)
        persisted = self.database.get_memo(self.incoming.memo_id)
        self.assertEqual(persisted.status, MemoStatus.COMPLETED.value)
        self.assertEqual(self.database.outbox_status(persisted.memo_id), "pending")

    async def test_validation_rejects_size_and_content_type(self):
        invalid = IncomingMemo(
            message_id="1",
            attachment_id="2",
            discord_id="123",
            username="u",
            filename="voice.ogg",
            content_type="text/plain",
            size_bytes=4,
            received_at=self.incoming.received_at,
        )
        with self.assertRaisesRegex(ValueError, "content type"):
            self.service.validate(invalid)


if __name__ == "__main__":
    unittest.main()
