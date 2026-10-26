import os
import shutil
import tempfile
import unittest

from mediasync.apply import PlanStaleError, _verify_directory, apply_plan, verify_plan
from mediasync.config import load_config
from mediasync.models import Change, Decision, FileInfo, Plan, TitleFingerprint
from mediasync.planner import pending_plans, plan_library
from mediasync.state import State
from mediasync.trash import restore
from mediasync.util import directory_fingerprint


def write(path: str, content: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)


def make_config(tmp: str, source: str, target: str):
    state = os.path.join(tmp, "state")
    path = os.path.join(tmp, "mediasync.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            f"""
state_dir = "{state}"
require_mount = false
min_source_titles = 0
max_source_title_drop_pct = 100
delete_mode = "trash"
report_html = false

[rsync]
modify_window = 2
fsync = false

[apply]
auto_approve = ["add_title", "add_media", "add_sidecar", "update_sidecar", "reencode"]
max_pending_deletions = 0
max_delete_gb = 0
bulk_reencode_count = 5

[[libraries]]
name = "TV"
kind = "tv"
source = "{source}"
target = "{target}"
"""
        )
    return load_config(path)


class ApplyVerificationTests(unittest.TestCase):
    def test_modify_window_tolerates_small_directory_mtime_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            title = "Show"
            path = os.path.join(tmp, title)
            os.makedirs(path)
            mtime, count = directory_fingerprint(path)
            self.assertIsNotNone(mtime)
            _verify_directory(
                tmp, title, mtime, count, 2_000_000_000, expected_exists=True
            )
            os.utime(path, ns=(mtime - 1_000_000_000, mtime - 1_000_000_000))
            _verify_directory(
                tmp, title, mtime, count, 2_000_000_000, expected_exists=True
            )
            os.utime(path, ns=(mtime - 4_000_000_000, mtime - 4_000_000_000))
            with self.assertRaises(PlanStaleError):
                _verify_directory(
                    tmp, title, mtime, count, 2_000_000_000, expected_exists=True
                )

    def test_verify_plan_detects_source_directory_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            write(os.path.join(source, "Show", "Episode.mkv"), b"data")
            os.makedirs(target)
            config = make_config(tmp, source, target)
            library = config.library("TV")
            source_title = os.path.join(source, "Show")
            mtime, count = directory_fingerprint(source_title)
            plan = Plan(
                plan_id="test-plan",
                created_at="2026-01-01T00:00:00+00:00",
                library="TV",
                kind="tv",
                source=os.path.realpath(source),
                target=os.path.realpath(target),
                config_path=config.config_path,
                changes=[
                    Change(
                        title="Show",
                        action="add_title",
                        path="Show",
                        source=FileInfo("Show", size=4, mtime_ns=mtime or 0, is_dir=True),
                        decision=Decision.ACCEPTED.value,
                    )
                ],
                fingerprints={
                    "Show": TitleFingerprint(
                        title="Show",
                        source_mtime_ns=mtime,
                        source_count=count,
                    )
                },
            )
            verify_plan(config, plan, library)
            write(os.path.join(source, "Show", "Another.mkv"), b"new")
            with self.assertRaises(PlanStaleError):
                verify_plan(config, plan, library)


@unittest.skipUnless(shutil.which("rsync"), "rsync is required")
class EndToEndApplyTests(unittest.TestCase):
    def test_add_title_then_delete_to_trash_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(os.path.join(source, "Show"))
            os.makedirs(target)
            media_path = os.path.join(source, "Show", "Episode.mkv")
            write(media_path, b"episode-bytes")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            with State(config.state_dir) as state:
                plan = plan_library(config, state, library)
                self.assertEqual(len(plan.changes), 1)
                change = plan.changes[0]
                self.assertEqual(change.action, "add_title")
                self.assertEqual(change.decision, Decision.ACCEPTED.value)

                result = apply_plan(
                    config, state, library, plan, run_id="run-add"
                )
                self.assertEqual(result.status, "applied")
                self.assertEqual(result.applied, 1)
                copied = os.path.join(target, "Show", "Episode.mkv")
                with open(copied, "rb") as fh:
                    self.assertEqual(fh.read(), b"episode-bytes")
                library_row = state.get_library("TV")
                self.assertEqual(library_row["last_target_titles"], 1)
                self.assertEqual(library_row["last_status"], "applied")

                shutil.rmtree(os.path.join(source, "Show"))
                delete_plan = plan_library(
                    config, state, library, allow_source_drop=True
                )
                self.assertEqual(len(delete_plan.changes), 1)
                delete_change = delete_plan.changes[0]
                self.assertEqual(delete_change.action, "delete_title")
                self.assertEqual(delete_change.decision, Decision.PENDING.value)

                delete_change.decision = Decision.ACCEPTED.value
                delete_plan.recompute_status()
                result = apply_plan(
                    config, state, library, delete_plan, run_id="run-delete"
                )
                self.assertEqual(result.status, "applied")
                self.assertFalse(os.path.exists(os.path.join(target, "Show")))

                entries = state.list_trash(library="TV")
                self.assertEqual(len(entries), 1)
                entry = entries[0]
                self.assertEqual(entry["entry_type"], "dir")
                self.assertEqual(entry["original_path"], "Show")

                restored = restore(
                    config, library, state, int(entry["id"]), overwrite=False
                )
                self.assertEqual(restored, os.path.join(os.path.realpath(target), "Show"))
                self.assertTrue(os.path.isdir(restored))
                with open(os.path.join(restored, "Episode.mkv"), "rb") as fh:
                    self.assertEqual(fh.read(), b"episode-bytes")
                self.assertEqual(state.list_trash(library="TV"), [])

    def test_existing_title_add_and_delete_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            source_title = os.path.join(source, "Show")
            target_title = os.path.join(target, "Show")
            os.makedirs(source_title)
            os.makedirs(target_title)
            common = os.path.join(source_title, "common.mkv")
            write(common, b"common")
            shutil.copy2(common, os.path.join(target_title, "common.mkv"))
            write(os.path.join(source_title, "new.mkv"), b"new")
            write(os.path.join(target_title, "old.mkv"), b"old")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            with State(config.state_dir) as state:
                plan = plan_library(config, state, library)
                actions = {change.action for change in plan.changes}
                self.assertEqual(actions, {"add", "delete"})
                for change in plan.changes:
                    if change.action == "delete":
                        change.decision = Decision.ACCEPTED.value
                plan.recompute_status()
                result = apply_plan(
                    config, state, library, plan, run_id="run-files"
                )
                self.assertEqual(result.status, "applied")
                self.assertEqual(result.applied, 2)
                with open(os.path.join(target_title, "new.mkv"), "rb") as fh:
                    self.assertEqual(fh.read(), b"new")
                self.assertFalse(os.path.exists(os.path.join(target_title, "old.mkv")))
                entries = state.list_trash(library="TV")
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["original_path"], "Show/old.mkv")

    def test_same_path_reencode_backs_up_old_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            source_title = os.path.join(source, "Show")
            target_title = os.path.join(target, "Show")
            os.makedirs(source_title)
            os.makedirs(target_title)
            media = "Episode.mkv"
            write(os.path.join(source_title, media), b"new media version")
            write(os.path.join(target_title, media), b"old")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            with State(config.state_dir) as state:
                plan = plan_library(config, state, library)
                self.assertEqual(len(plan.changes), 1)
                change = plan.changes[0]
                self.assertEqual(change.action, "reencode")
                self.assertEqual(change.decision, Decision.ACCEPTED.value)
                result = apply_plan(
                    config, state, library, plan, run_id="run-reencode"
                )
                self.assertEqual(result.status, "applied")
                with open(os.path.join(target_title, media), "rb") as fh:
                    self.assertEqual(fh.read(), b"new media version")
                entries = state.list_trash(library="TV")
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["original_path"], "Show/Episode.mkv")
                backup = os.path.join(
                    target, entries[0]["trash_path"]
                )
                with open(backup, "rb") as fh:
                    self.assertEqual(fh.read(), b"old")

    def test_blacklist_subpath_is_excluded_from_new_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(os.path.join(source, "Show", "Extras"))
            os.makedirs(target)
            write(os.path.join(source, "Show", "Episode.mkv"), b"episode")
            write(os.path.join(source, "Show", "Extras", "Feature.mkv"), b"extra")
            blacklist = os.path.join(tmp, "blacklist.txt")
            with open(blacklist, "w", encoding="utf-8") as fh:
                fh.write("Show/Extras\n")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            library.blacklist = blacklist
            with State(config.state_dir) as state:
                plan = plan_library(config, state, library)
                self.assertEqual(len(plan.changes), 1)
                add = plan.changes[0]
                self.assertEqual(add.action, "add_title")
                self.assertEqual(add.details["allowed_paths"], ["Episode.mkv"])
                self.assertEqual(add.details["excluded_count"], 1)
                result = apply_plan(
                    config, state, library, plan, run_id="run-filtered-add"
                )
                self.assertEqual(result.status, "applied")
                self.assertTrue(
                    os.path.exists(os.path.join(target, "Show", "Episode.mkv"))
                )
                self.assertFalse(
                    os.path.exists(
                        os.path.join(target, "Show", "Extras", "Feature.mkv")
                    )
                )

    def test_title_delete_respects_subpath_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            os.makedirs(os.path.join(target, "Show"))
            write(os.path.join(target, "Show", "Keep.mkv"), b"keep")
            write(os.path.join(target, "Show", "Ignore.mkv"), b"ignore")
            write(os.path.join(target, "Show", "Delete.mkv"), b"delete")
            whitelist = os.path.join(tmp, "whitelist.txt")
            blacklist = os.path.join(tmp, "blacklist.txt")
            with open(whitelist, "w", encoding="utf-8") as fh:
                fh.write("Show/Keep.mkv\n")
            with open(blacklist, "w", encoding="utf-8") as fh:
                fh.write("Show/Ignore.mkv\n")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            library.whitelist = whitelist
            library.blacklist = blacklist
            with State(config.state_dir) as state:
                plan = plan_library(
                    config, state, library, allow_source_drop=True
                )
                by_path = {change.path: change for change in plan.changes}
                self.assertNotIn("Show", by_path)
                self.assertNotIn("Show/Ignore.mkv", by_path)
                self.assertEqual(by_path["Show/Keep.mkv"].decision, Decision.HELD.value)
                delete = by_path["Show/Delete.mkv"]
                self.assertEqual(delete.decision, Decision.PENDING.value)
                delete.decision = Decision.ACCEPTED.value
                plan.recompute_status()
                result = apply_plan(
                    config, state, library, plan, run_id="run-policy-delete"
                )
                self.assertEqual(result.status, "applied")
                self.assertTrue(os.path.exists(os.path.join(target, "Show", "Keep.mkv")))
                self.assertTrue(
                    os.path.exists(os.path.join(target, "Show", "Ignore.mkv"))
                )
                self.assertFalse(
                    os.path.exists(os.path.join(target, "Show", "Delete.mkv"))
                )
                entries = state.list_trash(library="TV")
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["original_path"], "Show/Delete.mkv")

    def test_held_only_plan_is_not_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(source)
            os.makedirs(os.path.join(target, "Show"))
            write(os.path.join(target, "Show", "Keep.mkv"), b"keep")
            whitelist = os.path.join(tmp, "whitelist.txt")
            with open(whitelist, "w", encoding="utf-8") as fh:
                fh.write("Show\n")
            config = make_config(tmp, source, target)
            library = config.library("TV")
            library.whitelist = whitelist
            with State(config.state_dir) as state:
                plan = plan_library(
                    config, state, library, allow_source_drop=True
                )
                self.assertEqual(plan.status, "ready")
                self.assertEqual(
                    {change.decision for change in plan.changes}, {Decision.HELD.value}
                )
                self.assertEqual(pending_plans(config, "TV"), [])

    def test_allow_pending_without_accepted_changes_does_not_mark_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            target = os.path.join(tmp, "target")
            os.makedirs(os.path.join(source, "Show"))
            os.makedirs(target)
            write(os.path.join(source, "Show", "Episode.mkv"), b"data")
            config = make_config(tmp, source, target)
            config.apply.auto_approve = []
            library = config.library("TV")
            with State(config.state_dir) as state:
                plan = plan_library(config, state, library)
                self.assertEqual(plan.changes[0].decision, Decision.PENDING.value)
                result = apply_plan(
                    config,
                    state,
                    library,
                    plan,
                    run_id="run-pending",
                    allow_pending=True,
                )
                self.assertEqual(result.status, "approval_required")
                self.assertEqual(result.applied, 0)
                self.assertIsNone(plan.applied_at)
                self.assertEqual(plan.status, "approval_required")
                self.assertFalse(os.path.exists(os.path.join(target, "Show")))


if __name__ == "__main__":
    unittest.main()
