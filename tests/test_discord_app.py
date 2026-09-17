from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    import discord
except ImportError:  # Offline stdlib-only run; CI and smoke tests install runtime deps.
    discord = None

from tests.helpers import make_settings
from voicebot.discord_app import create_bot
from voicebot.models import Memo, MemoStatus, SummaryData
from voicebot.service import ProcessingResult


class FakeDM:
    pass


class FakeStatusMessage:
    def __init__(self, fail_edits=False):
        self.edits = []
        self.fail_edits = fail_edits

    async def edit(self, *, content):
        self.edits.append(content)
        if self.fail_edits:
            raise RuntimeError("Discord edit failed")


class FakeAttachment:
    def __init__(self, attachment_id):
        self.id = attachment_id
        self.filename = f"voice-{attachment_id}.ogg"
        self.content_type = "audio/ogg"
        self.size = 4

    async def save(self, path):
        path.write_bytes(b"OggS")


class FakeMessage:
    def __init__(self, attachments):
        self.author = type("Author", (), {"id": 123, "name": "user", "bot": False})()
        self.channel = FakeDM()
        self.attachments = attachments
        self.id = 100
        self.created_at = datetime(2026, 9, 17, tzinfo=UTC)
        self.replies = []

    async def reply(self, content):
        status = FakeStatusMessage(fail_edits=len(self.replies) == 0)
        self.replies.append((content, status))
        return status


class FakeDatabase:
    def get_memo(self, memo_id):
        return None


class FakeObsidian:
    async def flush(self):
        return 0


class FakeService:
    def __init__(self, message):
        self.message = message
        self.accepted = []
        self.in_flight_count = 0

    def validate(self, incoming):
        return None

    async def accept(self, incoming, saver, progress):
        # All attachments must be acknowledged before the first slow job starts.
        assert len(self.message.replies) == len(self.message.attachments)
        self.accepted.append(incoming.attachment_id)
        await progress("transcribing")
        summary = SummaryData("Done")
        memo = Memo(
            memo_id=incoming.memo_id,
            message_id=incoming.message_id,
            attachment_id=incoming.attachment_id,
            discord_id=incoming.discord_id,
            username=incoming.username,
            original_filename=incoming.filename,
            content_type=incoming.content_type,
            size_bytes=incoming.size_bytes,
            received_at=incoming.received_at.isoformat(),
            status=MemoStatus.COMPLETED.value,
            summary_json=summary.to_json(),
            summary_text=summary.summary,
        )
        return ProcessingResult(memo=memo, created=True, obsidian_synced=False)

    async def resume_incomplete(self):
        return 0


@unittest.skipIf(discord is None, "discord.py is not installed")
class DiscordAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_attachment_acknowledges_first_and_edit_failure_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory), ALLOWED_USER_IDS="123")
            message = FakeMessage([FakeAttachment(200), FakeAttachment(201)])
            service = FakeService(message)
            bot = create_bot(settings, FakeDatabase(), service, FakeObsidian())
            bot.process_commands = AsyncMock()
            with patch.object(discord, "DMChannel", FakeDM):
                await bot.on_message(message)
            self.assertEqual(service.accepted, ["200", "201"])
            self.assertEqual(len(message.replies), 2)
            bot.process_commands.assert_awaited_once_with(message)
            await bot.close()

    async def test_unauthorized_user_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory), ALLOWED_USER_IDS="999")
            message = FakeMessage([FakeAttachment(200)])
            service = FakeService(message)
            bot = create_bot(settings, FakeDatabase(), service, FakeObsidian())
            bot.process_commands = AsyncMock()
            with patch.object(discord, "DMChannel", FakeDM):
                await bot.on_message(message)
            self.assertEqual(message.replies, [])
            bot.process_commands.assert_not_awaited()
            await bot.close()


if __name__ == "__main__":
    unittest.main()
