import os
import tempfile
import unittest

from mediasync import gui
from mediasync.config import load_config
from mediasync.policy import PolicySet
from mediasync.prune import (
    PruneCandidate,
    find_prune_candidates,
    format_prune_report,
    prune_targets,
    write_prune_report,
)
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


class PruneEdgeCaseTests(unittest.TestCase):
    def make_library(self, tmp: str):
        source = os.path.join(tmp, "source")
        target = os.path.join(tmp, "target")
        os.makedirs(source)
        os.makedirs(target)
        config = make_config(tmp, source, target)
        return config, config.library("TV")

    def test_blacklisted_file_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            write(os.path.join(target, "Remove Show"), b"file not dir")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            candidates = find_prune_candidates(config, library)
            self.assertEqual(candidates, [])

    def test_missing_target_returns_no_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "missing-target")
            os.makedirs(source)
            config = make_config(tmp, source, target)
            library = config.library("TV")
            self.assertEqual(find_prune_candidates(config, library), [])

    def test_whitelist_with_higher_specificity_protects(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            write(os.path.join(target, "Remove Show", "Keep", "Episode.mkv"), b"x")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            with open(library.blacklist, "w", encoding="utf-8") as fh:
                fh.write("Remove*\n")
            with open(library.whitelist, "w", encoding="utf-8") as fh:
                fh.write("Remove Show\n")
            policy = PolicySet.load(library.whitelist, library.blacklist)
            candidates = find_prune_candidates(config, library, policy)
            by_name = {cand.title: cand for cand in candidates}
            self.assertTrue(by_name["Remove Show"].protected)
            self.assertGreater(by_name["Remove Show"].whitelist_spec, 0)

    def test_prune_reports_missing_and_outside_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, library = self.make_library(tmp)
            outside = PruneCandidate(
                title="Outside",
                rel_path="Outside",
                abs_path=os.path.realpath(os.path.join(tmp, "elsewhere", "Outside")),
                is_dir=True,
                size=0,
                blacklisted_by="*",
                blacklist_spec=1,
                reason="outside",
            )
            missing = PruneCandidate(
                title="Missing",
                rel_path="Missing",
                abs_path=os.path.realpath(os.path.join(library.target, "Missing")),
                is_dir=True,
                size=0,
                blacklisted_by="*",
                blacklist_spec=1,
                reason="missing",
            )
            with State(config.state_dir) as state:
                result = prune_targets(
                    config,
                    state,
                    library,
                    [outside, missing],
                    "prune-errors",
                )
            self.assertEqual(result.skipped_other, 1)
            self.assertEqual(result.moved, 0)
            self.assertEqual(result.status, "failed")
            self.assertTrue(any("outside target root" in e for e in result.errors))
            self.assertEqual(missing.status, "skipped_missing")
            self.assertEqual(outside.status, "error")

    def test_permanent_delete_removes_content_without_trash(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, library = self.make_library(tmp)
            path = os.path.join(library.target, "Remove Show", "Episode.mkv")
            write(path, b"remove")
            config.delete_mode = "permanent"
            candidate = PruneCandidate(
                title="Remove Show",
                rel_path="Remove Show",
                abs_path=os.path.realpath(os.path.dirname(path)),
                is_dir=True,
                size=6,
                blacklisted_by="Remove Show",
                blacklist_spec=11,
                reason="blacklisted",
            )
            with State(config.state_dir) as state:
                result = prune_targets(
                    config,
                    state,
                    library,
                    [candidate],
                    "prune-permanent",
                    permanent=True,
                )
                entries = state.list_trash("TV")
            self.assertEqual(result.moved, 1)
            self.assertFalse(os.path.exists(os.path.dirname(path)))
            self.assertEqual(entries, [])

    def test_report_includes_candidates_errors_and_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, library = self.make_library(tmp)
            config.report_html = True
            candidate = PruneCandidate(
                title="Missing",
                rel_path="Missing",
                abs_path=os.path.realpath(os.path.join(library.target, "Missing")),
                is_dir=True,
                size=0,
                blacklisted_by="*",
                blacklist_spec=1,
                reason="missing",
            )
            with State(config.state_dir) as state:
                result = prune_targets(
                    config,
                    state,
                    library,
                    [candidate],
                    "prune-report",
                )
            self.assertIsNotNone(result.report_path)
            self.assertIsNotNone(result.report_html_path)
            from pathlib import Path

            report = Path(result.report_path).read_text(encoding="utf-8")
            html = Path(result.report_html_path).read_text(encoding="utf-8")
            self.assertIn("MediaSync prune report", report)
            self.assertIn("[skipped_missing] Missing", report)
            self.assertIn("MediaSync prune report", html)
            self.assertIn("Missing", html)
            self.assertIn("skipped_missing", html)

    def test_format_and_write_report_without_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, library = self.make_library(tmp)
            config.report_html = False
            candidate = PruneCandidate(
                title="Remove Show",
                rel_path="Remove Show",
                abs_path=os.path.realpath(os.path.join(library.target, "Remove Show")),
                is_dir=True,
                size=10,
                blacklisted_by="Remove Show",
                blacklist_spec=11,
                reason="blacklisted",
                status="moved",
            )
            write(os.path.join(library.target, "Remove Show", "a.mkv"), b"x")
            from mediasync.prune import PruneResult

            result = PruneResult(
                run_id="fmt",
                library="TV",
                candidates=[candidate],
                moved=1,
                errors=["oops"],
                status="partial",
            )
            text = format_prune_report(result, [candidate])
            self.assertIn("Status: partial", text)
            self.assertIn("Errors:", text)
            path = write_prune_report(config, result, [candidate])
            self.assertEqual(result.report_path, path)
            self.assertIsNone(result.report_html_path)


class PruneProgressTests(unittest.TestCase):
    def test_progress_callback_is_used_and_exceptions_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            write(os.path.join(target, "Remove Show", "Episode.mkv"), b"remove")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            candidate = PruneCandidate(
                title="Remove Show",
                rel_path="Remove Show",
                abs_path=os.path.realpath(os.path.join(target, "Remove Show")),
                is_dir=True,
                size=6,
                blacklisted_by="Remove Show",
                blacklist_spec=11,
                reason="blacklisted",
                status="pending",
            )
            messages = []
            with State(config.state_dir) as state:
                first = prune_targets(
                    config,
                    state,
                    library,
                    [candidate],
                    "prune-progress",
                    progress=messages.append,
                )
            self.assertEqual(first.moved, 1)
            self.assertTrue(messages)

            write(os.path.join(target, "Remove Show", "Episode.mkv"), b"remove")
            c2 = PruneCandidate(
                title="Remove Show",
                rel_path="Remove Show",
                abs_path=os.path.realpath(os.path.join(target, "Remove Show")),
                is_dir=True,
                size=6,
                blacklisted_by="Remove Show",
                blacklist_spec=11,
                reason="blacklisted",
                status="pending",
            )
            with State(config.state_dir) as state:
                second = prune_targets(
                    config,
                    state,
                    library,
                    [c2],
                    "prune-progress-2",
                    progress=lambda _msg: (_ for _ in ()).throw(RuntimeError("boom")),
                )
            self.assertEqual(second.moved, 1)


class GuiHelperTests(unittest.TestCase):
    def test_list_top_level_titles_unions_source_target_and_filters_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(os.path.join(source, "A"))
            write(os.path.join(source, "A", "x"), b"x")
            write(os.path.join(source, ".DS_Store"), b"x")
            os.makedirs(os.path.join(target, "B"))
            write(os.path.join(target, "B", "y"), b"y")
            os.makedirs(os.path.join(source, "Both"))
            os.makedirs(os.path.join(target, "Both"))
            policy = PolicySet([], [])
            rows = gui.list_top_level_titles(source, target, policy)
            by_name = {r["name"]: r for r in rows}
            self.assertIn("A", by_name)
            self.assertIn("B", by_name)
            self.assertIn("Both", by_name)
            self.assertEqual(by_name["A"]["origin"], "Source")
            self.assertEqual(by_name["B"]["origin"], "Target")
            self.assertEqual(by_name["Both"]["origin"], "Source/Target")
            self.assertNotIn(".DS_Store", by_name)

    def test_cancel_running_for_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            with State(tmp) as state:
                state.update_run_progress("r1", library="TV", status="running")
                state.update_run_progress("r2", library="Movies", status="running")
                cancelled = gui.cancel_running_for_libraries(state, ["TV"])
                self.assertEqual(cancelled, 1)
                self.assertEqual(state.get_run_progress("r1")["status"], "cancelled")
                self.assertEqual(state.get_run_progress("r2")["status"], "running")

    def test_run_prune_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            write(os.path.join(target, "Remove Show", "Episode.mkv"), b"remove")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            policy = PolicySet.load(library.whitelist, library.blacklist)
            candidates = find_prune_candidates(config, library, policy)
            result = gui.run_prune_action(config, library, candidates, force=False)
            self.assertEqual(result.moved, 1)


if __name__ == "__main__":
    unittest.main()
