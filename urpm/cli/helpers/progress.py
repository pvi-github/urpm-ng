"""Transaction progress display for install/upgrade commands.

Provides a factory that returns a callback compatible with
TransactionQueue.progress_callback.  The three-line display shows:

Install phase (extraction):
    Installing 15 packages...                    shared-mime-info
    [████████████████████░░░░░░░░░░░░░░░] 12/15  80%
    [██████████████░░░░░░░░░░░░░░░░░░░░░] extracting

Per-package %post scriptlet (interleaved with extractions):
    Installing 15 packages...                    shared-mime-info
    [████████████████████░░░░░░░░░░░░░░░] 12/15  80%
    [░░░░████░░░░░░░░░░░░░░░░░░░░░░░░░░] running %post

File triggers (after all extractions):
    Running triggers...                 Rebuilding MIME database
    [████████████████████████████████████] 15/15 100%
    [░░░░░░░████░░░░░░░░░░░░░░░░░░░░░░░] 3 triggers
"""

import atexit
import os
import signal
import sys
import threading
import time
from ...i18n import _, ngettext

from ...core.transaction_queue import TransactionProgress, TransactionPhase
from ...core.triggers import describe_trigger

# ANSI
_ORANGE = '\033[33m'
_RESET = '\033[0m'
_CLR = '\033[K'  # clear to end of line

# Bouncing segment width
_BOUNCE_WIDTH = 6
# Animation interval (seconds)
_ANIM_INTERVAL = 0.15

# Widget geometry.  The 3-line widget lives in the bottom 3 rows of the
# terminal ; the top ``rows-3`` rows form a scroll region where normal
# log output flows.  On SIGWINCH the region is redefined so the widget
# stays anchored to the new bottom.
_WIDGET_LINES = 3
_MIN_TERM_ROWS = _WIDGET_LINES + 3  # scroll region needs at least 3 rows


def _detect_term_width() -> int:
    """Detect terminal width, robust to stdout being redirected.

    ``os.get_terminal_size()`` defaults to stdout; when stdout is piped
    (``| tee``, ``| less``, …) the ioctl fails and we lose the real
    terminal width, collapsing bars to the 79-column fallback even
    though the user's terminal is much wider. Probe stderr and
    ``/dev/tty`` before giving up.
    """
    for fd in (1, 2):
        try:
            return os.get_terminal_size(fd).columns - 1
        except OSError:
            continue
    try:
        with open('/dev/tty') as tty:
            return os.get_terminal_size(tty.fileno()).columns - 1
    except (OSError, FileNotFoundError):
        pass
    try:
        cols = int(os.environ.get('COLUMNS', '80'))
        return max(cols - 1, 20)
    except ValueError:
        return 79


def _detect_term_size() -> tuple[int, int] | None:
    """Detect ``(rows, cols)`` from a TTY, or ``None`` when unavailable.

    Same fd cascade as :func:`_detect_term_width` (stdout → stderr →
    ``/dev/tty``).  Returns ``None`` when every probe fails — the
    caller uses that as the signal to skip the scroll-region setup
    (piped output, headless container, cron job).  Unlike the width
    helper this never falls back to environment defaults : we need a
    real geometry to safely reserve the widget rows.
    """
    for fd in (1, 2):
        try:
            sz = os.get_terminal_size(fd)
            return (sz.lines, sz.columns)
        except OSError:
            continue
    try:
        with open('/dev/tty') as tty:
            sz = os.get_terminal_size(tty.fileno())
            return (sz.lines, sz.columns)
    except (OSError, FileNotFoundError):
        return None


def make_progress_callback(
    header_template: str,
    total: int | None = None,
    full_sync: bool = False,
):
    """Create a transaction progress callback.

    Args:
        header_template: ngettext template with ``{count}`` placeholder.
        total: Number of packages (None = deferred to first callback).
        full_sync: If True, use human-readable trigger descriptions.

    Returns:
        A callable ``(TransactionProgress) -> None``.
    """
    term_width = _detect_term_width()

    # Scroll-region eligibility : we need an interactive TTY with enough
    # rows to host a 3-line widget below a scroll region.  When any
    # check fails (piped stdout, tiny terminal, headless container) we
    # fall back to a no-op widget rather than emit half-broken escapes
    # that would end up in log files.
    _is_tty = sys.stdout.isatty()
    _term_size = _detect_term_size() if _is_tty else None
    _scroll_region_ok = (
        _is_tty
        and _term_size is not None
        and _term_size[0] >= _MIN_TERM_ROWS
    )
    if _scroll_region_ok:
        _initial_rows, _initial_cols = _term_size
        term_width = _initial_cols - 1  # keep local name stable

    _state = {
        'header': None,
        'bar_width': 0,
        'dw': 0,
        'started': False,
        'last': None,
        # Current display values (shared with animator thread)
        'header_line': '',
        'bar_line': '',
        'sub_line': '',
        # Trigger/script tracking
        'all_extracted': False,
        'trigger_count': 0,
        'bounce_pos': 0,
        'bounce_dir': 1,
        'in_script': False,
        'script_label': '',
        # Animation thread
        'lock': threading.Lock(),
        'animator': None,
        'stop_anim': threading.Event(),
        # Scroll-region bookkeeping
        'scroll_region_ok': _scroll_region_ok,
        'rows': _term_size[0] if _term_size else None,
        'cols': _term_size[1] if _term_size else None,
        'widget_top': (_term_size[0] - _WIDGET_LINES + 1) if _term_size else None,
        'region_active': False,
        'resize_pending': False,
        # Last raw progress (needed to repaint on resize)
        'last_progress': None,
    }

    # Pre-compute if total is known
    if total is not None:
        _state['header'] = ngettext(
            header_template.replace('{count}', '{0}'),
            header_template.replace('{count}', '{0}'),
            total,
        ).format(total)
        _state['dw'] = len(str(total))
        count_w = 1 + _state['dw'] + 1 + _state['dw'] + 1 + 4
        _state['bar_width'] = max(int(term_width * 0.8), 20)

    def _clip(text, maxw):
        """Clip visible text (ignoring ANSI codes) to maxw chars."""
        if len(text) <= maxw:
            return text
        return text[:maxw - 1] + "…"

    def _advance_bounce():
        bw = _state['bar_width']
        seg = min(_BOUNCE_WIDTH, bw)
        _state['bounce_pos'] += _state['bounce_dir']
        max_pos = bw - seg
        if _state['bounce_pos'] >= max_pos:
            _state['bounce_pos'] = max_pos
            _state['bounce_dir'] = -1
        elif _state['bounce_pos'] <= 0:
            _state['bounce_pos'] = 0
            _state['bounce_dir'] = 1

    def _sub_label(label):
        """Clip label so sub-line fits in term_width with same bar as main."""
        # Sub-line: [████░░░] label  →  2 + bar_width + 1 + 1 + len(label)
        max_label = term_width - _state['bar_width'] - 4
        return _clip(label, max(max_label, 3))

    def _bounce_bar(label):
        bw = _state['bar_width']
        seg = min(_BOUNCE_WIDTH, bw)
        pos = min(_state['bounce_pos'], bw - seg)
        bar = '░' * pos + '█' * seg + '░' * (bw - pos - seg)
        return f"{_ORANGE}[{bar}] {_sub_label(label)}{_RESET}"

    def _progress_sub_bar(bytes_done, bytes_total, label):
        bw = _state['bar_width']
        if bytes_total > 0:
            pct = min(int(bytes_done * 100 / bytes_total), 100)
        else:
            pct = 0
        filled = int(bw * pct / 100)
        bar = '█' * filled + '░' * (bw - filled)
        return f"[{bar}] {_sub_label(label)}"

    def _install_region():
        """Set up the VT100 scroll region for the widget.

        Reserves the bottom ``_WIDGET_LINES`` rows for the widget and
        constrains normal output scrolling to the rows above.  Called
        lazily on the first render so we don't perturb the terminal
        until we actually have something to draw.
        """
        if _state['region_active'] or not _state['scroll_region_ok']:
            return
        rows = _state['rows']
        # Scroll existing content up so the widget area is clear.  We
        # print blank lines equal to widget height ; the terminal
        # scrolls its own scrollback in the process, so no user
        # content is lost.
        sys.stdout.write("\n" * _WIDGET_LINES)
        # Hide cursor to avoid it flickering between top region and
        # widget rows.  Restored on cleanup.
        sys.stdout.write("\033[?25l")
        # DECSTBM : scroll region = rows 1..(rows - _WIDGET_LINES).
        bottom = rows - _WIDGET_LINES
        sys.stdout.write(f"\033[1;{bottom}r")
        # Park cursor at the bottom of the scroll region so subsequent
        # normal print() calls append there (and trigger normal scroll
        # inside the region).
        sys.stdout.write(f"\033[{bottom};1H")
        sys.stdout.flush()
        _state['region_active'] = True

    def _teardown_region():
        """Reset scroll region + cursor visibility, best-effort."""
        if not _state['region_active']:
            return
        rows = _state['rows'] or 24
        # \033[r resets DECSTBM to full screen ; \033[?25h shows the
        # cursor ; move cursor below widget so caller output resumes
        # cleanly (\n at bottom would scroll and clobber the widget).
        try:
            sys.stdout.write(f"\033[r\033[?25h\033[{rows};1H\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        _state['region_active'] = False

    def _render():
        """Write 3 lines to the widget region. Must hold _state['lock']."""
        if _state['resize_pending']:
            _apply_resize()
        if not _state['scroll_region_ok']:
            # Non-TTY / tiny terminal : callback is a no-op.  We keep
            # the state bookkeeping so display_scriptlet_output & co.
            # still work at end-of-tx.
            return
        if not _state['region_active']:
            _install_region()
        h = _state['header_line']
        b = _state['bar_line']
        s = _state['sub_line']
        top = _state['widget_top']
        # DECSC (\033 7) saves cursor position + attributes ; DECRC
        # (\033 8) restores.  Cursor lives in the top scroll region
        # between renders so log output from the caller continues to
        # scroll naturally there.
        sys.stdout.write(
            f"\0337"
            f"\033[{top};1H\033[K{h}"
            f"\033[{top + 1};1H\033[K{b}"
            f"\033[{top + 2};1H\033[K{s}"
            f"\0338"
        )
        sys.stdout.flush()

    def _apply_resize():
        """Consume resize_pending, redraw the widget at new geometry.

        Called with the lock held.  Discovers the new terminal size,
        wipes any stale widget content lingering at the old rows,
        redefines the scroll region, and rebuilds the 3 widget lines
        from the last raw progress event so the display snaps to the
        new width in the same frame.
        """
        _state['resize_pending'] = False
        new_size = _detect_term_size()
        if new_size is None:
            return  # terminal is gone — nothing sensible to do
        new_rows, new_cols = new_size
        old_rows = _state['rows']
        _state['rows'] = new_rows
        _state['cols'] = new_cols
        # Bail out if the new geometry is too small to host the widget
        # — degrade to fallback mode until the terminal is grown back.
        if new_rows < _MIN_TERM_ROWS or new_cols < 20:
            _teardown_region()
            _state['scroll_region_ok'] = False
            return
        _state['scroll_region_ok'] = True
        nonlocal term_width
        term_width = new_cols - 1
        _state['bar_width'] = max(int(term_width * 0.8), 20)
        _state['widget_top'] = new_rows - _WIDGET_LINES + 1
        # Reset DECSTBM so we can move the cursor anywhere, clear from
        # the new widget top to end of screen (wipes any stale content
        # left by the terminal's own reflow), then redefine the region.
        bottom = new_rows - _WIDGET_LINES
        sys.stdout.write(
            f"\033[r"
            f"\033[{_state['widget_top']};1H\033[J"
            f"\033[1;{bottom}r"
        )
        sys.stdout.flush()
        # Repaint immediately from the last raw progress so the user
        # sees a clean widget in the next frame, not stale strings.
        prog = _state['last_progress']
        if prog is not None:
            _recompute_lines_for(prog)
        # ``started`` is meaningless in scroll-region mode but keep
        # region_active so subsequent renders skip _install_region.
        _state['region_active'] = True

    def _animator():
        """Background thread: animate bounce during script phases."""
        while not _state['stop_anim'].is_set():
            time.sleep(_ANIM_INTERVAL)
            with _state['lock']:
                if _state['in_script']:
                    _advance_bounce()
                    _state['sub_line'] = _bounce_bar(_state['script_label'])
                    _render()

    def _start_animator():
        if _state['animator'] is None:
            _state['stop_anim'].clear()
            t = threading.Thread(target=_animator, daemon=True)
            t.start()
            _state['animator'] = t

    def _stop_animator():
        _state['stop_anim'].set()
        if _state['animator'] is not None:
            _state['animator'].join(timeout=0.5)
            _state['animator'] = None

    def _build_header_line(header_text, info_text):
        """Build header line: title left, info right, clipped to term_width."""
        info_clipped = _clip(info_text, term_width - len(header_text) - 2)
        padding = term_width - len(header_text) - len(info_clipped)
        line = f"{header_text}{' ' * max(padding, 1)}{info_clipped}"
        return line[:term_width]

    def _build_main_bar(done, pkg_total, pct):
        bw = _state['bar_width']
        dw = _state['dw']
        filled = int(bw * pct / 100)
        count_suffix = f" {done:>{dw}}/{pkg_total} {pct:>3}%"
        return f"[{'█' * filled}{'░' * (bw - filled)}]{count_suffix}"

    def _recompute_lines_for(progress: TransactionProgress) -> None:
        """Rebuild header_line / bar_line / sub_line from a raw event.

        Pure string computation — no output, no state transitions.
        Called from :func:`_callback` after dedup, and from
        :func:`_apply_resize` to repaint the widget at the fresh
        geometry without waiting for the next RPM event.  Must hold
        ``_state['lock']``.
        """
        pkg_total = progress.packages_total
        done = progress.packages_done
        if progress.phase == TransactionPhase.ERASE:
            header_text = _state['header'] or ""
            info_text = progress.package_name or ""
            pct = int(done * 100 / pkg_total) if pkg_total else 100
            _state['header_line'] = _build_header_line(header_text, info_text)
            _state['bar_line'] = _build_main_bar(done, pkg_total, pct)
            _state['sub_line'] = _progress_sub_bar(
                progress.bytes_done, progress.bytes_total, _("removing"))
        elif progress.phase == TransactionPhase.SCRIPT:
            if _state['all_extracted']:
                header_text = _("Running triggers...")
                if full_sync and progress.script_name:
                    info_text = describe_trigger(progress.script_name)
                else:
                    info_text = progress.script_name or progress.package_name
                label = ngettext(
                    "{n} trigger", "{n} triggers",
                    _state['trigger_count']).format(n=_state['trigger_count'])
                _state['header_line'] = (
                    f"{_ORANGE}"
                    f"{_build_header_line(header_text, info_text)}"
                    f"{_RESET}"
                )
            else:
                header_text = _state['header'] or ""
                info_text = progress.script_name or progress.package_name
                label = _("running %post")
                _state['header_line'] = _build_header_line(header_text, info_text)
            _state['script_label'] = label
            pct = int(done * 100 / pkg_total) if pkg_total else 100
            _state['bar_line'] = _build_main_bar(done, pkg_total, pct)
            _state['sub_line'] = _bounce_bar(label)
        else:
            # INSTALL phase (extraction)
            header_text = _state['header'] or ""
            info_text = progress.package_name or ""
            if pkg_total > 0:
                pkg_frac = done / pkg_total
                if progress.bytes_total > 0:
                    pkg_frac += (
                        (progress.bytes_done / progress.bytes_total) / pkg_total
                    )
                pct = int(pkg_frac * 100)
            else:
                pct = 0
            _state['header_line'] = _build_header_line(header_text, info_text)
            _state['bar_line'] = _build_main_bar(done, pkg_total, pct)
            _state['sub_line'] = _progress_sub_bar(
                progress.bytes_done, progress.bytes_total, _("extracting"))

    def _callback(progress: TransactionProgress):
        if progress.phase in (TransactionPhase.VERIFY, TransactionPhase.PREPARE):
            return

        pkg_total = progress.packages_total

        with _state['lock']:
            # Deferred init
            if _state['header'] is None:
                _state['header'] = ngettext(
                    header_template.replace('{count}', '{0}'),
                    header_template.replace('{count}', '{0}'),
                    pkg_total,
                ).format(pkg_total)
                _state['dw'] = len(str(pkg_total))
                count_w = 1 + _state['dw'] + 1 + _state['dw'] + 1 + 4
                _state['bar_width'] = max(int(term_width * 0.8), 20)

            # Dedup
            state_key = (progress.phase, progress.packages_done,
                         progress.package_name, progress.script_name,
                         progress.bytes_done)
            if state_key == _state['last']:
                return
            _state['last'] = state_key
            _state['last_progress'] = progress

            done = progress.packages_done

            if done >= pkg_total and not _state['all_extracted']:
                _state['all_extracted'] = True

            # Update flags that _recompute_lines_for reads
            if progress.phase == TransactionPhase.SCRIPT:
                _state['trigger_count'] += 1
                _state['in_script'] = True
                _advance_bounce()
            else:
                _state['in_script'] = False

            _recompute_lines_for(progress)
            _render()

            if progress.phase == TransactionPhase.SCRIPT:
                _start_animator()

    # ── SIGWINCH plumbing ──────────────────────────────────────────
    # Handler chained onto the previous handler so we cooperate with
    # any other listener already installed (mkimage, daemon, tests).
    # The handler itself only flips a flag — the actual redraw runs
    # from within the render lock on the next tick, so we never race
    # with the animator thread.
    _prev_sigwinch = None

    def _sigwinch_handler(signum, frame):
        _state['resize_pending'] = True
        prev = _prev_sigwinch
        if callable(prev):
            try:
                prev(signum, frame)
            except Exception:  # noqa: BLE001
                pass

    if _state['scroll_region_ok'] and hasattr(signal, 'SIGWINCH'):
        try:
            _prev_sigwinch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, _sigwinch_handler)
        except (ValueError, OSError):
            # signal.signal outside main thread, or restricted env
            _prev_sigwinch = None

    def _cleanup():
        """Restore terminal state.  Idempotent — safe from atexit."""
        _stop_animator()
        _teardown_region()
        if (_state['scroll_region_ok']
                and hasattr(signal, 'SIGWINCH')
                and _prev_sigwinch is not None):
            try:
                signal.signal(signal.SIGWINCH, _prev_sigwinch)
            except (ValueError, OSError):
                pass

    # Register an at-exit safety net so an unhandled crash (or a
    # sys.exit deep inside a caller) can't leave the terminal with a
    # stuck scroll region + hidden cursor.
    atexit.register(_cleanup)

    _callback.state = _state
    _callback.cleanup = _cleanup

    return _callback


def display_scriptlet_output(queue_result, verbose: bool = False,
                             transaction_id: int | None = None) -> None:
    """Display captured scriptlet output after a transaction.

    In verbose mode, shows all output grouped by package.  In normal mode,
    shows only packages that had scriptlet errors, with a summary count
    for packages that produced output without errors.

    Args:
        queue_result: A ``QueueResult`` with ``scriptlet_output`` (JSON
            string mapping package names to their output) and
            ``script_error_packages`` (list of names that errored).
        verbose: If True, show all output; otherwise show only errors.
        transaction_id: If set, include in the hint so users can review
            output later via ``urpm history --detail``.
    """
    import json
    from .. import colors

    if queue_result is None:
        return

    scriptlet_output = getattr(queue_result, 'scriptlet_output', '')
    error_packages = set(getattr(queue_result, 'script_error_packages', None) or [])

    # Parse the output dict
    script_dict = {}
    if scriptlet_output:
        try:
            script_dict = json.loads(scriptlet_output)
        except (json.JSONDecodeError, TypeError):
            # Fallback: show raw output if JSON parse fails
            if verbose or error_packages:
                print(colors.dim("\n  " + _("Scriptlet output:")))
                for line in scriptlet_output.splitlines():
                    print(colors.dim(f"    {line}"))
            return

    if not script_dict and not error_packages:
        return

    # Separate packages into error vs. normal
    error_with_output = {p: script_dict[p] for p in script_dict if p in error_packages}
    normal_with_output = {p: script_dict[p] for p in script_dict if p not in error_packages}
    # Error packages with no captured output still need display
    error_no_output = error_packages - set(script_dict.keys())

    if verbose:
        # Show everything: errors in red, normal in dim
        if not script_dict and not error_no_output:
            return
        print(colors.dim("\n  " + _("Scriptlet output:")))
        for pkg, output in script_dict.items():
            color_fn = colors.error if pkg in error_packages else colors.dim
            if pkg:
                print(color_fn(f"    {pkg}:"))
                for line in output.splitlines():
                    print(color_fn(f"      {line}"))
            else:
                # Pre-marker output (no package name)
                for line in output.splitlines():
                    print(color_fn(f"    {line}"))
        # Error packages that produced no output
        for pkg in sorted(error_no_output):
            print(colors.error(f"    {pkg}: " + _("scriptlet error (no output)")))
    else:
        # Non-verbose: show only errors, summarize the rest
        has_error_display = bool(error_with_output) or bool(error_no_output)
        if has_error_display:
            print(colors.dim("\n  " + _("Scriptlet output:")))
            for pkg, output in error_with_output.items():
                if pkg:
                    print(colors.error(f"    {pkg}:"))
                    for line in output.splitlines():
                        print(colors.error(f"      {line}"))
                else:
                    for line in output.splitlines():
                        print(colors.error(f"    {line}"))
            for pkg in sorted(error_no_output):
                print(colors.error(f"    {pkg}: " + _("scriptlet error (no output)")))

        # Summary for normal (non-error) packages that had output
        normal_count = len(normal_with_output)
        if normal_count > 0:
            if transaction_id is not None:
                hint = _("use --verbose to see, or urpm history --detail {tid}").format(
                    tid=transaction_id)
            else:
                hint = _("use --verbose to see")
            summary = ngettext(
                "{count} package had scriptlet output ({hint})",
                "{count} packages had scriptlet output ({hint})",
                normal_count,
            ).format(count=normal_count, hint=hint)
            if has_error_display:
                print(colors.dim(f"    {summary}"))
            else:
                print(colors.dim(f"\n  {summary}"))
