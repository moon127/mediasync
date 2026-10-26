from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import uuid
from typing import Any

from . import __version__
from .apply import ApplyError, PlanStaleError, apply_plan
from .config import (
    ConfigError,
    config_summary,
    init_config,
    load_config,
)
from .models import Change, Config, Decision, LibraryConfig, Plan
from .planner import (
    find_plan,
    pending_plans,
    plan_library,
    save_plan,
    supersede_pending,
)
from .policy import PolicySet, add_pattern, load_rules, remove_pattern
from .report import format_plan, format_trash, write_html
from .safety import SafetyError, describe_mount, inspect_library
from .state import State
from .trash import (
    TrashError,
    list_trash,
    purge as purge_trash,
    restore as restore_trash,
)
from .util import (
    FileLock,
    LockHeld,
    human_size,
    now_iso,
    now_stamp,
    notify,
    setup_logging,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_SAFETY = 3
EXIT_PLAN = 4
EXIT_APPROVAL = 10
EXIT_BLOCKED = 11
EXIT_APPLY = 12
EXIT_LOCK = 13
EXIT_STALE = 14


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _library(config: Config, name: str) -> LibraryConfig:
    try:
        return config.library(name)
    except KeyError as exc:
        raise ConfigError(f"unknown library: {name}") from exc


def _lock(config: Config) -> FileLock:
    return FileLock(os.path.join(config.state_dir, "mediasync.lock"))


def _write_plan_report(
    config: Config, plan: Plan, html_enabled: bool
) -> str:
    if html_enabled and config.report_dir:
        path = os.path.join(config.report_dir, f"{plan.plan_id}.html")
        write_html(plan, path)
        plan.report_path = path
        save_plan(config, plan)
        return path
    return os.path.join(config.state_dir, "plans", f"{plan.plan_id}.json")


def _write_run_report(config: Config, run_id: str, text: str) -> str:
    directory = config.report_dir or os.path.join(config.state_dir, "reports")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{run_id}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _html_flag(config: Config, value: bool | None) -> bool:
    return config.report_html if value is None else value


def _select_changes(
    plan: Plan, all_changes: bool, patterns: list[str]
) -> list[Change]:
    pending = [c for c in plan.changes if c.decision == Decision.PENDING.value]
    if all_changes:
        return pending
    if not patterns:
        raise ConfigError("select changes with --all or one or more TITLE/PATH patterns")
    selected: list[Change] = []
    for change in pending:
        for pattern in patterns:
            if fnmatch.fnmatchcase(change.title, pattern) or fnmatch.fnmatchcase(
                change.path, pattern
            ):
                selected.append(change)
                break
    return selected


def cmd_init(args: argparse.Namespace) -> int:
    path, created = init_config(args.path, force=args.force)
    print(f"Created config: {path}")
    for item in created[1:]:
        print(f"Created policy: {item}")
    print("Edit the source/target paths, then run: mediasync config validate")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.action == "show":
        print(json.dumps(config_summary(config), indent=2, default=str))
        return EXIT_OK
    if args.action == "validate":
        for library in config.enabled_libraries():
            inspection = inspect_library(config, library)
            print(
                f"{library.name}: OK source={inspection.source_mount} "
                f"({inspection.source_count} titles), "
                f"target={inspection.target_mount} ({inspection.target_count} titles)"
            )
            if args.show_rsync:
                print(json.dumps(config.rsync_for(library).__dict__, indent=2))
        print("Configuration is valid.")
        return EXIT_OK
    raise ConfigError(f"unknown config action: {args.action}")


def cmd_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    libraries = config.enabled_libraries(args.library)
    if not libraries:
        raise ConfigError("no enabled libraries selected")
    with _lock(config):
        with State(config.state_dir) as state:
            if not args.force:
                existing: list[Plan] = []
                for library in libraries:
                    existing.extend(pending_plans(config, library.name))
                if existing:
                    print("Pending plan(s) already exist:")
                    for plan in existing:
                        print(f"  {plan.plan_id}  {plan.library}  {plan.status}")
                    print("Use --force to create a new plan, or approve/apply the existing one.")
                    return EXIT_APPROVAL
            else:
                for library in libraries:
                    supersede_pending(config, library.name)
            plans: list[Plan] = []
            exit_code = EXIT_OK
            for library in libraries:
                try:
                    plan = plan_library(
                        config,
                        state,
                        library,
                        deep=args.deep,
                        allow_empty_target=args.allow_empty_target,
                        allow_source_drop=args.allow_source_drop,
                        progress=_progress,
                    )
                except SafetyError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    exit_code = EXIT_SAFETY
                    continue
                report_path = _write_plan_report(
                    config, plan, _html_flag(config, args.html)
                )
                plans.append(plan)
                if plan.errors:
                    exit_code = EXIT_PLAN
            if args.json:
                print(json.dumps([plan.to_dict() for plan in plans], indent=2, default=str))
                return exit_code
            for plan in plans:
                print(format_plan(plan))
                if plan.report_path:
                    print(f"Report: {plan.report_path}\n")
            return exit_code


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    libraries = config.enabled_libraries(args.library)
    if not libraries:
        raise ConfigError("no enabled libraries selected")
    codes: list[int] = []
    report_parts: list[str] = []
    run_base = f"run-{now_stamp()}-{uuid.uuid4().hex[:6]}"
    with _lock(config):
        with State(config.state_dir) as state:
            for index, library in enumerate(libraries, 1):
                run_id = f"{run_base}-{index}"
                pending = pending_plans(config, library.name)
                if pending and not args.force:
                    text = (
                        f"{library.name}: {len(pending)} pending plan(s) require approval. "
                        "Nothing was applied for this library."
                    )
                    print(text)
                    for plan in pending:
                        print(f"  {plan.plan_id}  {plan.status}")
                    report_parts.append(text)
                    codes.append(EXIT_APPROVAL)
                    continue
                if args.force:
                    supersede_pending(config, library.name)
                try:
                    plan = plan_library(
                        config,
                        state,
                        library,
                        deep=args.deep,
                        allow_empty_target=args.allow_empty_target,
                        allow_source_drop=args.allow_source_drop,
                        progress=_progress,
                    )
                except SafetyError as exc:
                    text = f"{library.name}: SAFETY ERROR: {exc}"
                    print(text, file=sys.stderr)
                    report_parts.append(text)
                    codes.append(EXIT_SAFETY)
                    continue
                report_path = _write_plan_report(
                    config, plan, _html_flag(config, None)
                )
                report_parts.append(format_plan(plan))
                if plan.errors:
                    codes.append(EXIT_PLAN)
                    continue
                if plan.status == "approval_required" and not args.apply_safe:
                    print(format_plan(plan))
                    print(
                        f"{library.name}: approval required; no changes applied.",
                        file=sys.stderr,
                    )
                    codes.append(EXIT_APPROVAL)
                    continue
                if plan.status == "blocked" and not args.apply_safe:
                    print(format_plan(plan))
                    print(
                        f"{library.name}: blocked by policy; no changes applied.",
                        file=sys.stderr,
                    )
                    codes.append(EXIT_BLOCKED)
                    continue
                if plan.status == "error":
                    codes.append(EXIT_PLAN)
                    continue
                state.start_run(run_id, library.name, "run", plan.plan_id)
                try:
                    result = apply_plan(
                        config,
                        state,
                        library,
                        plan,
                        run_id=run_id,
                        allow_pending=args.apply_safe,
                        progress=_progress,
                    )
                except PlanStaleError as exc:
                    text = f"{library.name}: plan is stale: {exc}"
                    print(text, file=sys.stderr)
                    state.finish_run(run_id, "stale")
                    report_parts.append(text)
                    codes.append(EXIT_STALE)
                    continue
                except ApplyError as exc:
                    text = f"{library.name}: apply refused: {exc}"
                    print(text, file=sys.stderr)
                    state.finish_run(run_id, "refused")
                    report_parts.append(text)
                    codes.append(EXIT_APPLY)
                    continue
                state.finish_run(
                    run_id,
                    result.status,
                    {
                        "plan_id": plan.plan_id,
                        "applied": result.applied,
                        "errors": result.errors,
                    },
                )
                summary = (
                    f"{library.name}: apply {result.status}; "
                    f"applied={result.applied}, retained={result.retained}, "
                    f"denied={result.denied}, errors={len(result.errors)}"
                )
                print(summary)
                for error in result.errors:
                    print(f"  ERROR {error}", file=sys.stderr)
                report_parts.append(summary)
                if result.errors:
                    report_parts.extend(f"  ERROR {error}" for error in result.errors)
                if result.status in {"partial", "failed"}:
                    codes.append(EXIT_APPLY)
                elif result.status == "approval_required":
                    codes.append(EXIT_APPROVAL)
                elif result.status == "applied":
                    codes.append(EXIT_OK)
                elif plan.blocking_reasons:
                    codes.append(EXIT_BLOCKED)
                else:
                    codes.append(EXIT_OK)
                if result.status == "applied":
                    supersede_pending(config, library.name, plan.plan_id)
    code = max(codes) if codes else EXIT_OK
    run_report = _write_run_report(config, run_base, "\n\n".join(report_parts) + "\n")
    last_report = run_report
    try:
        notify(config.notify_command, last_report, code)
    except Exception:
        pass
    return code


def cmd_pending(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    plans = pending_plans(config, args.library)
    if args.json:
        print(json.dumps([plan.to_dict() for plan in plans], indent=2, default=str))
        return EXIT_OK
    if not plans:
        print("No pending plans.")
        return EXIT_OK
    print(f"{'PLAN':<28} {'LIBRARY':<12} {'STATUS':<18} {'CHANGES':>7} {'PENDING':>7} CREATED")
    for plan in plans:
        pending = sum(
            1 for change in plan.changes if change.decision == Decision.PENDING.value
        )
        print(
            f"{plan.plan_id:<28} {plan.library:<12} {plan.status:<18} "
            f"{len(plan.changes):>7} {pending:>7} {plan.created_at}"
        )
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    plan = find_plan(config, args.plan_id)
    if args.html:
        path = os.path.join(
            config.report_dir or config.state_dir, f"{plan.plan_id}.html"
        )
        write_html(plan, path)
        print(f"Report: {path}")
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2, default=str))
    else:
        print(format_plan(plan, verbose=args.verbose))
    return EXIT_OK


def _decide(args: argparse.Namespace, decision: str) -> int:
    config = load_config(args.config)
    plan = find_plan(config, args.plan_id)
    selected = _select_changes(plan, args.all, args.patterns)
    if not selected:
        print("No matching pending changes.")
        return EXIT_OK
    for change in selected:
        change.decision = decision
        change.decision_reason = (
            "approved by CLI" if decision == Decision.ACCEPTED.value else "denied by CLI"
        )
    if not any(
        change.decision == Decision.PENDING.value for change in plan.changes
    ):
        plan.blocking_reasons = []
    plan.recompute_status()
    save_plan(config, plan)
    verb = "approved" if decision == Decision.ACCEPTED.value else "denied"
    print(f"{verb.capitalize()} {len(selected)} change(s) in plan {plan.plan_id}.")
    print(f"Plan status: {plan.status}")
    if plan.status == "approval_required":
        remaining = sum(
            1 for change in plan.changes if change.decision == Decision.PENDING.value
        )
        print(f"{remaining} change(s) still pending.")
    return EXIT_OK


def cmd_approve(args: argparse.Namespace) -> int:
    return _decide(args, Decision.ACCEPTED.value)


def cmd_deny(args: argparse.Namespace) -> int:
    return _decide(args, Decision.DENIED.value)


def cmd_apply(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    plan = find_plan(config, args.plan_id)
    library = _library(config, plan.library)
    run_id = f"run-{now_stamp()}-{uuid.uuid4().hex[:6]}"
    with _lock(config):
        with State(config.state_dir) as state:
            state.start_run(run_id, library.name, "apply", plan.plan_id)
            try:
                result = apply_plan(
                    config,
                    state,
                    library,
                    plan,
                    run_id=run_id,
                    allow_pending=args.allow_pending,
                    progress=_progress,
                )
            except PlanStaleError as exc:
                state.finish_run(run_id, "stale")
                print(f"Plan is stale: {exc}", file=sys.stderr)
                return EXIT_STALE
            except ApplyError as exc:
                state.finish_run(run_id, "refused")
                print(f"Apply refused: {exc}", file=sys.stderr)
                if "require approval" in str(exc):
                    return EXIT_APPROVAL
                return EXIT_APPLY
            state.finish_run(
                run_id,
                result.status,
                {
                    "plan_id": plan.plan_id,
                    "applied": result.applied,
                    "errors": result.errors,
                },
            )
            print(
                f"Apply {result.status}: applied={result.applied}, "
                f"retained={result.retained}, denied={result.denied}, "
                f"errors={len(result.errors)}"
            )
            for error in result.errors:
                print(f"ERROR {error}", file=sys.stderr)
            if result.status == "applied":
                supersede_pending(config, library.name, plan.plan_id)
            if result.status in {"partial", "failed"}:
                return EXIT_APPLY
            if result.status == "approval_required":
                return EXIT_APPROVAL
            return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with State(config.state_dir) as state:
        libraries: list[dict[str, Any]] = []
        for library in config.enabled_libraries(args.library):
            row = state.get_library(library.name) or {}
            trash = state.trash_summary(library.name)
            pending = pending_plans(config, library.name)
            runs = state.recent_runs(limit=1, library=library.name)
            active = state.active_run_progress(library.name)
            libraries.append(
                {
                    "name": library.name,
                    "source": library.source,
                    "target": library.target,
                    "source_mount": describe_mount(library.source),
                    "target_mount": describe_mount(library.target),
                    "source_titles": int(row.get("last_source_titles") or 0),
                    "target_titles": int(row.get("last_target_titles") or 0),
                    "last_run": row.get("last_run"),
                    "last_status": row.get("last_status"),
                    "pending_plans": len(pending),
                    "trash_entries": trash["entries"],
                    "trash_size": trash["size"],
                    "recent_run": runs[0] if runs else None,
                    "active": active,
                    "pending_list": pending,
                }
            )
    if args.json:
        out = []
        for item in libraries:
            d = dict(item)
            d["pending_plans_detail"] = [
                {"plan_id": p.plan_id, "status": p.status, "changes": len(p.changes)}
                for p in d.pop("pending_list", [])
            ]
            out.append(d)
        print(json.dumps({"libraries": out}, indent=2, default=str))
        return EXIT_OK
    for item in libraries:
        print(f"{item['name']}")
        print(f"  source: {item['source']}  [{item['source_mount']}]")
        print(f"  target: {item['target']}  [{item['target_mount']}]")
        print(
            f"  known titles: source={item['source_titles']} "
            f"target={item['target_titles']}"
        )
        print(
            f"  last run: {item['last_run'] or '-'} "
            f"status={item['last_status'] or '-'}"
        )
        if item.get("pending_list"):
            for p in item["pending_list"][:3]:
                pending_cnt = sum(
                    1 for c in p.changes if c.decision == Decision.PENDING.value
                )
                acc = sum(
                    1 for c in p.changes if c.decision == Decision.ACCEPTED.value
                )
                print(
                    f"  PLAN {p.plan_id}: status={p.status} changes={len(p.changes)} "
                    f"pending={pending_cnt} accepted={acc}"
                )
        print(
            f"  pending plans: {item['pending_plans']}  "
            f"trash: {item['trash_entries']} entries, "
            f"{human_size(item['trash_size'])}"
        )
        if item.get("active"):
            a = item["active"]
            ft = int(a.get("files_total") or 0)
            fd = int(a.get("files_done") or 0)
            bt = int(a.get("bytes_total") or 0)
            bd = int(a.get("bytes_done") or 0)
            pct = f"{int((fd/ft)*100)}%" if ft else "-"
            print(
                f"  APPLYING: {a['id']} plan={a.get('plan_id')} "
                f"files {fd}/{ft} ({pct}) {human_size(bd)}/{human_size(bt)} "
                f"{a.get('current') or ''}"
            )
        elif item["recent_run"]:
            run = item["recent_run"]
            print(
                f"  recent run: {run['id']} {run['status']} "
                f"started={run['started']}"
            )
    return EXIT_OK


def cmd_policy(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    library = _library(config, args.library)
    if args.action in {"add-whitelist", "add-blacklist", "remove"} and not args.pattern:
        raise ConfigError(f"policy {args.action} requires a pattern")
    if args.action == "list":
        policy = PolicySet.load(library.whitelist, library.blacklist)
        print(f"Whitelist: {library.whitelist}")
        for rule in policy.whitelist:
            print(f"  {rule.raw}  (specificity={rule.specificity})")
        print(f"Blacklist: {library.blacklist}")
        for rule in policy.blacklist:
            print(f"  {rule.raw}  (specificity={rule.specificity})")
        return EXIT_OK
    if args.action == "test":
        policy = PolicySet.load(library.whitelist, library.blacklist)
        match = policy.evaluate(args.path)
        print(f"{args.path}: {match.kind}")
        if match.pattern:
            print(f"  matched: {match.pattern} (specificity={match.specificity})")
        return EXIT_OK
    if args.action in {"add-whitelist", "add-blacklist"}:
        path = library.whitelist if args.action == "add-whitelist" else library.blacklist
        if not path:
            raise ConfigError(f"{args.action} file is not configured for {library.name}")
        added = add_pattern(path, args.pattern)
        print(f"{'Added' if added else 'Already present'}: {args.pattern} -> {path}")
        return EXIT_OK
    if args.action == "remove":
        paths: list[str] = []
        if args.kind in {"whitelist", "both"} and library.whitelist:
            paths.append(library.whitelist)
        if args.kind in {"blacklist", "both"} and library.blacklist:
            paths.append(library.blacklist)
        if not paths:
            raise ConfigError("no policy files configured")
        removed = False
        for path in paths:
            removed = remove_pattern(path, args.pattern) or removed
        print(f"{'Removed' if removed else 'Not found'}: {args.pattern}")
        return EXIT_OK
    raise ConfigError(f"unknown policy action: {args.action}")


def cmd_trash(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with State(config.state_dir) as state:
        if args.action == "list":
            entries = list_trash(state, args.library, include_done=args.all)
            if args.json:
                print(json.dumps(entries, indent=2, default=str))
            else:
                print(format_trash(entries))
            return EXIT_OK
        if args.action == "restore":
            if not args.library:
                raise ConfigError("trash restore requires --library")
            if args.id is None:
                raise ConfigError("trash restore requires --id")
            library = _library(config, args.library)
            path = restore_trash(
                config, library, state, args.id, overwrite=args.overwrite
            )
            print(f"Restored trash entry {args.id} to {path}")
            return EXIT_OK
        if args.action == "purge":
            if not args.yes:
                print(
                    "Refusing to purge without --yes. "
                    "This permanently deletes quarantined content.",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            libraries: list[LibraryConfig]
            if args.id is not None:
                row = state.get_trash(args.id)
                if not row:
                    raise ConfigError(f"trash entry not found: {args.id}")
                libraries = [_library(config, row["library"])]
            elif args.library:
                libraries = [_library(config, args.library)]
            else:
                libraries = config.enabled_libraries()
            total_removed = 0
            total_bytes = 0
            older = args.older_than
            if older is None and not args.all and args.id is None:
                older = config.trash_retention_days
            for library in libraries:
                result = purge_trash(
                    config,
                    library,
                    state,
                    older_than_days=older,
                    trash_id=args.id,
                    all_entries=args.all,
                )
                total_removed += result["removed"]
                total_bytes += result["bytes"]
            print(
                f"Purged {total_removed} trash entr"
                f"{'y' if total_removed == 1 else 'ies'}, {human_size(total_bytes)}."
            )
            return EXIT_OK
    raise ConfigError(f"unknown trash action: {args.action}")


def cmd_gui(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    from .gui import launch_gui

    return launch_gui(config, library=args.library)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mediasync",
        description="Media-aware one-way sync with approvals, policy files and quarantine.",
    )
    parser.add_argument("--config", "-c", help="path to config TOML")
    parser.add_argument("--verbose", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version=f"mediasync {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create an example config and policy files")
    init.add_argument("--path", default="mediasync.toml")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    config = sub.add_parser("config", help="validate or show configuration")
    config.add_argument("action", choices=["validate", "show"])
    config.add_argument("--show-rsync", action="store_true")
    config.set_defaults(func=cmd_config)

    plan = sub.add_parser("plan", help="scan and create a plan without changing target")
    plan.add_argument("--library", "-l")
    plan.add_argument("--deep", action="store_true")
    plan.add_argument("--html", dest="html", action="store_true", default=None)
    plan.add_argument("--no-html", dest="html", action="store_false")
    plan.add_argument("--json", action="store_true")
    plan.add_argument("--force", action="store_true")
    plan.add_argument("--allow-empty-target", action="store_true")
    plan.add_argument("--allow-source-drop", action="store_true")
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="plan and apply according to policy (cron-safe)")
    run.add_argument("--library", "-l")
    run.add_argument("--deep", action="store_true")
    run.add_argument("--apply-safe", action="store_true")
    run.add_argument("--force", action="store_true")
    run.add_argument("--non-interactive", action="store_true")
    run.add_argument("--allow-empty-target", action="store_true")
    run.add_argument("--allow-source-drop", action="store_true")
    run.set_defaults(func=cmd_run)

    pending = sub.add_parser("pending", help="list plans awaiting approval")
    pending.add_argument("--library", "-l")
    pending.add_argument("--json", action="store_true")
    pending.set_defaults(func=cmd_pending)

    show = sub.add_parser("show", help="show a plan")
    show.add_argument("plan_id")
    show.add_argument("--json", action="store_true")
    show.add_argument("--html", action="store_true")
    show.set_defaults(func=cmd_show)

    approve = sub.add_parser("approve", help="approve pending changes")
    approve.add_argument("plan_id")
    approve.add_argument("patterns", nargs="*")
    approve.add_argument("--all", action="store_true")
    approve.set_defaults(func=cmd_approve)

    deny = sub.add_parser("deny", help="deny pending changes")
    deny.add_argument("plan_id")
    deny.add_argument("patterns", nargs="*")
    deny.add_argument("--all", action="store_true")
    deny.set_defaults(func=cmd_deny)

    apply = sub.add_parser("apply", help="apply an approved plan")
    apply.add_argument("plan_id")
    apply.add_argument("--allow-pending", action="store_true")
    apply.set_defaults(func=cmd_apply)

    status = sub.add_parser("status", help="show library, plan and trash status")
    status.add_argument("--library", "-l")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    policy = sub.add_parser("policy", help="manage whitelist/blacklist files")
    policy.add_argument("action", choices=["list", "test", "add-whitelist", "add-blacklist", "remove"])
    policy.add_argument("--library", "-l", required=True)
    policy.add_argument("pattern", nargs="?")
    policy.add_argument("--path")
    policy.add_argument("--kind", choices=["whitelist", "blacklist", "both"], default="both")
    policy.set_defaults(func=cmd_policy)

    trash = sub.add_parser("trash", help="inspect, restore or purge quarantined content")
    trash.add_argument("action", choices=["list", "restore", "purge"])
    trash.add_argument("--library", "-l")
    trash.add_argument("--json", action="store_true")
    trash.add_argument("--all", action="store_true")
    trash.add_argument("--id", type=int)
    trash.add_argument("--older-than", type=int)
    trash.add_argument("--overwrite", action="store_true")
    trash.add_argument("--yes", action="store_true")
    trash.set_defaults(func=cmd_trash)

    gui = sub.add_parser("gui", help="launch the tkinter review GUI")
    gui.add_argument("--library", "-l")
    gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        setup_logging(True)
    if args.command == "policy" and args.action == "test":
        if not args.path:
            parser.error("policy test requires --path")
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SafetyError as exc:
        print(f"SAFETY ERROR: {exc}", file=sys.stderr)
        return EXIT_SAFETY
    except LockHeld as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_LOCK
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except PlanStaleError as exc:
        print(f"STALE PLAN: {exc}", file=sys.stderr)
        return EXIT_STALE
    except ApplyError as exc:
        print(f"APPLY ERROR: {exc}", file=sys.stderr)
        return EXIT_APPLY
    except TrashError as exc:
        print(f"TRASH ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
