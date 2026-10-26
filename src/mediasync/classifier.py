from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from .models import (
    Action,
    Change,
    FileInfo,
    is_media_file,
    path_extension,
)
from .rsync_backend import RawChange
from .util import human_delta, is_junk_name

EPISODE_PATTERNS = [
    re.compile(r"(?i)\bS(\d{1,2})[ ._-]?E(\d{1,3})\b"),
    re.compile(r"(?i)\b(\d{1,2})x(\d{2,3})\b"),
    re.compile(r"(?i)\bSeason[ ._-]?(\d{1,2}).*?Episode[ ._-]?(\d{1,3})\b"),
]

QUALITY_TAG = re.compile(
    r"(?i)\b(2160p|1080p|720p|480p|4k|uhd|web[- ]?dl|webrip|bluray|blu-ray|"
    r"remux|proper|repack|x264|x265|h\.?264|h\.?265|hevc|avc|aac|dts|"
    r"ddp?5\.1|dd5\.1|atmos|hdr|dv|10bit|8bit|amzn|nf|dsnp)\b"
)
BRACKETS = re.compile(r"[\[\(\{].*?[\]\)\}]")


@dataclass
class ClassifiedTitle:
    changes: list[Change]
    warnings: list[str]


def episode_key(path: str) -> str | None:
    name = os.path.basename(path)
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(name)
        if match:
            return f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}"
    return None


def clean_media_name(path: str) -> str:
    name = os.path.basename(path)
    name = os.path.splitext(name)[0]
    name = BRACKETS.sub(" ", name)
    name = QUALITY_TAG.sub(" ", name)
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name


def logical_key(kind: str, title: str, path: str) -> str:
    if kind == "tv":
        key = episode_key(path)
        if key:
            return f"tv:{key}"
        return f"tv:{clean_media_name(path)}"
    return f"movie:{clean_media_name(path)}"


def media_bool(path: str) -> bool:
    return is_media_file(path)


def _info(path: str, rel_path: str) -> FileInfo | None:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return FileInfo(
        path=rel_path,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        is_dir=os.path.isdir(path) and not os.path.islink(path),
        exists=True,
    )


def classify_title(
    kind: str,
    title: str,
    raw_changes: list[RawChange],
    source_title: str,
    target_title: str,
    *,
    probe: bool = False,
    probe_timeout: int = 20,
) -> ClassifiedTitle:
    warnings: list[str] = []
    adds: list[Change] = []
    updates: list[Change] = []
    deletes: list[Change] = []

    for raw in raw_changes:
        rel = raw.path.strip("/")
        if not rel or is_junk_name(os.path.basename(rel)):
            continue
        if any(part.startswith(".mediasync-") for part in rel.split("/")):
            continue
        library_rel = f"{title}/{rel}"
        source_path = os.path.join(source_title, rel)
        target_path = os.path.join(target_title, rel)
        media = media_bool(rel)
        if raw.kind == "add":
            source = _info(source_path, library_rel)
            if source is None:
                warnings.append(f"source file vanished during scan: {library_rel}")
                continue
            changes = Change(
                title=title,
                action=Action.ADD.value,
                path=library_rel,
                source=source,
                logical_key=logical_key(kind, title, rel),
                media=media,
                reason="new file at source",
            )
            if media and probe:
                probe_info = probe_media(source_path, probe_timeout)
                if probe_info:
                    changes.details["probe"] = probe_info
            adds.append(changes)
        elif raw.kind == "update":
            source = _info(source_path, library_rel)
            target = _info(target_path, library_rel)
            if source is None:
                warnings.append(f"source file vanished during scan: {library_rel}")
                continue
            if target is None:
                changes = Change(
                    title=title,
                    action=Action.ADD.value,
                    path=library_rel,
                    source=source,
                    logical_key=logical_key(kind, title, rel),
                    media=media,
                    reason="file appeared at source during scan",
                )
                adds.append(changes)
                continue
            if media:
                action = Action.REENCODE.value
                delta = human_delta(target.size, source.size)
                reason = (
                    f"media file changed ({target.size} -> {source.size} bytes, {delta})"
                )
            else:
                action = Action.UPDATE.value
                reason = "sidecar/metadata file changed"
            change = Change(
                title=title,
                action=action,
                path=library_rel,
                source=source,
                target=target,
                logical_key=logical_key(kind, title, rel),
                media=media,
                reason=reason,
                details={
                    "old_size": target.size,
                    "new_size": source.size,
                    "delta_pct": _delta_pct(target.size, source.size),
                },
            )
            if media and probe:
                old_probe = probe_media(target_path, probe_timeout)
                new_probe = probe_media(source_path, probe_timeout)
                if old_probe:
                    change.details["old_probe"] = old_probe
                if new_probe:
                    change.details["new_probe"] = new_probe
            updates.append(change)
        elif raw.kind == "delete":
            target = _info(target_path, library_rel)
            if target is None:
                continue
            if target.is_dir:
                continue
            change = Change(
                title=title,
                action=Action.DELETE.value,
                path=library_rel,
                target=target,
                logical_key=logical_key(kind, title, rel),
                media=media,
                reason="removed at source",
            )
            deletes.append(change)

    warnings.extend(_merge_reencodes(deletes, adds, kind))
    return ClassifiedTitle(
        changes=deletes + updates + adds,
        warnings=warnings,
    )


def _merge_reencodes(
    deletes: list[Change], adds: list[Change], kind: str
) -> list[str]:
    warnings: list[str] = []
    deleted_media: dict[str, list[Change]] = {}
    added_media: dict[str, list[Change]] = {}
    for change in deletes:
        if change.media and change.logical_key:
            deleted_media.setdefault(change.logical_key, []).append(change)
    for change in adds:
        if change.media and change.logical_key:
            added_media.setdefault(change.logical_key, []).append(change)

    for key, old_changes in list(deleted_media.items()):
        new_changes = added_media.get(key)
        if not new_changes:
            continue
        if len(old_changes) != 1 or len(new_changes) != 1:
            warnings.append(
                f"multiple media files share logical key {key}; "
                "leaving them as separate add/delete changes"
            )
            continue
        old = old_changes[0]
        new = new_changes[0]
        old_size = old.target.size if old.target else 0
        new_size = new.source.size if new.source else 0
        details = {
            "old_size": old_size,
            "new_size": new_size,
            "delta_pct": _delta_pct(old_size, new_size),
            "old_path": old.path,
            "new_path": new.path,
            "reencode": True,
        }
        if old.details.get("old_probe"):
            details["old_probe"] = old.details["old_probe"]
        if new.details.get("probe"):
            details["new_probe"] = new.details["probe"]
        merged = Change(
            title=new.title,
            action=Action.REENCODE.value,
            path=new.path,
            old_path=old.path,
            source=new.source,
            target=old.target,
            logical_key=key,
            media=True,
            reason=(
                f"likely re-encode: {old_size} -> {new_size} bytes "
                f"({human_delta(old_size, new_size)})"
            ),
            details=details,
        )
        deletes.remove(old)
        adds.remove(new)
        adds.append(merged)
        added_media[key] = [merged]
    return warnings


def _delta_pct(old: int, new: int) -> float | None:
    if old <= 0:
        return None
    return round((new - old) / old * 100.0, 2)


def probe_media(path: str, timeout: int = 20) -> dict[str, Any] | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,bit_rate:format=duration,size,bit_rate",
        "-of",
        "json",
        path,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    fmt = data.get("format") or {}
    return {
        "codec": stream.get("codec_name"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "bit_rate": stream.get("bit_rate") or fmt.get("bit_rate"),
        "duration": fmt.get("duration"),
    }


def probe_summary(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    if value.get("width") and value.get("height"):
        parts.append(f"{value['width']}x{value['height']}")
    if value.get("codec"):
        parts.append(str(value["codec"]))
    if value.get("bit_rate"):
        try:
            parts.append(f"{int(value['bit_rate']) / 1_000_000:.1f} Mbps")
        except (TypeError, ValueError):
            pass
    return " ".join(parts)


def sidecar_hint(path: str) -> str:
    ext = path_extension(path)
    if not ext:
        return "file"
    return ext.lstrip(".")
