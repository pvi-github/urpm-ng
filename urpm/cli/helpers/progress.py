"""Transaction progress display for install/upgrade commands.

Provides a factory that returns a callback compatible with
TransactionQueue.progress_callback.  The two-line display shows:

    Line 1: header (left-aligned) + package/trigger info (right-aligned)
    Line 2: [████░░░░░░░░░░░░░░░░░░░░░░░] XX/XX 100%
"""

import os
from gettext import ngettext

from ...core.transaction_queue import TransactionProgress, TransactionPhase
from ...core.triggers import describe_trigger


def make_progress_callback(
    header_template: str,
    total: int | None = None,
    full_sync: bool = False,
):
    """Create a transaction progress callback.

    Args:
        header_template: ngettext template with ``{count}`` placeholder,
            e.g. ``"Installing {count} packages..."``.
            If *total* is None the template is formatted on the first
            callback invocation; otherwise it is formatted immediately.
        total: Number of packages.  When known upfront (install), pass it
            directly.  When discovered at runtime (upgrade), pass None and
            the callback will read ``progress.packages_total`` on first call.
        full_sync: If True, use human-readable trigger descriptions.

    Returns:
        A callable ``(TransactionProgress) -> None`` suitable for
        ``TransactionQueue(progress_callback=...)``.
    """
    try:
        term_width = os.get_terminal_size().columns - 1
    except OSError:
        term_width = 79

    # Mutable state shared with the inner closure
    _state = {
        'header': None,
        'bar_width': 0,
        'dw': 0,
        'started': False,
        'last': None,
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
        _state['bar_width'] = max(term_width - count_w - 2, 10)

    def _callback(progress: TransactionProgress):
        if progress.phase in (TransactionPhase.VERIFY, TransactionPhase.PREPARE):
            return

        pkg_total = progress.packages_total

        # Deferred init (upgrade path: total unknown until first callback)
        if _state['header'] is None:
            _state['header'] = ngettext(
                header_template.replace('{count}', '{0}'),
                header_template.replace('{count}', '{0}'),
                pkg_total,
            ).format(pkg_total)
            _state['dw'] = len(str(pkg_total))
            count_w = 1 + _state['dw'] + 1 + _state['dw'] + 1 + 4
            _state['bar_width'] = max(term_width - count_w - 2, 10)
            print("\n" + _state['header'])

        # Dedup: skip if state unchanged
        state_key = (progress.phase, progress.packages_done,
                     progress.package_name, progress.script_name,
                     progress.bytes_done)
        if state_key == _state['last']:
            return
        _state['last'] = state_key

        done = progress.packages_done
        header = _state['header']
        dw = _state['dw']
        bar_width = _state['bar_width']

        # --- Info text (right-aligned on header line) ---
        if progress.phase == TransactionPhase.SCRIPT:
            pct = int(done * 100 / pkg_total) if pkg_total else 100
            if full_sync and progress.script_name:
                info = describe_trigger(progress.script_name)
            else:
                info = progress.script_name or progress.package_name
        else:
            if pkg_total > 0:
                pkg_frac = done / pkg_total
                if progress.bytes_total > 0:
                    pkg_frac += (progress.bytes_done / progress.bytes_total) / pkg_total
                pct = int(pkg_frac * 100)
            else:
                pct = 0
            info = progress.package_name

        # Truncate info so header + space + info fits in terminal width
        max_info = term_width - len(header) - 2
        if len(info) > max_info:
            info = info[:max_info - 1] + "…"

        # --- Header line: title left, info right ---
        padding = term_width - len(header) - len(info)
        header_line = f"{header}{' ' * max(padding, 1)}{info}"
        if len(header_line) > term_width:
            header_line = header_line[:term_width]

        # --- Bar line: full-width bar + fixed-width count ---
        filled = int(bar_width * pct / 100)
        count_suffix = f" {done:>{dw}}/{pkg_total} {pct:>3}%"
        bar_line = f"[{'█' * filled}{'░' * (bar_width - filled)}]{count_suffix}"
        if len(bar_line) > term_width:
            bar_line = bar_line[:term_width]

        if not _state['started']:
            _state['started'] = True
        print(f"\033[A\r\033[K{header_line}\n\033[K{bar_line}",
              end='', flush=True)

    # Expose state for callers that need post-transaction "done" line
    _callback.state = _state

    return _callback
