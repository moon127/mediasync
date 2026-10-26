from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .models import Action, Change, Decision, LibraryConfig, Plan, TitleFingerprint
from .rsync_backend import copy_files, copy_title
from .safety import inspect_library
from .state import State
from .trash import quarantine, trash_root
from .util import (
    directory_fingerprint,
    ensure_dir,
    list_visible_titles,
    now_iso,
    tree_fingerprint,
    validate_rel_path,
)

LOG = logging.getLogger(__name__)
Progress = Callable[[str], None]


class PlanStaleError(RuntimeError):
    pass


class ApplyError(RuntimeError):
    pass


@dataclass
class ApplyResult:
    plan_id: str
    library: str
    run_id: str
    applied: int = 0
    retained: int = 0
    denied: int = 0
    quarantined: int = 0
    errors: list[str] = field(default_factory=list)
    status: str = "applied"


def verify_plan(config: Config, plan: Plan, library: LibraryConfig) -> None:
    if any(change.applied for change in plan.changes):
        raise ApplyError(
            "plan has already been partially or fully applied; create a new plan"
        )
    options = config.rsync_for(library)
    window_ns = options.modify_window * 1_000_000_000
    issues: list[str] = []
    for change in plan.changes:
        if change.decision != Decision.ACCEPTED.value:
            continue
        try:
            _verify_change(plan, change, window_ns)
        except PlanStaleError as exc:
            issues.append(str(exc))
    if issues:
        raise PlanStaleError("; ".join(issues[:8]))


def _verify_change(plan: Plan, change: Change, window_ns: int) -> None:
    if change.action == Action.ADD_TITLE.value:
        fingerprint = plan.fingerprints.get(change.title)
        if fingerprint is None:
            raise PlanStaleError(f"{change.title}: missing title fingerprint")
        _verify_directory(
            plan.source,
            change.title,
            fingerprint.source_mtime_ns,
            fingerprint.source_count,
            window_ns,
            expected_exists=True,
        )
        if os.path.lexists(os.path.join(plan.target, change.title)):
            raise PlanStaleError(f"{change.title}: target title now exists")
        return
    if change.action == Action.DELETE_TITLE.value:
        fingerprint = plan.fingerprints.get(change.title)
        if fingerprint is None:
            raise PlanStaleError(f"{change.title}: missing title fingerprint")
        if os.path.lexists(os.path.join(plan.source, change.title)):
            raise PlanStaleError(f"{change.title}: source title now exists")
        _verify_directory(
            plan.target,
            change.title,
            fingerprint.target_mtime_ns,
            fingerprint.target_count,
            window_ns,
            expected_exists=True,
        )
        return

    if change.source:
        source_path = os.path.join(plan.source, change.path)
        if not _stat_matches(source_path, change.source.size, change.source.mtime_ns, window_ns):
            raise PlanStaleError(f"{change.path}: source changed since planning")
    if change.action == Action.DELETE.value:
        if os.path.lexists(os.path.join(plan.source, change.path)):
            raise PlanStaleError(f"{change.path}: source path reappeared")
    if change.action == Action.ADD.value:
        if os.path.lexists(os.path.join(plan.target, change.path)):
            raise PlanStaleError(f"{change.path}: target path now exists")
    if change.target:
        target_rel = change.old_path if change.action == Action.REENCODE.value and change.old_path else change.path
        target_path = os.path.join(plan.target, target_rel)
        if not _stat_matches(target_path, change.target.size, change.target.mtime_ns, window_ns):
            raise PlanStaleError(f"{target_rel}: target changed since planning")
    if change.action == Action.REENCODE.value and change.old_path and change.old_path != change.path:
        if os.path.lexists(os.path.join(plan.source, change.old_path)):
            raise PlanStaleError(f"{change.old_path}: old source path reappeared")
        if os.path.lexists(os.path.join(plan.target, change.path)):
            raise PlanStaleError(f"{change.path}: new target path now exists")


def _verify_directory(
    root: str,
    title: str,
    planned_mtime: int | None,
    planned_count: int,
    window_ns: int,
    *,
    expected_exists: bool,
) -> None:
    path = os.path.join(root, title)
    mtime, count = directory_fingerprint(path)
    if expected_exists and mtime is None:
        raise PlanStaleError(f"{title}: directory disappeared from {root}")
    if mtime is None:
        return
    if planned_mtime is None:
        raise PlanStaleError(f"{title}: unexpected directory in {root}")
    if abs(mtime - planned_mtime) > window_ns:
        raise PlanStaleError(f"{title}: directory mtime changed in {root}")
    if count != planned_count:
        raise PlanStaleError(f"{title}: directory entry count changed in {root}")


def _stat_matches(path: str, size: int, mtime_ns: int, window_ns: int) -> bool:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if st.st_size != size:
        return False
    return abs(st.st_mtime_ns - mtime_ns) <= window_ns


def apply_plan(
    config: Config,
    state: State,
    library: LibraryConfig,
    plan: Plan,
    *,
    run_id: str,
    allow_pending: bool = False,
    progress: Progress | None = None,
) -> ApplyResult:
    def report(message: str) -> None:
        LOG.info(message)
        if progress:
            progress(message)

    inspection = inspect_library(config, library)
    if os.path.realpath(plan.source) != inspection.source:
        raise ApplyError(f"plan source no longer matches config: {plan.source}")
    if os.path.realpath(plan.target) != inspection.target:
        raise ApplyError(f"plan target no longer matches config: {plan.target}")
    if plan.errors:
        raise ApplyError("refusing to apply a plan with scan errors")
    pending = [c for c in plan.changes if c.decision == Decision.PENDING.value]
    if pending and not allow_pending:
        raise ApplyError(
            f"{len(pending)} change(s) still require approval; approve or deny them first"
        )
    verify_plan(config, plan, library)

    accepted = [c for c in plan.changes if c.decision == Decision.ACCEPTED.value]
    result = ApplyResult(plan_id=plan.plan_id, library=library.name, run_id=run_id)
    if not accepted:
        if pending:
            result.status = "approval_required"
            plan.status = "approval_required"
        else:
            result.status = "applied"
            plan.status = "applied"
            plan.applied_at = now_iso()
        _save_plan(config, plan)
        return result

    options = config.rsync_for(library)
    target_root = os.path.realpath(plan.target)
    source_root = os.path.realpath(plan.source)
    trash_base = trash_root(config, library)
    partial_dir = os.path.join(target_root, ".mediasync-partial", run_id)
    by_title: dict[str, list[Change]] = {}
    for change in accepted:
        by_title.setdefault(change.title, []).append(change)

    report(f"{library.name}: applying {len(accepted)} accepted change(s)")
    for title, title_changes in sorted(by_title.items()):
        source_title = os.path.join(source_root, title)
        target_title = os.path.join(target_root, title)
        title_errors: list[str] = []
        applied_here = 0
        try:
            title_errors, applied_here = _apply_title(
                config,
                state,
                library,
                plan,
                run_id,
                title,
                source_title,
                target_title,
                title_changes,
                options,
                trash_base,
                partial_dir,
            )
        except Exception as exc:  # pragma: no cover - defensive
            title_errors = [f"{title}: {exc}"]
        result.errors.extend(title_errors)
        result.applied += applied_here
        result.quarantined += sum(
            1
            for change in title_changes
            if change.applied
            and change.action in {Action.DELETE.value, Action.DELETE_TITLE.value}
        )
        _refresh_title_cache(config, state, library, plan, title, title_changes)
        if applied_here:
            report(f"{library.name}: applied {applied_here} change(s) in {title}")
        for error in title_errors:
            report(f"{library.name}: ERROR {error}")

    result.retained = sum(
        1 for change in plan.changes if change.decision == Decision.HELD.value
    )
    result.denied = sum(
        1 for change in plan.changes if change.decision == Decision.DENIED.value
    )
    if result.errors and result.applied == 0:
        result.status = "failed"
    elif result.errors:
        result.status = "partial"
    elif any(c.decision == Decision.PENDING.value for c in plan.changes):
        result.status = "approval_required"
    else:
        result.status = "applied"
    plan.status = result.status
    if result.applied:
        plan.applied_at = now_iso()
    _save_plan(config, plan)
    state.upsert_library(
        library.name,
        inspection.source,
        inspection.target,
        source_titles=inspection.source_count,
        target_titles=len(list_visible_titles(inspection.target)),
        last_run=now_iso(),
        last_status=result.status,
    )
    state.audit(
        run_id,
        library.name,
        "apply_plan",
        plan.plan_id,
        f"applied={result.applied} errors={len(result.errors)}",
    )
    return result


def _apply_title(
    config: Config,
    state: State,
    library: LibraryConfig,
    plan: Plan,
    run_id: str,
    title: str,
    source_title: str,
    target_title: str,
    changes: list[Change],
    options,
    trash_base: str,
    partial_dir: str,
) -> tuple[list[str], int]:
    errors: list[str] = []
    applied = 0
    title_level = [
        change
        for change in changes
        if change.action in {Action.ADD_TITLE.value, Action.DELETE_TITLE.value}
    ]
    if title_level:
        for change in title_level:
            if change.action == Action.ADD_TITLE.value:
                allowed = change.details.get("allowed_paths")
                if allowed:
                    try:
                        allowed = [validate_rel_path(path) for path in allowed]
                    except Exception as exc:
                        errors.append(f"{title}: invalid allowed path list: {exc}")
                        change.apply_error = errors[-1]
                        continue
                    ensure_dir(target_title)
                    backup_dir = os.path.join(trash_base, run_id, "backups", title)
                    result = copy_files(
                        source_title,
                        target_title,
                        allowed,
                        options,
                        backup_dir,
                        partial_dir,
                    )
                else:
                    result = copy_title(source_title, target_title, options, partial_dir)
                if not result.ok:
                    errors.append(
                        f"{title}: copy failed: {result.stderr.strip() or result.returncode}"
                    )
                    change.apply_error = errors[-1]
                    continue
                if not os.path.isdir(target_title):
                    errors.append(f"{title}: target title was not created")
                    change.apply_error = errors[-1]
                    continue
                missing = [
                    path
                    for path in (allowed or [])
                    if not os.path.lexists(os.path.join(target_title, path))
                ]
                if missing:
                    errors.append(
                        f"{title}: {len(missing)} copied file(s) missing at target"
                    )
                    change.apply_error = errors[-1]
                    continue
                _mark_applied(state, run_id, library.name, change)
                applied += 1
            elif change.action == Action.DELETE_TITLE.value:
                try:
                    quarantine(
                        config,
                        library,
                        state,
                        run_id,
                        change.path,
                        "title removed at source",
                        size=change.target.size if change.target else 0,
                        entry_type="dir",
                        permanent=config.delete_mode == "permanent",
                    )
                except Exception as exc:
                    errors.append(f"{title}: quarantine failed: {exc}")
                    change.apply_error = errors[-1]
                    continue
                _mark_applied(state, run_id, library.name, change)
                applied += 1
        return errors, applied

    new_changes = [
        change
        for change in changes
        if change.action in {Action.ADD.value, Action.UPDATE.value, Action.REENCODE.value}
    ]
    new_paths: list[str] = []
    for change in new_changes:
        rel = _rel_within(change.path, title)
        if rel not in new_paths:
            new_paths.append(rel)
    if new_paths:
        backup_dir = os.path.join(trash_base, run_id, "backups", title)
        ensure_dir(backup_dir)
        result = copy_files(
            source_title,
            target_title,
            new_paths,
            options,
            backup_dir,
            partial_dir,
        )
        if not result.ok:
            message = f"{title}: copy failed: {result.stderr.strip() or result.returncode}"
            errors.append(message)
            for change in new_changes:
                change.apply_error = message
            return errors, applied
        verify_errors = _verify_copied(plan, new_changes, options)
        if verify_errors:
            errors.extend(verify_errors)
            for change in new_changes:
                change.apply_error = "; ".join(verify_errors)
            return errors, applied
        for change in new_changes:
            _record_backup_if_present(
                state,
                library,
                run_id,
                plan,
                change,
                title,
                backup_dir,
            )
            _mark_applied(state, run_id, library.name, change)
            applied += 1

    for change in changes:
        if change.action == Action.DELETE.value:
            try:
                quarantine(
                    config,
                    library,
                    state,
                    run_id,
                    change.path,
                    change.reason or "removed at source",
                    size=change.target.size if change.target else 0,
                    permanent=config.delete_mode == "permanent",
                )
            except Exception as exc:
                message = f"{title}: quarantine failed for {change.path}: {exc}"
                errors.append(message)
                change.apply_error = message
                continue
            _mark_applied(state, run_id, library.name, change)
            applied += 1
        elif (
            change.action == Action.REENCODE.value
            and change.old_path
            and change.old_path != change.path
        ):
            try:
                quarantine(
                    config,
                    library,
                    state,
                    run_id,
                    change.old_path,
                    "old media file replaced by re-encode",
                    size=change.target.size if change.target else 0,
                    permanent=config.delete_mode == "permanent",
                )
            except Exception as exc:
                message = f"{title}: quarantine failed for {change.old_path}: {exc}"
                errors.append(message)
                change.apply_error = message
                continue
    return errors, applied


def _verify_copied(plan: Plan, changes: list[Change], options) -> list[str]:
    errors: list[str] = []
    window_ns = options.modify_window * 1_000_000_000
    for change in changes:
        if not change.source:
            continue
        target_path = os.path.join(plan.target, change.path)
        try:
            st = os.stat(target_path, follow_symlinks=False)
        except OSError:
            errors.append(f"{change.path}: copied file is missing at target")
            continue
        if st.st_size != change.source.size:
            errors.append(
                f"{change.path}: copied size {st.st_size} != source {change.source.size}"
            )
        elif abs(st.st_mtime_ns - change.source.mtime_ns) > window_ns:
            errors.append(f"{change.path}: copied mtime differs from source")
    return errors


def _record_backup_if_present(
    state: State,
    library: LibraryConfig,
    run_id: str,
    plan: Plan,
    change: Change,
    title: str,
    backup_dir: str,
) -> None:
    if not change.target:
        return
    if change.action not in {Action.UPDATE.value, Action.REENCODE.value}:
        return
    if (
        change.action == Action.REENCODE.value
        and change.old_path
        and change.old_path != change.path
    ):
        return
    rel = _rel_within(change.path, title)
    backup_path = os.path.join(backup_dir, rel)
    if not os.path.lexists(backup_path):
        return
    target_root = os.path.realpath(plan.target)
    trash_rel = os.path.relpath(backup_path, target_root)
    state.add_trash(
        library.name,
        run_id,
        change.path,
        trash_rel,
        change.target.size,
        "file",
        f"replaced by {change.action}",
    )
    state.audit(
        run_id,
        library.name,
        "backup",
        change.path,
        f"replaced by {change.action}",
    )


def _mark_applied(
    state: State, run_id: str, library: str, change: Change
) -> None:
    change.applied = True
    change.applied_at = now_iso()
    state.audit(
        run_id,
        library,
        f"applied_{change.action}",
        change.path,
        change.decision_reason,
    )


def _rel_within(library_rel: str, title: str) -> str:
    prefix = title + "/"
    if not library_rel.startswith(prefix):
        raise ApplyError(f"{library_rel} is not inside title {title}")
    return validate_rel_path(library_rel[len(prefix):])


def _refresh_title_cache(
    config: Config,
    state: State,
    library: LibraryConfig,
    plan: Plan,
    title: str,
    changes: list[Change],
) -> None:
    source_mtime, source_count, source_digest = tree_fingerprint(
        os.path.join(plan.source, title)
    )
    target_mtime, target_count, target_digest = tree_fingerprint(
        os.path.join(plan.target, title)
    )
    fingerprint = TitleFingerprint(
        title=title,
        source_mtime_ns=source_mtime,
        target_mtime_ns=target_mtime,
        source_count=source_count,
        target_count=target_count,
        source_digest=source_digest,
        target_digest=target_digest,
    )
    unresolved = False
    for change in changes:
        if change.apply_error:
            unresolved = True
            continue
        if change.decision in {Decision.PENDING.value, Decision.HELD.value}:
            unresolved = True
        elif change.decision == Decision.DENIED.value and change.policy != "blacklist":
            unresolved = True
        elif change.decision == Decision.ACCEPTED.value and not change.applied:
            unresolved = True
    state.set_title_cache(
        library.name,
        title,
        fingerprint,
        clean=not unresolved,
        size=sum(change.size for change in changes),
    )


def _save_plan(config: Config, plan: Plan) -> None:
    path = os.path.join(config.state_dir, "plans", f"{plan.plan_id}.json")
    plan.save(path)
