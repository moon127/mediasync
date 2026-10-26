from __future__ import annotations

import html
import os
from collections import Counter
from typing import Any

from .classifier import probe_summary
from .models import Action, Decision, Plan
from .util import ensure_dir, human_delta, human_size, now_iso

ACTION_ORDER = {
    Action.DELETE_TITLE.value: 0,
    Action.DELETE.value: 1,
    Action.REENCODE.value: 2,
    Action.REPLACE.value: 3,
    Action.UPDATE.value: 4,
    Action.ADD.value: 5,
    Action.ADD_TITLE.value: 6,
}

ACTION_LABEL = {
    Action.ADD_TITLE.value: "ADD TITLE",
    Action.ADD.value: "ADD",
    Action.UPDATE.value: "UPDATE",
    Action.DELETE_TITLE.value: "DELETE TITLE",
    Action.DELETE.value: "DELETE",
    Action.REPLACE.value: "REPLACE",
    Action.REENCODE.value: "REENCODE",
}

DECISION_LABEL = {
    Decision.PENDING.value: "PENDING",
    Decision.ACCEPTED.value: "ACCEPT",
    Decision.DENIED.value: "DENY",
    Decision.HELD.value: "RETAIN",
}


def plan_stats(plan: Plan) -> dict[str, Any]:
    counts = Counter(change.category for change in plan.changes)
    decisions = Counter(change.decision for change in plan.changes)
    total_bytes = sum(change.size for change in plan.changes)
    destructive_bytes = sum(
        change.size for change in plan.changes if change.destructive
    )
    return {
        "changes": len(plan.changes),
        "counts": dict(counts),
        "decisions": dict(decisions),
        "total_bytes": total_bytes,
        "destructive_bytes": destructive_bytes,
        "reencodes": counts.get("reencode", 0),
        "deletes": counts.get("delete_title", 0) + counts.get("delete_media", 0),
        "adds": counts.get("add_title", 0)
        + counts.get("add_media", 0)
        + counts.get("add_sidecar", 0),
    }


def format_plan(plan: Plan, *, verbose: bool = False) -> str:
    stats = plan_stats(plan)
    lines: list[str] = []
    lines.append(f"Library: {plan.library} ({plan.kind})")
    lines.append(f"Plan:    {plan.plan_id}")
    lines.append(f"Status:  {plan.status.upper()}")
    lines.append(f"Created: {plan.created_at}")
    lines.append(f"Source:  {plan.source}")
    lines.append(f"Target:  {plan.target}")
    if plan.applied_at:
        lines.append(f"Applied: {plan.applied_at}")
    lines.append("")
    lines.append(
        "Summary: "
        f"{stats['changes']} change(s), {human_size(stats['total_bytes'])}; "
        f"adds={stats['adds']}, deletes={stats['deletes']}, "
        f"re-encodes={stats['reencodes']}"
    )
    decisions = stats["decisions"]
    lines.append(
        "Decisions: "
        + ", ".join(
            f"{DECISION_LABEL.get(key, key).lower()}={value}"
            for key, value in sorted(decisions.items())
        )
    )
    if plan.blocking_reasons:
        lines.append("")
        lines.append("Blocking reasons:")
        for reason in plan.blocking_reasons:
            lines.append(f"  ! {reason}")
    if plan.errors:
        lines.append("")
        lines.append("Errors:")
        for error in plan.errors:
            lines.append(f"  ERROR {error}")
    if plan.warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in plan.warnings:
            lines.append(f"  WARN {warning}")

    if plan.changes:
        lines.append("")
        by_title: dict[str, list] = {}
        for change in plan.changes:
            by_title.setdefault(change.title, []).append(change)
        for title in sorted(by_title):
            title_changes = sorted(
                by_title[title],
                key=lambda change: (
                    ACTION_ORDER.get(change.action, 9),
                    change.path,
                ),
            )
            lines.append(f"{title}/")
            for change in title_changes:
                lines.append(_format_change(change, verbose=verbose))
            lines.append("")
    else:
        lines.append("")
        lines.append("No changes detected.")

    pending = [c for c in plan.changes if c.decision == Decision.PENDING.value]
    if pending:
        lines.append("")
        lines.append(
            f"{len(pending)} change(s) need approval. "
            f"Use: mediasync approve {plan.plan_id} --all  or  "
            f"mediasync deny {plan.plan_id} --all"
        )
    return "\n".join(lines).rstrip() + "\n"


def _format_change(change, *, verbose: bool) -> str:
    label = ACTION_LABEL.get(change.action, change.action.upper())
    decision = DECISION_LABEL.get(change.decision, change.decision.upper())
    size_text = ""
    if change.action == Action.REENCODE.value:
        size_text = (
            f" [{human_size(change.old_size)} -> {human_size(change.size)} "
            f"{human_delta(change.old_size, change.size)}]"
        )
        path_text = (
            f"{change.old_path} -> {change.path}"
            if change.old_path and change.old_path != change.path
            else change.path
        )
    elif change.action in {Action.DELETE.value, Action.DELETE_TITLE.value}:
        size_text = f" [{human_size(change.size)}]"
        path_text = change.path
    elif change.action == Action.UPDATE.value and change.old_size:
        size_text = (
            f" [{human_size(change.old_size)} -> {human_size(change.size)} "
            f"{human_delta(change.old_size, change.size)}]"
        )
        path_text = change.path
    else:
        size_text = f" [{human_size(change.size)}]" if change.size else ""
        path_text = change.path
    policy = ""
    if change.policy != "default":
        policy = f" policy={change.policy}"
        if change.policy_pattern:
            policy += f"({change.policy_pattern})"
    line = f"  {decision:<7} {label:<12} {path_text}{size_text}"
    if change.reason:
        line += f"  # {change.reason}"
    line += policy
    if change.apply_error:
        line += f"  ERROR={change.apply_error}"
    if verbose and change.details.get("new_probe"):
        line += f"  new={probe_summary(change.details['new_probe'])}"
    if verbose and change.details.get("old_probe"):
        line += f"  old={probe_summary(change.details['old_probe'])}"
    return line


def format_trash(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "Trash is empty.\n"
    lines = [f"{'ID':>5}  {'LIB':<10} {'SIZE':>10}  {'TYPE':<4}  {'DELETED':<20} PATH"]
    for entry in entries:
        lines.append(
            f"{entry['id']:>5}  {entry['library']:<10} "
            f"{human_size(entry['size']):>10}  {entry['entry_type']:<4}  "
            f"{entry['deleted_at'][:19]:<20} {entry['original_path']}"
        )
    return "\n".join(lines) + "\n"


def write_html(plan: Plan, path: str) -> str:
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    stats = plan_stats(plan)
    rows: list[str] = []
    by_title: dict[str, list] = {}
    for change in plan.changes:
        by_title.setdefault(change.title, []).append(change)
    for title in sorted(by_title):
        for change in sorted(
            by_title[title],
            key=lambda item: (ACTION_ORDER.get(item.action, 9), item.path),
        ):
            if change.action == Action.REENCODE.value:
                path_text = (
                    f"{html.escape(change.old_path)} &rarr; "
                    f"{html.escape(change.path)}"
                    if change.old_path and change.old_path != change.path
                    else html.escape(change.path)
                )
                size_text = (
                    f"{human_size(change.old_size)} &rarr; {human_size(change.size)} "
                    f"({human_delta(change.old_size, change.size)})"
                )
            else:
                path_text = html.escape(change.path)
                size_text = html.escape(human_size(change.size))
            rows.append(
                "<tr>"
                f"<td>{html.escape(title)}</td>"
                f"<td>{html.escape(ACTION_LABEL.get(change.action, change.action))}</td>"
                f"<td>{path_text}</td>"
                f"<td>{size_text}</td>"
                f"<td>{html.escape(DECISION_LABEL.get(change.decision, change.decision))}</td>"
                f"<td>{html.escape(change.policy)}</td>"
                f"<td>{html.escape(change.decision_reason or change.reason)}</td>"
                "</tr>"
            )
    reasons = "".join(
        f"<li>{html.escape(reason)}</li>" for reason in plan.blocking_reasons
    )
    errors = "".join(f"<li>{html.escape(error)}</li>" for error in plan.errors)
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>mediasync {html.escape(plan.plan_id)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 2rem; color: #202124; }}
h1 {{ font-size: 1.4rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.86rem; }}
th, td {{ border-bottom: 1px solid #ddd; padding: 0.4rem; text-align: left; vertical-align: top; }}
th {{ background: #f3f4f6; position: sticky; top: 0; }}
.status {{ font-weight: bold; }}
.warn {{ background: #fff4e5; padding: 0.8rem; margin: 1rem 0; }}
.error {{ background: #fdecea; padding: 0.8rem; margin: 1rem 0; }}
</style>
</head>
<body>
<h1>mediasync plan {html.escape(plan.plan_id)}</h1>
<p><b>Library:</b> {html.escape(plan.library)} &middot;
<b>Status:</b> <span class="status">{html.escape(plan.status)}</span> &middot;
<b>Created:</b> {html.escape(plan.created_at)}</p>
<p><b>Source:</b> {html.escape(plan.source)}<br>
<b>Target:</b> {html.escape(plan.target)}</p>
<p>{stats['changes']} change(s), {html.escape(human_size(stats['total_bytes']))};
adds={stats['adds']}, deletes={stats['deletes']}, re-encodes={stats['reencodes']}</p>
{('<div class="warn"><b>Blocking reasons</b><ul>' + reasons + '</ul></div>') if reasons else ''}
{('<div class="error"><b>Errors</b><ul>' + errors + '</ul></div>') if errors else ''}
<table>
<thead><tr><th>Title</th><th>Action</th><th>Path</th><th>Size</th><th>Decision</th><th>Policy</th><th>Reason</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
<p>Generated by mediasync at {html.escape(now_iso())}.</p>
</body>
</html>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(document)
    return path
