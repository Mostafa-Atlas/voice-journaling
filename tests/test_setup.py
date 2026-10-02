from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from voicebot.config import ConfigurationError
from voicebot.setup import (
    build_invite_url,
    run_setup,
    validate_timezone_name,
    validate_token_shape,
    validate_user_id,
)

FAKE_PROBES = {
    "discord": lambda token: (True, f"valid ({token[:4]}…)"),
    "provider": lambda provider, key: (True, "valid"),
}


class SetupValidationTests(unittest.TestCase):
    def test_invite_url_format(self):
        url = build_invite_url("123456")
        self.assertIn("client_id=123456", url)
        self.assertIn("scope=bot", url)
        self.assertIn("permissions=", url)
        with self.assertRaises(ConfigurationError):
            build_invite_url("not-a-number")

    def test_token_shape_rejects_placeholders(self):
        with self.assertRaisesRegex(ConfigurationError, "placeholder"):
            validate_token_shape("<replace-me>", "Discord token")
        with self.assertRaisesRegex(ConfigurationError, "whitespace"):
            validate_token_shape("abc def", "Discord token")
        self.assertEqual(validate_token_shape("  real-token-value  ", "X"), "real-token-value")

    def test_user_id_and_timezone(self):
        self.assertEqual(validate_user_id("123"), "123")
        with self.assertRaises(ConfigurationError):
            validate_user_id("abc")
        self.assertEqual(validate_timezone_name(""), "UTC")
        self.assertEqual(validate_timezone_name("Africa/Cairo"), "Africa/Cairo")
        with self.assertRaises(ConfigurationError):
            validate_timezone_name("Mars/Olympus")


class SetupWizardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _env_values(self):
        values: dict[str, str] = {}
        for line in (self.root / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                values[key] = value
        return values

    def test_non_interactive_writes_env(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = run_setup(
                self.root,
                discord_token="discord-token-value",
                client_id="123456",
                provider="groq",
                provider_key="groq-key-value",
                owner_id="789",
                timezone_name="UTC",
                non_interactive=True,
                probes=FAKE_PROBES,
            )
        self.assertEqual(result, 0)
        values = self._env_values()
        self.assertEqual(values["DISCORD_TOKEN"], "discord-token-value")
        self.assertEqual(values["STT_PROVIDER"], "groq")
        self.assertEqual(values["GROQ_API_KEY"], "groq-key-value")
        self.assertEqual(values["ALLOWED_USER_IDS"], "789")
        self.assertEqual(values["TIMEZONE"], "UTC")
        self.assertIn("client_id=123456", buffer.getvalue())

    def test_failing_probe_aborts(self):
        probes = {
            "discord": lambda token: (False, "rejected"),
            "provider": lambda provider, key: (True, "valid"),
        }
        with self.assertRaisesRegex(ConfigurationError, "rejected"):
            run_setup(
                self.root,
                discord_token="discord-token-value",
                provider="groq",
                provider_key="groq-key-value",
                owner_id="789",
                non_interactive=True,
                probes=probes,
            )
        self.assertFalse((self.root / ".env").exists())

    def test_interactive_second_run_keeps_secrets(self):
        run_setup(
            self.root,
            discord_token="discord-token-value",
            provider="openai",
            provider_key="openai-key-value",
            owner_id="789",
            non_interactive=True,
            skip_validation=True,
        )
        # Second run: empty answers keep existing values; only owner changes.
        # (input_fn is called for provider, owner, timezone; secrets via getpass.)
        answers = iter(["", "790", ""])
        with redirect_stdout(io.StringIO()):
            result = run_setup(
                self.root,
                probes=FAKE_PROBES,
                input_fn=lambda prompt: next(answers),
                getpass_fn=lambda prompt: "",
            )
        self.assertEqual(result, 0)
        values = self._env_values()
        self.assertEqual(values["OPENAI_API_KEY"], "openai-key-value")
        self.assertEqual(values["ALLOWED_USER_IDS"], "790")

    def test_invalid_provider_rejected(self):
        with self.assertRaises(ConfigurationError):
            run_setup(
                self.root,
                discord_token="discord-token-value",
                provider="anthropic",
                provider_key="key-value-here",
                owner_id="789",
                non_interactive=True,
                skip_validation=True,
            )


if __name__ == "__main__":
    unittest.main()
