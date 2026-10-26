from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field

from .models import RsyncOptions
from .util import ensure_dir

LOG = logging.getLogger(__name__)

SEPARATOR = "|"

ALLOWED_EXTRA_OPTIONS = {
    "--bwlimit",
    "--compress",
    "--compress-level",
    "--compress-choice",
    "--whole-file",
    "--no-whole-file",
    "--block-size",
    "--checksum-choice",
    "--sparse",
    "--preallocate",
    "--no-motd",
    "--contimeout",
    "--sockopts",
    "--ipv4",
    "--ipv6",
}


class RsyncError(RuntimeError):
    pass


class RsyncOptionError(ValueError):
    pass


def validate_extra_args(args: list[str]) -> None:
    for arg in args:
        if not arg.startswith("--"):
            raise RsyncOptionError(
                f"unsafe rsync extra arg (long-form options only): {arg!r}"
            )
        name = arg.split("=", 1)[0]
        if name not in ALLOWED_EXTRA_OPTIONS:
            raise RsyncOptionError(
                f"rsync extra arg not allowed: {arg!r}; "
                f"allowed: {', '.join(sorted(ALLOWED_EXTRA_OPTIONS))}"
            )


def _common_args(options: RsyncOptions, apply: bool) -> list[str]:
    args = [
        "-rlt",
        "--no-perms",
        "--no-owner",
        "--no-group",
        "--no-xattrs",
        "--no-acls",
        "--no-devices",
        "--no-specials",
        "--timeout",
        str(options.timeout),
    ]
    if options.modify_window > 0:
        args.extend(["--modify-window", str(options.modify_window)])
    if options.checksum:
        args.append("--checksum")
    for pattern in options.excludes:
        args.extend(["--exclude", pattern])
    args.extend(options.extra_args)
    if apply and options.fsync:
        args.append("--fsync")
    return args


def scan_command(
    source_title: str,
    target_title: str,
    options: RsyncOptions,
) -> list[str]:
    cmd = [
        "rsync",
        "-n",
        "-i",
        "--delete",
        "--out-format",
        f"%i{SEPARATOR}%l{SEPARATOR}%n",
    ]
    cmd.extend(_common_args(options, apply=False))
    cmd.extend([_with_slash(source_title), _with_slash(target_title)])
    return cmd


def copy_title_command(
    source_title: str,
    target_title: str,
    options: RsyncOptions,
    partial_dir: str | None = None,
) -> list[str]:
    cmd = ["rsync"]
    cmd.extend(_common_args(options, apply=True))
    if partial_dir:
        ensure_dir(partial_dir)
        cmd.extend(["--partial-dir", partial_dir])
    cmd.extend([_with_slash(source_title), _with_slash(target_title)])
    return cmd


def copy_files_command(
    source_title: str,
    target_title: str,
    options: RsyncOptions,
    backup_dir: str,
    partial_dir: str | None = None,
) -> list[str]:
    ensure_dir(backup_dir)
    cmd = [
        "rsync",
        "-R",
        "--files-from=-",
        "--from0",
        "--backup",
        "--backup-dir",
        backup_dir,
    ]
    cmd.extend(_common_args(options, apply=True))
    if partial_dir:
        ensure_dir(partial_dir)
        cmd.extend(["--partial-dir", partial_dir])
    cmd.extend([_with_slash(source_title), _with_slash(target_title)])
    return cmd


def _with_slash(path: str) -> str:
    return path if path.endswith(os.sep) else path + os.sep


@dataclass
class RawChange:
    kind: str
    path: str
    size: int = 0
    itemize: str = ""


@dataclass
class ScanResult:
    changes: list[RawChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    returncode: int = 0
    command: list[str] = field(default_factory=list)


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    command: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode in {0, 24}


def _format_command(cmd: list[str]) -> str:
    return " ".join(_quote(part) for part in cmd)


def _quote(value: str) -> str:
    if not value:
        return "''"
    if all(ch.isalnum() or ch in "-_./:=%+," for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def parse_scan_output(stdout: str) -> tuple[list[RawChange], list[str]]:
    changes: list[RawChange] = []
    warnings: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        parts = line.split(SEPARATOR, 2)
        if len(parts) != 3:
            warnings.append(f"unparsed rsync output: {line!r}")
            continue
        itemize, length_text, path = parts
        itemize = itemize.strip()
        try:
            size = int(length_text.strip() or "0")
        except ValueError:
            size = 0
        if not itemize or not path:
            continue
        if itemize.startswith("*deleting"):
            changes.append(RawChange("delete", path, size, itemize))
        elif itemize.startswith(">f"):
            kind = "add" if "+++++++++" in itemize else "update"
            changes.append(RawChange(kind, path, size, itemize))
        elif itemize.startswith(("cd", ".d", "cL", ">L", "h", ".f", ".L")):
            continue
        else:
            warnings.append(f"unhandled rsync itemize: {itemize!r} {path!r}")
    return changes, warnings


def scan_title(
    source_title: str,
    target_title: str,
    options: RsyncOptions,
) -> ScanResult:
    cmd = scan_command(source_title, target_title, options)
    process_timeout = max(options.timeout + 30, 60)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=process_timeout,
        )
    except FileNotFoundError as exc:
        raise RsyncError("rsync executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        return ScanResult(
            errors=[f"rsync scan timed out after {process_timeout}s: {_format_command(cmd)}"],
            returncode=124,
            command=cmd,
        )
    result = ScanResult(
        returncode=proc.returncode,
        command=cmd,
    )
    changes, warnings = parse_scan_output(proc.stdout)
    result.changes = changes
    result.warnings.extend(warnings)
    if proc.stderr.strip():
        for line in proc.stderr.splitlines():
            line = line.strip()
            if not line:
                continue
            if "skipping non-regular" in line.lower():
                result.warnings.append(line)
            elif "file has vanished" in line.lower():
                result.warnings.append(line)
            else:
                result.errors.append(line)
    if proc.returncode not in {0, 24}:
        if not result.errors:
            result.errors.append(f"rsync exited with code {proc.returncode}")
    return result


def run_command(
    cmd: list[str],
    *,
    input_data: bytes | None = None,
    timeout: int = 3600,
) -> CommandResult:
    try:
        proc = subprocess.run(
            cmd,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RsyncError("rsync executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=124,
            stderr=f"rsync timed out after {timeout}s: {_format_command(cmd)}",
            command=cmd,
        )
    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout.decode("utf-8", "surrogateescape"),
        stderr=proc.stderr.decode("utf-8", "surrogateescape"),
        command=cmd,
    )


def copy_title(
    source_title: str,
    target_title: str,
    options: RsyncOptions,
    partial_dir: str | None = None,
) -> CommandResult:
    cmd = copy_title_command(source_title, target_title, options, partial_dir)
    LOG.debug("running: %s", _format_command(cmd))
    return run_command(cmd, timeout=max(options.timeout + 60, 120))


def copy_files(
    source_title: str,
    target_title: str,
    rel_paths: list[str],
    options: RsyncOptions,
    backup_dir: str,
    partial_dir: str | None = None,
) -> CommandResult:
    cmd = copy_files_command(
        source_title, target_title, options, backup_dir, partial_dir
    )
    payload = ("\0".join(rel_paths) + "\0").encode("utf-8", "surrogateescape")
    LOG.debug("running: %s", _format_command(cmd))
    return run_command(
        cmd, input_data=payload, timeout=max(options.timeout + 60, 120)
    )
