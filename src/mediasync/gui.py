from __future__ import annotations

import json
import os
import queue
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover - depends on the Python build
    tk = None
    ttk = None
    messagebox = None

from .apply import apply_plan
from .config import Config, ConfigError
from .models import Change, Decision, Plan
from .planner import pending_plans, plan_library, save_plan, supersede_pending
from .policy import PolicySet, add_pattern, remove_pattern
from .prune import find_prune_candidates, prune_targets
from .report import ACTION_LABEL, ACTION_ORDER, DECISION_LABEL, plan_stats
from .state import State
from .trash import TrashError, list_trash, purge, restore
from .util import FileLock, human_size, is_junk_name, now_stamp, now_iso

Progress = Callable[[str], None]


def _find_icon() -> str | None:
    candidates = []
    here = Path(__file__).resolve()
    candidates.append(here.parent / "resources" / "icon.png")
    candidates.append(here.parent / "resources" / "icon.gif")
    candidates.append(here.parent.parent / "assets" / "icon.png")
    candidates.append(here.parent.parent / "assets" / "icon.gif")
    candidates.append(Path.cwd() / "assets" / "icon.png")
    candidates.append(Path.cwd() / "assets" / "icon.gif")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except Exception:
            continue
    return None


def _lock(config: Config) -> FileLock:
    return FileLock(os.path.join(config.state_dir, "mediasync.lock"))


def _change_path_text(change: Change) -> str:
    if change.action == "reencode" and change.old_path:
        return f"{change.old_path} -> {change.path}"
    return change.path


def _change_size_text(change: Change) -> str:
    if change.action == "reencode":
        return f"{human_size(change.old_size)} -> {human_size(change.size)}"
    return human_size(change.size) if change.size else "-"


class MediasyncGui:
    def __init__(self, root: Any, config: Config, library: str | None = None) -> None:
        self.root = root
        self.config = config
        self.plan: Plan | None = None
        self.rows: dict[str, Change] = {}
        self.busy = False
        self._busy_widgets: list[Any] = []
        self._worker: queue.Queue | None = None
        try:
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            style.configure(
                "Treeview",
                background="#ffffff",
                fieldbackground="#ffffff",
                foreground="#000000",
                rowheight=22,
            )
            style.configure(
                "Treeview.Heading",
                background="#e9e9e9",
                foreground="#000000",
                relief="raised",
            )
            style.map(
                "Treeview",
                background=[("selected", "#4a90e2")],
                foreground=[("selected", "#ffffff")],
            )
        except Exception:
            pass

        names = [lib.name for lib in config.enabled_libraries()]
        if not names:
            raise ConfigError("no enabled libraries are configured")
        self.current_library = library or names[0]
        if self.current_library not in names:
            raise ConfigError(f"unknown or disabled library: {self.current_library}")

        self.root.title("MediaSync")
        self.root.geometry("1280x780")
        self.root.minsize(960, 620)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._icon_image = None
        icon_path = _find_icon()
        if icon_path and tk is not None:
            try:
                self._icon_image = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, self._icon_image)
            except Exception:
                self._icon_image = None
        try:
            if sys.platform == "darwin":
                self.root.createcommand("::tk::mac::Quit", self._close)
                self.root.tk.call("tk", "appname", "MediaSync")
        except Exception:
            pass
        self._build(names)
        self._build_menubar(names)
        self._build_context_menu()
        self._refresh_library_state()

    def _build(self, names: list[str]) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text="Library").pack(side="left")
        self.library_var = tk.StringVar(value=self.current_library)
        self.library_box = ttk.Combobox(
            top,
            textvariable=self.library_var,
            values=names,
            state="readonly",
            width=18,
        )
        self.library_box.pack(side="left", padx=(6, 16))
        self.library_box.bind("<<ComboboxSelected>>", self._on_library_change)

        self.plan_button = ttk.Button(top, text="New Plan", command=self._new_plan)
        self.pending_button = ttk.Button(
            top, text="Open Pending", command=self._open_pending
        )
        self.apply_button = ttk.Button(top, text="Apply Plan", command=self._apply)
        self.trash_button = ttk.Button(top, text="Trash", command=self._show_trash)
        self.policy_button = ttk.Button(
            top, text="Test Policy", command=self._show_policy_tester
        )
        self.prune_preview_button = ttk.Button(
            top, text="Preview Prune", command=self._preview_prune
        )
        self.prune_button = ttk.Button(
            top, text="Prune Blacklisted", command=self._prune_blacklisted
        )
        for widget in (
            self.plan_button,
            self.pending_button,
            self.apply_button,
            self.trash_button,
            self.policy_button,
            self.prune_preview_button,
            self.prune_button,
        ):
            widget.pack(side="left", padx=3)
        self._busy_widgets.extend(
            [self.plan_button, self.pending_button, self.apply_button, self.prune_preview_button, self.prune_button]
        )

        summary = ttk.LabelFrame(outer, text="Plan summary", padding=8)
        summary.pack(fill="x", pady=(10, 8))
        self.summary_var = tk.StringVar(value="No plan loaded")
        self.decision_var = tk.StringVar(value="")
        ttk.Label(summary, textvariable=self.summary_var).pack(anchor="w")
        ttk.Label(summary, textvariable=self.decision_var).pack(anchor="w")

        tree_frame = ttk.Frame(outer)
        tree_frame.pack(fill="both", expand=True)
        columns = ("title", "action", "path", "size", "decision", "policy", "reason")
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended"
        )
        headings = {
            "title": "Title",
            "action": "Action",
            "path": "Path",
            "size": "Size",
            "decision": "Decision",
            "policy": "Policy",
            "reason": "Reason",
        }
        widths = {
            "title": 230,
            "action": 100,
            "path": 330,
            "size": 125,
            "decision": 90,
            "policy": 85,
            "reason": 280,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=60,
                stretch=column in {"title", "path", "reason"},
            )
        y_scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview
        )
        x_scroll = ttk.Scrollbar(
            tree_frame, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.tag_configure("pending", background="#fff4e5")
        self.tree.tag_configure("accepted", background="#e8f5e9")
        self.tree.tag_configure("denied", background="#eeeeee", foreground="#666666")
        self.tree.tag_configure("held", background="#e3f2fd")
        self._busy_widgets.append(self.tree)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(8, 4))
        self.approve_button = ttk.Button(
            actions, text="Approve", command=lambda: self._decide(Decision.ACCEPTED.value)
        )
        self.deny_button = ttk.Button(
            actions, text="Deny", command=lambda: self._decide(Decision.DENIED.value)
        )
        self.whitelist_button = ttk.Button(
            actions, text="Whitelist Path", command=lambda: self._add_policy("whitelist")
        )
        self.blacklist_button = ttk.Button(
            actions, text="Blacklist Path", command=lambda: self._add_policy("blacklist")
        )
        for widget in (
            self.approve_button,
            self.deny_button,
            self.whitelist_button,
            self.blacklist_button,
        ):
            widget.pack(side="left", padx=3)
        note = ttk.Label(
            actions,
            text="Approve/Deny acts on selected rows; with no selection it acts on every pending row.",
        )
        note.pack(side="left", padx=(14, 0))
        self._busy_widgets.extend(
            [
                self.approve_button,
                self.deny_button,
                self.whitelist_button,
                self.blacklist_button,
            ]
        )

        details_frame = ttk.LabelFrame(outer, text="Selected change", padding=6)
        details_frame.pack(fill="x", pady=(4, 6))
        self.details = tk.Text(
            details_frame,
            height=6,
            wrap="word",
            state="disabled",
            background="#ffffff",
            foreground="#000000",
            insertbackground="#000000",
            selectbackground="#4a90e2",
            selectforeground="#ffffff",
        )
        details_scroll = ttk.Scrollbar(
            details_frame, orient="vertical", command=self.details.yview
        )
        self.details.configure(yscrollcommand=details_scroll.set)
        self.details.pack(side="left", fill="both", expand=True)
        details_scroll.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(
            outer, textvariable=self.status_var, relief="sunken", anchor="w", padding=4
        ).pack(fill="x")

    def _library(self):
        return self.config.library(self.current_library)

    def _on_library_change(self, _event: Any = None) -> None:
        if self.busy:
            self.library_var.set(self.current_library)
            return
        self.current_library = self.library_var.get()
        self.plan = None
        self._refresh_library_state()

    def _refresh_library_state(self) -> None:
        pending = pending_plans(self.config, self.current_library)
        self.status_var.set(
            f"{self.current_library}: {len(pending)} pending plan(s)."
            if pending
            else f"{self.current_library}: no pending plans."
        )
        self._refresh_plan()

    def _refresh_plan(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.rows.clear()
        self._set_details("")
        if self.plan is None:
            self.summary_var.set("No plan loaded")
            self.decision_var.set("")
            return
        stats = plan_stats(self.plan)
        decisions = stats["decisions"]
        self.summary_var.set(
            f"{self.plan.plan_id}  status={self.plan.status.upper()}  "
            f"changes={stats['changes']}  size={human_size(stats['total_bytes'])}  "
            f"adds={stats['adds']}  deletes={stats['deletes']}  "
            f"re-encodes={stats['reencodes']}"
        )
        self.decision_var.set(
            "  ".join(
                f"{DECISION_LABEL.get(key, key)}={value}"
                for key, value in sorted(decisions.items())
            )
        )
        changes = sorted(
            self.plan.changes,
            key=lambda change: (
                change.title.lower(),
                ACTION_ORDER.get(change.action, 9),
                change.path.lower(),
            ),
        )
        for index, change in enumerate(changes):
            iid = f"row-{index}"
            self.rows[iid] = change
            tags = (change.decision,) if change.decision in {
                "pending",
                "accepted",
                "denied",
                "held",
            } else ()
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    change.title,
                    ACTION_LABEL.get(change.action, change.action),
                    _change_path_text(change),
                    _change_size_text(change),
                    DECISION_LABEL.get(change.decision, change.decision),
                    change.policy,
                    change.decision_reason or change.reason,
                ),
                tags=tags,
            )
        if self.plan.blocking_reasons:
            self.status_var.set("Blocked: " + "; ".join(self.plan.blocking_reasons))
        elif self.plan.errors:
            self.status_var.set("Plan errors: " + "; ".join(self.plan.errors))
        elif self.plan.warnings:
            self.status_var.set(f"Plan loaded with {len(self.plan.warnings)} warning(s).")
        else:
            self.status_var.set("Plan loaded.")

    def _on_select(self, _event: Any = None) -> None:
        selected = self.tree.selection()
        if not selected:
            self._set_details("")
            return
        change = self.rows.get(selected[0])
        if change is None:
            self._set_details("")
            return
        data = change.to_dict()
        lines = [
            f"Action:   {data['action']} ({data['category']})",
            f"Path:     {_change_path_text(change)}",
            f"Decision: {data['decision']} - {data['decision_reason'] or data['reason']}",
            f"Policy:   {data['policy']}"
            + (f" ({data['policy_pattern']})" if data["policy_pattern"] else ""),
            f"Size:     {human_size(data['size'])}"
            + (f" (was {human_size(data['old_size'])})" if data["old_size"] else ""),
        ]
        if change.logical_key:
            lines.append(f"Logical:  {change.logical_key}")
        if data.get("details"):
            lines.append("Details:  " + json.dumps(data["details"], indent=2, default=str))
        if data.get("apply_error"):
            lines.append(f"Error:    {data['apply_error']}")
        self._set_details("\n".join(lines))

    def _set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        if text:
            self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _about(self) -> None:
        if messagebox is None:
            return
        try:
            from . import __version__

            version = __version__
        except Exception:
            version = "unknown"
        messagebox.showinfo(
            "About MediaSync",
            f"MediaSync {version}\n\nMedia-aware one-way sync with approvals, policy files and quarantine.",
        )

    def _refresh_view(self) -> None:
        self._refresh_library_state()

    def _approve_all_pending(self) -> None:
        self._decide_all(Decision.ACCEPTED.value)

    def _deny_all_pending(self) -> None:
        self._decide_all(Decision.DENIED.value)

    def _decide_all(self, decision: str) -> None:
        if self.busy or self.plan is None:
            return
        if any(change.applied for change in self.plan.changes):
            messagebox.showinfo(
                "Plan already applied",
                "This plan has already been applied. Create a new plan to make more changes.",
            )
            return
        pending = [
            change
            for change in self.plan.changes
            if change.decision == Decision.PENDING.value
        ]
        if not pending:
            messagebox.showinfo("Nothing pending", "No pending changes remain.")
            return
        verb = "approve" if decision == Decision.ACCEPTED.value else "deny"
        if not messagebox.askyesno(
            "Confirm",
            f"{verb.capitalize()} all {len(pending)} pending change(s)?",
        ):
            return
        for change in pending:
            change.decision = decision
            change.decision_reason = (
                "approved in GUI (all)" if decision == Decision.ACCEPTED.value else "denied in GUI (all)"
            )
        if not any(change.decision == Decision.PENDING.value for change in self.plan.changes):
            self.plan.blocking_reasons = []
        self.plan.recompute_status()
        save_plan(self.config, self.plan)
        self._refresh_plan()
        verb2 = "Approved" if decision == Decision.ACCEPTED.value else "Denied"
        self.status_var.set(f"{verb2} all {len(pending)} pending change(s).")

    def _copy_selected_path(self) -> None:
        if tk is None or self.plan is None:
            return
        targets = self._selected_changes()
        if not targets:
            messagebox.showinfo("Copy", "Select one or more rows first.")
            return
        text = "\n".join(change.path for change in targets)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_var.set(f"Copied {len(targets)} path(s) to clipboard.")
        except Exception as exc:
            self._show_error(exc)

    def _copy_selected_title(self) -> None:
        if tk is None or self.plan is None:
            return
        targets = self._selected_changes()
        if not targets:
            messagebox.showinfo("Copy", "Select one or more rows first.")
            return
        text = "\n".join(change.title for change in targets)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_var.set(f"Copied {len(targets)} title(s) to clipboard.")
        except Exception as exc:
            self._show_error(exc)

    def _show_details_focus(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        self._on_select()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in self._busy_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        self.library_box.configure(state="disabled" if busy else "readonly")

    def _run_async(
        self,
        label: str,
        function: Callable[[Progress], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.status_var.set(label)
        work_queue: queue.Queue = queue.Queue()
        self._worker = work_queue

        def target() -> None:
            try:
                result = function(lambda message: work_queue.put(("progress", message)))
                work_queue.put(("done", result))
            except BaseException as exc:  # noqa: BLE001 - surfaced in the UI
                work_queue.put(("error", exc))

        threading.Thread(target=target, daemon=True).start()
        self._poll(work_queue, on_done, on_error)

    def _poll(
        self,
        work_queue: queue.Queue,
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None] | None,
    ) -> None:
        try:
            while True:
                kind, payload = work_queue.get_nowait()
                if kind == "progress":
                    self.status_var.set(str(payload))
                elif kind == "done":
                    self._set_busy(False)
                    try:
                        on_done(payload)
                    except BaseException as exc:  # noqa: BLE001
                        self._show_error(exc)
                    return
                else:
                    self._set_busy(False)
                    if on_error is not None:
                        on_error(payload)
                    else:
                        self._show_error(payload)
                    return
        except queue.Empty:
            pass
        self.root.after(100, lambda: self._poll(work_queue, on_done, on_error))

    def _show_error(self, exc: BaseException) -> None:
        self.status_var.set(f"Error: {exc}")
        messagebox.showerror("mediasync", str(exc))

    def _new_plan(self) -> None:
        if self.busy:
            return
        library = self._library()
        pending = pending_plans(self.config, library.name)
        force = False
        if pending:
            if not messagebox.askyesno(
                "Pending plans",
                f"{len(pending)} pending plan(s) exist for {library.name}.\n\n"
                "Create a new plan and supersede them?",
            ):
                return
            force = True
        deep = self.config.scan_mode == "deep"

        def work(progress: Progress) -> Plan:
            with _lock(self.config):
                with State(self.config.state_dir) as state:
                    if force:
                        supersede_pending(self.config, library.name)
                    return plan_library(
                        self.config,
                        state,
                        library,
                        deep=deep,
                        progress=progress,
                    )

        def done(plan: Plan) -> None:
            self.plan = plan
            self._refresh_plan()
            if plan.errors:
                messagebox.showwarning(
                    "Plan created with errors",
                    f"{len(plan.errors)} error(s). See the details pane and log.",
                )
            elif not plan.changes:
                messagebox.showinfo("Plan complete", "No changes detected.")

        self._run_async(f"Planning {library.name}...", work, done)

    def _load_plan(self, plan: Plan) -> None:
        self.current_library = plan.library
        self.library_var.set(plan.library)
        self.plan = plan
        self._refresh_plan()

    def _open_pending(self) -> None:
        if self.busy:
            return
        plans = pending_plans(self.config, self.current_library)
        if not plans:
            messagebox.showinfo("Pending plans", "No pending plans for this library.")
            return
        if len(plans) == 1:
            self._load_plan(plans[0])
            return
        _PlanPicker(self.root, plans, self._load_plan)

    def _selected_changes(self) -> list[Change]:
        return [
            self.rows[iid]
            for iid in self.tree.selection()
            if iid in self.rows
        ]

    def _decide(self, decision: str) -> None:
        if self.busy or self.plan is None:
            return
        if any(change.applied for change in self.plan.changes):
            messagebox.showinfo(
                "Plan already applied",
                "This plan has already been applied. Create a new plan to make more changes.",
            )
            return
        pending = [
            change
            for change in self.plan.changes
            if change.decision == Decision.PENDING.value
        ]
        selected = [
            change
            for change in self._selected_changes()
            if change.decision == Decision.PENDING.value
        ]
        if not selected:
            if not pending:
                messagebox.showinfo("Nothing pending", "No pending changes remain.")
                return
            verb = "approve" if decision == Decision.ACCEPTED.value else "deny"
            if not messagebox.askyesno(
                "No rows selected",
                f"No pending rows are selected. {verb.capitalize()} all "
                f"{len(pending)} pending change(s)?",
            ):
                return
            selected = pending
        for change in selected:
            change.decision = decision
            change.decision_reason = (
                "approved in GUI"
                if decision == Decision.ACCEPTED.value
                else "denied in GUI"
            )
        if not any(
            change.decision == Decision.PENDING.value
            for change in self.plan.changes
        ):
            self.plan.blocking_reasons = []
        self.plan.recompute_status()
        save_plan(self.config, self.plan)
        self._refresh_plan()
        verb = "Approved" if decision == Decision.ACCEPTED.value else "Denied"
        self.status_var.set(f"{verb} {len(selected)} change(s).")

    def _add_policy(self, kind: str) -> None:
        if self.busy or self.plan is None:
            return
        library = self.config.library(self.plan.library)
        path = library.whitelist if kind == "whitelist" else library.blacklist
        if not path:
            messagebox.showerror(
                "No policy file", f"No {kind} file is configured for {library.name}."
            )
            return
        targets = self._selected_changes()
        if not targets:
            targets = [
                change
                for change in self.plan.changes
                if change.decision == Decision.PENDING.value
            ]
        if not targets:
            messagebox.showinfo("Nothing selected", "Select one or more changes first.")
            return
        patterns = sorted({change.path for change in targets})
        preview = "\n".join(patterns[:12])
        if len(patterns) > 12:
            preview += f"\n... and {len(patterns) - 12} more"
        if not messagebox.askyesno(
            "Update policy",
            f"Add {len(patterns)} pattern(s) to the {kind} for {library.name}?\n\n"
            f"{preview}\n\nThis affects future plans.",
        ):
            return
        added = 0
        for pattern in patterns:
            if add_pattern(path, pattern):
                added += 1
        self.status_var.set(
            f"Added {added} new {kind} pattern(s); {len(patterns) - added} already present."
        )

    def _apply(self) -> None:
        if self.busy or self.plan is None:
            return
        plan = self.plan
        if any(change.applied for change in plan.changes):
            messagebox.showinfo(
                "Plan already applied",
                "This plan has already been applied. Create a new plan.",
            )
            return
        pending = [
            change
            for change in plan.changes
            if change.decision == Decision.PENDING.value
        ]
        if pending:
            messagebox.showwarning(
                "Approval required",
                f"{len(pending)} change(s) still need a decision. "
                "Approve or deny them before applying.",
            )
            return
        accepted = [
            change
            for change in plan.changes
            if change.decision == Decision.ACCEPTED.value
        ]
        if not accepted:
            messagebox.showinfo("Nothing to apply", "No accepted changes in this plan.")
            return
        library = self.config.library(plan.library)
        destructive = sum(1 for change in accepted if change.destructive)
        if not messagebox.askyesno(
            "Apply plan",
            f"Apply {len(accepted)} accepted change(s) to {library.name}?\n\n"
            f"Destructive changes: {destructive}\n"
            "Deleted content is moved to trash when delete_mode is 'trash'.",
        ):
            return
        run_id = f"run-{now_stamp()}-{uuid.uuid4().hex[:6]}"

        def work(progress: Progress):
            with _lock(self.config):
                with State(self.config.state_dir) as state:
                    state.start_run(run_id, library.name, "gui-apply", plan.plan_id)
                    try:
                        result = apply_plan(
                            self.config,
                            state,
                            library,
                            plan,
                            run_id=run_id,
                            allow_pending=False,
                            progress=progress,
                        )
                    except Exception as exc:
                        msg = str(exc)
                        if "stale" in msg.lower():
                            try:
                                state.finish_run(run_id, "stale")
                            except Exception:
                                pass
                            if messagebox.askyesno(
                                "Plan is stale",
                                "The plan is stale (source/target changed since planning).\n\n"
                                "Re-plan and apply now?",
                            ):
                                try:
                                    new_run_id = f"run-{now_stamp()}-{uuid.uuid4().hex[:6]}"
                                    state.start_run(new_run_id, library.name, "gui-apply-replan", None)
                                    fresh = plan_library(
                                        self.config,
                                        state,
                                        library,
                                        deep=False,
                                        progress=progress,
                                    )
                                    if fresh.errors:
                                        raise exc
                                    pending_f = [
                                        c for c in fresh.changes if c.decision == Decision.PENDING.value
                                    ]
                                    if pending_f:
                                        raise exc
                                    accepted_f = [
                                        c for c in fresh.changes if c.decision == Decision.ACCEPTED.value
                                    ]
                                    if not accepted_f:
                                        raise exc
                                    result = apply_plan(
                                        self.config,
                                        state,
                                        library,
                                        fresh,
                                        run_id=new_run_id,
                                        allow_pending=False,
                                        progress=progress,
                                    )
                                    try:
                                        state.finish_run(new_run_id, result.status, {"plan_id": fresh.plan_id})
                                    except Exception:
                                        pass
                                    if result.status == "applied":
                                        try:
                                            supersede_pending(self.config, library.name, fresh.plan_id)
                                        except Exception:
                                            pass
                                    self.plan = fresh
                                    return result
                                except Exception:
                                    pass
                        try:
                            state.finish_run(run_id, "error", {"error": msg})
                        except Exception:
                            pass
                        raise
                    try:
                        state.finish_run(
                            run_id,
                            result.status,
                            {
                                "plan_id": plan.plan_id,
                                "applied": result.applied,
                                "errors": result.errors,
                            },
                        )
                    except Exception:
                        pass
                    if result.status == "applied":
                        try:
                            supersede_pending(self.config, library.name, plan.plan_id)
                        except Exception:
                            pass
                    return result

        def done(result) -> None:
            try:
                if self.plan is not None:
                    from .planner import find_plan

                    try:
                        fresh = find_plan(self.config, self.plan.plan_id)
                        self.plan = fresh
                    except Exception:
                        pass
            except Exception:
                pass
            self._refresh_plan()
            message = (
                f"Apply {result.status}: applied={result.applied}, "
                f"retained={result.retained}, denied={result.denied}, "
                f"quarantined={result.quarantined}, errors={len(result.errors)}"
            )
            self.status_var.set(message)
            if result.errors:
                messagebox.showwarning(
                    "Apply completed with errors",
                    message + "\n\n" + "\n".join(result.errors[:10]),
                )
            elif result.status == "applied":
                messagebox.showinfo("Apply complete", message)
            else:
                messagebox.showwarning("Apply result", message)

        self._run_async(f"Applying {library.name}...", work, done)

    def _show_trash(self) -> None:
        if tk is None or messagebox is None:
            return
        _TrashWindow(self.root, self.config, library=self.current_library)

    def _show_policy_tester(self) -> None:
        if self.busy:
            return
        _PolicyTester(self.root, self.config, self.current_library)

    def _preview_prune(self) -> None:
        if self.busy:
            return
        try:
            with State(self.config.state_dir) as state:
                active = state.active_run_progress(self.current_library)
                if active:
                    messagebox.showerror("Busy", "An apply/prune is already running for this library.")
                    return
        except Exception:
            pass
        library = self._library()
        policy = PolicySet.load(library.whitelist, library.blacklist)
        candidates = find_prune_candidates(self.config, library, policy)
        if not candidates:
            messagebox.showinfo("Prune", "No blacklisted titles found on target to prune.")
            return
        _PrunePreviewDialog(self.root, self.config, library, candidates, self._prune_blacklisted_confirm)

    def _prune_blacklisted(self) -> None:
        self._preview_prune()

    def _prune_blacklisted_confirm(self, candidates, force=False, permanent=False) -> None:
        if self.busy:
            return
        protected = [c for c in candidates if c.protected]
        if protected and not force:
            messagebox.showwarning("Protected", "Some items are protected by whitelist. Use Preview and force with confirmation.")
            return
        if protected and force:
            if not messagebox.askyesno("Override protection", "You are about to override whitelist protection. This is dangerous. Continue?"):
                return
            try:
                resp = simpledialog.askstring("Confirm", "Type PRUNE to confirm:", parent=self.root) if simpledialog else None
            except Exception:
                resp = None
            if not resp or resp.strip() != "PRUNE":
                messagebox.showerror("Cancelled", "Confirmation failed.")
                return
        if not messagebox.askyesno("Confirm prune", f"Move {len([c for c in candidates if not c.protected or force])} items to {'trash (permanent)' if permanent else 'trash'}?"):
            return
        import uuid
        from .util import now_stamp
        run_id = f"prune-{now_stamp()}-{uuid.uuid4().hex[:6]}"

        def work(progress):
            with _lock(self.config):
                with State(self.config.state_dir) as state:
                    return prune_targets(self.config, state, library, candidates, run_id, permanent=permanent, force=force, progress=progress)

        def done(result):
            msg = f"Pruned: moved={result.moved} skipped_protected={result.skipped_protected} errors={len(result.errors)}"
            self.status_var.set(msg)
            if result.report_path:
                msg += f"\nReport: {result.report_path}"
            messagebox.showinfo("Prune complete", msg)

        self._run_async("Pruning blacklisted...", work, done)

    def _browse_library(self) -> None:
        if self.busy:
            return
        if tk is None or messagebox is None:
            return
        _LibraryBrowser(self.root, self.config, self.current_library)

    def _build_menubar(self, names: list[str]) -> None:
        if tk is None or not hasattr(self.root, "menu"):
            pass
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        app_menu = tk.Menu(menubar, tearoff=0, name="apple")
        menubar.add_cascade(label="MediaSync", menu=app_menu)
        app_menu.add_command(label="About MediaSync", command=self._about)
        app_menu.add_separator()
        app_menu.add_command(label="Quit MediaSync", command=self._close)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="New Plan", command=self._new_plan)
        file_menu.add_command(label="Open Pending Plan...", command=self._open_pending)
        file_menu.add_command(label="Apply Plan", command=self._apply)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._close)

        lib_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Library", menu=lib_menu)
        for name in names:
            lib_menu.add_command(label=name, command=lambda n=name: self._switch_library_menu(n))
        lib_menu.add_separator()
        lib_menu.add_command(label="Browse Titles...", command=self._browse_library)

        plan_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Plan", menu=plan_menu)
        plan_menu.add_command(label="Approve Selected", command=lambda: self._decide(Decision.ACCEPTED.value))
        plan_menu.add_command(label="Deny Selected", command=lambda: self._decide(Decision.DENIED.value))
        plan_menu.add_separator()
        plan_menu.add_command(label="Approve All Pending", command=self._approve_all_pending)
        plan_menu.add_command(label="Deny All Pending", command=self._deny_all_pending)

        policy_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Policy", menu=policy_menu)
        policy_menu.add_command(label="Browse Titles...", command=self._browse_library)
        policy_menu.add_command(label="Test Policy...", command=self._show_policy_tester)
        policy_menu.add_separator()
        policy_menu.add_command(label="Add Selected to Whitelist", command=lambda: self._add_policy("whitelist"))
        policy_menu.add_command(label="Add Selected to Blacklist", command=lambda: self._add_policy("blacklist"))

        trash_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Trash", menu=trash_menu)
        trash_menu.add_command(label="Open Trash...", command=self._show_trash)

        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_menu)
        view_menu.add_command(label="Refresh Library Status", command=self._refresh_view)

        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About MediaSync", command=self._about)

    def _switch_library_menu(self, name: str) -> None:
        if self.busy:
            return
        if name == self.current_library:
            return
        self.library_var.set(name)
        self._on_library_change()

    def _build_context_menu(self) -> None:
        if tk is None:
            return
        self._context_menu = tk.Menu(self.root, tearoff=0)
        self._context_menu.add_command(label="Approve Selected", command=lambda: self._decide(Decision.ACCEPTED.value))
        self._context_menu.add_command(label="Deny Selected", command=lambda: self._decide(Decision.DENIED.value))
        self._context_menu.add_separator()
        self._context_menu.add_command(label="Approve All Pending", command=self._approve_all_pending)
        self._context_menu.add_command(label="Deny All Pending", command=self._deny_all_pending)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="Add to Whitelist", command=lambda: self._add_policy("whitelist"))
        self._context_menu.add_command(label="Add to Blacklist", command=lambda: self._add_policy("blacklist"))
        self._context_menu.add_separator()
        self._context_menu.add_command(label="Copy Path", command=self._copy_selected_path)
        self._context_menu.add_command(label="Copy Title", command=self._copy_selected_title)
        self._context_menu.add_command(label="Show Details", command=self._show_details_focus)

        def _on_context(event: Any) -> None:
            try:
                iid = self.tree.identify_row(event.y)
                if iid:
                    if iid not in self.tree.selection():
                        self.tree.selection_set(iid)
                    self.tree.focus(iid)
                self._context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    self._context_menu.grab_release()
                except Exception:
                    pass

        self.tree.bind("<Button-2>", _on_context)
        self.tree.bind("<Button-3>", _on_context)
        try:
            self.tree.bind("<Control-Button-1>", _on_context)
        except Exception:
            pass

    def _close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(
                "Quit", "An operation is still running. Quit anyway?"
            ):
                return
        self.root.destroy()


class _PlanPicker:
    def __init__(
        self, parent: Any, plans: list[Plan], on_select: Callable[[Plan], None]
    ) -> None:
        self.plans = plans
        self.on_select = on_select
        self.top = tk.Toplevel(parent)
        self.top.title("Pending plans")
        self.top.geometry("720x320")
        self.top.transient(parent)
        icon_path = _find_icon()
        if icon_path:
            try:
                self._icon_image = tk.PhotoImage(file=icon_path)
                self.top.iconphoto(False, self._icon_image)
            except Exception:
                pass
        self.listbox = tk.Listbox(
            self.top,
            selectmode="browse",
            background="#ffffff",
            foreground="#000000",
            selectbackground="#4a90e2",
            selectforeground="#ffffff",
            highlightthickness=0,
        )
        for plan in plans:
            pending = sum(
                1
                for change in plan.changes
                if change.decision == Decision.PENDING.value
            )
            self.listbox.insert(
                "end",
                f"{plan.plan_id}  {plan.library}  {plan.status}  "
                f"{len(plan.changes)} changes ({pending} pending)  {plan.created_at}",
            )
        self.listbox.pack(fill="both", expand=True, padx=8, pady=8)
        self.listbox.selection_set(0)
        self.listbox.bind("<Double-Button-1>", lambda _event: self._choose())
        buttons = ttk.Frame(self.top, padding=(8, 0, 8, 8))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Open", command=self._choose).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.top.destroy).pack(
            side="right", padx=6
        )
        self.top.grab_set()

    def _choose(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        plan = self.plans[selection[0]]
        self.top.destroy()
        self.on_select(plan)


class _PolicyTester:
    def __init__(self, parent: Any, config: Config, library_name: str) -> None:
        self.config = config
        self.library_name = library_name
        self.top = tk.Toplevel(parent)
        self.top.title("Policy tester")
        self.top.geometry("760x420")
        icon_path = _find_icon()
        if icon_path:
            try:
                self._icon_image = tk.PhotoImage(file=icon_path)
                self.top.iconphoto(False, self._icon_image)
            except Exception:
                pass
        frame = ttk.Frame(self.top, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Library-relative path").pack(anchor="w")
        self.path_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.path_var)
        entry.pack(fill="x", pady=(4, 8))
        entry.focus_set()
        entry.bind("<Return>", lambda _event: self._test())
        ttk.Button(frame, text="Test", command=self._test).pack(anchor="w")
        self.result = tk.Text(frame, height=14, wrap="word", state="disabled")
        self.result.pack(fill="both", expand=True, pady=(10, 0))
        self._show_rules()

    def _show_rules(self) -> None:
        library = self.config.library(self.library_name)
        policy = PolicySet.load(library.whitelist, library.blacklist)
        lines = [f"Whitelist: {library.whitelist or '(none)'}"]
        for rule in policy.whitelist:
            lines.append(f"  {rule.raw}  (specificity={rule.specificity})")
        lines.append(f"Blacklist: {library.blacklist or '(none)'}")
        for rule in policy.blacklist:
            lines.append(f"  {rule.raw}  (specificity={rule.specificity})")
        self._set_text("\n".join(lines))

    def _test(self) -> None:
        path = self.path_var.get().strip().strip("/")
        if not path:
            return
        library = self.config.library(self.library_name)
        policy = PolicySet.load(library.whitelist, library.blacklist)
        match = policy.evaluate(path)
        lines = [f"Path: {path}", f"Decision: {match.kind}"]
        if match.pattern:
            lines.append(f"Matched rule: {match.pattern}")
            lines.append(f"Specificity: {match.specificity}")
        self._set_text("\n".join(lines))

    def _set_text(self, text: str) -> None:
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)
        self.result.configure(state="disabled")


class _PrunePreviewDialog:
    def __init__(self, parent: Any, config: Config, library, candidates, on_confirm):
        self.candidates = candidates
        self.on_confirm = on_confirm
        self.library = library
        self.top = tk.Toplevel(parent)
        self.top.title("Prune Blacklisted Content - Preview")
        self.top.geometry("820x420")
        self.top.transient(parent)
        frame = ttk.Frame(self.top, padding=10)
        frame.pack(fill="both", expand=True)
        movable = len([c for c in candidates if not c.protected])
        protected = len([c for c in candidates if c.protected])
        total_size = sum(c.size for c in candidates)
        ttk.Label(frame, text=f"{len(candidates)} titles/dirs, {movable} movable, {protected} protected by whitelist, {human_size(total_size)} total").pack(anchor="w")
        columns = ("path", "type", "size", "status", "reason")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings")
        for col, txt in zip(columns, ["Path", "Type", "Size", "Status", "Reason"]):
            self.tree.heading(col, text=txt)
        self.tree.column("path", width=320)
        self.tree.column("type", width=60)
        self.tree.column("size", width=80)
        self.tree.column("status", width=140)
        self.tree.column("reason", width=200)
        for c in candidates:
            self.tree.insert("", "end", values=(c.rel_path, "dir" if c.is_dir else "file", human_size(c.size), c.status, c.reason))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(fill="both", expand=True)
        scroll.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        btns = ttk.Frame(self.top, padding=(10,0,10,10))
        btns.pack(fill="x")
        self.force_var = tk.BooleanVar(value=False)
        self.perm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Force override whitelist protection", variable=self.force_var).pack(side="left")
        ttk.Checkbutton(btns, text="Delete permanently", variable=self.perm_var).pack(side="left", padx=8)
        ttk.Button(btns, text="Cancel", command=self.top.destroy).pack(side="right")
        ttk.Button(btns, text="Prune...", command=self._confirm).pack(side="right", padx=6)
        self.top.grab_set()

    def _confirm(self):
        self.top.destroy()
        self.on_confirm(self.candidates, force=self.force_var.get(), permanent=self.perm_var.get())


class _TrashWindow:
    def __init__(self, parent: Any, config: Config, library: str | None = None) -> None:
        self.config = config
        self.default_library = library
        self.entries: dict[str, dict[str, Any]] = {}
        self.top = tk.Toplevel(parent)
        self.top.title("Trash")
        self.top.geometry("980x480")
        icon_path = _find_icon()
        if icon_path:
            try:
                self._icon_image = tk.PhotoImage(file=icon_path)
                self.top.iconphoto(False, self._icon_image)
            except Exception:
                pass
        frame = ttk.Frame(self.top, padding=10)
        frame.pack(fill="both", expand=True)
        filter_frame = ttk.Frame(frame)
        filter_frame.pack(fill="x", pady=(0, 6))
        ttk.Label(filter_frame, text="Library:").pack(side="left")
        lib_names = ["All libraries"] + [lib.name for lib in config.enabled_libraries()]
        self.library_var = tk.StringVar(value=self.default_library or "All libraries")
        if self.library_var.get() not in lib_names:
            self.library_var.set("All libraries")
        self.library_combo = ttk.Combobox(filter_frame, textvariable=self.library_var, values=lib_names, state="readonly", width=28)
        self.library_combo.pack(side="left", padx=6)
        self.library_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh())
        columns = ("id", "library", "size", "type", "deleted", "path")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "id": "ID",
            "library": "Library",
            "size": "Size",
            "type": "Type",
            "deleted": "Deleted",
            "path": "Original path",
        }
        widths = {
            "id": 60,
            "library": 100,
            "size": 100,
            "type": 60,
            "deleted": 170,
            "path": 500,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], stretch=column == "path")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="top", fill="both", expand=True)
        scroll.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        buttons = ttk.Frame(self.top, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Refresh", command=self._refresh).pack(side="left")
        ttk.Button(buttons, text="Restore Selected", command=self._restore).pack(side="left", padx=6)
        ttk.Button(buttons, text="Purge Selected", command=self._purge).pack(side="left")
        ttk.Button(buttons, text="Purge All", command=self._purge_all).pack(side="left", padx=6)
        ttk.Button(buttons, text="Close", command=self.top.destroy).pack(side="right")
        self._refresh()

    def _refresh(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.entries.clear()
        lib_filter = self.library_var.get()
        if lib_filter == "All libraries":
            lib_filter = None
        with State(self.config.state_dir) as state:
            entries = list_trash(state, lib_filter)
        for index, entry in enumerate(entries):
            iid = f"trash-{index}"
            self.entries[iid] = entry
            deleted = entry.get("deleted_at") or ""
            if len(deleted) >= 19:
                deleted = deleted[:19]
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    entry["id"],
                    entry["library"],
                    human_size(entry["size"]),
                    entry["entry_type"],
                    deleted,
                    entry["original_path"],
                ),
            )

    def _selected(self) -> dict[str, Any] | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.entries.get(selection[0])

    def _selected_entries(self) -> list[dict[str, Any]]:
        entries = []
        for iid in self.tree.selection():
            entry = self.entries.get(iid)
            if entry is not None:
                entries.append(entry)
        return entries

    def _library(self, entry: dict[str, Any]):
        try:
            return self.config.library(entry["library"])
        except KeyError:
            messagebox.showerror(
                "Trash", f"Library {entry['library']} is not configured."
            )
            return None

    def _restore(self) -> None:
        entries = self._selected_entries()
        if not entries:
            messagebox.showinfo("Trash", "Select one or more entries first.")
            return
        if len(entries) == 1:
            entry = entries[0]
            library = self._library(entry)
            if library is None:
                return
            if not messagebox.askyesno(
                "Restore", f"Restore {entry['original_path']}?"
            ):
                return
            overwrite = False
            while True:
                try:
                    with State(self.config.state_dir) as state:
                        path = restore(
                            self.config,
                            library,
                            state,
                            int(entry["id"]),
                            overwrite=overwrite,
                        )
                except TrashError as exc:
                    if "already exists" in str(exc) and not overwrite:
                        if messagebox.askyesno(
                            "Restore",
                            f"{entry['original_path']} already exists. "
                            "Quarantine the existing path and restore this entry?",
                        ):
                            overwrite = True
                            continue
                    messagebox.showerror("Restore failed", str(exc))
                    return
                except Exception as exc:  # noqa: BLE001
                    messagebox.showerror("Restore failed", str(exc))
                    return
                messagebox.showinfo("Restored", f"Restored to {path}")
                self._refresh()
                return

        if not messagebox.askyesno(
            "Restore", f"Restore {len(entries)} selected entr{'y' if len(entries) == 1 else 'ies'}?"
        ):
            return
        restored = 0
        errors = []
        for entry in entries:
            library = self._library(entry)
            if library is None:
                errors.append(f"{entry['original_path']}: library not configured")
                continue
            overwrite = False
            ok = False
            while not ok:
                try:
                    with State(self.config.state_dir) as state:
                        restore(
                            self.config,
                            library,
                            state,
                            int(entry["id"]),
                            overwrite=overwrite,
                        )
                    restored += 1
                    ok = True
                except TrashError as exc:
                    if "already exists" in str(exc) and not overwrite:
                        if not messagebox.askyesno(
                            "Restore",
                            f"{entry['original_path']} already exists. "
                            "Quarantine the existing path and restore this entry?",
                        ):
                            errors.append(f"{entry['original_path']}: skipped")
                            break
                        overwrite = True
                        continue
                    errors.append(f"{entry['original_path']}: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{entry['original_path']}: {exc}")
                    break
        if errors:
            messagebox.showwarning(
                "Restored with errors",
                f"Restored {restored}/{len(entries)}.\n\n" + "\n".join(errors[:10]),
            )
        else:
            messagebox.showinfo("Restored", f"Restored {restored} entr{'y' if restored == 1 else 'ies'}.")
        self._refresh()

    def _purge(self) -> None:
        entries = self._selected_entries()
        if not entries:
            messagebox.showinfo("Trash", "Select one or more entries first, or use Purge All.")
            return
        if len(entries) == 1:
            entry = entries[0]
            library = self._library(entry)
            if library is None:
                return
            if not messagebox.askyesno(
                "Purge permanently",
                f"Permanently delete {entry['original_path']}?\n\n"
                "This cannot be undone.",
            ):
                return
            try:
                with State(self.config.state_dir) as state:
                    result = purge(
                        self.config,
                        library,
                        state,
                        trash_id=int(entry["id"]),
                        all_entries=False,
                    )
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Purge failed", str(exc))
                return
            messagebox.showinfo(
                "Purged",
                f"Removed {result['removed']} entr"
                f"{'y' if result['removed'] == 1 else 'ies'}, "
                f"{human_size(result['bytes'])}.",
            )
            self._refresh()
            return

        total_bytes = sum(int(e.get("size") or 0) for e in entries)
        if not messagebox.askyesno(
            "Purge permanently",
            f"Permanently delete {len(entries)} selected entr{'y' if len(entries) == 1 else 'ies'} "
            f"({human_size(total_bytes)})?\n\nThis cannot be undone.",
        ):
            return
        removed = 0
        bytes_removed = 0
        errors = []
        for entry in entries:
            library = self._library(entry)
            if library is None:
                errors.append(f"{entry['original_path']}: library not configured")
                continue
            try:
                with State(self.config.state_dir) as state:
                    result = purge(
                        self.config,
                        library,
                        state,
                        trash_id=int(entry["id"]),
                        all_entries=False,
                    )
                removed += int(result.get("removed") or 0)
                bytes_removed += int(result.get("bytes") or 0)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{entry['original_path']}: {exc}")
        if errors:
            messagebox.showwarning(
                "Purged with errors",
                f"Removed {removed} entr{'y' if removed == 1 else 'ies'} ({human_size(bytes_removed)}).\n\n"
                + "\n".join(errors[:10]),
            )
        else:
            messagebox.showinfo(
                "Purged",
                f"Removed {removed} entr{'y' if removed == 1 else 'ies'}, {human_size(bytes_removed)}.",
            )
        self._refresh()

    def _purge_all(self) -> None:
        lib_filter = self.library_var.get()
        if lib_filter == "All libraries":
            lib_filter = None
        with State(self.config.state_dir) as state:
            entries = list_trash(state, lib_filter)
        if not entries:
            messagebox.showinfo("Trash", "Trash is empty.")
            return
        total_bytes = sum(int(e.get("size") or 0) for e in entries)
        scope = f"all entr{'y' if len(entries) == 1 else 'ies'} in {lib_filter}" if lib_filter else f"ALL {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
        if not messagebox.askyesno(
            "Purge all permanently",
            f"Permanently delete {scope} ({human_size(total_bytes)})?\n\nThis cannot be undone.",
        ):
            return
        removed = 0
        bytes_removed = 0
        errors = []
        for entry in entries:
            library = self._library(entry)
            if library is None:
                errors.append(f"{entry['original_path']}: library not configured")
                continue
            try:
                with State(self.config.state_dir) as state:
                    result = purge(
                        self.config,
                        library,
                        state,
                        trash_id=int(entry["id"]),
                        all_entries=False,
                    )
                removed += int(result.get("removed") or 0)
                bytes_removed += int(result.get("bytes") or 0)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{entry['original_path']}: {exc}")
        if errors:
            messagebox.showwarning(
                "Purged with errors",
                f"Removed {removed}/{len(entries)} entr{'y' if removed == 1 else 'ies'} ({human_size(bytes_removed)}).\n\n"
                + "\n".join(errors[:10]),
            )
        else:
            messagebox.showinfo(
                "Purged all",
                f"Removed {removed} entr{'y' if removed == 1 else 'ies'}, {human_size(bytes_removed)}.",
            )
        self._refresh()


def launch_gui(config: Config, library: str | None = None) -> int:
    if tk is None:
        raise ConfigError(
            "tkinter is not available in this Python build; install it or use the CLI"
        )
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise ConfigError(f"unable to start the GUI: {exc}") from exc
    try:
        root.tk.call("wm", "iconname", root._w, "MediaSync")
    except Exception:
        pass
    MediasyncGui(root, config, library)
    root.mainloop()
    return 0


class _LibraryBrowser:
    def __init__(self, parent: Any, config: Config, library_name: str) -> None:
        self.config = config
        self.library_name = library_name
        try:
            self.library = config.library(library_name)
        except Exception:
            self.library = None
        self.policy = None
        if self.library is not None:
            try:
                self.policy = PolicySet.load(self.library.whitelist, self.library.blacklist)
            except Exception:
                self.policy = PolicySet([], [])
        self.entries: list[dict[str, Any]] = []
        self.top = tk.Toplevel(parent)
        self.top.title(f"Browse Library Titles - {library_name}")
        self.top.geometry("920x560")
        self.top.transient(parent)
        icon_path = _find_icon()
        if icon_path:
            try:
                self._icon_image = tk.PhotoImage(file=icon_path)
                self.top.iconphoto(False, self._icon_image)
            except Exception:
                pass

        top = ttk.Frame(self.top, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text=f"Library: {library_name}").pack(side="left")
        ttk.Button(top, text="Refresh", command=self._refresh).pack(side="right")
        ttk.Label(top, text="Union of source + target (top-level only)").pack(side="left", padx=12)

        frame = ttk.Frame(self.top, padding=(10, 0, 10, 10))
        frame.pack(fill="both", expand=True)
        columns = ("name", "origin", "type", "policy")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        headings = {"name": "Name", "origin": "Source/Target", "type": "Type", "policy": "Policy"}
        widths = {"name": 420, "origin": 140, "type": 80, "policy": 200}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], stretch=col == "name")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="top", fill="both", expand=True)
        scroll.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")

        info = ttk.Frame(self.top, padding=(10, 0, 10, 6))
        info.pack(fill="x")
        self.info_var = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.info_var).pack(side="left")

        buttons = ttk.Frame(self.top, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Add to Whitelist", command=lambda: self._add("whitelist")).pack(side="left")
        ttk.Button(buttons, text="Add to Blacklist", command=lambda: self._add("blacklist")).pack(side="left", padx=6)
        ttk.Button(buttons, text="Test in Policy Tester", command=self._test_policy).pack(side="left", padx=6)
        ttk.Button(buttons, text="Close", command=self.top.destroy).pack(side="right")

        self._refresh()

    def _reload_policy(self) -> None:
        if self.library is None:
            self.policy = PolicySet([], [])
            return
        try:
            self.policy = PolicySet.load(self.library.whitelist, self.library.blacklist)
        except Exception:
            self.policy = PolicySet([], [])

    def _policy_status(self, name: str) -> str:
        if self.policy is None:
            return "—"
        match = self.policy.evaluate(name)
        if match.kind == "blacklist" and match.pattern:
            return f"Blacklisted ({match.pattern})"
        if match.kind == "whitelist" and match.pattern:
            return f"Whitelisted ({match.pattern})"
        return "—"

    def _refresh(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.entries = []
        if self.library is None:
            self.info_var.set("Library not found.")
            return
        self._reload_policy()
        source = self.library.source
        target = self.library.target
        names = set()
        items = []
        # scan source
        try:
            if os.path.isdir(source):
                for entry in os.listdir(source):
                    if is_junk_name(entry):
                        continue
                    full = os.path.join(source, entry)
                    names.add(entry)
                    items.append({
                        "name": entry,
                        "in_source": True,
                        "in_target": False,
                        "is_dir": os.path.isdir(full),
                    })
        except Exception:
            pass
        # scan target
        try:
            if os.path.isdir(target):
                for entry in os.listdir(target):
                    if is_junk_name(entry):
                        continue
                    full = os.path.join(target, entry)
                    if entry in names:
                        for it in items:
                            if it["name"] == entry:
                                it["in_target"] = True
                                if not it.get("is_dir"):
                                    it["is_dir"] = os.path.isdir(full)
                                break
                    else:
                        names.add(entry)
                        items.append({
                            "name": entry,
                            "in_source": False,
                            "in_target": True,
                            "is_dir": os.path.isdir(full),
                        })
        except Exception:
            pass
        items.sort(key=lambda x: x["name"].lower())
        self.entries = items
        for idx, it in enumerate(items):
            origin = []
            if it["in_source"]:
                origin.append("Source")
            if it["in_target"]:
                origin.append("Target")
            origin_str = "/".join(origin) if origin else "—"
            typ = "dir" if it.get("is_dir") else "file"
            status = self._policy_status(it["name"])
            self.tree.insert("", "end", iid=f"lib-{idx}", values=(it["name"], origin_str, typ, status))
        src_ok = os.path.isdir(source)
        tgt_ok = os.path.isdir(target)
        self.info_var.set(f"Source: {'OK' if src_ok else 'missing/unreadable'}  |  Target: {'OK' if tgt_ok else 'missing/unreadable'}  |  {len(items)} top-level items")

    def _selected_names(self) -> list[str]:
        names = []
        for iid in self.tree.selection():
            vals = self.tree.item(iid, "values")
            if vals:
                names.append(vals[0])
        return names

    def _add(self, kind: str) -> None:
        if self.library is None:
            return
        path = self.library.whitelist if kind == "whitelist" else self.library.blacklist
        if not path:
            messagebox.showerror("No policy file", f"No {kind} file is configured for {self.library.name}.")
            return
        selected = self._selected_names()
        if not selected:
            messagebox.showinfo("Nothing selected", "Select one or more titles first.")
            return
        patterns = sorted(set(selected))
        preview = "\n".join(patterns[:12])
        if len(patterns) > 12:
            preview += f"\n... and {len(patterns) - 12} more"
        if not messagebox.askyesno(
            "Update policy",
            f"Add {len(patterns)} pattern(s) to the {kind} for {self.library.name}?\n\n"
            f"{preview}\n\nWriting to: {path}\n\nThis affects future plans.",
        ):
            return
        added = 0
        for pattern in patterns:
            try:
                if add_pattern(path, pattern):
                    added += 1
            except Exception as exc:
                messagebox.showerror("Policy update failed", str(exc))
                return
        messagebox.showinfo(
            "Policy updated",
            f"Added {added} new {kind} pattern(s); {len(patterns) - added} already present.\n\n{path}",
        )
        self._refresh()

    def _test_policy(self) -> None:
        selected = self._selected_names()
        if not selected:
            messagebox.showinfo("Test Policy", "Select one or more titles first.")
            return
        _PolicyTester(self.top, self.config, self.library_name)
