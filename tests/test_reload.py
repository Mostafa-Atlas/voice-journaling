from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from voicebot.reload import (
    build_exec_args,
    env_fingerprint,
    restart_process,
    watch_env_file,
)


class FingerprintTests(unittest.TestCase):
    def test_stable_and_sensitive_to_content(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / ".env"
            path.write_text("A=1\n", encoding="utf-8")
            first = env_fingerprint(path)
            self.assertTrue(first)
            self.assertEqual(env_fingerprint(path), first)
            path.write_text("A=2\n", encoding="utf-8")
            self.assertNotEqual(env_fingerprint(path), first)

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            self.assertEqual(env_fingerprint(Path(directory) / ".env"), "")


class ExecArgsTests(unittest.TestCase):
    def test_script_restart_uses_absolute_path(self):
        args = build_exec_args(executable="/fake/python", argv=["bot.py", "--dashboard"])
        self.assertEqual(args[0], "/fake/python")
        self.assertTrue(Path(args[1]).is_absolute())
        self.assertTrue(args[1].endswith("bot.py"))
        self.assertEqual(args[2:], ["--dashboard"])

    def test_module_mode_is_preserved(self):
        args = build_exec_args(executable="/fake/python", argv=["-m", "voicebot", "dashboard"])
        self.assertEqual(args, ["/fake/python", "-m", "voicebot", "dashboard"])


class RestartTests(unittest.TestCase):
    def test_restart_replaces_process(self):
        with patch("voicebot.reload.os.execv") as execv:
            with (
                patch("sys.argv", ["bot.py", "--dashboard"]),
                patch("sys.executable", "/fake/python"),
            ):
                try:
                    restart_process("test reason")
                except RuntimeError:
                    pass  # only if the mock returns instead of raising
        execv.assert_called_once()
        called_path, called_args = execv.call_args[0]
        self.assertEqual(called_path, "/fake/python")
        self.assertEqual(called_args[0], "/fake/python")
        self.assertTrue(
            Path(called_args[1]).is_absolute() or called_args[1] == "-m"
        )
        self.assertIn("--dashboard", called_args)


class WatcherTests(unittest.TestCase):
    def test_callback_fires_on_change(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / ".env"
            path.write_text("A=1\n", encoding="utf-8")
            fired = threading.Event()
            stop = threading.Event()
            watch_env_file(
                path,
                interval=0.05,
                on_change=fired.set,
                stop=stop,
            )
            try:
                path.write_text("A=2\n", encoding="utf-8")
                self.assertTrue(fired.wait(timeout=5))
            finally:
                stop.set()

    def test_no_callback_without_change(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / ".env"
            path.write_text("A=1\n", encoding="utf-8")
            fired = threading.Event()
            stop = threading.Event()
            watch_env_file(
                path,
                interval=0.05,
                on_change=fired.set,
                stop=stop,
            )
            try:
                self.assertFalse(fired.wait(timeout=0.4))
            finally:
                stop.set()


if __name__ == "__main__":
    unittest.main()
