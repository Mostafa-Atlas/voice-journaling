from __future__ import annotations

import unittest

from voicebot.models import SummaryData, escape_markdown


class SummaryTests(unittest.TestCase):
    def test_structured_summary_round_trip_and_markdown(self):
        summary = SummaryData.from_dict(
            {
                "summary": " Plan the release. ",
                "key_points": ["Version 2", ""],
                "decisions": [],
                "action_items": [
                    {"task": "Ship it", "owner": "Aether", "deadline": None}
                ],
                "mentioned": ["Friday"],
            }
        )
        restored = SummaryData.from_json(summary.to_json())
        self.assertEqual(restored, summary)
        markdown = summary.to_markdown()
        self.assertIn("## Summary", markdown)
        self.assertIn("- [ ] Ship it (owner: Aether)", markdown)
        self.assertNotIn("## Decisions", markdown)

    def test_invalid_or_extra_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            SummaryData.from_dict({"summary": "x"})
        payload = {
            "summary": "x",
            "key_points": [],
            "decisions": [],
            "action_items": [],
            "mentioned": [],
            "extra": True,
        }
        with self.assertRaises(ValueError):
            SummaryData.from_dict(payload)

    def test_untrusted_markdown_is_escaped(self):
        value = "![beacon](https://example.test/x) <img src=x> `code`"
        escaped = escape_markdown(value)
        self.assertNotIn("![", escaped)
        self.assertNotIn("<img", escaped)
        self.assertIn("\\`code\\`", escaped)

    def test_teaser_handles_long_unicode_without_crashing(self):
        summary = SummaryData("مرحبا " * 100)
        teaser = summary.teaser(40)
        self.assertLessEqual(len(teaser), 40)
        self.assertTrue(teaser.endswith("…"))


if __name__ == "__main__":
    unittest.main()
