from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from voicebot.lock import AlreadyRunningError, ProcessLock


class ProcessLockTests(unittest.TestCase):
    def test_second_acquire_fails_while_first_is_held(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "voicebot.lock"
            first = ProcessLock(path).acquire()
            try:
                self.assertTrue(path.is_file())
                with self.assertRaisesRegex(AlreadyRunningError, "already running"):
                    ProcessLock(path).acquire()
            finally:
                first.release()

    def test_reacquire_after_release(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "voicebot.lock"
            ProcessLock(path).acquire().release()
            with ProcessLock(path):
                pass

    def test_lock_file_records_owner_pid(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "voicebot.lock"
            ProcessLock(path).acquire().release()
            # Read back via a fresh handle: the PID is diagnostic metadata.
            self.assertEqual(ProcessLock(path).owner_pid, os.getpid())

    def test_error_names_the_conflict(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "voicebot.lock"
            first = ProcessLock(path).acquire()
            try:
                try:
                    ProcessLock(path).acquire()
                    self.fail("expected AlreadyRunningError")
                except AlreadyRunningError as exc:
                    message = str(exc)
                    self.assertIn("already running", message)
                    self.assertIn("session has been invalidated", message)
            finally:
                first.release()


if __name__ == "__main__":
    unittest.main()
