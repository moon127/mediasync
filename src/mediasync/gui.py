from __future__ import annotations

import json
import os
import queue
import threading
import uuid
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
from .policy import PolicySet, add_pattern
from .report import ACTION_LABEL, ACTION_ORDER, DECISION_LABEL, plan_stats
from .state import State
from .trash import TrashError, list_trash, purge, restore
from .util import FileLock, human_size, now_stamp

Progress = Callable[[str], None]


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

        names = [lib.name for lib in config.enabled_libraries()]
        if not names:
            raise ConfigError("no enabled libraries are configured")
        self.current_library = library or names[0]
        if self.current_library not in names:
            raise ConfigError(f"unknown or disabled library: {self.current_library}")

        self.root.title("mediasync")
        self.root.geometry("1280x780")
        self.root.minsize(960, 620)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._build(names)
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
        for widget in (
            self.plan_button,
            self.pending_button,
            self.apply_button,
            self.trash_button,
            self.policy_button,
        ):
            widget.pack(side="left", padx=3)
        self._busy_widgets.extend(
            [self.plan_button, self.pending_button, self.apply_button]
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
        self.details = tk.Text(details_frame, height=6, wrap="word", state="disabled")
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
                    except BaseException as exc:
                        state.finish_run(run_id, "error", {"error": str(exc)})
                        raise
                    state.finish_run(
                        run_id,
                        result.status,
                        {
                            "plan_id": plan.plan_id,
                            "applied": result.applied,
                            "errors": result.errors,
                        },
                    )
                    if result.status == "applied":
                        supersede_pending(self.config, library.name, plan.plan_id)
                    return result

        def done(result) -> None:
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
        if self.busy:
            return
        _TrashWindow(self.root, self.config)

    def _show_policy_tester(self) -> None:
        if self.busy:
            return
        _PolicyTester(self.root, self.config, self.current_library)

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
        self.listbox = tk.Listbox(self.top, selectmode="browse")
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


class _TrashWindow:
    def __init__(self, parent: Any, config: Config) -> None:
        self.config = config
        self.entries: dict[str, dict[str, Any]] = {}
        self.top = tk.Toplevel(parent)
        self.top.title("Trash")
        self.top.geometry("980x480")
        frame = ttk.Frame(self.top, padding=10)
        frame.pack(fill="both", expand=True)
        columns = ("id", "library", "size", "type", "deleted", "path")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings")
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
        ttk.Button(buttons, text="Restore", command=self._restore).pack(side="left", padx=6)
        ttk.Button(buttons, text="Purge", command=self._purge).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.top.destroy).pack(side="right")
        self._refresh()

    def _refresh(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.entries.clear()
        with State(self.config.state_dir) as state:
            entries = list_trash(state)
        for index, entry in enumerate(entries):
            iid = f"trash-{index}"
            self.entries[iid] = entry
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    entry["id"],
                    entry["library"],
                    human_size(entry["size"]),
                    entry["entry_type"],
                    entry["deleted_at"][:19],
                    entry["original_path"],
                ),
            )

    def _selected(self) -> dict[str, Any] | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Trash", "Select an entry first.")
            return None
        return self.entries.get(selection[0])

    def _library(self, entry: dict[str, Any]):
        try:
            return self.config.library(entry["library"])
        except KeyError:
            messagebox.showerror(
                "Trash", f"Library {entry['library']} is not configured."
            )
            return None

    def _restore(self) -> None:
        entry = self._selected()
        if entry is None:
            return
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

    def _purge(self) -> None:
        entry = self._selected()
        if entry is None:
            return
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


def launch_gui(config: Config, library: str | None = None) -> int:
    if tk is None:
        raise ConfigError(
            "tkinter is not available in this Python build; install it or use the CLI"
        )
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise ConfigError(f"unable to start the GUI: {exc}") from exc
    MediasyncGui(root, config, library)
    root.mainloop()
    return 0
