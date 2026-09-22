from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicebot.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_paths_are_rooted_at_project_not_cwd(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
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
            "OBSIDIAN_ENABLED": "true",
            "OBSIDIAN_VAULT_PATH": "/tmp/vault",
            "OBSIDIAN_SUBFOLDER": "../outside",
        }
        with self.assertRaisesRegex(ConfigurationError, "OBSIDIAN_SUBFOLDER"):
            Settings.load(env)

    def test_obsidian_defaults_to_disabled(self):
        settings = Settings.load(
            {
                "DISCORD_TOKEN": "token",
                "GROQ_API_KEY": "key",
                "ALLOWED_USER_IDS": "123",
            }
        )
        self.assertFalse(settings.obsidian_enabled)
        self.assertIsNone(settings.obsidian_vault_path)
        self.assertIsNone(settings.obsidian_dir)

    def test_obsidian_enabled_requires_vault_path(self):
        with self.assertRaisesRegex(ConfigurationError, "OBSIDIAN_VAULT_PATH"):
            Settings.load(
                {
                    "DISCORD_TOKEN": "token",
                    "GROQ_API_KEY": "key",
                    "ALLOWED_USER_IDS": "123",
                    "OBSIDIAN_ENABLED": "true",
                }
            )

    def test_openai_provider_requires_only_openai_key(self):
        settings = Settings.load(
            {
                "DISCORD_TOKEN": "token",
                "OPENAI_API_KEY": "openai-key",
                "ALLOWED_USER_IDS": "123",
                "STT_PROVIDER": "openai",
                "SUMMARY_PROVIDER": "openai",
            }
        )
        self.assertEqual(settings.stt_provider, "openai")
        self.assertEqual(settings.transcription_model, "whisper-1")
        self.assertIn("gpt", settings.summary_model)

    def test_groq_provider_requires_groq_key(self):
        with self.assertRaisesRegex(ConfigurationError, "GROQ_API_KEY"):
            Settings.load(
                {
                    "DISCORD_TOKEN": "token",
                    "ALLOWED_USER_IDS": "123",
                    "STT_PROVIDER": "groq",
                    "SUMMARY_PROVIDER": "groq",
                }
            )

    def test_invalid_provider_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "STT_PROVIDER"):
            Settings.load(
                {
                    "DISCORD_TOKEN": "token",
                    "GROQ_API_KEY": "key",
                    "ALLOWED_USER_IDS": "123",
                    "STT_PROVIDER": "anthropic",
                }
            )

    def test_placeholder_secrets_are_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "placeholder"):
            Settings.load(
                {
                    "DISCORD_TOKEN": "<replace-me>",
                    "GROQ_API_KEY": "key",
                    "ALLOWED_USER_IDS": "123",
                }
            )

    def test_redacted_summary_never_contains_secrets(self):
        settings = Settings.load(
            {
                "DISCORD_TOKEN": "real-discord-token",
                "GROQ_API_KEY": "real-groq-key",
                "OPENAI_API_KEY": "real-openai-key",
                "ALLOWED_USER_IDS": "123",
            }
        )
        summary = str(settings.redacted_summary())
        self.assertNotIn("real-discord-token", summary)
        self.assertNotIn("real-groq-key", summary)
        self.assertNotIn("real-openai-key", summary)


if __name__ == "__main__":
    unittest.main()
