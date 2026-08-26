"""Interactive triage session for orphan packages.

Turns a list of :class:`OrphanInfo` into two decision sets — packages to
``keep`` (mark as explicit so future ``autoremove --orphans`` runs won't
propose them) and packages to ``remove`` (erase in a single transaction).
The session itself does not touch the rpmdb : it only collects the
operator's choices and returns them as a :class:`TriageResult`.  The
caller (``cmd_autoremove``) is responsible for the actual apply.

Design constraints :

* **Deterministic and testable.**  The session reads from a ``stdin``
  stream and writes to a ``stdout`` stream, both injectable, so the
  whole flow is exerciseable from unit tests with scripted keystrokes.
* **No cursor tricks.**  Plain ``print`` calls, no ANSI cursor moves.
  Terminals without a cursor (piped output, dumb terminals) still
  render coherent transcripts.
* **Never applies mid-session.**  Every keep/remove is recorded ;
  nothing hits the rpmdb until the user confirms at the summary
  screen or the caller triggers apply.  ``quit`` prompts about
  pending decisions.
* **Number-based shortcuts across every menu.**  Letters would either
  break under translation (the ``[k] keep`` mnemonic has no French
  equivalent that also starts with ``k``) or become locale-specific
  (which then breaks cross-locale peer support on forums / chats).
  Numbers give a stable protocol every human can dictate, regardless
  of UI language.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, TextIO

from ...i18n import _, ngettext
from .. import colors
from ...core.resolution.orphan_classify import (
    CATEGORY_PREVIOUS_RELEASE,
    CATEGORY_SUBLIB,
    CATEGORY_USERLAND,
    OrphanInfo,
    classify_orphans,
)
from ...core.resolution.orphan_filters import FilterSpec, parse_filters


# --- Decision model -------------------------------------------------------


DECISION_KEEP = "keep"
DECISION_REMOVE = "remove"


@dataclass
class TriageResult:
    """Output of a completed triage session.

    ``to_remove`` and ``to_keep`` are lists of package names ; the
    caller feeds them into the existing erase / mark_as_explicit
    pipelines.  Order is preserved as decisions were made — that is
    the order the operator will see reflected in any downstream
    logging / history.
    """

    to_remove: List[str] = field(default_factory=list)
    to_keep: List[str] = field(default_factory=list)
    quit_reason: str = "normal"  # "normal" | "cancelled" | "eof"


# --- Formatting helpers ---------------------------------------------------


_SIZE_UNITS = [("TB", 1024 ** 4), ("GB", 1024 ** 3),
               ("MB", 1024 ** 2), ("KB", 1024), ("B", 1)]


def _fmt_size(n: int) -> str:
    for unit, thr in _SIZE_UNITS:
        if n >= thr:
            return f"{n / thr:.1f} {unit}"
    return "0 B"


def _fmt_age(ts: int, now_ts: Optional[float] = None) -> str:
    if not ts:
        return "?"
    if now_ts is None:
        import time as _t
        now_ts = _t.time()
    delta = int(now_ts - ts)
    if delta < 0:
        return _("future date")
    days = delta // 86400
    if days < 1:
        return _("today")
    if days < 30:
        return ngettext("{n} day", "{n} days", days).format(n=days)
    if days < 365:
        months = days // 30
        return ngettext("{n} month", "{n} months", months).format(n=months)
    years = days // 365
    remaining_months = (days % 365) // 30
    if remaining_months:
        return _("{y}y, {m}m").format(y=years, m=remaining_months)
    return ngettext("{n} year", "{n} years", years).format(n=years)


def _num(n) -> str:
    """Colourise a bracketed shortcut number, e.g. ``[1]``."""
    return colors.cyan(f"[{n}]")


# --- Session --------------------------------------------------------------


@dataclass
class TriageSession:
    """Interactive triage over a list of orphans.

    ``run()`` is the single entry point.  All I/O goes through the
    injected streams (default ``sys.stdin``/``sys.stdout``).  ``now_ts``
    freezes the reference clock for age formatting — tests pass a
    fixed value.
    """

    orphans: List[OrphanInfo]
    current_major: Optional[int] = None
    initial_filters: Optional[List[str]] = None
    stdin: TextIO = None
    stdout: TextIO = None
    now_ts: Optional[float] = None

    def __post_init__(self):
        if self.stdin is None:
            self.stdin = sys.stdin
        if self.stdout is None:
            self.stdout = sys.stdout
        self._decisions: Dict[str, str] = {}
        self._filter_spec: FilterSpec = parse_filters(
            self.initial_filters or [])
        # Precompute categories for the welcome screen ; recomputed
        # only if the operator resets/adjusts filters mid-session.
        self._buckets = classify_orphans(
            self.orphans, current_major=self.current_major)

    # ---- Entry point ----

    def run(self) -> TriageResult:
        """Drive the session ; return the collected decisions."""
        try:
            self._welcome_loop()
        except EOFError:
            return self._finalize(quit_reason="eof")
        return self._finalize(quit_reason="normal")

    # ---- Welcome / dispatcher ----
    #
    # Welcome shortcuts :
    #   1  → previous-release bucket
    #   2  → sublib bucket
    #   3  → userland bucket
    #   4  → browse all with no filter
    #   5  → open filter menu
    #   6  → go to summary + apply
    #   7  → quit

    def _welcome_loop(self):
        while True:
            self._print_welcome()
            choice = self._prompt("> ").strip()
            if not choice:
                continue
            if choice == "7":
                if not self._confirm_quit():
                    continue
                return
            if choice == "4":
                self._package_loop(self._visible_all())
            elif choice == "1":
                self._package_loop(self._buckets[CATEGORY_PREVIOUS_RELEASE])
            elif choice == "2":
                self._package_loop(self._buckets[CATEGORY_SUBLIB])
            elif choice == "3":
                self._package_loop(self._buckets[CATEGORY_USERLAND])
            elif choice == "5":
                self._filter_menu()
            elif choice == "6":
                if self._summary_and_apply():
                    return
            else:
                self._out(colors.warning(
                    _("Unknown choice: {c!r}").format(c=choice)))

    def _print_welcome(self):
        total = len(self.orphans)
        pending = sum(1 for _ in self._decisions.values())
        self._out("")
        self._out(colors.bold("[urpm autoremove --interactive]"))
        self._out("")
        header = ngettext(
            "{n} orphan package detected",
            "{n} orphan packages detected",
            total).format(n=colors.count(total))
        if pending:
            header += " " + colors.dim(
                _("({n} pending decisions)").format(n=pending))
        self._out(header + ".")
        self._out("")
        self._out(colors.info(_("Suggested categories:")))
        cats = (
            ("1", _("Previous-release relics"),
             CATEGORY_PREVIOUS_RELEASE),
            ("2", _("SONAME sublibs, no revdep"), CATEGORY_SUBLIB),
            ("3", _("Rest (user-facing)"), CATEGORY_USERLAND),
        )
        for num, label, cat_key in cats:
            pkgs = self._buckets[cat_key]
            size = sum(p.size for p in pkgs)
            self._out(f"  {_num(num)}  {label:<30}  "
                      f"{colors.count(len(pkgs)):>4} " + _("pkgs") + "  "
                      f"{colors.dim(_fmt_size(size))}")
        self._out("")
        if not self._filter_spec.is_empty():
            self._out(colors.info(_("Active filters: {f}").format(
                f=", ".join(self._filter_spec.raw))))
        # Meta actions
        self._out(f"  {_num(4)} " + _("browse all with no filter")
                  + f"    {_num(5)} " + _("free-form filter"))
        self._out(f"  {_num(6)} " + _("summary and apply")
                  + f"       {_num(7)} " + _("quit"))

    # ---- Package loop ----
    #
    # Per-package shortcuts :
    #   1  → keep
    #   2  → remove
    #   3  → skip
    #   4  → next
    #   5  → previous
    #   6  → batch (apply last action to all visible)
    #   7  → filter menu
    #   8  → quit
    #   9  → details

    def _package_loop(self, source: List[OrphanInfo]):
        visible = self._filter_spec.apply(source)
        if not visible:
            self._out(colors.warning(_("Nothing to triage in this selection.")))
            return
        idx = 0
        last_action: Optional[str] = None
        while 0 <= idx < len(visible):
            pkg = visible[idx]
            self._print_package(pkg, idx, len(visible))
            choice = self._prompt("> ").strip()
            if not choice:
                continue
            if choice == "8":
                return
            if choice == "9":
                self._print_details(pkg)
                self._prompt(colors.dim(
                    _("(press enter to continue) ")))
                continue
            if choice == "1":
                self._decisions[pkg.name] = DECISION_KEEP
                last_action = "keep"
                idx += 1
            elif choice == "2":
                self._decisions[pkg.name] = DECISION_REMOVE
                last_action = "remove"
                idx += 1
            elif choice == "3":
                idx += 1
            elif choice == "4":
                idx += 1
            elif choice == "5":
                idx = max(0, idx - 1)
            elif choice == "7":
                self._filter_menu()
                visible = self._filter_spec.apply(source)
                idx = min(idx, max(0, len(visible) - 1))
                if not visible:
                    self._out(colors.warning(
                        _("Filter leaves nothing to triage.")))
                    return
            elif choice == "6":
                if last_action not in ("keep", "remove"):
                    self._out(colors.warning(_(
                        "No previous action to apply in batch. "
                        "Press [1] keep or [2] remove on a package first.")))
                    continue
                if self._confirm_batch(last_action, visible):
                    decision = (DECISION_KEEP if last_action == "keep"
                                else DECISION_REMOVE)
                    for p in visible:
                        self._decisions[p.name] = decision
                    return
            else:
                self._out(colors.warning(
                    _("Unknown choice: {c!r}").format(c=choice)))

    def _print_package(self, pkg: OrphanInfo, idx: int, total: int):
        current = self._decisions.get(pkg.name)
        if current == DECISION_KEEP:
            current_str = "  " + colors.success(
                _("[decided: keep]"))
        elif current == DECISION_REMOVE:
            current_str = "  " + colors.error(
                _("[decided: remove]"))
        else:
            current_str = ""

        self._out("")
        header = (colors.dim(f"[{idx + 1}/{total}]") + "  "
                  + colors.bold(pkg.nevra) + "     "
                  + colors.dim(_("Size: {sz}").format(sz=_fmt_size(pkg.size)))
                  + current_str)
        self._out(header)
        label = colors.dim
        self._out(_("  Group    : {v}").format(v=label(pkg.group or "-")))
        self._out(_("  Summary  : {v}").format(v=pkg.summary or "-"))
        self._out(_("  Installed: {v}").format(
            v=label(_fmt_age(pkg.install_time, self.now_ts))))
        provs = [p for p in pkg.provides if p != pkg.name][:6]
        if provs:
            self._out(_("  Provides : {v}").format(
                v=label(", ".join(provs))))
        self._out("")
        # Two-line prompt : decisions first, navigation second.
        self._out(f"  {_num(1)} " + colors.success(_("keep"))
                  + f"   {_num(2)} " + colors.error(_("remove"))
                  + f"   {_num(3)} " + _("skip")
                  + f"   {_num(6)} " + _("batch"))
        self._out(f"  {_num(4)} " + _("next")
                  + f"   {_num(5)} " + _("prev")
                  + f"   {_num(7)} " + _("filter")
                  + f"   {_num(8)} " + _("quit")
                  + f"   {_num(9)} " + _("details"))

    def _print_details(self, pkg: OrphanInfo):
        install_dt = (
            datetime.fromtimestamp(pkg.install_time).strftime("%Y-%m-%d %H:%M")
            if pkg.install_time else "?"
        )
        self._out("")
        self._out(colors.bold(_("Details — {nevra}").format(nevra=pkg.nevra)))
        pairs = [
            (_("  Name           : {v}"), pkg.name),
            (_("  EVR            : {v}"), pkg.evr),
            (_("  Arch           : {v}"), pkg.arch),
            (_("  Installed size : {v}"), _fmt_size(pkg.size)),
            (_("  Group          : {v}"), pkg.group or "-"),
            (_("  Summary        : {v}"), pkg.summary or "-"),
            (_("  Installed on   : {v}"), install_dt),
        ]
        for template, value in pairs:
            self._out(template.format(v=colors.dim(value)))
        self._out(_("  Provides ({n}):").format(n=len(pkg.provides)))
        for p in pkg.provides[:20]:
            self._out(colors.dim(f"    {p}"))
        if len(pkg.provides) > 20:
            self._out(colors.dim(
                _("    … ({n} more)").format(n=len(pkg.provides) - 20)))

    # ---- Filter menu ----
    #
    # Filter shortcuts :
    #   1  → prompt for a new criterion to add
    #   2  → reset all filters
    #   3  → back

    def _filter_menu(self):
        self._out("")
        if self._filter_spec.is_empty():
            self._out(colors.dim(_("No active filter.")))
        else:
            self._out(colors.info(_("Active filters: {f}").format(
                f=", ".join(self._filter_spec.raw))))
        self._out(colors.dim(_(
            "Syntax: disttag=mga9  kind=sublib  size>10M  "
            "installed<30d  name~=^lib64.*")))
        self._out(f"  {_num(1)} " + _("add a criterion"))
        self._out(f"  {_num(2)} " + _("reset filters"))
        self._out(f"  {_num(3)} " + _("back"))
        while True:
            choice = self._prompt(_("filters> ")).strip()
            if not choice:
                continue
            if choice == "3":
                return
            if choice == "2":
                self._filter_spec = parse_filters([])
                self._out(colors.success(_("Filters reset.")))
                return
            if choice == "1":
                expr = self._prompt(
                    colors.dim(_("criterion: "))).strip()
            else:
                # treat any other input as a raw criterion the user
                # typed directly (power-user shortcut : you can just
                # type the expression at the prompt).
                expr = choice
            if not expr:
                continue
            new_raw = self._filter_spec.raw + [expr]
            try:
                self._filter_spec = parse_filters(new_raw)
            except Exception as exc:  # noqa: BLE001
                self._out(colors.error(
                    _("Filter rejected: {err}").format(err=exc)))
                continue
            self._out(colors.success(
                _("Active filters: {f}").format(
                    f=", ".join(self._filter_spec.raw))))

    def _confirm_batch(self, action: str, visible: List[OrphanInfo]) -> bool:
        """Prompt before applying a batch action to every visible entry.

        Shortcuts :
          1  → yes, apply
          2  → list first (then re-prompt)
          3  → no
        """
        self._out("")
        if action == "remove":
            question = ngettext(
                "Apply « remove » to {n} visible package?",
                "Apply « remove » to {n} visible packages?",
                len(visible)).format(n=colors.count(len(visible)))
        else:
            question = ngettext(
                "Apply « keep » to {n} visible package?",
                "Apply « keep » to {n} visible packages?",
                len(visible)).format(n=colors.count(len(visible)))
        self._out(colors.bold(question))
        self._out(f"  {_num(1)} " + colors.success(_("yes"))
                  + f"   {_num(2)} " + _("list first")
                  + f"   {_num(3)} " + colors.error(_("no")))
        answer = self._prompt("> ").strip()
        if answer == "2":
            for p in visible:
                self._out(f"  {p.nevra}")
            self._out(f"  {_num(1)} " + colors.success(_("confirm"))
                      + f"   {_num(3)} " + colors.error(_("cancel")))
            answer = self._prompt("> ").strip()
        return answer == "1"

    # ---- Summary / apply ----
    #
    # Summary shortcuts :
    #   1  → apply
    #   2  → back to triage (keep decisions)
    #   3  → cancel (drop decisions)

    def _summary_and_apply(self) -> bool:
        to_remove = [n for n, d in self._decisions.items()
                     if d == DECISION_REMOVE]
        to_keep = [n for n, d in self._decisions.items()
                   if d == DECISION_KEEP]
        undecided = len(self.orphans) - len(self._decisions)
        freed = sum(
            p.size for p in self.orphans if p.name in to_remove
        )
        self._out("")
        self._out(colors.bold(_("Triage summary:")))
        self._out("   " + colors.error(f"{len(to_remove):>4} ")
                  + _("to remove   ({sz} freed)").format(
                      sz=colors.dim(_fmt_size(freed))))
        self._out("   " + colors.success(f"{len(to_keep):>4} ")
                  + _("to keep     (marked keep)"))
        self._out("   " + colors.dim(f"{undecided:>4} ")
                  + _("untouched   (still orphans)"))
        if not (to_remove or to_keep):
            self._out(colors.warning(_("No decision — nothing applied.")))
            return False
        self._out("")
        self._out(f"  {_num(1)} " + colors.success(_("apply"))
                  + f"   {_num(2)} " + _("back to triage")
                  + f"   {_num(3)} " + colors.error(_("cancel")))
        answer = self._prompt("> ").strip()
        if answer == "1":
            return True
        if answer == "2":
            return False
        # any other reply = cancel
        for n in to_remove + to_keep:
            self._decisions.pop(n, None)
        return False

    def _confirm_quit(self) -> bool:
        """Prompt before dropping pending decisions on quit.

        Shortcuts :
          1  → confirm and quit
          2  → go back
        """
        if not self._decisions:
            return True
        self._out(colors.warning(ngettext(
            "{n} unapplied decision will be lost.",
            "{n} unapplied decisions will be lost.",
            len(self._decisions)).format(n=len(self._decisions))))
        self._out(f"  {_num(1)} " + colors.error(_("confirm and quit"))
                  + f"   {_num(2)} " + _("go back"))
        return self._prompt("> ").strip() == "1"

    # ---- I/O primitives ----

    def _out(self, line: str = ""):
        print(line, file=self.stdout)

    def _prompt(self, msg: str) -> str:
        self.stdout.write(msg)
        self.stdout.flush()
        line = self.stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\n")

    # ---- Result assembly ----

    def _visible_all(self) -> List[OrphanInfo]:
        return list(self.orphans)

    def _finalize(self, *, quit_reason: str) -> TriageResult:
        to_remove: List[str] = []
        to_keep: List[str] = []
        # Iterate in original orphan order for stable output.
        for pkg in self.orphans:
            d = self._decisions.get(pkg.name)
            if d == DECISION_REMOVE:
                to_remove.append(pkg.name)
            elif d == DECISION_KEEP:
                to_keep.append(pkg.name)
        return TriageResult(
            to_remove=to_remove, to_keep=to_keep, quit_reason=quit_reason)
