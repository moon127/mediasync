from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone

from .config import Config
from .models import LibraryConfig
from .state import State
from .util import (
    SafetyError,
    du_size,
    ensure_dir,
    now_stamp,
    path_is_within,
    remove_empty_dirs,
    resolve_target_path,
)


class TrashError(RuntimeError):
    pass


def trash_root(config: Config, library: LibraryConfig) -> str:
    return os.path.join(os.path.realpath(library.target), config.trash_dir_name)


def _unique_path(path: str) -> str:
    if not os.path.lexists(path):
        return path
    base, ext = os.path.splitext(path)
    index = 1
    while True:
        candidate = f"{base}.{index}{ext}"
        if not os.path.lexists(candidate):
            return candidate
        index += 1


def quarantine(
    config: Config,
    library: LibraryConfig,
    state: State,
    run_id: str,
    rel_path: str,
    reason: str,
    *,
    size: int = 0,
    entry_type: str = "file",
    permanent: bool = False,
) -> dict:
    target_root = os.path.realpath(library.target)
    source = resolve_target_path(target_root, rel_path)
    if not os.path.lexists(source):
        raise TrashError(f"cannot quarantine missing path: {rel_path}")
    if os.path.islink(source):
        raise SafetyError(f"refusing to quarantine symlink: {rel_path}")
    if entry_type == "dir" or os.path.isdir(source):
        entry_type = "dir"
        actual_size = du_size(source)
    else:
        entry_type = "file"
        actual_size = os.stat(source, follow_symlinks=False).st_size

    if permanent:
        if entry_type == "dir":
            shutil.rmtree(source)
        else:
            os.remove(source)
        state.audit(run_id, library.name, "delete_permanent", rel_path, reason)
        return {
            "permanent": True,
            "original_path": rel_path,
            "size": actual_size,
            "entry_type": entry_type,
        }

    root = trash_root(config, library)
    destination = os.path.join(root, run_id, "deleted", rel_path)
    ensure_dir(os.path.dirname(destination))
    destination = _unique_path(destination)
    os.rename(source, destination)
    trash_rel = os.path.relpath(destination, target_root)
    if trash_rel.startswith(".."):
        raise SafetyError("trash destination escaped target root")
    state.add_trash(
        library.name,
        run_id,
        rel_path,
        trash_rel,
        actual_size or size,
        entry_type,
        reason,
    )
    state.audit(run_id, library.name, "quarantine", rel_path, reason)
    parent = os.path.dirname(source)
    if entry_type == "file":
        stop = os.path.join(target_root, rel_path.split("/", 1)[0]) if "/" in rel_path else target_root
        remove_empty_dirs(parent, stop)
    return {
        "permanent": False,
        "original_path": rel_path,
        "trash_path": trash_rel,
        "size": actual_size or size,
        "entry_type": entry_type,
    }


def list_trash(
    state: State, library: str | None = None, include_done: bool = False
) -> list[dict]:
    return state.list_trash(library=library, include_done=include_done)


def restore(
    config: Config,
    library: LibraryConfig,
    state: State,
    trash_id: int,
    *,
    overwrite: bool = False,
) -> str:
    row = state.get_trash(trash_id)
    if not row:
        raise TrashError(f"trash entry not found: {trash_id}")
    if row["library"] != library.name:
        raise TrashError(
            f"trash entry {trash_id} belongs to library {row['library']}, not {library.name}"
        )
    if row.get("restored_at") or row.get("purged_at"):
        raise TrashError(f"trash entry {trash_id} is already closed")
    target_root = os.path.realpath(library.target)
    trash_path = os.path.join(target_root, row["trash_path"])
    if not path_is_within(trash_path, trash_root(config, library)):
        raise SafetyError("trash entry escapes the trash directory")
    if not os.path.lexists(trash_path):
        raise TrashError(f"trash payload is missing: {row['trash_path']}")
    original = resolve_target_path(target_root, row["original_path"])
    if os.path.lexists(original):
        if not overwrite:
            raise TrashError(
                f"restore target already exists: {row['original_path']} "
                "(use --overwrite to quarantine it first)"
            )
        quarantine(
            config,
            library,
            state,
            f"restore-{now_stamp()}",
            row["original_path"],
            "existing target replaced during restore",
        )
    ensure_dir(os.path.dirname(original))
    os.rename(trash_path, original)
    state.mark_trash_restored(trash_id)
    state.audit(
        None,
        library.name,
        "trash_restore",
        row["original_path"],
        f"trash id {trash_id}",
    )
    try:
        remove_empty_dirs(os.path.dirname(trash_path), trash_root(config, library))
    except OSError:
        pass
    return original


def purge(
    config: Config,
    library: LibraryConfig,
    state: State,
    *,
    older_than_days: int | None = None,
    trash_id: int | None = None,
    all_entries: bool = False,
) -> dict[str, int]:
    rows = state.list_trash(library=library.name)
    if trash_id is not None:
        rows = [row for row in rows if int(row["id"]) == trash_id]
    cutoff = None
    if older_than_days is not None and older_than_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    removed = 0
    bytes_removed = 0
    target_root = os.path.realpath(library.target)
    for row in rows:
        if not all_entries and cutoff is not None:
            try:
                deleted = datetime.fromisoformat(row["deleted_at"])
            except ValueError:
                deleted = datetime.now(timezone.utc)
            if deleted > cutoff:
                continue
        if not all_entries and cutoff is None and trash_id is None:
            continue
        path = os.path.join(target_root, row["trash_path"])
        if not path_is_within(path, trash_root(config, library)):
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
        state.mark_trash_purged(int(row["id"]))
        state.audit(
            None,
            library.name,
            "trash_purge",
            row["original_path"],
            f"trash id {row['id']}",
        )
        removed += 1
        bytes_removed += int(row["size"] or 0)
        try:
            remove_empty_dirs(os.path.dirname(path), trash_root(config, library))
        except OSError:
            pass
    return {"removed": removed, "bytes": bytes_removed}
