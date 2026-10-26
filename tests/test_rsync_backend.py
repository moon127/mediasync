import os
import shutil
import tempfile
import unittest

from mediasync.models import DEFAULT_EXCLUDES, RsyncOptions
from mediasync.rsync_backend import (
    SEPARATOR,
    RsyncOptionError,
    copy_files_command,
    copy_title_command,
    parse_scan_output,
    scan_command,
    scan_title,
    validate_extra_args,
)


class RsyncOptionTests(unittest.TestCase):
    def test_allowed_extra_args(self):
        validate_extra_args(
            ["--bwlimit=50m", "--compress", "--ipv4", "--no-motd"]
        )

    def test_dangerous_flags_are_rejected(self):
        for arg in [
            "-a",
            "--delete",
            "--exclude=secret",
            "--rsync-path=/tmp/evil",
            "--remove-source-files",
        ]:
            with self.subTest(arg=arg):
                with self.assertRaises(RsyncOptionError):
                    validate_extra_args([arg])


class RsyncCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = os.path.join(self.tmp.name, "source", "Show")
        self.target = os.path.join(self.tmp.name, "target", "Show")
        self.partial = os.path.join(self.tmp.name, "target", ".partial")
        self.backup = os.path.join(self.tmp.name, "target", ".trash", "backups", "Show")
        self.options = RsyncOptions(
            modify_window=2,
            fsync=True,
            timeout=45,
            extra_args=["--bwlimit=20m"],
            excludes=[".DS_Store", "*.tmp"],
        )

    def test_scan_command_is_dry_run_and_never_fsyncs(self):
        cmd = scan_command(self.source, self.target, self.options)
        self.assertEqual(cmd[0], "rsync")
        self.assertIn("-n", cmd)
        self.assertIn("--delete", cmd)
        self.assertIn("--modify-window", cmd)
        self.assertIn("2", cmd)
        self.assertIn("--exclude", cmd)
        self.assertIn(".DS_Store", cmd)
        self.assertIn("--bwlimit=20m", cmd)
        self.assertNotIn("--fsync", cmd)
        self.assertEqual(cmd[-2], self.source + os.sep)
        self.assertEqual(cmd[-1], self.target + os.sep)

    def test_copy_title_command_fsyncs_and_does_not_delete(self):
        cmd = copy_title_command(self.source, self.target, self.options)
        self.assertIn("--fsync", cmd)
        self.assertNotIn("-n", cmd)
        self.assertNotIn("--delete", cmd)
        with_partial = copy_title_command(
            self.source, self.target, self.options, self.partial
        )
        self.assertIn("--partial-dir", with_partial)
        self.assertIn(self.partial, with_partial)

    def test_copy_files_command_uses_files_from_and_backup(self):
        cmd = copy_files_command(
            self.source,
            self.target,
            self.options,
            self.backup,
            self.partial,
        )
        self.assertIn("--files-from=-", cmd)
        self.assertIn("--from0", cmd)
        self.assertIn("--backup", cmd)
        self.assertIn("--backup-dir", cmd)
        self.assertIn("--fsync", cmd)
        self.assertNotIn("--delete", cmd)


@unittest.skipUnless(shutil.which("rsync"), "rsync is required")
class ScanIntegrationTests(unittest.TestCase):
    def test_actual_rsync_scan_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source", "Show")
            target = os.path.join(tmp, "target", "Show")
            os.makedirs(source)
            os.makedirs(target)
            common = os.path.join(source, "common.mkv")
            with open(common, "wb") as fh:
                fh.write(b"common")
            shutil.copy2(common, os.path.join(target, "common.mkv"))
            with open(os.path.join(source, "new.mkv"), "wb") as fh:
                fh.write(b"new")
            with open(os.path.join(target, "old.mkv"), "wb") as fh:
                fh.write(b"old")
            options = RsyncOptions(
                modify_window=2,
                fsync=False,
                timeout=20,
                excludes=list(DEFAULT_EXCLUDES),
            )
            result = scan_title(source, target, options)
            self.assertEqual(result.errors, [])
            found = {(change.kind, change.path) for change in result.changes}
            self.assertIn(("add", "new.mkv"), found)
            self.assertIn(("delete", "old.mkv"), found)
            self.assertNotIn(("update", "common.mkv"), found)


class ScanParserTests(unittest.TestCase):
    def test_parses_add_update_delete_and_ignores_directories(self):
        stdout = "\n".join(
            [
                f">f+++++++++{SEPARATOR}100{SEPARATOR}Show/new.mkv",
                f">f.st......{SEPARATOR}99{SEPARATOR}Show/changed.nfo",
                f"*deleting  {SEPARATOR}50{SEPARATOR}Show/old.srt",
                f"cd+++++++++{SEPARATOR}0{SEPARATOR}Show/Extras/",
                f".d..t......{SEPARATOR}0{SEPARATOR}./",
            ]
        )
        changes, warnings = parse_scan_output(stdout)
        self.assertEqual(warnings, [])
        self.assertEqual(
            [(change.kind, change.path, change.size) for change in changes],
            [
                ("add", "Show/new.mkv", 100),
                ("update", "Show/changed.nfo", 99),
                ("delete", "Show/old.srt", 50),
            ],
        )

    def test_unparsed_lines_become_warnings(self):
        changes, warnings = parse_scan_output("nonsense\n")
        self.assertEqual(changes, [])
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
