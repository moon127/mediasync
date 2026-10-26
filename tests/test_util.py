import json
import os
import tempfile
import unittest

from mediasync.util import (
    FileLock,
    LockHeld,
    SafetyError,
    atomic_write_json,
    directory_fingerprint,
    human_size,
    is_junk_name,
    resolve_target_path,
    tree_fingerprint,
    validate_rel_path,
)


class PathSafetyTests(unittest.TestCase):
    def test_valid_relative_paths(self):
        self.assertEqual(validate_rel_path("Show/Season 1/file.mkv"), "Show/Season 1/file.mkv")

    def test_unsafe_relative_paths_are_rejected(self):
        for value in ["", "/etc/passwd", "../escape", "Show/../../escape", "Show//x", "./x"]:
            with self.subTest(value=value):
                with self.assertRaises(SafetyError):
                    validate_rel_path(value)

    def test_resolve_target_path_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "target")
            outside = os.path.join(tmp, "outside")
            os.makedirs(root)
            os.makedirs(outside)
            os.symlink(outside, os.path.join(root, "link"))
            with self.assertRaises(SafetyError):
                resolve_target_path(root, "link/file.mkv")

    def test_resolve_target_path_accepts_nested_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = resolve_target_path(tmp, "Show/Season 1/file.mkv")
            self.assertTrue(path.startswith(os.path.realpath(tmp)))


class UtilityTests(unittest.TestCase):
    def test_junk_names(self):
        for name in [".DS_Store", "._resource", "Thumbs.db", "desktop.ini", "x.tmp", "x.part", "@Recycle"]:
            with self.subTest(name=name):
                self.assertTrue(is_junk_name(name))
        self.assertFalse(is_junk_name("Episode.mkv"))

    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1024), "1.0 KB")
        self.assertEqual(human_size(1024 ** 3), "1.0 GB")

    def test_atomic_write_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "data.json")
            atomic_write_json(path, {"b": 2, "a": 1})
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"a": 1, "b": 2})

    def test_directory_fingerprint_changes_with_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, empty_count = directory_fingerprint(tmp)
            with open(os.path.join(tmp, "file"), "w", encoding="utf-8") as fh:
                fh.write("x")
            _, count = directory_fingerprint(tmp)
            self.assertEqual(empty_count, 0)
            self.assertEqual(count, 1)

    def test_directory_fingerprint_detects_nested_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            season = os.path.join(tmp, "Season 1")
            os.makedirs(season)
            _, before = directory_fingerprint(tmp)
            with open(os.path.join(season, "Episode.mkv"), "w", encoding="utf-8") as fh:
                fh.write("x")
            _, after = directory_fingerprint(tmp)
            self.assertEqual(before, 1)
            self.assertEqual(after, 2)

    def test_tree_fingerprint_detects_changes_beyond_max_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "Season 1")
            os.makedirs(nested)
            newer = os.path.join(nested, "Newer.mkv")
            changed = os.path.join(nested, "Changed.mkv")
            future = 2_000_000_000_000_000_000
            for path in (newer, changed):
                with open(path, "wb") as fh:
                    fh.write(b"old")
            os.utime(newer, ns=(future, future))
            os.utime(changed, ns=(1_000_000_000_000_000_000,) * 2)
            before_mtime, count, before = tree_fingerprint(tmp)
            with open(changed, "wb") as fh:
                fh.write(b"new content")
            os.utime(changed, ns=(1_000_000_000_000_000_001,) * 2)
            after_mtime, _, after = tree_fingerprint(tmp)
            self.assertEqual(before_mtime, after_mtime)
            self.assertNotEqual(before, after)
            self.assertEqual(count, 3)

    def test_file_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock")
            with FileLock(path):
                with self.assertRaises(LockHeld):
                    with FileLock(path):
                        pass


if __name__ == "__main__":
    unittest.main()
