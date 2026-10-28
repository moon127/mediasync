import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

from mediasync.models import DEFAULT_EXCLUDES, RsyncOptions
from mediasync.rsync_backend import (
    SEPARATOR,
    RsyncOptionError,
    _parse_progress_line,
    active_rsync_count,
    copy_files_command,
    copy_title_command,
    kill_all_active,
    parse_scan_output,
    run_command,
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


class ProgressParserTests(unittest.TestCase):
    def test_parses_progress2_line(self):
        parsed = _parse_progress_line("  1,234,567  45%  12.34MB/s  0:00:12")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["transferred"], 1234567)
        self.assertEqual(parsed["percent"], 45)
        self.assertEqual(parsed["speed"], "12.34MB/s")
        self.assertEqual(parsed["eta"], "0:00:12")

    def test_parses_classic_progress_line_with_trailing_details(self):
        parsed = _parse_progress_line(
            "  1,234,567  45%  12.34MB/s    0:00:12 (xfr#1, to-chk=0/5)"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["transferred"], 1234567)
        self.assertEqual(parsed["percent"], 45)
        self.assertEqual(parsed["eta"], "0:00:12")

    def test_ignores_non_progress_lines(self):
        self.assertIsNone(_parse_progress_line(""))
        self.assertIsNone(_parse_progress_line("sending incremental file list"))
        self.assertIsNone(_parse_progress_line("rsync error: some warning"))


@unittest.skipIf(os.name == "nt", "process group handling is POSIX-specific")
class ActiveProcessTests(unittest.TestCase):
    def test_run_command_registers_and_unregisters_process(self):
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        thread = threading.Thread(
            target=run_command,
            kwargs={
                "cmd": cmd,
                "progress_cb": lambda _data: None,
                "timeout": 30,
            },
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(kill_all_active, 0.1)

        deadline = time.monotonic() + 5
        while active_rsync_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(active_rsync_count(), 1)

        result = kill_all_active(grace_timeout=0.1)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(active_rsync_count(), 0)
        self.assertGreaterEqual(result["killed"], 1)

    def test_kill_all_active_with_no_processes_is_safe(self):
        self.assertEqual(active_rsync_count(), 0)
        result = kill_all_active(grace_timeout=0)
        self.assertEqual(result["killed"], 0)
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()
