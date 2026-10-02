from __future__ import annotations

import unittest
from unittest.mock import patch

from bot import build_parser


class DashboardDefaultsTests(unittest.TestCase):
    def test_env_values_feed_cli_defaults(self):
        env = {"DASHBOARD_HOST": "0.0.0.0", "DASHBOARD_PORT": "3000"}
        with patch.dict("os.environ", env, clear=False):
            args = build_parser().parse_args([])
        self.assertEqual(args.dashboard_host, "0.0.0.0")
        self.assertEqual(args.dashboard_port, 3000)

    def test_builtins_when_env_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            args = build_parser().parse_args([])
        self.assertEqual(args.dashboard_host, "127.0.0.1")
        self.assertEqual(args.dashboard_port, 8080)
        self.assertFalse(args.dashboard)
        self.assertFalse(args.no_auto_reload)


if __name__ == "__main__":
    unittest.main()
