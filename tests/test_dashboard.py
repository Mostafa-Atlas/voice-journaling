from __future__ import annotations

import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tests.helpers import dispose_database, make_settings
from voicebot.dashboard import (
    resolve_dashboard_token,
    sanitize_log_path,
    start_dashboard,
)
from voicebot.database import Database
from voicebot.models import MemoStatus, SummaryData
from voicebot.storage import FileStorage


def _fetch(
    port: int,
    path: str,
    token: str | None = None,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict | None = None,
):
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(url, data=data, method=method)
    if token and "?token=" not in path and "token=" not in (data or b"").decode():
        separator = "&" if "?" in path else "?"
        url = f"{url}{separator}{urllib.parse.urlencode({'token': token})}"
        request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if data:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        self.settings = make_settings(self.root)
        self.database = Database(self.settings.database_path)
        self.database.initialize()
        self.storage = FileStorage(self.settings.data_root, self.settings.voice_log_dir)
        memo, _ = self.database.register_memo(
            memo_id="discord-1-2",
            message_id="1",
            attachment_id="2",
            discord_id="123",
            username="user",
            original_filename="voice.ogg",
            content_type="audio/ogg",
            size_bytes=4,
            received_at="2026-09-26T10:00:00+00:00",
        )
        summary = SummaryData("Finish <b>the</b> project.")
        audio = self.root / "voice_logs" / "clip.ogg"
        audio.write_bytes(b"OggS")
        self.memo = self.database.update_memo(
            memo.memo_id,
            status=MemoStatus.COMPLETED.value,
            audio_path="voice_logs/clip.ogg",
            transcript="hello <script>alert(1)</script>",
            summary_json=summary.to_json(),
            summary_text=summary.summary,
        )
        self.token = "test-token"
        # Seed a .env mirroring make_settings defaults so validation sees the
        # required secrets, exactly like a real deployment.
        (self.root / ".env").write_text(
            "DISCORD_TOKEN=test-discord-token\n"
            "GROQ_API_KEY=test-groq-key\n"
            "OPENAI_API_KEY=test-openai-key\n"
            "STT_PROVIDER=groq\n"
            "SUMMARY_PROVIDER=groq\n"
            "ALLOWED_USER_IDS=123,456\n"
            f"DATA_ROOT={self.root}\n"
            "VOICE_LOG_DIR=voice_logs\n"
            "DATABASE_PATH=transcripts.db\n"
            "LOG_DIR=logs\n"
            "OBSIDIAN_ENABLED=false\n"
            f"OBSIDIAN_VAULT_PATH={self.root / 'vault'}\n"
            "OBSIDIAN_SUBFOLDER=Voice Journal\n"
            "OBSIDIAN_REQUIRE_MOUNT=false\n"
            "TIMEZONE=UTC\n",
            encoding="utf-8",
        )
        self.server, self.thread = start_dashboard(
            self.settings,
            self.database,
            self.storage,
            host="127.0.0.1",
            port=0,
            token=self.token,
        )
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        dispose_database(self.database)
        self.temporary.cleanup()

    def test_healthz_needs_no_token(self):
        status, body, _ = _fetch(self.port, "/healthz")
        self.assertEqual(status, 200)
        self.assertIn(b'"status": "ok"', body)

    def test_overview_requires_token(self):
        status, _, _ = _fetch(self.port, "/")
        self.assertEqual(status, 403)
        status, body, _ = _fetch(self.port, "/", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"Status", body)
        self.assertIn(b"groq", body)

    def test_header_token_is_accepted(self):
        status, body, _ = _fetch(self.port, "/", headers={"X-Dashboard-Token": self.token})
        self.assertEqual(status, 200)
        self.assertIn(b"Status", body)

    def test_history_search_and_detail_escape_html(self):
        status, body, _ = _fetch(self.port, f"/history?q=hello&token={self.token}")
        self.assertEqual(status, 200)
        self.assertIn(b"discord-1-2", body)
        status, body, _ = _fetch(self.port, f"/memo?id=discord-1-2&token={self.token}")
        self.assertEqual(status, 200)
        self.assertNotIn(b"<script>", body)
        self.assertIn(b"&lt;script&gt;", body)
        self.assertIn(b"<audio", body)

    def test_audio_serving_and_missing(self):
        status, body, headers = _fetch(self.port, f"/audio?id=discord-1-2&token={self.token}")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"OggS")
        self.assertIn("audio/ogg", headers.get("Content-Type", ""))
        status, _, _ = _fetch(self.port, "/audio?id=nope&token=" + self.token)
        self.assertEqual(status, 404)

    def test_settings_post_validates_and_flags_restart(self):
        from voicebot.dashboard import Dashboard

        dash = Dashboard(self.settings, self.database, self.storage)
        self.assertEqual(dash.restart_needed(), [])
        payload = urllib.parse.urlencode({"token": self.token, "MAX_CONCURRENT_JOBS": "3"}).encode()
        status, _, headers = _fetch(
            self.port, "/settings", token=self.token, method="POST", data=payload
        )
        # urllib follows the 303 redirect; the landing page confirms the save.
        self.assertEqual(status, 200)
        env_text = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("MAX_CONCURRENT_JOBS=3", env_text)
        self.assertNotEqual(dash.restart_needed(), [])

    def test_settings_post_rejects_bad_provider(self):
        payload = urllib.parse.urlencode(
            {"token": self.token, "STT_PROVIDER": "anthropic"}
        ).encode()
        status, body, _ = _fetch(
            self.port, "/settings", token=self.token, method="POST", data=payload
        )
        self.assertEqual(status, 200)
        self.assertIn(b"must be groq or openai", body)
        env_text = (self.root / ".env").read_text(encoding="utf-8")
        self.assertNotIn("anthropic", env_text)

    def test_token_resolution_prefers_explicit(self):
        token, generated = resolve_dashboard_token("abc")
        self.assertEqual((token, generated), ("abc", False))
        token, generated = resolve_dashboard_token(None)
        self.assertTrue(token and generated)

    def test_log_path_sanitizer_strips_token(self):
        self.assertEqual(sanitize_log_path("/?token=secret&a=1"), "/?a=1")
        self.assertEqual(sanitize_log_path("/history"), "/history")

    def test_export_json_and_csv(self):
        status, body, headers = _fetch(self.port, f"/export?format=json&token={self.token}")
        self.assertEqual(status, 200)
        self.assertIn(b"discord-1-2", body)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        status, body, headers = _fetch(self.port, f"/export?format=csv&token={self.token}")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"memo_id,received_at"))
        self.assertIn("text/csv", headers.get("Content-Type", ""))


if __name__ == "__main__":
    unittest.main()
