from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from .config import Config
from .models import LibraryConfig
from .state import State
from .util import (
    SafetyError,
    find_mount_point,
    list_visible_titles,
    path_is_within,
)


@dataclass
class LibraryInspection:
    library: str
    source: str
    target: str
    source_mount: str
    target_mount: str
    source_titles: list[str]
    target_titles: list[str]

    @property
    def source_count(self) -> int:
        return len(self.source_titles)

    @property
    def target_count(self) -> int:
        return len(self.target_titles)


def check_rsync() -> str:
    found = shutil.which("rsync")
    if not found:
        raise SafetyError("rsync executable not found in PATH")
    return found


def inspect_library(config: Config, library: LibraryConfig) -> LibraryInspection:
    check_rsync()
    source = os.path.realpath(library.source)
    target = os.path.realpath(library.target)
    if not os.path.exists(source):
        raise SafetyError(f"{library.name}: source does not exist: {source}")
    if not os.path.exists(target):
        raise SafetyError(f"{library.name}: target does not exist: {target}")
    if not os.path.isdir(source):
        raise SafetyError(f"{library.name}: source is not a directory: {source}")
    if not os.path.isdir(target):
        raise SafetyError(f"{library.name}: target is not a directory: {target}")
    if source == target:
        raise SafetyError(f"{library.name}: source and target are the same directory")
    if path_is_within(source, target) or path_is_within(target, source):
        raise SafetyError(
            f"{library.name}: source and target must not overlap: {source} / {target}"
        )
    if not os.access(source, os.R_OK | os.X_OK):
        raise SafetyError(f"{library.name}: source is not readable: {source}")
    if not os.access(target, os.W_OK | os.X_OK):
        raise SafetyError(f"{library.name}: target is not writable: {target}")

    source_mount = find_mount_point(source)
    target_mount = find_mount_point(target)
    if config.require_mount:
        if source_mount == os.path.sep:
            raise SafetyError(
                f"{library.name}: source is not on a mounted volume: {source}"
            )
        if target_mount == os.path.sep:
            raise SafetyError(
                f"{library.name}: target is not on a mounted volume: {target}"
            )
    return LibraryInspection(
        library=library.name,
        source=source,
        target=target,
        source_mount=source_mount,
        target_mount=target_mount,
        source_titles=list_visible_titles(source),
        target_titles=list_visible_titles(target),
    )


def check_source_sanity(
    config: Config,
    state: State,
    inspection: LibraryInspection,
    *,
    allow_empty_target: bool = False,
    allow_source_drop: bool = False,
) -> list[str]:
    warnings: list[str] = []
    current_source = inspection.source_count
    current_target = inspection.target_count
    if current_source < config.min_source_titles:
        raise SafetyError(
            f"{inspection.library}: source only has {current_source} title(s); "
            f"min_source_titles is {config.min_source_titles}"
        )
    previous = state.get_library(inspection.library)
    if previous:
        previous_source = int(previous.get("last_source_titles") or 0)
        previous_target = int(previous.get("last_target_titles") or 0)
        if previous_source > 0:
            drop = (previous_source - current_source) / previous_source * 100.0
            if drop > config.max_source_title_drop_pct:
                if allow_source_drop:
                    warnings.append(
                        f"{inspection.library}: source title count dropped "
                        f"{drop:.1f}% (override enabled)"
                    )
                else:
                    raise SafetyError(
                        f"{inspection.library}: source title count dropped "
                        f"{drop:.1f}% ({previous_source} -> {current_source}); "
                        "refusing to delete. Use --allow-source-drop to override."
                    )
        if previous_target > 0 and current_target == 0 and not allow_empty_target:
            raise SafetyError(
                f"{inspection.library}: target is unexpectedly empty "
                f"(previously {previous_target} titles). "
                "Use --allow-empty-target if this is intentional."
            )
    return warnings


def describe_mount(path: str) -> str:
    mount = find_mount_point(path)
    if mount == os.path.sep:
        return "local filesystem"
    try:
        st = os.statvfs(mount)
        free = st.f_bavail * st.f_frsize
        return f"{mount} ({free / (1024 ** 3):.1f} GB free)"
    except OSError:
        return mount
