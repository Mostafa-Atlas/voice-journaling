from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicebot.models import Memo, SummaryData
from voicebot.storage import FileStorage, UnsafePathError, atomic_write, safe_component


def memo(**overrides) -> Memo:
    values = dict(
        memo_id="discord-1-2",
        message_id="1",
        attachment_id="2",
        discord_id="123",
        username="user[name]",
        original_filename="../unsafe name.OGG",
        content_type="audio/ogg",
        size_bytes=12,
        received_at="2026-09-17T14:05:00+03:00",
        status="summarized",
        transcript="hello [track](https://example.test)",
        summary_json=SummaryData("Useful summary").to_json(),
        summary_text="Useful summary",
    )
    values.update(overrides)
    return Memo(**values)


class StorageTests(unittest.TestCase):
    def test_paths_are_controlled_and_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = FileStorage(root, root / "voice_logs")
            temporary, final = storage.audio_paths(memo())
            self.assertEqual(final.name, "audio.ogg")
            self.assertTrue(final.is_relative_to(root / "voice_logs"))
            self.assertNotIn("unsafe", str(final))

    def test_root_escape_is_rejected_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside-voicebot-test"
            with self.assertRaises(UnsafePathError):
                FileStorage(root, outside)
            self.assertFalse(outside.exists())

    def test_atomic_artifacts_and_idempotent_daily_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = FileStorage(root, root / "voice_logs")
            item = memo()
            transcript, summary = storage.write_artifacts(item, item.summary)
            updated = memo(transcript_path=transcript, summary_path=summary)
            first = storage.rebuild_daily_index("2026-09-17", [updated])
            second = storage.rebuild_daily_index("2026-09-17", [updated])
            self.assertEqual(first, second)
            text = storage.resolve(first).read_text(encoding="utf-8")
            self.assertEqual(text.count("voice-memo:discord-1-2"), 1)
            transcript_text = storage.resolve(transcript).read_text(encoding="utf-8")
            self.assertNotIn("[track](https://example.test)", transcript_text)

    def test_atomic_write_preserves_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.md"
            atomic_write(path, "مرحبا\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "مرحبا\n")

    def test_safe_component_handles_windows_reserved_names(self):
        self.assertEqual(safe_component("../CON"), "_CON")
        self.assertNotIn("/", safe_component("../../evil/name"))

    def test_truncated_audio_is_rejected_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = FileStorage(root, root / "voice_logs")
            temporary, final = storage.audio_paths(memo(size_bytes=10))
            temporary.write_bytes(b"short")
            with self.assertRaisesRegex(OSError, "size mismatch"):
                storage.finalize_audio(temporary, final, 10)
            self.assertFalse(temporary.exists())
            self.assertFalse(final.exists())


if __name__ == "__main__":
    unittest.main()
