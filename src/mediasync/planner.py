from __future__ import annotations

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from .classifier import ClassifiedTitle, classify_title, logical_key
from .config import Config, ConfigError
from .models import (
    Action,
    Change,
    Decision,
    FileInfo,
    LibraryConfig,
    Plan,
    TitleFingerprint,
    is_media_file,
)
from .policy import PolicySet, PolicyMatch
from .rsync_backend import ScanResult, scan_title
from .safety import check_source_sanity, inspect_library
from .state import State
from .util import (
    du_size,
    human_size,
    is_junk_name,
    now_iso,
    now_stamp,
    stat_info,
    tree_fingerprint,
)

LOG = logging.getLogger(__name__)
Progress = Callable[[str], None]


@dataclass
class TitleWork:
    title: str
    fingerprint: TitleFingerprint
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    size: int = 0
    file_count: int = 0


@dataclass
class TitleContext:
    title: str
    kind: str
    source_title: str
    target_title: str
    in_source: bool
    in_target: bool
    cache: dict | None
    deep: bool
    probe: bool
    probe_timeout: int
    rsync_options: object
    policy: PolicySet
    progress: Progress | None = None
    library_name: str = ""


def plan_library(
    config: Config,
    state: State,
    library: LibraryConfig,
    *,
    deep: bool = False,
    allow_empty_target: bool = False,
    allow_source_drop: bool = False,
    progress: Progress | None = None,
) -> Plan:
    def report(message: str) -> None:
        LOG.info(message)
        if progress:
            progress(message)

    report(f"{library.name}: inspecting source and target")
    inspection = inspect_library(config, library)
    warnings = check_source_sanity(
        config,
        state,
        inspection,
        allow_empty_target=allow_empty_target,
        allow_source_drop=allow_source_drop,
    )
    policy = PolicySet.load(library.whitelist, library.blacklist)
    deep = deep or config.scan_mode == "deep"
    cache = state.get_title_cache(library.name)
    options = config.rsync_for(library)

    source_set = set(inspection.source_titles)
    target_set = set(inspection.target_titles)
    all_titles = sorted(source_set | target_set)

    contexts = [
        TitleContext(
            title=title,
            kind=library.kind,
            source_title=os.path.join(inspection.source, title),
            target_title=os.path.join(inspection.target, title),
            in_source=title in source_set,
            in_target=title in target_set,
            cache=cache.get(title),
            deep=deep,
            probe=config.probe,
            probe_timeout=options.timeout,
            rsync_options=options,
            policy=policy,
            progress=progress,
            library_name=library.name,
        )
        for title in all_titles
    ]

    results: list[TitleWork] = []
    total = len(contexts)
    completed = 0
    report(f"{library.name}: scanning {total} title(s)")
    with ThreadPoolExecutor(max_workers=config.concurrency) as executor:
        futures = {
            executor.submit(_process_title, context): context for context in contexts
        }
        for future in as_completed(futures):
            context = futures[future]
            completed += 1
            try:
                work = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                work = TitleWork(
                    title=context.title,
                    fingerprint=TitleFingerprint(context.title),
                    errors=[f"{context.title}: {exc}"],
                )
            results.append(work)
            if completed == total or completed % 50 == 0:
                report(f"{library.name}: scanned {completed}/{total} titles")

    results.sort(key=lambda item: item.title)
    changes: list[Change] = []
    for work in results:
        changes.extend(work.changes)
        warnings.extend(work.warnings)

    errors: list[str] = []
    for work in results:
        errors.extend(work.errors)

    apply_policy(config, policy, changes)
    blocking_reasons = check_bulk_guards(config, changes)
    status = _status(changes, errors, blocking_reasons)

    now = now_iso()
    plan_id = f"{now_stamp()}-{uuid.uuid4().hex[:8]}"
    plan = Plan(
        plan_id=plan_id,
        created_at=now,
        library=library.name,
        kind=library.kind,
        source=inspection.source,
        target=inspection.target,
        config_path=config.config_path,
        changes=changes,
        fingerprints={work.title: work.fingerprint for work in results},
        warnings=warnings,
        errors=errors,
        blocking_reasons=blocking_reasons,
        status=status,
    )
    for work in results:
        title_changes = [change for change in changes if change.title == work.title]
        clean = not work.errors and (
            not title_changes
            or all(
                change.decision == Decision.DENIED.value
                and change.policy == "blacklist"
                for change in title_changes
            )
        )
        size = work.size or sum(change.size for change in title_changes)
        state.set_title_cache(
            library.name,
            work.title,
            work.fingerprint,
            clean=clean,
            size=size,
            file_count=work.file_count,
        )
    state.upsert_library(
        library.name,
        inspection.source,
        inspection.target,
        source_titles=inspection.source_count,
        target_titles=inspection.target_count,
        last_run=now,
        last_status=status,
    )
    plan_path = os.path.join(config.state_dir, "plans", f"{plan_id}.json")
    plan.save(plan_path)
    report(
        f"{library.name}: plan {plan_id} {status} "
        f"({len(changes)} change(s), {sum(c.size for c in changes)} bytes)"
    )
    return plan


def _process_title(context: TitleContext) -> TitleWork:
    if context.progress and context.library_name:
        context.progress(f"{context.library_name}: scanning {context.title}")
    source_mtime, source_count, source_digest = tree_fingerprint(
        context.source_title
    )
    target_mtime, target_count, target_digest = tree_fingerprint(
        context.target_title
    )
    fingerprint = TitleFingerprint(
        title=context.title,
        source_mtime_ns=source_mtime,
        target_mtime_ns=target_mtime,
        source_count=source_count,
        target_count=target_count,
        source_digest=source_digest,
        target_digest=target_digest,
    )
    work = TitleWork(title=context.title, fingerprint=fingerprint)

    if context.in_source and context.in_target:
        if not context.deep and _cache_clean_match(context.cache, fingerprint):
            work.skipped = True
            work.size = int((context.cache or {}).get("size", 0) or 0)
            work.file_count = int((context.cache or {}).get("file_count", 0) or 0)
            return work
        result: ScanResult = scan_title(
            context.source_title,
            context.target_title,
            context.rsync_options,
        )
        work.warnings.extend(result.warnings)
        work.errors.extend(result.errors)
        if result.errors:
            return work
        classified: ClassifiedTitle = classify_title(
            context.kind,
            context.title,
            result.changes,
            context.source_title,
            context.target_title,
            probe=context.probe,
            probe_timeout=context.probe_timeout,
        )
        work.changes = classified.changes
        work.warnings.extend(classified.warnings)
        work.size = sum(change.size for change in classified.changes)
        for change in classified.changes:
            if change.action in {Action.ADD.value, Action.UPDATE.value, Action.REENCODE.value}:
                work.file_count += 1
        return work

    if context.in_source:
        size = _cached_size(context.cache, fingerprint)
        allowed_paths: list[str] | None = None
        excluded = 0
        if context.policy.evaluate(context.title).kind == "blacklist":
            work.warnings.append(
                f"{context.title}: skipped new title; title is blacklisted"
            )
            return work
        if context.policy.blacklist:
            filtered, excluded = _filter_new_title(context, context.policy)
            if not filtered and excluded:
                work.warnings.append(
                    f"{context.title}: skipped new title; all files are blacklisted"
                )
                return work
            if excluded:
                allowed_paths = filtered
        if size is None or allowed_paths is not None:
            if allowed_paths is not None:
                size = _paths_size(context.source_title, allowed_paths)
            else:
                size = du_size(
                    context.source_title,
                    timeout=int(getattr(context.rsync_options, "timeout", 600)),
                )
        details: dict = {"total_size": size}
        if excluded:
            details["allowed_paths"] = allowed_paths
            details["excluded_count"] = excluded
            work.warnings.append(
                f"{context.title}: {excluded} blacklisted file(s) excluded from new title"
            )
        change = Change(
            title=context.title,
            action=Action.ADD_TITLE.value,
            path=context.title,
            source=FileInfo(
                path=context.title,
                size=size,
                mtime_ns=source_mtime or 0,
                is_dir=True,
            ),
            media=True,
            reason="title exists at source but not at target",
            details=details,
        )
        work.changes = [change]
        work.size = size
        return work

    if context.in_target:
        size = _cached_size(context.cache, fingerprint)
        changes: list[Change] | None = None
        if context.policy.whitelist or context.policy.blacklist:
            changes = _protected_title_deletes(context)
            if changes is not None and not changes:
                work.warnings.append(
                    f"{context.title}: skipped target title; all files are blacklisted"
                )
                return work
        if changes:
            work.changes = changes
            work.size = sum(change.size for change in changes)
            work.file_count = len(changes)
            return work
        if size is None:
            size = du_size(
                context.target_title,
                timeout=int(getattr(context.rsync_options, "timeout", 600)),
            )
        change = Change(
            title=context.title,
            action=Action.DELETE_TITLE.value,
            path=context.title,
            target=FileInfo(
                path=context.title,
                size=size,
                mtime_ns=target_mtime or 0,
                is_dir=True,
            ),
            media=True,
            reason="title exists at target but is missing from source",
            details={"total_size": size},
        )
        work.changes = [change]
        work.size = size
        return work

    return work


def _walk_title_files(root: str) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not is_junk_name(name))
        for name in sorted(names):
            if is_junk_name(name):
                continue
            path = os.path.join(base, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            files.append((rel, path))
    return files


def _filter_new_title(
    context: TitleContext, policy: PolicySet
) -> tuple[list[str], int]:
    allowed: list[str] = []
    excluded = 0
    for rel, _path in _walk_title_files(context.source_title):
        match = policy.evaluate(f"{context.title}/{rel}")
        if match.kind == "blacklist":
            excluded += 1
            continue
        allowed.append(rel)
    return allowed, excluded


def _paths_size(root: str, rel_paths: list[str]) -> int:
    total = 0
    for rel in rel_paths:
        try:
            total += os.stat(os.path.join(root, rel), follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _protected_title_deletes(context: TitleContext) -> list[Change] | None:
    title_match = context.policy.evaluate(context.title)
    if title_match.kind == "whitelist":
        return None
    if title_match.kind == "blacklist":
        return []
    files = _walk_title_files(context.target_title)
    matches: list[tuple[str, str, PolicyMatch]] = []
    protected = False
    for rel, path in files:
        library_rel = f"{context.title}/{rel}"
        match = context.policy.evaluate(library_rel)
        matches.append((rel, path, match))
        if match.kind in {"whitelist", "blacklist"}:
            protected = True
    if not protected:
        return None
    changes: list[Change] = []
    for rel, path, match in matches:
        if match.kind == "blacklist":
            continue
        library_rel = f"{context.title}/{rel}"
        info = FileInfo.from_dict(stat_info(path, library_rel))
        if info is None:
            continue
        changes.append(
            Change(
                title=context.title,
                action=Action.DELETE.value,
                path=library_rel,
                target=info,
                logical_key=logical_key(context.kind, context.title, rel),
                media=is_media_file(rel),
                reason="title is missing at source",
            )
        )
    return changes


def _cache_clean_match(cache: dict | None, fingerprint: TitleFingerprint) -> bool:
    if not cache or not int(cache.get("clean") or 0):
        return False
    return (
        cache.get("source_digest") == fingerprint.source_digest
        and cache.get("target_digest") == fingerprint.target_digest
        and int(cache.get("source_count") or 0) == fingerprint.source_count
        and int(cache.get("target_count") or 0) == fingerprint.target_count
    )


def _cached_size(cache: dict | None, fingerprint: TitleFingerprint) -> int | None:
    if not cache:
        return None
    if not (
        cache.get("source_digest") == fingerprint.source_digest
        and cache.get("target_digest") == fingerprint.target_digest
        and int(cache.get("source_count") or 0) == fingerprint.source_count
        and int(cache.get("target_count") or 0) == fingerprint.target_count
    ):
        return None
    value = int(cache.get("size") or 0)
    return value if value > 0 else None


def apply_policy(config: Config, policy: PolicySet, changes: list[Change]) -> None:
    auto = set(config.apply.auto_approve)
    for change in changes:
        matches: list[PolicyMatch] = [policy.evaluate(change.path)]
        if change.old_path:
            matches.append(policy.evaluate(change.old_path))
        black = [m for m in matches if m.kind == "blacklist"]
        white = [m for m in matches if m.kind == "whitelist"]
        if black:
            match = max(black, key=lambda m: m.specificity)
            change.policy = "blacklist"
            change.policy_pattern = match.pattern
            change.decision = Decision.DENIED.value
            change.decision_reason = f"blacklisted by {match.pattern}"
            continue
        if white:
            match = max(white, key=lambda m: m.specificity)
            change.policy = "whitelist"
            change.policy_pattern = match.pattern
            if change.category not in {"add_title", "add_media", "add_sidecar"}:
                change.decision = Decision.HELD.value
                change.decision_reason = (
                    f"whitelisted by {match.pattern}; target content retained"
                )
                continue
        if change.category in auto:
            change.decision = Decision.ACCEPTED.value
            change.decision_reason = "auto-approved by policy"
        else:
            change.decision = Decision.PENDING.value
            change.decision_reason = "requires approval"


def check_bulk_guards(config: Config, changes: list[Change]) -> list[str]:
    reasons: list[str] = []
    bulk = config.apply.bulk_reencode_count
    if bulk > 0:
        by_title: dict[str, list[Change]] = {}
        for change in changes:
            if change.action == Action.REENCODE.value:
                by_title.setdefault(change.title, []).append(change)
        for title, title_changes in sorted(by_title.items()):
            if len(title_changes) >= bulk:
                for change in title_changes:
                    if change.decision == Decision.ACCEPTED.value:
                        change.decision = Decision.PENDING.value
                        change.decision_reason = (
                            f"bulk re-encode guard: {len(title_changes)} "
                            f"re-encodes in this title"
                        )
                reasons.append(
                    f"{title}: {len(title_changes)} re-encodes require approval "
                    f"(bulk_reencode_count={bulk})"
                )

    pending_deletes = [
        change
        for change in changes
        if change.category in {"delete_title", "delete_media"}
        and change.decision == Decision.PENDING.value
    ]
    if pending_deletes:
        count = len(pending_deletes)
        total = sum(change.size for change in pending_deletes)
        if config.apply.max_pending_deletions == 0:
            reasons.append(
                f"{count} deletion(s) require approval "
                f"({human_size(total)}); max_pending_deletions=0"
            )
        elif count > config.apply.max_pending_deletions:
            reasons.append(
                f"{count} pending deletion(s) exceed max_pending_deletions="
                f"{config.apply.max_pending_deletions}"
            )
        if config.apply.max_delete_gb > 0:
            limit = config.apply.max_delete_gb * 1024 ** 3
            if total > limit:
                reasons.append(
                    f"pending deletions total {human_size(total)} exceed "
                    f"max_delete_gb={config.apply.max_delete_gb}"
                )
    return reasons


def _status(changes: list[Change], errors: list[str], blocking: list[str]) -> str:
    if errors:
        return "error"
    if any(change.decision == Decision.PENDING.value for change in changes):
        return "approval_required"
    if blocking:
        return "blocked"
    return "ready"


def pending_plans(config: Config, library: str | None = None) -> list[Plan]:
    plan_dir = os.path.join(config.state_dir, "plans")
    if not os.path.isdir(plan_dir):
        return []
    plans: list[Plan] = []
    for name in sorted(os.listdir(plan_dir), reverse=True):
        if not name.endswith(".json"):
            continue
        path = os.path.join(plan_dir, name)
        try:
            plan = Plan.load(path)
        except (OSError, KeyError, TypeError, ValueError):
            continue
        if library and plan.library != library:
            continue
        if not plan.changes and not plan.errors:
            continue
        actionable = bool(plan.errors) or any(
            change.decision in {Decision.PENDING.value, Decision.ACCEPTED.value}
            for change in plan.changes
        )
        if actionable and plan.status in {
            "ready",
            "approval_required",
            "blocked",
            "error",
        }:
            plans.append(plan)
    return plans


def supersede_pending(
    config: Config, library: str, exclude_plan_id: str | None = None
) -> int:
    count = 0
    for plan in pending_plans(config, library):
        if exclude_plan_id and plan.plan_id == exclude_plan_id:
            continue
        plan.status = "superseded"
        save_plan(config, plan)
        count += 1
    return count


def find_plan(config: Config, plan_id: str) -> Plan:
    plan_dir = os.path.join(config.state_dir, "plans")
    exact = os.path.join(plan_dir, f"{plan_id}.json")
    if os.path.exists(exact):
        return _load_plan_file(exact)
    matches = [
        name
        for name in os.listdir(plan_dir)
        if name.endswith(".json") and name.startswith(plan_id)
    ] if os.path.isdir(plan_dir) else []
    if not matches:
        raise FileNotFoundError(f"plan not found: {plan_id}")
    if len(matches) > 1:
        raise ConfigError(f"plan id is ambiguous: {plan_id}")
    return _load_plan_file(os.path.join(plan_dir, matches[0]))


def _load_plan_file(path: str) -> Plan:
    try:
        return Plan.load(path)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid plan file {path}: {exc}") from exc


def plan_path(config: Config, plan: Plan) -> str:
    return os.path.join(config.state_dir, "plans", f"{plan.plan_id}.json")


def save_plan(config: Config, plan: Plan) -> str:
    path = plan_path(config, plan)
    plan.save(path)
    return path
