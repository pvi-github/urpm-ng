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

import os
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

    def _render():
        """Write 3 lines to terminal. Must hold _state['lock']."""
        h = _state['header_line']
        b = _state['bar_line']
        s = _state['sub_line']
        if not _state['started']:
            _state['started'] = True
            # First render: print blank line for spacing, then 3 display lines.
            # No cursor-up — we're establishing the 3-line region.
            print(f"\n{_CLR}{h}\n{_CLR}{b}\n{_CLR}{s}",
                  end='', flush=True)
        else:
            # Subsequent renders: go up 2 lines to rewrite all 3 in place.
            print(f"\033[2A\r{_CLR}{h}\n{_CLR}{b}\n{_CLR}{s}",
                  end='', flush=True)

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

            done = progress.packages_done

            if done >= pkg_total and not _state['all_extracted']:
                _state['all_extracted'] = True

            # ── SCRIPT phase ──
            if progress.phase == TransactionPhase.SCRIPT:
                _state['trigger_count'] += 1
                _state['in_script'] = True

                if _state['all_extracted']:
                    # File triggers / posttrans
                    header_text = _("Running triggers...")
                    if full_sync and progress.script_name:
                        info_text = describe_trigger(progress.script_name)
                    else:
                        info_text = progress.script_name or progress.package_name
                    label = ngettext(
                        "{n} trigger", "{n} triggers",
                        _state['trigger_count']).format(n=_state['trigger_count'])
                    _state['header_line'] = f"{_ORANGE}{_build_header_line(header_text, info_text)}{_RESET}"
                else:
                    # Per-package %post
                    header_text = _state['header']
                    info_text = progress.script_name or progress.package_name
                    label = _("running %post")
                    _state['header_line'] = _build_header_line(header_text, info_text)

                _state['script_label'] = label
                pct = int(done * 100 / pkg_total) if pkg_total else 100
                _state['bar_line'] = _build_main_bar(done, pkg_total, pct)
                _advance_bounce()
                _state['sub_line'] = _bounce_bar(label)
                _render()
                _start_animator()

            # ── INSTALL phase ──
            elif not _state['all_extracted']:
                _state['in_script'] = False

                header_text = _state['header']
                info_text = progress.package_name or ""

                if pkg_total > 0:
                    pkg_frac = done / pkg_total
                    if progress.bytes_total > 0:
                        pkg_frac += (progress.bytes_done / progress.bytes_total) / pkg_total
                    pct = int(pkg_frac * 100)
                else:
                    pct = 0

                _state['header_line'] = _build_header_line(header_text, info_text)
                _state['bar_line'] = _build_main_bar(done, pkg_total, pct)
                _state['sub_line'] = _progress_sub_bar(
                    progress.bytes_done, progress.bytes_total, _("extracting"))
                _render()

    def _cleanup():
        """Stop animator thread. Call after transaction completes."""
        _stop_animator()

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
