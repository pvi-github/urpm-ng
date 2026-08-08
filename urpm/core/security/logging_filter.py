"""Sanitising logging.Filter attached to every urpm-installed handler.

SPEC_DISTUPGRADE §3.C TC.5.  Handler-level rather than logger-level
because Python's ``logging`` only runs a logger's ``.filter()`` for
records emitted directly by that logger — records from child
loggers propagate through the parent's ``handlers`` but bypass its
``filters`` (documented CPython behaviour).  A handler-level filter
sees every record that traverses the handler regardless of its
originating logger.

Complementary :func:`install_sanitising_factory` sets a
``LogRecordFactory`` that scrubs records at creation time — useful
when third-party code emits a record via ``logging.getLogger("random.
module")`` whose destination handler urpm doesn't own.

Four attack surfaces neutralised per record :

- ``msg`` + ``args`` — the format string and its lazy-format args.
- ``exc_info`` / ``exc_text`` — the exception message and formatted
  traceback lines that ``Formatter.formatException`` would emit
  downstream.
- ``extra`` attributes whose name is in ``_EXTRA_ALLOWLIST`` — the
  spec mandates an explicit allowlist rather than a wildcard so a
  new attribute silently added by a caller can't smuggle content
  through.
- Cached ``record.message`` — invalidated in case a prior handler /
  filter formatted the message; the next :meth:`getMessage` call
  recomputes from our sanitized ``msg`` / ``args``.
"""

from __future__ import annotations

import logging
from typing import Optional

from .sanitize import sanitize_scriptlet_output


# Attributes conventionally used by urpm callers to attach unsanitized
# content to a LogRecord.  New keys should be added deliberately after
# review — a caller that adds a bespoke attribute without extending
# this set gets its payload passed through unchanged, which is the
# expected fail-open behaviour for a sanitizer we haven't taught yet.
_EXTRA_ALLOWLIST = frozenset({
    "pkg_stderr",
    "scriptlet_output",
    "media_reason",
    "nevra",
    "server_url",
})


class SanitisingFilter(logging.Filter):
    """Rewrite untrusted fields of a :class:`logging.LogRecord`."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # 1. msg + args ---------------------------------------------
        if isinstance(record.msg, str):
            record.msg = sanitize_scriptlet_output(record.msg)
        if record.args:
            record.args = tuple(
                sanitize_scriptlet_output(a) if isinstance(a, str) else a
                for a in record.args
            )

        # 2. exc_info / exc_text -----------------------------------
        # Stringify eagerly + sanitize, then wipe exc_info so any
        # downstream Formatter re-uses our sanitized exc_text instead
        # of re-formatting from the raw tuple.
        if record.exc_info and record.exc_info != (None, None, None):
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = sanitize_scriptlet_output(formatted)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = sanitize_scriptlet_output(record.exc_text)

        # 3. Allowlisted extras ------------------------------------
        for attr in _EXTRA_ALLOWLIST:
            val = getattr(record, attr, None)
            if isinstance(val, str):
                setattr(record, attr, sanitize_scriptlet_output(val))

        # 4. Invalidate cached message -----------------------------
        # ``LogRecord.getMessage`` caches on ``self.message`` — a
        # Formatter that already called it would have stored a stale
        # copy.  Drop it so the next call recomputes from our
        # sanitized msg/args.
        record.__dict__.pop("message", None)

        return True  # never drop records — this filter only rewrites


def install_sanitising_filter(handler: logging.Handler) -> None:
    """Attach a :class:`SanitisingFilter` to ``handler``.

    Idempotent : if the handler already carries one, no second
    instance is added.  Call this from every place that constructs a
    handler urpm hands over to :func:`logging.Logger.addHandler`.
    """
    if any(isinstance(f, SanitisingFilter) for f in handler.filters):
        return
    handler.addFilter(SanitisingFilter())


# ---------------------------------------------------------------------
# Defense-in-depth : LogRecordFactory
# ---------------------------------------------------------------------

_original_factory: Optional[logging.LogRecordFactory] = None


def install_sanitising_factory() -> None:
    """Install a ``LogRecordFactory`` that pre-sanitises every record.

    Belt-and-braces for records whose destination handler wasn't
    created via :func:`install_sanitising_filter` — for instance
    handlers installed by a third-party plugin that pulls
    ``logging.getLogger(...)`` and calls ``addHandler`` without
    knowing about urpm's filter API.
    """
    global _original_factory
    if _original_factory is not None:
        return  # already installed
    _original_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = _original_factory(*args, **kwargs)
        # Reuse the same code path as the filter so behaviour stays
        # aligned.  The filter's ``filter()`` mutates in place ; we
        # discard the boolean return.
        SanitisingFilter().filter(record)
        return record

    logging.setLogRecordFactory(factory)


def uninstall_sanitising_factory() -> None:
    """Restore the previous ``LogRecordFactory`` (test-only)."""
    global _original_factory
    if _original_factory is None:
        return
    logging.setLogRecordFactory(_original_factory)
    _original_factory = None
