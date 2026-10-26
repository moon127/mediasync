from __future__ import annotations

import os
import tomllib
from dataclasses import asdict
from typing import Any

from .models import (
    ALL_CATEGORIES,
    DEFAULT_AUTO_APPROVE,
    DEFAULT_EXCLUDES,
    ApplyPolicy,
    Config,
    LibraryConfig,
    RsyncOptions,
)
from .util import ensure_dir, expand_path


class ConfigError(RuntimeError):
    pass


def default_config_path() -> str:
    env = os.environ.get("MEDIASYNC_CONFIG")
    if env:
        return expand_path(env, os.getcwd()) or ""
    candidate = os.path.join(os.getcwd(), "mediasync.toml")
    if os.path.exists(candidate):
        return candidate
    return os.path.expanduser("~/.config/mediasync/config.toml")


def load_config(path: str | None = None) -> Config:
    config_path = expand_path(path, os.getcwd()) if path else default_config_path()
    if not config_path or not os.path.exists(config_path):
        raise ConfigError(
            f"config not found: {config_path or '(none)'}; run 'mediasync init' first"
        )
    base = os.path.dirname(os.path.abspath(config_path))
    with open(config_path, "rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
    try:
        return config_from_dict(data, config_path, base)
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid config in {config_path}: {exc}") from exc


def config_from_dict(data: dict[str, Any], config_path: str, base: str) -> Config:
    rsync = parse_rsync(data.get("rsync", {}), None)
    apply_policy = parse_apply(data.get("apply", {}))
    libraries_data = data.get("libraries", [])
    if not isinstance(libraries_data, list) or not libraries_data:
        raise ConfigError("at least one [[libraries]] entry is required")
    libraries: list[LibraryConfig] = []
    names: set[str] = set()
    for index, item in enumerate(libraries_data):
        if not isinstance(item, dict):
            raise ConfigError(f"libraries[{index}] must be a table")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ConfigError(f"libraries[{index}] is missing name")
        if name in names:
            raise ConfigError(f"duplicate library name: {name}")
        names.add(name)
        kind = str(item.get("kind", "tv")).strip().lower()
        if kind not in {"tv", "movies", "mixed"}:
            raise ConfigError(f"library {name}: kind must be tv, movies or mixed")
        source = expand_path(item.get("source"), base)
        target = expand_path(item.get("target"), base)
        if not source or not target:
            raise ConfigError(f"library {name}: source and target are required")
        lib_rsync = None
        if "rsync" in item:
            lib_rsync = parse_rsync(item.get("rsync", {}), rsync)
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"library {name}: enabled must be true or false")
        library = LibraryConfig(
            name=name,
            kind=kind,
            source=source,
            target=target,
            whitelist=expand_path(item.get("whitelist"), base),
            blacklist=expand_path(item.get("blacklist"), base),
            enabled=enabled,
            rsync=lib_rsync,
        )
        libraries.append(library)

    state_dir = expand_path(data.get("state_dir", "~/.mediasync"), base)
    if not state_dir:
        raise ConfigError("state_dir is invalid")
    delete_mode = str(data.get("delete_mode", "trash")).lower()
    if delete_mode not in {"trash", "permanent"}:
        raise ConfigError("delete_mode must be 'trash' or 'permanent'")
    scan_mode = str(data.get("scan_mode", "fast")).lower()
    if scan_mode not in {"fast", "deep"}:
        raise ConfigError("scan_mode must be 'fast' or 'deep'")
    trash_dir_name = str(data.get("trash_dir_name", ".mediasync-trash")).strip("/")
    if not trash_dir_name or "/" in trash_dir_name or "\\" in trash_dir_name:
        raise ConfigError("trash_dir_name must be a single directory name")
    if not trash_dir_name.startswith(".mediasync-"):
        raise ConfigError("trash_dir_name must start with '.mediasync-'")
    for key, default in (
        ("require_mount", True),
        ("probe", False),
        ("report_html", True),
    ):
        if not isinstance(data.get(key, default), bool):
            raise ConfigError(f"{key} must be true or false")
    config = Config(
        config_path=os.path.abspath(config_path),
        state_dir=state_dir,
        libraries=libraries,
        rsync=rsync,
        apply=apply_policy,
        concurrency=int(data.get("concurrency", 8)),
        scan_mode=scan_mode,
        trash_dir_name=trash_dir_name,
        trash_retention_days=int(data.get("trash_retention_days", 30)),
        delete_mode=delete_mode,
        require_mount=data.get("require_mount", True),
        min_source_titles=int(data.get("min_source_titles", 1)),
        max_source_title_drop_pct=float(data.get("max_source_title_drop_pct", 20.0)),
        probe=data.get("probe", False),
        report_html=data.get("report_html", True),
        notify_command=data.get("notify_command"),
        log_dir=expand_path(data.get("log_dir", os.path.join(state_dir, "logs")), base),
        report_dir=expand_path(
            data.get("report_dir", os.path.join(state_dir, "reports")), base
        ),
    )
    validate_config(config)
    return config


def parse_rsync(data: dict[str, Any], base: RsyncOptions | None) -> RsyncOptions:
    if not isinstance(data, dict):
        raise ConfigError("rsync configuration must be a table")
    options = RsyncOptions(
        modify_window=base.modify_window if base else 2,
        fsync=base.fsync if base else True,
        checksum=base.checksum if base else False,
        timeout=base.timeout if base else 600,
        extra_args=list(base.extra_args) if base else [],
        excludes=list(base.excludes) if base else list(DEFAULT_EXCLUDES),
    )
    if "modify_window" in data:
        options.modify_window = int(data["modify_window"])
    if "fsync" in data:
        if not isinstance(data["fsync"], bool):
            raise ConfigError("rsync.fsync must be true or false")
        options.fsync = data["fsync"]
    if "checksum" in data:
        if not isinstance(data["checksum"], bool):
            raise ConfigError("rsync.checksum must be true or false")
        options.checksum = data["checksum"]
    if "timeout" in data:
        options.timeout = int(data["timeout"])
    if "extra_args" in data:
        raw = data.get("extra_args") or []
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise ConfigError("rsync.extra_args must be a list of strings")
        options.extra_args = list(raw)
    if "excludes" in data:
        raw = data.get("excludes") or []
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise ConfigError("rsync.excludes must be a list of strings")
        if base is None:
            options.excludes = list(DEFAULT_EXCLUDES) + list(raw)
        else:
            options.excludes = list(options.excludes) + list(raw)
    return options


def parse_apply(data: dict[str, Any]) -> ApplyPolicy:
    if not isinstance(data, dict):
        raise ConfigError("apply configuration must be a table")
    auto = data.get("auto_approve", DEFAULT_AUTO_APPROVE)
    if not isinstance(auto, list) or not all(isinstance(x, str) for x in auto):
        raise ConfigError("apply.auto_approve must be a list of strings")
    unknown = set(auto) - ALL_CATEGORIES
    if unknown:
        raise ConfigError(f"unknown apply.auto_approve categories: {sorted(unknown)}")
    return ApplyPolicy(
        auto_approve=list(auto),
        max_pending_deletions=int(data.get("max_pending_deletions", 0)),
        max_delete_gb=float(data.get("max_delete_gb", 0.0)),
        bulk_reencode_count=int(data.get("bulk_reencode_count", 5)),
    )


def validate_config(config: Config) -> None:
    from .rsync_backend import RsyncOptionError, validate_extra_args

    if config.concurrency < 1 or config.concurrency > 64:
        raise ConfigError("concurrency must be between 1 and 64")
    if config.min_source_titles < 0:
        raise ConfigError("min_source_titles must be >= 0")
    if config.trash_retention_days < 0:
        raise ConfigError("trash_retention_days must be >= 0")
    if config.max_source_title_drop_pct < 0 or config.max_source_title_drop_pct > 100:
        raise ConfigError("max_source_title_drop_pct must be between 0 and 100")
    if config.apply.max_pending_deletions < 0:
        raise ConfigError("max_pending_deletions must be >= 0")
    if config.apply.max_delete_gb < 0:
        raise ConfigError("max_delete_gb must be >= 0")
    if config.apply.bulk_reencode_count < 0:
        raise ConfigError("bulk_reencode_count must be >= 0")
    for lib in config.libraries:
        options = config.rsync_for(lib)
        if options.modify_window < 0:
            raise ConfigError(f"library {lib.name}: modify_window must be >= 0")
        if options.timeout <= 0:
            raise ConfigError(f"library {lib.name}: timeout must be > 0")
        try:
            validate_extra_args(options.extra_args)
        except RsyncOptionError as exc:
            raise ConfigError(f"library {lib.name}: {exc}") from exc
    ensure_dir(config.state_dir)
    ensure_dir(os.path.join(config.state_dir, "plans"))
    if config.log_dir:
        ensure_dir(config.log_dir)
    if config.report_dir:
        ensure_dir(config.report_dir)


INIT_TEMPLATE = """# mediasync configuration
state_dir = "~/.mediasync"
concurrency = 8
scan_mode = "fast"
trash_retention_days = 30
delete_mode = "trash"
require_mount = true
min_source_titles = 1
max_source_title_drop_pct = 20
report_html = true

[rsync]
modify_window = 2
fsync = true
# extra_args = ["--bwlimit=50m"]

[apply]
auto_approve = [
  "add_title",
  "add_media",
  "add_sidecar",
  "update_sidecar",
  "delete_sidecar",
  "reencode",
]
max_pending_deletions = 0
max_delete_gb = 0
bulk_reencode_count = 5

[[libraries]]
name = "TV"
kind = "tv"
source = "/path/to/source/TV"
target = "/path/to/target/TV"
whitelist = "policy/tv-whitelist.txt"
blacklist = "policy/tv-blacklist.txt"

[[libraries]]
name = "Movies"
kind = "movies"
source = "/path/to/source/Movies"
target = "/path/to/target/Movies"
whitelist = "policy/movies-whitelist.txt"
blacklist = "policy/movies-blacklist.txt"
"""

WHITELIST_TEMPLATE = """# Whitelist: keep existing target content.
# One path pattern per line, relative to the library root.
# Examples:
#   Andor
#   Battlestar Galactica (2003)
#   Babylon 5/Season 1
#   re:^Doctor Who/.*S0[1-9].*$
# Blacklist wins if both match at the same specificity.
"""

BLACKLIST_TEMPLATE = """# Blacklist: ignore this path entirely.
# Do not copy, update, or delete it. Existing target content is left alone.
# One path pattern per line, relative to the library root.
# Examples:
#   Some Show
#   */Extras
#   *.iso
#   re:.*Sample.*
"""


def init_config(path: str, force: bool = False) -> tuple[str, list[str]]:
    config_path = expand_path(path, os.getcwd())
    assert config_path is not None
    if os.path.exists(config_path) and not force:
        raise ConfigError(f"refusing to overwrite existing config: {config_path}")
    directory = os.path.dirname(config_path)
    ensure_dir(directory)
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(INIT_TEMPLATE)
    policy_dir = os.path.join(directory, "policy")
    ensure_dir(policy_dir)
    created = [config_path]
    for name, content in [
        ("tv-whitelist.txt", WHITELIST_TEMPLATE),
        ("tv-blacklist.txt", BLACKLIST_TEMPLATE),
        ("movies-whitelist.txt", WHITELIST_TEMPLATE),
        ("movies-blacklist.txt", BLACKLIST_TEMPLATE),
    ]:
        policy_path = os.path.join(policy_dir, name)
        if not os.path.exists(policy_path) or force:
            with open(policy_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            created.append(policy_path)
    return config_path, created


def config_summary(config: Config) -> dict[str, Any]:
    return {
        "config_path": config.config_path,
        "state_dir": config.state_dir,
        "libraries": [
            {
                "name": lib.name,
                "kind": lib.kind,
                "source": lib.source,
                "target": lib.target,
                "whitelist": lib.whitelist,
                "blacklist": lib.blacklist,
                "rsync": asdict(config.rsync_for(lib)),
            }
            for lib in config.libraries
        ],
    }
