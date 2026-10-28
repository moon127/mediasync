from __future__ import annotations

import atexit
import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from .models import RsyncOptions
from .util import ensure_dir

LOG = logging.getLogger(__name__)

_ACTIVE_PROCS: dict[subprocess.Popen, int | None] = {}
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)

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

_RSYNC_VERSION_CACHE: tuple[int, int] | None = None


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
    progress: bool = False,
) -> list[str]:
    cmd = ["rsync"]
    cmd.extend(_common_args(options, apply=True))
    if partial_dir:
        ensure_dir(partial_dir)
        cmd.extend(["--partial-dir", partial_dir])
    cmd.extend([_with_slash(source_title), _with_slash(target_title)])
    if progress:
        cmd = _add_progress_flags(cmd, enable=True)
    return cmd


def copy_files_command(
    source_title: str,
    target_title: str,
    options: RsyncOptions,
    backup_dir: str,
    partial_dir: str | None = None,
    progress: bool = False,
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
    if progress:
        cmd = _add_progress_flags(cmd, enable=True)
    return cmd


def _with_slash(path: str) -> str:
    return path if path.endswith(os.sep) else path + os.sep


def get_rsync_version() -> tuple[int, int] | None:
    global _RSYNC_VERSION_CACHE
    if _RSYNC_VERSION_CACHE is not None:
        return _RSYNC_VERSION_CACHE
    try:
        proc = subprocess.run(
            ["rsync", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=5,
        )
    except Exception:
        return None
    text = proc.stdout or proc.stderr or ""
    match = re.search(r"rsync\s+version\s+(\d+)\.(\d+)", text)
    if not match:
        return None
    try:
        version = (int(match.group(1)), int(match.group(2)))
    except Exception:
        return None
    _RSYNC_VERSION_CACHE = version
    return version


def supports_progress2() -> bool:
    version = get_rsync_version()
    if version is None:
        return False
    major, minor = version
    if major > 3:
        return True
    if major == 3 and minor >= 1:
        return True
    return False


def _add_progress_flags(cmd: list[str], *, enable: bool) -> list[str]:
    if not enable:
        return cmd
    if supports_progress2():
        result = list(cmd)
        result.insert(1, "--info=progress2")
        return result
    result = list(cmd)
    result.insert(1, "--progress")
    return result


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
    except subprocess.TimeoutExpired:
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


_PROGRESS_RE = re.compile(
    r"(?P<transferred>[\d,]+)\s+(?P<percent>\d+)%\s+(?P<speed>[^\s]+)\s+(?P<eta>[^\s]+)"
)
_PROGRESS2_RE = re.compile(
    r"^\s*(?P<transferred>[\d,]+)\s+(?P<percent>\d+)%\s+(?P<speed>[^\s\/]+)\s+(?P<eta>[^\s]+)"
)
_PROGRESS2_ALT = re.compile(
    r"(?P<transferred>[\d,]+)\s+(?P<percent>\d+)%\s+(?P<speed>[0-9\.]+[KMG]?[B]?/?[sS]?)\s+(?P<eta>(?:[\d:]+|[\d\.]+[smh]?|unknown|-))"
)


def _parse_progress_line(line: str) -> dict | None:
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    for pattern in (_PROGRESS2_RE, _PROGRESS2_ALT, _PROGRESS_RE):
        match = pattern.match(line) or pattern.search(line)
        if match:
            try:
                transferred = int(match.group("transferred").replace(",", ""))
            except Exception:
                transferred = 0
            try:
                percent = int(match.group("percent"))
            except Exception:
                percent = 0
            speed = match.group("speed")
            eta = match.group("eta")
            return {
                "transferred": transferred,
                "percent": percent,
                "speed": speed,
                "eta": eta,
                "raw": line,
            }
    # Try to catch lines with just numbers/percent
    if re.search(r"\d+%", line):
        m = re.search(r"([0-9,]+).*?(\d+)%.*?([0-9\.]+[KMG]?[B]/s).*?(eta\s+)?([0-9:]+|[0-9\.]+[smh]|unknown|-)", line)
        if m:
            try:
                transferred = int(m.group(1).replace(",", ""))
            except Exception:
                transferred = 0
            try:
                percent = int(m.group(2))
            except Exception:
                percent = 0
            return {
                "transferred": transferred,
                "percent": percent,
                "speed": m.group(3),
                "eta": m.group(5),
                "raw": line,
            }
    return None


def _register_process(proc: subprocess.Popen) -> int | None:
    pgid: int | None = None
    if proc.pid:
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
    _ACTIVE_PROCS[proc] = pgid
    return pgid


def _close_pipes(proc: subprocess.Popen) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _unregister_process(proc: subprocess.Popen) -> None:
    _ACTIVE_PROCS.pop(proc, None)
    _close_pipes(proc)


def active_rsync_count() -> int:
    dead = [proc for proc in _ACTIVE_PROCS if proc.poll() is not None]
    for proc in dead:
        _unregister_process(proc)
    return len(_ACTIVE_PROCS)


def _kill_target(proc: subprocess.Popen, pgid: int | None, sig: int) -> bool:
    try:
        if os.name != "nt" and pgid is not None:
            os.killpg(pgid, sig)
        elif sig == _SIGTERM:
            proc.terminate()
        else:
            proc.kill()
        return True
    except ProcessLookupError:
        return True
    except Exception:
        return False


def kill_all_active(grace_timeout: float = 2.0) -> dict:
    active = [(proc, pgid) for proc, pgid in _ACTIVE_PROCS.items() if proc.poll() is None]
    killed = len(active)
    failed = 0

    for proc, pgid in active:
        if not _kill_target(proc, pgid, _SIGTERM):
            failed += 1

    deadline = time.monotonic() + max(0.0, grace_timeout)
    for proc, _pgid in active:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

    for proc, pgid in active:
        if proc.poll() is None:
            _kill_target(proc, pgid, _SIGKILL)
        _unregister_process(proc)

    _ACTIVE_PROCS.clear()
    return {"killed": killed, "failed": failed}


def _atexit_cleanup() -> None:
    try:
        kill_all_active(grace_timeout=0.5)
    except Exception:
        pass


atexit.register(_atexit_cleanup)


def run_command(
    cmd: list[str],
    *,
    input_data: bytes | None = None,
    timeout: int = 3600,
    progress_cb: Callable[[dict], None] | None = None,
    total_bytes_hint: int | None = None,
) -> CommandResult:
    if progress_cb is None:
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
        except subprocess.TimeoutExpired:
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

    try:
        kwargs = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
    except FileNotFoundError as exc:
        raise RsyncError("rsync executable not found") from exc

    _register_process(proc)

    last_update = 0.0
    stderr_lines: list[str] = []

    def _emit(data: dict) -> None:
        nonlocal last_update
        now = time.monotonic()
        if now - last_update < 0.25:
            return
        last_update = now
        try:
            progress_cb(data)
        except Exception:
            pass

    try:
        if input_data is not None and proc.stdin is not None:
            try:
                proc.stdin.write(input_data)
                proc.stdin.close()
            except Exception:
                pass

        # Read stderr for progress (rsync outputs progress there)
        if proc.stderr is not None:
            buffer = ""
            try:
                while True:
                    chunk = proc.stderr.read(1024)
                    if not chunk:
                        break
                    try:
                        text = chunk.decode("utf-8", "surrogateescape")
                    except Exception:
                        continue
                    buffer += text
                    while True:
                        if "\r\n" in buffer:
                            line, buffer = buffer.split("\r\n", 1)
                            sep = "\r\n"
                        elif "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            sep = "\n"
                        elif "\r" in buffer:
                            line, buffer = buffer.split("\r", 1)
                            sep = "\r"
                        else:
                            break
                        stderr_lines.append(line + sep)
                        parsed = _parse_progress_line(line)
                        if parsed:
                            if total_bytes_hint and parsed.get("transferred"):
                                try:
                                    transferred = int(parsed["transferred"])
                                    if total_bytes_hint > 0:
                                        parsed["percent"] = min(100, int((transferred / total_bytes_hint) * 100))
                                except Exception:
                                    pass
                            _emit(parsed)
                    parsed = _parse_progress_line(buffer)
                    if parsed and ("\r" in text or "--info=progress2" in " ".join(cmd)):
                        if total_bytes_hint and parsed.get("transferred"):
                            try:
                                transferred = int(parsed["transferred"])
                                if total_bytes_hint > 0:
                                    parsed["percent"] = min(100, int((transferred / total_bytes_hint) * 100))
                            except Exception:
                                pass
                        _emit(parsed)
            except Exception:
                pass
    except Exception:
        pass

    try:
        stdout_data = b""
        if proc.stdout is not None:
            stdout_data = proc.stdout.read() or b""
    except Exception:
        stdout_data = b""

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, _SIGKILL)
                except Exception:
                    proc.kill()
            else:
                proc.kill()
        except Exception:
            pass
        _unregister_process(proc)
        return CommandResult(
            returncode=124,
            stderr=f"rsync timed out after {timeout}s: {_format_command(cmd)}\n" + "".join(stderr_lines),
            command=cmd,
        )

    _unregister_process(proc)

    return CommandResult(
        returncode=proc.returncode,
        stdout=stdout_data.decode("utf-8", "surrogateescape"),
        stderr="".join(stderr_lines),
        command=cmd,
    )


def copy_title(
    source_title: str,
    target_title: str,
    options: RsyncOptions,
    partial_dir: str | None = None,
    progress_cb: Callable[[dict], None] | None = None,
    total_bytes_hint: int | None = None,
    enable_progress: bool = True,
) -> CommandResult:
    cmd = copy_title_command(source_title, target_title, options, partial_dir, progress=enable_progress and progress_cb is not None)
    LOG.debug("running: %s", _format_command(cmd))
    return run_command(cmd, timeout=max(options.timeout + 60, 120), progress_cb=progress_cb, total_bytes_hint=total_bytes_hint)


def copy_files(
    source_title: str,
    target_title: str,
    rel_paths: list[str],
    options: RsyncOptions,
    backup_dir: str,
    partial_dir: str | None = None,
    progress_cb: Callable[[dict], None] | None = None,
    total_bytes_hint: int | None = None,
    enable_progress: bool = True,
) -> CommandResult:
    cmd = copy_files_command(
        source_title, target_title, options, backup_dir, partial_dir, progress=enable_progress and progress_cb is not None
    )
    payload = ("\0".join(rel_paths) + "\0").encode("utf-8", "surrogateescape")
    LOG.debug("running: %s", _format_command(cmd))
    return run_command(
        cmd, input_data=payload, timeout=max(options.timeout + 60, 120), progress_cb=progress_cb, total_bytes_hint=total_bytes_hint
    )
