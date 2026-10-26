import os
import tempfile
import unittest

from mediasync.models import Change, Decision, FileInfo, Plan
from mediasync.report import format_plan, plan_stats, write_html


def sample_plan() -> Plan:
    return Plan(
        plan_id="plan-1",
        created_at="2026-01-01T00:00:00+00:00",
        library="TV",
        kind="tv",
        source="/source/TV",
        target="/target/TV",
        config_path="/config.toml",
        changes=[
            Change(
                title="A & B",
                action="reencode",
                path="A & B/Episode.S01E01.1080p.mkv",
                old_path="A & B/Episode.S01E01.720p.mkv",
                source=FileInfo("new", size=2000, mtime_ns=1),
                target=FileInfo("old", size=1000, mtime_ns=1),
                media=True,
                decision=Decision.ACCEPTED.value,
                details={"old_size": 1000, "new_size": 2000},
            ),
            Change(
                title="Show",
                action="add_title",
                path="Show",
                source=FileInfo("Show", size=3000, mtime_ns=1, is_dir=True),
                media=True,
                decision=Decision.PENDING.value,
            ),
        ],
    )


class ReportTests(unittest.TestCase):
    def test_plan_stats(self):
        stats = plan_stats(sample_plan())
        self.assertEqual(stats["changes"], 2)
        self.assertEqual(stats["reencodes"], 1)
        self.assertEqual(stats["adds"], 1)
        self.assertEqual(stats["total_bytes"], 5000)

    def test_text_report_mentions_decisions_and_approval_hint(self):
        text = format_plan(sample_plan())
        self.assertIn("REENCODE", text)
        self.assertIn("PENDING", text)
        self.assertIn("1 change(s) need approval", text)

    def test_html_report_escapes_content_and_renders_arrow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.html")
            write_html(sample_plan(), path)
            with open(path, encoding="utf-8") as fh:
                document = fh.read()
            self.assertIn("A &amp; B", document)
            self.assertIn("&rarr;", document)
            self.assertNotIn("&amp;rarr;", document)


if __name__ == "__main__":
    unittest.main()
