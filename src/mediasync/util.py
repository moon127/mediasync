from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


JUNK_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "@Recycle",
    "@Recently-Snapshot",
    "#recycle",
}

JUNK_PREFIXES = ("._", ".mediasync-")
JUNK_SUFFIXES = (".partial", ".tmp", ".crdownload", ".!qb", ".part")


class LockHeld(RuntimeError):
    pass


class SafetyError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def human_size(value: int | float | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    sign = "-" if size < 0 else ""
    size = abs(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{sign}{int(size)} {unit}"
            return f"{sign}{size:.1f} {unit}"
        size /= 1024.0
    return f"{sign}{size:.1f} PB"


def human_delta(old: int, new: int) -> str:
    if old <= 0:
        return "-"
    pct = (new - old) / old * 100.0
    return f"{pct:+.0f}%"


def expand_path(value: str | None, base: str | None = None) -> str | None:
    if value is None:
        return None
    expanded = os.path.expandvars(os.path.expanduser(value))
    if not os.path.isabs(expanded) and base:
        expanded = os.path.join(base, expanded)
    return os.path.abspath(expanded)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def atomic_write_json(path: str, data: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    ensure_dir(directory)
    tmp = f"{path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def is_junk_name(name: str) -> bool:
    lower = name.lower()
    if name in JUNK_NAMES or lower in {n.lower() for n in JUNK_NAMES}:
        return True
    if name.startswith(JUNK_PREFIXES):
        return True
    return lower.endswith(JUNK_SUFFIXES)


def list_visible_titles(root: str) -> list[str]:
    try:
        with os.scandir(root) as entries:
            names = [
                entry.name
                for entry in entries
                if entry.is_dir(follow_symlinks=False) and not is_junk_name(entry.name)
            ]
    except FileNotFoundError:
        return []
    return sorted(names)


def find_mount_point(path: str) -> str:
    current = os.path.realpath(path)
    while True:
        if os.path.ismount(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return current
        current = parent


def path_is_within(child: str, parent: str) -> bool:
    try:
        child_real = os.path.realpath(child)
        parent_real = os.path.realpath(parent)
        return os.path.commonpath([child_real, parent_real]) == parent_real
    except ValueError:
        return False


def validate_rel_path(value: str) -> str:
    if not value or "\x00" in value:
        raise SafetyError("empty or invalid path")
    if value.startswith(("/", "\\")):
        raise SafetyError(f"absolute path is not allowed: {value}")
    parts = value.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SafetyError(f"unsafe path component: {value}")
    pure = PurePosixPath(*parts)
    if pure.is_absolute():
        raise SafetyError(f"absolute path is not allowed: {value}")
    return pure.as_posix()


def assert_no_symlink_components(path: str, root: str) -> None:
    root_real = os.path.realpath(root)
    current = os.path.realpath(path)
    if not path_is_within(current, root_real):
        raise SafetyError(f"path escapes root: {path}")
    probe = current
    while probe != root_real and probe != os.path.dirname(probe):
        if os.path.islink(probe):
            raise SafetyError(f"symlink not allowed in target path: {probe}")
        probe = os.path.dirname(probe)


def resolve_target_path(root: str, rel_path: str) -> str:
    rel = validate_rel_path(rel_path)
    root_real = os.path.realpath(root)
    full = os.path.join(root_real, rel)
    if not path_is_within(full, root_real):
        raise SafetyError(f"path escapes target root: {rel_path}")
    assert_no_symlink_components(full, root_real)
    return full


def stat_info(path: str, rel_path: str | None = None) -> dict[str, Any] | None:
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return {
        "path": rel_path or path,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "is_dir": os.path.isdir(path) and not os.path.islink(path),
        "exists": True,
    }


def tree_fingerprint(path: str) -> tuple[int | None, int, str | None]:
    try:
        root_stat = os.stat(path, follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return None, 0, None
    max_mtime = root_stat.st_mtime_ns
    count = 0
    digest = hashlib.sha256()
    stack: list[tuple[str, str]] = [(path, "")]
    while stack:
        current, rel_base = stack.pop()
        try:
            with os.scandir(current) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except (FileNotFoundError, OSError):
            digest.update(f"?\0{rel_base}\n".encode("utf-8", "surrogateescape"))
            continue
        for entry in entries:
            count += 1
            rel = rel_base + entry.name
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                digest.update(f"?\0{rel}\n".encode("utf-8", "surrogateescape"))
                continue
            if entry.is_dir(follow_symlinks=False):
                kind = "d"
                stack.append((entry.path, rel + "/"))
            elif entry.is_symlink():
                kind = "l"
            else:
                kind = "f"
            digest.update(
                f"{kind}\0{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode(
                    "utf-8", "surrogateescape"
                )
            )
            if st.st_mtime_ns > max_mtime:
                max_mtime = st.st_mtime_ns
    return max_mtime, count, digest.hexdigest()


def directory_fingerprint(path: str) -> tuple[int | None, int]:
    mtime, count, _digest = tree_fingerprint(path)
    return mtime, count


def du_size(path: str, timeout: int = 120) -> int:
    if not os.path.exists(path):
        return 0
    try:
        proc = subprocess.run(
            ["du", "-sk", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return int(proc.stdout.split()[0]) * 1024
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    total = 0
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                try:
                    total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


class FileLock:
    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def __enter__(self) -> FileLock:
        ensure_dir(os.path.dirname(os.path.abspath(self.path)))
        self.fh = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                self.fh.close()
                self.fh = None
                raise LockHeld(f"another mediasync process holds {self.path}") from exc
            raise
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"{os.getpid()}\n{now_iso()}\n")
        self.fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def notify(command: str | None, report_path: str | None, code: int) -> None:
    if not command:
        return
    args = shlex.split(command)
    args.extend([report_path or "", str(code)])
    try:
        subprocess.run(args, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        logging.getLogger(__name__).warning("notification command failed: %s", command)


def iter_files(root: str) -> Iterable[str]:
    for base, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            yield os.path.join(base, name)


def remove_empty_dirs(start: str, stop: str) -> None:
    current = start
    stop_real = os.path.realpath(stop)
    while path_is_within(current, stop_real) and os.path.realpath(current) != stop_real:
        try:
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)
