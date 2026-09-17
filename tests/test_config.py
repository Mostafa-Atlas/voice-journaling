from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicebot.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_paths_are_rooted_at_project_not_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings.load(
                {
                    "DISCORD_TOKEN": "token",
                    "GROQ_API_KEY": "key",
                    "ALLOWED_USER_IDS": "123, 456;789",
                    "TIMEZONE": "UTC",
                },
                project_root=root,
            )
            self.assertEqual(settings.data_root, root.resolve())
            self.assertEqual(settings.voice_log_dir, (root / "voice_logs").resolve())
            self.assertEqual(settings.allowed_users, frozenset({123, 456, 789}))

    def test_missing_secrets_have_clear_errors(self):
        with self.assertRaisesRegex(ConfigurationError, "DISCORD_TOKEN"):
            Settings.load({"TIMEZONE": "UTC"})

    def test_invalid_numbers_are_rejected(self):
        env = {
            "DISCORD_TOKEN": "token",
            "GROQ_API_KEY": "key",
            "ALLOWED_USER_IDS": "123",
            "TIMEZONE": "UTC",
            "MAX_CONCURRENT_JOBS": "0",
        }
        with self.assertRaisesRegex(ConfigurationError, "MAX_CONCURRENT_JOBS"):
            Settings.load(env)

    def test_secretless_maintenance_configuration(self):
        settings = Settings.load({"TIMEZONE": "UTC"}, require_secrets=False)
        self.assertEqual(settings.allowed_users, frozenset())

    def test_obsidian_subfolder_cannot_escape_vault(self):
        env = {
            "DISCORD_TOKEN": "token",
            "GROQ_API_KEY": "key",
            "ALLOWED_USER_IDS": "123",
            "TIMEZONE": "UTC",
            "OBSIDIAN_SUBFOLDER": "../outside",
        }
        with self.assertRaisesRegex(ConfigurationError, "OBSIDIAN_SUBFOLDER"):
            Settings.load(env)


if __name__ == "__main__":
    unittest.main()
