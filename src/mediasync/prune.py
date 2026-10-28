from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import report
from .config import Config
from .models import LibraryConfig
from .policy import PolicySet
from .state import State
from .trash import quarantine, trash_root
from .util import human_size, list_visible_titles, now_iso, now_stamp

LOG = logging.getLogger(__name__)
Progress = Callable[[str], None]


@dataclass
class PruneCandidate:
    title: str
    rel_path: str
    abs_path: str
    is_dir: bool
    size: int
    blacklisted_by: str
    blacklist_spec: int
    protected: bool = False
    whitelist_by: str | None = None
    whitelist_spec: int = 0
    reason: str = ""
    status: str = "pending"


@dataclass
class PruneResult:
    run_id: str
    library: str
    candidates: list[PruneCandidate]
    moved: int = 0
    skipped_protected: int = 0
    skipped_other: int = 0
    errors: list[str] = field(default_factory=list)
    trash_ids: list[int] = field(default_factory=list)
    report_path: str | None = None
    report_html_path: str | None = None
    status: str = "ok"


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in files:
                fp = os.path.join(root, name)
                try:
                    if os.path.islink(fp):
                        continue
                    st = os.lstat(fp)
                    total += st.st_size
                except Exception:
                    continue
    except Exception:
        pass
    return total


def _best_blacklist_match(policy: PolicySet, path: str):
    from .policy import best_match

    return best_match(policy.blacklist, path)


def _best_whitelist_match(policy: PolicySet, path: str):
    from .policy import best_match

    return best_match(policy.whitelist, path)


def find_prune_candidates(
    config: Config,
    library: LibraryConfig,
    policy: PolicySet | None = None,
) -> list[PruneCandidate]:
    if policy is None:
        policy = PolicySet.load(library.whitelist, library.blacklist)
    candidates: list[PruneCandidate] = []
    target_root = os.path.realpath(library.target)
    try:
        titles = list_visible_titles(target_root)
    except Exception:
        titles = []

    for title in titles:
        title_path = os.path.join(target_root, title)
        if not os.path.lexists(title_path):
            continue
        title_rel = title.strip("/")
        black_match = _best_blacklist_match(policy, title_rel)
        if not black_match:
            continue
        white_match = _best_whitelist_match(policy, title_rel)
        if white_match and white_match.specificity >= black_match.specificity:
            candidates.append(
                PruneCandidate(
                    title=title,
                    rel_path=title_rel,
                    abs_path=title_path,
                    is_dir=os.path.isdir(title_path) and not os.path.islink(title_path),
                    size=_dir_size(title_path) if (os.path.isdir(title_path) and not os.path.islink(title_path)) else 0,
                    blacklisted_by=black_match.pattern or black_match.raw or "",
                    blacklist_spec=black_match.specificity,
                    protected=True,
                    whitelist_by=white_match.pattern or white_match.raw or "",
                    whitelist_spec=white_match.specificity,
                    reason=f"protected by whitelist {white_match.pattern or white_match.raw} (>= specificity)",
                    status="protected",
                )
            )
            continue
        if os.path.isdir(title_path) and not os.path.islink(title_path):
            candidates.append(
                PruneCandidate(
                    title=title,
                    rel_path=title_rel,
                    abs_path=title_path,
                    is_dir=True,
                    size=_dir_size(title_path),
                    blacklisted_by=black_match.pattern or black_match.raw or "",
                    blacklist_spec=black_match.specificity,
                    protected=False,
                    reason=f"blacklisted by {black_match.pattern or black_match.raw}",
                    status="pending",
                )
            )
        else:
            continue
    return candidates


def prune_targets(
    config: Config,
    state: State,
    library: LibraryConfig,
    candidates: list[PruneCandidate],
    run_id: str,
    permanent: bool = False,
    force: bool = False,
    progress: Progress | None = None,
) -> PruneResult:
    def report_msg(msg: str) -> None:
        LOG.info(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    result = PruneResult(run_id=run_id, library=library.name, candidates=candidates)
    target_root = os.path.realpath(library.target)

    try:
        state.start_run(run_id, library.name, "prune", None)
    except Exception:
        pass

    for cand in candidates:
        if cand.protected and not force:
            result.skipped_protected += 1
            cand.status = "skipped_protected"
            continue
        if not (cand.abs_path.startswith(target_root + os.sep) or cand.abs_path == target_root):
            result.errors.append(f"{cand.rel_path}: path outside target root")
            cand.status = "error"
            continue
        if not os.path.lexists(cand.abs_path):
            result.skipped_other += 1
            cand.status = "skipped_missing"
            continue
        try:
            quarantine(
                config,
                library,
                state,
                run_id,
                cand.rel_path,
                cand.reason or "blacklisted - removed from target",
                size=cand.size,
                entry_type="dir" if cand.is_dir else "file",
                permanent=permanent or config.delete_mode == "permanent",
            )
            result.moved += 1
            cand.status = "moved"
            report_msg(f"{library.name}: pruned {cand.rel_path}")
        except Exception as exc:
            result.errors.append(f"{cand.rel_path}: {exc}")
            cand.status = "error"
            continue

    if result.errors and result.moved == 0:
        result.status = "failed"
    elif result.errors:
        result.status = "partial"
    else:
        result.status = "ok"

    try:
        state.finish_run(
            run_id,
            result.status,
            {"moved": result.moved, "skipped_protected": result.skipped_protected, "errors": len(result.errors)},
        )
    except Exception:
        pass

    try:
        report_path = write_prune_report(config, result, candidates)
        result.report_path = report_path
    except Exception as exc:
        result.errors.append(f"report: {exc}")

    try:
        state.audit(run_id, library.name, "prune_blacklisted", None, f"moved={result.moved} skipped_protected={result.skipped_protected}")
    except Exception:
        pass

    return result


def format_prune_report(result: PruneResult, candidates: list[PruneCandidate]) -> str:
    lines = []
    lines.append("MediaSync prune report")
    lines.append(f"Run: {result.run_id}")
    lines.append(f"Library: {result.library}")
    lines.append(f"Status: {result.status}")
    lines.append(f"Time: {now_iso()}")
    lines.append("")
    lines.append("Summary:")
    lines.append(f"  Moved: {result.moved}")
    lines.append(f"  Skipped (protected): {result.skipped_protected}")
    lines.append(f"  Skipped (other): {result.skipped_other}")
    lines.append(f"  Errors: {len(result.errors)}")
    lines.append("")
    lines.append("Candidates:")
    for cand in candidates:
        lines.append(f"  [{cand.status}] {cand.rel_path} ({'dir' if cand.is_dir else 'file'}) {human_size(cand.size)} - {cand.reason}")
    if result.errors:
        lines.append("")
        lines.append("Errors:")
        for err in result.errors:
            lines.append(f"  {err}")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_prune_report(config: Config, result: PruneResult, candidates: list[PruneCandidate]) -> str:
    directory = config.report_dir or os.path.join(config.state_dir, "reports")
    os.makedirs(directory, exist_ok=True)
    text_path = os.path.join(directory, f"prune-{result.run_id}.txt")
    text = format_prune_report(result, candidates)
    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    result.report_path = text_path
    if getattr(config, "report_html", True):
        try:
            html_path = os.path.join(directory, f"prune-{result.run_id}.html")
            html = _prune_html(result, candidates)
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(html)
            result.report_html_path = html_path
        except Exception:
            pass
    return text_path


def _prune_html(result: PruneResult, candidates: list[PruneCandidate]) -> str:
    rows = []
    for cand in candidates:
        rows.append(
            f"<tr><td>{cand.rel_path}</td><td>{'dir' if cand.is_dir else 'file'}</td><td>{human_size(cand.size)}</td><td>{cand.status}</td><td>{cand.reason}</td></tr>"
        )
    table = "\n".join(rows)
    errors = "".join(f"<li>{e}</li>" for e in result.errors)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>MediaSync prune {result.run_id}</title>
<style>body{{font-family:sans-serif}} table{{border-collapse:collapse}} td,th{{border:1px solid #ccc;padding:4px 6px}}</style>
</head><body>
<h1>MediaSync prune report</h1>
<p>Run: {result.run_id}<br>Library: {result.library}<br>Status: {result.status}</p>
<h2>Summary</h2>
<ul><li>Moved: {result.moved}</li><li>Skipped (protected): {result.skipped_protected}</li><li>Skipped (other): {result.skipped_other}</li><li>Errors: {len(result.errors)}</li></ul>
<h2>Candidates</h2>
<table><thead><tr><th>Path</th><th>Type</th><th>Size</th><th>Status</th><th>Reason</th></tr></thead><tbody>{table}</tbody></table>
<h2>Errors</h2><ul>{errors}</ul>
</body></html>"""
