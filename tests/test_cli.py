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


if __name__ == "__main__":
    unittest.main()
