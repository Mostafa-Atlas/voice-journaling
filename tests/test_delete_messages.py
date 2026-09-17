from __future__ import annotations

import unittest
from unittest.mock import patch

import delete_messages


class DeleteMessagesTests(unittest.TestCase):
    def test_import_is_safe_and_dry_run_is_default(self):
        args = delete_messages.build_parser().parse_args(["--target-user", "123"])
        self.assertFalse(args.execute)
        self.assertFalse(args.yes)
        self.assertEqual(args.limit, 100)

    def test_missing_token_fails_before_discord_import(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "DISCORD_CLEANUP_TOKEN"):
                delete_messages.main(["--target-user", "123"])

    def test_execute_requires_exact_confirmation(self):
        with patch.dict("os.environ", {"DISCORD_CLEANUP_TOKEN": "test-token"}, clear=True):
            with patch("builtins.input", return_value="no"):
                with self.assertRaisesRegex(SystemExit, "nothing was deleted"):
                    delete_messages.main(
                        ["--target-user", "123", "--limit", "1", "--execute"]
                    )


if __name__ == "__main__":
    unittest.main()
