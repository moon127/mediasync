# AGENTS.md - mediasync

This document captures the requirements, design decisions, and implementation choices made while building `mediasync`. It is intended for future agents/models working in this repo.

## Project summary
- Name: `mediasync` (v0.1.0)
- Purpose: One-way, media-aware sync from SMB sources (`/Volumes/Multimedia/TV`, `/Volumes/Multimedia/Movies`) to SMB targets (`/Volumes/Media_UNAS/Dale/TV`, `/Volumes/Media_UNAS/Dale/Movies`). Never writes to source.
- Interface: CLI (`mediasync`) + tkinter GUI (`mediasync gui`)
- Repo: `/Users/garethwoolridge/sync-tool` (git)

## Requirements
### Functional
- One-way source→target sync
- Per-title parallel planning with fingerprint cache
- Whitelist/blacklist policy files with deterministic precedence
- Approval workflow (pending/accepted/denied/held); cron-safe `run`
- Re-encode detection (extension/quality tag/size changes) with reporting
- Reviewable plans (terminal/JSON/HTML)
- Target-side quarantine via `.mediasync-trash/<run-id>/{deleted,backups}/` (trash default, permanent optional)
- Source sanity checks (mount presence, title count drop, empty target)
- GUI for review/approve/policy/trash
- Optional ffprobe metadata

### Non-functional
- Large SMB shares must be efficient (parallel per-title scans)
- Safe by default (read-only source, path validation, locks, stale detection)
- Deterministic exits/codes for scripting/cron
- Stdlib-only tests

## Architecture
### Modules (src/mediasync/)
| Module | Responsibility |
| --- | --- |
| `cli.py` | Argparse, commands (init/config/plan/run/pending/show/approve/deny/apply/status/policy/trash/gui), exit codes, locking, run orchestration |
| `config.py` | TOML load/validate, defaults, `init_config` (writes templates), per-library rsync overrides, allowlists |
| `models.py` | Enums (`Action`, `Decision`, `PlanStatus`), `FileInfo`, `Change` (categories/destructive/size/ident/signature), `TitleFingerprint`, `Plan`, `LibraryConfig`, `Config` |
| `planner.py` | Per-title planning (thread pool), policy evaluation, fingerprints/cache, bulk guards, title walks, per-file policy expansion, `pending_plans` filter, plan load/find/save |
| `apply.py` | Stale verification, apply accepted changes by title, rsync copy/add/delete/quarantine, re-encode handling/backups, verification, cache refresh, audit |
| `rsync_backend.py` | Safe rsync command builder (base flags), allowlist validation for `extra_args`, itemized scan parser (`SEPARATOR="|"`, handles `*deleting`), `copy_title`/`copy_files` |
| `classifier.py` | Re-encode detection, episode keys, ffprobe integration, merge logic |
| `policy.py` | Load rules, match types (exact/glob/regex), specificity calc, precedence (specificity; blacklist wins ties), evaluate |
| `safety.py` | Mount checks, overlap checks, root filesystem rejection, title counts, `inspect_library` |
| `state.py` | SQLite schema, migrations (`_migrate()` adds digest columns), title cache (with digests), runs, plans index, trash, audit, upserts |
| `trash.py` | Quarantine to trash paths, restore, purge, tracking |
| `report.py` | Text/HTML reports, human sizes, same-path re-encode display |
| `util.py` | `tree_fingerprint` (names/sizes/mtimes/types + sha256 digest), `directory_fingerprint` (mtime/count wrapper), locks, paths, junk detection, formatting, notify |
| `gui.py` | tkinter UI (plan picker, policy tester, trash window, apply/approve) |

### Data flow
1. `inspect_library` validates mounts/counts/sanity
2. `plan_library` walks titles (parallel), fingerprints source/target, uses cache if clean (`fast`), evaluates policy, classifies changes, applies bulk guards
3. Plan saved as JSON under `state_dir/plans/`, HTML report optional
4. `approve/deny` updates decisions; `apply` verifies staleness, copies via rsync, quarantines deletes/replaces to trash, updates cache/audit
5. `run` orchestrates plan+apply per library with exit codes

## Key design decisions & rationale
| Decision | Choice | Rationale |
| --- | --- | --- |
| Sync direction | One-way source→target only | Safety; source never modified |
| Scan strategy | Per-title parallel + fingerprint cache | SMB latency; avoid rescanning unchanged titles |
| Cache keying | `tree_fingerprint` (max_mtime_ns, count, sha256 digest) over names/sizes/mtimes/types | Detects nested changes even if `max_mtime` unchanged; digest-based invalidation more robust than mtime-only |
| Rsync flags | Base: `-rlt --no-perms --no-owner --no-group --no-xattrs --no-acls --no-devices --no-specials` + timeout; `--fsync` apply-only; `--modify-window` default 2 | Safe, portable, avoids permission/ownership issues, handles SMB timestamp skew |
| Itemized parsing | Separator `\|` with `%i\|%l\|%n` (not `\x1f`) | rsync escaped `\x1f` as literal escapes; `\|` unambiguous |
| Policy precedence | Specificity = path length + 100*slash count; if tie, blacklist wins | Predictable, intuitive for nested paths |
| Whitelist semantics | Protects existing target content; holds updates/replaces/re-encodes/deletes of existing target; allows adding new source content | Conservative for curated libraries |
| Blacklist semantics | Ignore path entirely (no copy/update/delete) | Hard exclusion |
| Per-file policy (new titles) | If title blacklisted → skip whole title. Else filter `allowed_paths` to non-blacklisted files; pass subset to rsync/copy | Avoid copying unwanted subpaths into new titles |
| Per-file policy (target-only) | Expand target-only titles into per-file DELETE changes unless whitelisted (held) or blacklisted (skipped) | Granular control over deletions |
| Title-level whitelist | Skip expansion to file deletes (protected) | Preserve curated content |
| Re-encode handling | Detect by classification; same-path re-encode quarantines old target to backups (fixed bug) | Preserve replaceable media safely; backups tracked in trash |
| Deletions | Trash by default to `<target>/.mediasync-trash/<run-id>/{deleted,backups}/` with SQLite tracking | Recoverable, auditable |
| Pending plans | `pending_plans` only returns plans with actionable changes (PENDING/ACCEPTED) or errors; HELD-only plans don't block `run` | Avoid blocking cron on fully-held plans |
| Run IDs | Per-library `run_id = f"{run_base}-{index}"` | Clear separation of library operations |
| Config validation | Strict types (bools required), negative limits rejected, TOML/KeyError/TypeError → `ConfigError` | Fail fast |
| Rsync extra_args | Allowlist only: `--bwlimit`, `--compress`, `--compress-level`, `--compress-choice`, `--whole-file`, `--no-whole-file`, `--block-size`, `--checksum-choice`, `--sparse`, `--preallocate`, `--no-motd`, `--contimeout`, `--sockopts`, `--ipv4`, `--ipv6` | Prevent dangerous filter/deletion changes |
| Staleness | Verify fingerprints/mtimes/counts before apply (window-based) | Prevent applying against changed state |
| Concurrency | Thread pool for per-title scans/copies | Parallelize SMB work |

## Safety model
- Source read-only; all writes under target root (path validation)
- Source/target must exist, be dirs, no overlap
- `require_mount` rejects root filesystem
- Title count drop / empty target aborts (configurable)
- Lock file prevents concurrent operations
- Stale plan detection → exit 14
- Trash quarantine, path validation
- Rsync allowlist, no archive perms/ownership

## Exit codes
| Code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Usage/config error |
| 3 | Safety check failed |
| 4 | Plan has scan errors |
| 10 | Approval required; nothing applied |
| 11 | Blocked by policy/bulk guard |
| 12 | Apply failure/partial apply |
| 13 | Lock held |
| 14 | Stale plan |
| 130 | Interrupted |

## Testing
- Framework: stdlib `unittest` (no pytest required)
- Run: `PYTHONPATH=src python3 -m unittest discover -s tests -q` (56 tests OK)
- Coverage areas: policy, rsync backend (incl. integration), classifier, util (tree_fingerprint), config, apply (per-file policy, re-encode backup, held-only), report, cli

## Conventions
- Python 3.11+, type hints
- Dataclasses for models
- No comments unless necessary
- Follow existing patterns/imports
- Relative policy paths resolved from config dir
- Per-library `[libraries.rsync]` overrides globals
- Junk excludes applied by default

## Configuration notes
- `state_dir`, `concurrency`, `scan_mode` (`fast`/`deep`), `trash_retention_days`, `delete_mode` (`trash`/`permanent`), `require_mount`, `min_source_titles`, `max_source_title_drop_pct`, `report_html`
- `[rsync]`: `modify_window`, `fsync`, `timeout` (default 600s, passed as `--timeout` to rsync; affects scan/copy subprocess timeouts), `extra_args` (allowlisted)
- `[apply]`: `auto_approve` list, `max_pending_deletions`, `max_delete_gb`, `bulk_reencode_count`
- `[[libraries]]`: `name`, `kind`, `source`, `target`, `whitelist`, `blacklist` (+ optional `[libraries.rsync]`)

## Gotchas & lessons learned
- Rsync itemized output escaping: use `|` separator
- SMB timestamp skew: `--modify-window=2`
- Nested changes need digest-based fingerprints (mtime-only insufficient)
- Same-path re-encode needs special backup handling
- HELD-only plans must not block cron
- Per-file policy requires careful expansion for target-only deletes

## Quick start (dev)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
PYTHONPATH=src python3 -m mediasync --help
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## File map (key)
- Config: `pyproject.toml`, `examples/config.toml`, `examples/policy/*.txt`
- Core: `src/mediasync/{cli,config,planner,apply,rsync_backend,classifier,policy,safety,state,trash,report,gui,util,models,__init__,__main__}.py`
- Tests: `tests/test_*.py`
