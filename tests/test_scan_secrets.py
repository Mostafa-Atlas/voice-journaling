import unittest

from scripts.scan_secrets import matching_patterns


class SecretScannerTests(unittest.TestCase):
    def test_uv_artifact_url_is_not_a_discord_token(self) -> None:
        line = (
            'url = "https://files.pythonhosted.org/packages/58/d9/'
            '22ce5786ac0c1653ae8b6c23bded02c1686d11f0dbb45b31ce128e0df985/'
            'aiohttp-3.14.3-cp311-cp311.whl"'
        )
        self.assertNotIn("Discord token", matching_patterns(line))

    def test_discord_token_shape_is_detected(self) -> None:
        token = ".".join(("A" * 24, "B" * 6, "C" * 38))
        self.assertIn("Discord token", matching_patterns(f"token = {token}"))


if __name__ == "__main__":
    unittest.main()
