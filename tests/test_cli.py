import contextlib
import io
import os
import shutil
import tempfile
import unittest

from mediasync.cli import main
from mediasync.state import State


@unittest.skipUnless(shutil.which("rsync"), "rsync is required")
class CliRunTests(unittest.TestCase):
    def test_run_applies_multiple_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            for root, title, filename in (
                (source, "TV/Show", "Episode.mkv"),
                (source, "Movies/Film", "Film.mkv"),
            ):
                path = os.path.join(root, title, filename)
                os.makedirs(os.path.dirname(path))
                with open(path, "wb") as fh:
                    fh.write(b"data")
            os.makedirs(os.path.join(target, "TV"))
            os.makedirs(os.path.join(target, "Movies"))
            config = os.path.join(tmp, "mediasync.toml")
            with open(config, "w", encoding="utf-8") as fh:
                fh.write(
                    f"""
state_dir = "{os.path.join(tmp, 'state')}"
require_mount = false
min_source_titles = 0
max_source_title_drop_pct = 100
report_html = false

[rsync]
fsync = false
timeout = 20

[[libraries]]
name = "TV"
kind = "tv"
source = "{os.path.join(source, 'TV')}"
target = "{os.path.join(target, 'TV')}"

[[libraries]]
name = "Movies"
kind = "movies"
source = "{os.path.join(source, 'Movies')}"
target = "{os.path.join(target, 'Movies')}"
"""
                )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(["--config", config, "run"])
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertTrue(
                os.path.exists(os.path.join(target, "TV", "Show", "Episode.mkv"))
            )
            self.assertTrue(
                os.path.exists(os.path.join(target, "Movies", "Film", "Film.mkv"))
            )
            with State(os.path.join(tmp, "state")) as state:
                runs = state.recent_runs(limit=10)
                self.assertEqual(len(runs), 2)
                self.assertEqual({row["library"] for row in runs}, {"TV", "Movies"})


class CliPruneTests(unittest.TestCase):
    def _make_config(self, tmp: str, target: str) -> str:
        source = os.path.join(tmp, "source")
        os.makedirs(source, exist_ok=True)
        whitelist = os.path.join(tmp, "whitelist.txt")
        blacklist = os.path.join(tmp, "blacklist.txt")
        with open(whitelist, "w", encoding="utf-8") as fh:
            fh.write("Keep Show\n")
        with open(blacklist, "w", encoding="utf-8") as fh:
            fh.write("Remove Show\nKeep Show\n")
        config = os.path.join(tmp, "mediasync.toml")
        with open(config, "w", encoding="utf-8") as fh:
            fh.write(
                f"""
state_dir = "{os.path.join(tmp, 'state')}"
require_mount = false
min_source_titles = 0
max_source_title_drop_pct = 100
report_html = false

[rsync]
fsync = false

[[libraries]]
name = "TV"
kind = "tv"
source = "{source}"
target = "{target}"
whitelist = "{whitelist}"
blacklist = "{blacklist}"
"""
            )
        return config

    def test_prune_dry_run_leaves_target_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            os.makedirs(os.path.join(target, "Remove Show"))
            with open(os.path.join(target, "Remove Show", "Episode.mkv"), "wb") as fh:
                fh.write(b"bytes")
            config = self._make_config(tmp, target)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--config",
                        config,
                        "prune-blacklisted",
                        "--library",
                        "TV",
                        "--dry-run",
                    ]
                )

            self.assertEqual(code, 0, stderr.getvalue())
            self.assertIn("Remove Show", stdout.getvalue())
            self.assertTrue(os.path.exists(os.path.join(target, "Remove Show")))

    def test_prune_moves_blacklisted_title_to_trash(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            os.makedirs(os.path.join(target, "Remove Show"))
            with open(os.path.join(target, "Remove Show", "Episode.mkv"), "wb") as fh:
                fh.write(b"bytes")
            config = self._make_config(tmp, target)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--config",
                        config,
                        "prune-blacklisted",
                        "--library",
                        "TV",
                        "--yes",
                    ]
                )

            self.assertEqual(code, 0, stderr.getvalue())
            self.assertFalse(os.path.exists(os.path.join(target, "Remove Show")))
            with State(os.path.join(tmp, "state")) as state:
                entries = state.list_trash("TV")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["original_path"], "Remove Show")

    def test_prune_blocked_by_protected_requires_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            os.makedirs(os.path.join(target, "Keep Show"))
            with open(os.path.join(target, "Keep Show", "Episode.mkv"), "wb") as fh:
                fh.write(b"bytes")
            config = self._make_config(tmp, target)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--config",
                        config,
                        "prune-blacklisted",
                        "--library",
                        "TV",
                        "--yes",
                    ]
                )

            self.assertEqual(code, 11)
            self.assertIn("protected", stdout.getvalue() + stderr.getvalue())
            self.assertTrue(os.path.exists(os.path.join(target, "Keep Show")))

    def test_prune_force_overrides_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            os.makedirs(os.path.join(target, "Keep Show"))
            with open(os.path.join(target, "Keep Show", "Episode.mkv"), "wb") as fh:
                fh.write(b"bytes")
            config = self._make_config(tmp, target)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--config",
                        config,
                        "prune-blacklisted",
                        "--library",
                        "TV",
                        "--yes",
                        "--force",
                    ]
                )

            self.assertEqual(code, 0, stderr.getvalue() + stdout.getvalue())
            self.assertFalse(os.path.exists(os.path.join(target, "Keep Show")))

    def test_prune_all_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            os.makedirs(os.path.join(target, "Remove Show"))
            with open(os.path.join(target, "Remove Show", "Episode.mkv"), "wb") as fh:
                fh.write(b"bytes")
            config = self._make_config(tmp, target)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = main(["--config", config, "prune-blacklisted", "--library", "TV", "--yes"])
                except SystemExit as exc:
                    code = exc.code
            self.assertEqual(code, 0)

    def test_prune_clear_stale_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            config = self._make_config(tmp, target)
            import sqlite3
            from mediasync.state import SCHEMA

            state_path = os.path.join(tmp, "state", "mediasync.db")
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with sqlite3.connect(state_path) as conn:
                conn.executescript(SCHEMA)
                conn.execute(
                    "INSERT INTO runs(id, library, mode, plan_id, status, started) VALUES(?,?,?,?,?,?)",
                    (
                        "old-run",
                        "TV",
                        "plan",
                        None,
                        "running",
                        "2020-01-01T00:00:00+00:00",
                    ),
                )
                conn.commit()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = main(["--config", config, "prune-blacklisted", "--library", "TV", "--dry-run"])
                except SystemExit as exc:
                    code = exc.code
            self.assertEqual(code, 0)
            with State(os.path.join(tmp, "state")) as state:
                run = state.get_run_progress("old-run")
            self.assertIn(run["status"], {"cancelled", "failed"})


if __name__ == "__main__":
    unittest.main()
