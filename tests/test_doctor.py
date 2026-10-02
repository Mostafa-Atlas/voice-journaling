from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from voicebot.doctor import main as doctor_main
from voicebot.doctor import run_doctor

FAKE_PROBES = {
    "discord": lambda token: (True, "valid"),
    "provider": lambda provider, key: (True, "valid"),
}


def seed_env(root: Path, **overrides: str) -> None:
    values = {
        "DISCORD_TOKEN": "discord-token-value",
        "GROQ_API_KEY": "groq-key-value",
        "OPENAI_API_KEY": "openai-key-value",
        "STT_PROVIDER": "groq",
        "SUMMARY_PROVIDER": "groq",
        "ALLOWED_USER_IDS": "123",
        "DATA_ROOT": str(root),
        "VOICE_LOG_DIR": "voice_logs",
        "DATABASE_PATH": "transcripts.db",
        "LOG_DIR": "logs",
        "OBSIDIAN_ENABLED": "false",
        "TIMEZONE": "UTC",
    }
    values.update(overrides)
    (root / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_env_file_fails(self):
        report = run_doctor(self.root, probes=FAKE_PROBES)
        self.assertFalse(report.ok)
        self.assertTrue(any("setup" in check.fix for check in report.checks if not check.ok))

    def test_healthy_project_passes(self):
        seed_env(self.root)
        report = run_doctor(self.root, probes=FAKE_PROBES)
        self.assertTrue(report.ok, report.human())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = doctor_main(self.root, probes=FAKE_PROBES)
        self.assertEqual(result, 0)
        self.assertIn("All checks passed", buffer.getvalue())

    def test_failing_probe_fails_with_json(self):
        seed_env(self.root)
        probes = {
            "discord": lambda token: (False, "rejected (401)"),
            "provider": lambda provider, key: (True, "valid"),
        }
        report = run_doctor(self.root, probes=probes)
        self.assertFalse(report.ok)
        payload = json.loads(report.to_json())
        self.assertFalse(payload["ok"])
        discord_check = next(c for c in payload["checks"] if c["name"] == "discord token")
        self.assertIn("401", discord_check["detail"])
        self.assertTrue(discord_check["fix"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = doctor_main(self.root, output_json=True, probes=probes)
        self.assertEqual(result, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])

    def test_broken_config_reports_fix(self):
        seed_env(self.root, STT_PROVIDER="anthropic")
        report = run_doctor(self.root, probes=FAKE_PROBES)
        self.assertFalse(report.ok)
        config = next(c for c in report.checks if c.name == "configuration")
        self.assertIn("STT_PROVIDER", config.detail)


if __name__ == "__main__":
    unittest.main()
