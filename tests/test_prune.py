import os
import tempfile
import unittest

from mediasync.config import load_config
from mediasync.policy import PolicySet
from mediasync.prune import find_prune_candidates, prune_targets
from mediasync.state import State


def write(path: str, content: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)


def make_config(tmp: str, source: str, target: str):
    state_dir = os.path.join(tmp, "state")
    whitelist = os.path.join(tmp, "whitelist.txt")
    blacklist = os.path.join(tmp, "blacklist.txt")
    with open(whitelist, "w", encoding="utf-8") as fh:
        fh.write("Keep Show\n")
    with open(blacklist, "w", encoding="utf-8") as fh:
        fh.write("Keep Show\nRemove Show\n")
    path = os.path.join(tmp, "mediasync.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            f"""
state_dir = "{state_dir}"
require_mount = false
min_source_titles = 0
max_source_title_drop_pct = 100
delete_mode = "trash"
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
    return load_config(path)


class PruneCandidateTests(unittest.TestCase):
    def test_find_prune_candidates_marks_whitelisted_as_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            write(os.path.join(target, "Keep Show", "Episode.mkv"), b"keep")
            write(os.path.join(target, "Remove Show", "Episode.mkv"), b"remove")
            write(os.path.join(target, "Unrelated Show", "Episode.mkv"), b"other")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            policy = PolicySet.load(library.whitelist, library.blacklist)

            candidates = find_prune_candidates(config, library, policy)
            by_name = {cand.title: cand for cand in candidates}

            self.assertEqual(set(by_name), {"Keep Show", "Remove Show"})
            self.assertTrue(by_name["Keep Show"].protected)
            self.assertEqual(by_name["Keep Show"].status, "protected")
            self.assertFalse(by_name["Remove Show"].protected)
            self.assertEqual(by_name["Remove Show"].status, "pending")
            self.assertGreater(by_name["Remove Show"].size, 0)

    def test_prune_skips_protected_and_moves_blacklisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            write(os.path.join(target, "Keep Show", "Episode.mkv"), b"keep")
            write(os.path.join(target, "Remove Show", "Episode.mkv"), b"remove")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            policy = PolicySet.load(library.whitelist, library.blacklist)
            candidates = find_prune_candidates(config, library, policy)

            with State(config.state_dir) as state:
                result = prune_targets(
                    config,
                    state,
                    library,
                    candidates,
                    "prune-test",
                    progress=None,
                )
                entries = state.list_trash("TV")

            self.assertEqual(result.moved, 1)
            self.assertEqual(result.skipped_protected, 1)
            self.assertEqual(result.errors, [])
            self.assertEqual(result.status, "ok")
            self.assertFalse(os.path.exists(os.path.join(target, "Remove Show")))
            self.assertTrue(os.path.exists(os.path.join(target, "Keep Show", "Episode.mkv")))
            self.assertTrue(any(e["original_path"] == "Remove Show" for e in entries))
            self.assertTrue(os.path.exists(result.report_path))

    def test_force_prunes_protected_whitelist_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            write(os.path.join(target, "Keep Show", "Episode.mkv"), b"keep")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            policy = PolicySet.load(library.whitelist, library.blacklist)
            candidates = find_prune_candidates(config, library, policy)

            with State(config.state_dir) as state:
                result = prune_targets(
                    config,
                    state,
                    library,
                    candidates,
                    "prune-force",
                    force=True,
                )

            self.assertEqual(result.moved, 1)
            self.assertEqual(result.skipped_protected, 0)
            self.assertFalse(os.path.exists(os.path.join(target, "Keep Show")))


if __name__ == "__main__":
    unittest.main()
