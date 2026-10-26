from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Action(str, Enum):
    ADD_TITLE = "add_title"
    ADD = "add"
    UPDATE = "update"
    DELETE_TITLE = "delete_title"
    DELETE = "delete"
    REPLACE = "replace"
    REENCODE = "reencode"


class Decision(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DENIED = "denied"
    HELD = "held"


class PlanStatus(str, Enum):
    READY = "ready"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"
    ERROR = "error"
    APPLIED = "applied"
    PARTIAL = "partial"
    FAILED = "failed"


MEDIA_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".m2ts",
    ".mts",
    ".ts",
    ".iso",
    ".vob",
    ".ogm",
    ".divx",
}

SIDECAR_EXTENSIONS = {
    ".nfo",
    ".xml",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".srt",
    ".ass",
    ".ssa",
    ".sub",
    ".idx",
    ".sup",
    ".metathumb",
    ".txt",
    ".sfv",
    ".md5",
}


def path_extension(path: str) -> str:
    lower = path.lower()
    idx = lower.rfind(".")
    if idx < 0:
        return ""
    return lower[idx:]


def is_media_file(path: str) -> bool:
    return path_extension(path) in MEDIA_EXTENSIONS


@dataclass
class FileInfo:
    path: str
    size: int = 0
    mtime_ns: int = 0
    is_dir: bool = False
    exists: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FileInfo | None:
        if not data:
            return None
        return cls(**data)


@dataclass
class Change:
    title: str
    action: str
    path: str
    old_path: str | None = None
    logical_key: str | None = None
    media: bool = False
    source: FileInfo | None = None
    target: FileInfo | None = None
    decision: str = Decision.PENDING.value
    decision_reason: str = ""
    policy: str = "default"
    policy_pattern: str | None = None
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    applied: bool = False
    applied_at: str | None = None
    apply_error: str | None = None

    @property
    def category(self) -> str:
        if self.action == Action.ADD_TITLE.value:
            return "add_title"
        if self.action == Action.ADD.value:
            return "add_media" if self.media else "add_sidecar"
        if self.action == Action.UPDATE.value:
            return "replace_media" if self.media else "update_sidecar"
        if self.action == Action.DELETE_TITLE.value:
            return "delete_title"
        if self.action == Action.DELETE.value:
            return "delete_media" if self.media else "delete_sidecar"
        if self.action == Action.REPLACE.value:
            return "replace_media"
        if self.action == Action.REENCODE.value:
            return "reencode"
        return "unknown"

    @property
    def destructive(self) -> bool:
        return self.category in {
            "delete_title",
            "delete_media",
            "replace_media",
            "reencode",
        }

    @property
    def size(self) -> int:
        if self.source and self.source.exists:
            return self.source.size
        if self.target and self.target.exists:
            return self.target.size
        try:
            return int(self.details.get("total_size", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def old_size(self) -> int:
        if self.target and self.target.exists:
            return self.target.size
        try:
            return int(self.details.get("old_size", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def ident(self) -> str:
        raw = f"{self.title}\0{self.action}\0{self.path}\0{self.old_path or ''}"
        return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()[:16]

    def signature(self) -> str:
        return json.dumps(
            {
                "title": self.title,
                "action": self.action,
                "path": self.path,
                "old_path": self.old_path,
                "source": self.source.to_dict() if self.source else None,
                "target": self.target.to_dict() if self.target else None,
                "logical_key": self.logical_key,
            },
            sort_keys=True,
            default=str,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "action": self.action,
            "category": self.category,
            "path": self.path,
            "old_path": self.old_path,
            "logical_key": self.logical_key,
            "media": self.media,
            "source": self.source.to_dict() if self.source else None,
            "target": self.target.to_dict() if self.target else None,
            "decision": self.decision,
            "decision_reason": self.decision_reason,
            "policy": self.policy,
            "policy_pattern": self.policy_pattern,
            "reason": self.reason,
            "destructive": self.destructive,
            "size": self.size,
            "old_size": self.old_size,
            "details": self.details,
            "applied": self.applied,
            "applied_at": self.applied_at,
            "apply_error": self.apply_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Change:
        return cls(
            title=data["title"],
            action=data["action"],
            path=data.get("path", ""),
            old_path=data.get("old_path"),
            logical_key=data.get("logical_key"),
            media=bool(data.get("media", False)),
            source=FileInfo.from_dict(data.get("source")),
            target=FileInfo.from_dict(data.get("target")),
            decision=data.get("decision", Decision.PENDING.value),
            decision_reason=data.get("decision_reason", ""),
            policy=data.get("policy", "default"),
            policy_pattern=data.get("policy_pattern"),
            reason=data.get("reason", ""),
            details=dict(data.get("details", {})),
            applied=bool(data.get("applied", False)),
            applied_at=data.get("applied_at"),
            apply_error=data.get("apply_error"),
        )


@dataclass
class TitleFingerprint:
    title: str
    source_mtime_ns: int | None = None
    target_mtime_ns: int | None = None
    source_count: int = 0
    target_count: int = 0
    source_digest: str | None = None
    target_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TitleFingerprint:
        return cls(**data)


@dataclass
class Plan:
    plan_id: str
    created_at: str
    library: str
    kind: str
    source: str
    target: str
    config_path: str
    changes: list[Change] = field(default_factory=list)
    fingerprints: dict[str, TitleFingerprint] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)
    status: str = PlanStatus.READY.value
    tool_version: str = "0.1.0"
    applied_at: str | None = None
    report_path: str | None = None

    def recompute_status(self) -> str:
        if self.errors:
            self.status = PlanStatus.ERROR.value
        elif any(c.decision == Decision.PENDING.value for c in self.changes):
            self.status = PlanStatus.APPROVAL_REQUIRED.value
        elif self.blocking_reasons:
            self.status = PlanStatus.BLOCKED.value
        elif self.applied_at:
            self.status = PlanStatus.APPLIED.value
        else:
            self.status = PlanStatus.READY.value
        return self.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "library": self.library,
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
            "config_path": self.config_path,
            "changes": [c.to_dict() for c in self.changes],
            "fingerprints": {
                k: v.to_dict() for k, v in sorted(self.fingerprints.items())
            },
            "warnings": self.warnings,
            "errors": self.errors,
            "blocking_reasons": self.blocking_reasons,
            "status": self.status,
            "tool_version": self.tool_version,
            "applied_at": self.applied_at,
            "report_path": self.report_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        return cls(
            plan_id=data["plan_id"],
            created_at=data["created_at"],
            library=data.get("library", ""),
            kind=data.get("kind", ""),
            source=data.get("source", ""),
            target=data.get("target", ""),
            config_path=data.get("config_path", ""),
            changes=[Change.from_dict(c) for c in data.get("changes", [])],
            fingerprints={
                k: TitleFingerprint.from_dict(v)
                for k, v in data.get("fingerprints", {}).items()
            },
            warnings=list(data.get("warnings", [])),
            errors=list(data.get("errors", [])),
            blocking_reasons=list(data.get("blocking_reasons", [])),
            status=data.get("status", PlanStatus.READY.value),
            tool_version=data.get("tool_version", "0.1.0"),
            applied_at=data.get("applied_at"),
            report_path=data.get("report_path"),
        )

    def save(self, path: str) -> None:
        from .util import atomic_write_json

        atomic_write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str) -> Plan:
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


@dataclass
class RsyncOptions:
    modify_window: int = 2
    fsync: bool = True
    checksum: bool = False
    timeout: int = 600
    extra_args: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)


DEFAULT_EXCLUDES = [
    ".DS_Store",
    "._*",
    ".mediasync-*",
    "@Recycle",
    "@Recently-Snapshot",
    "#recycle",
    "Thumbs.db",
    "desktop.ini",
    "*.partial",
    "*.tmp",
    "*.crdownload",
    "*.!qB",
    "*.part",
]

DEFAULT_AUTO_APPROVE = [
    "add_title",
    "add_media",
    "add_sidecar",
    "update_sidecar",
    "delete_sidecar",
    "reencode",
]

ALL_CATEGORIES = {
    "add_title",
    "add_media",
    "add_sidecar",
    "update_sidecar",
    "delete_sidecar",
    "replace_media",
    "reencode",
    "delete_media",
    "delete_title",
}


@dataclass
class ApplyPolicy:
    auto_approve: list[str] = field(default_factory=lambda: list(DEFAULT_AUTO_APPROVE))
    max_pending_deletions: int = 0
    max_delete_gb: float = 0.0
    bulk_reencode_count: int = 5


@dataclass
class LibraryConfig:
    name: str
    kind: str
    source: str
    target: str
    whitelist: str | None = None
    blacklist: str | None = None
    enabled: bool = True
    rsync: RsyncOptions | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"tv", "movies", "mixed"}:
            raise ValueError(f"unknown library kind: {self.kind}")


@dataclass
class Config:
    config_path: str
    state_dir: str
    libraries: list[LibraryConfig]
    rsync: RsyncOptions = field(default_factory=RsyncOptions)
    apply: ApplyPolicy = field(default_factory=ApplyPolicy)
    concurrency: int = 8
    scan_mode: str = "fast"
    trash_dir_name: str = ".mediasync-trash"
    trash_retention_days: int = 30
    delete_mode: str = "trash"
    require_mount: bool = True
    min_source_titles: int = 1
    max_source_title_drop_pct: float = 20.0
    probe: bool = False
    report_html: bool = True
    notify_command: str | None = None
    log_dir: str | None = None
    report_dir: str | None = None

    def library(self, name: str) -> LibraryConfig:
        for lib in self.libraries:
            if lib.name == name:
                return lib
        raise KeyError(f"unknown library: {name}")

    def enabled_libraries(self, name: str | None = None) -> list[LibraryConfig]:
        libs = [lib for lib in self.libraries if lib.enabled]
        if name:
            libs = [lib for lib in libs if lib.name == name]
        return libs

    def rsync_for(self, library: LibraryConfig) -> RsyncOptions:
        if library.rsync is not None:
            return library.rsync
        return self.rsync
