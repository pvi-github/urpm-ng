"""Sanitize untrusted text before it reaches a user-visible channel.

SPEC_DISTUPGRADE §3.C : `sanitize_scriptlet_output` is the **single
source of truth** for the six frontiers listed in the spec (DB writes,
IPC messages, CLI stderr rendering, API-derived text, logging, argv).
One function everywhere — no per-frontier variant.

Five layers, applied in order.  Every layer strips or bounds a class
of Unicode weaponry :

1. **Control chars ASCII** (``\x00-\x08 \x0b-\x1f \x7f``) —
   terminal escape sequences (``\x1b]0;pwned\x07\x1b[2J``), bell,
   NUL byte injection.  Preserved : ``\t`` and ``\n``.
2. **Bidirectional overrides** (CVE-2021-42574 « Trojan Source ») —
   the RLO / LRO / PDI / LRE / RLE / PDF / LRI / RLI / FSI code
   points that reverse how a line is rendered vs. how it's stored.
3. **Unicode Tag Characters** (Plane 14, ``U+E0020-U+E007F``) —
   invisible mirror of ASCII printable, used post-CVE-2021-42574
   to hide payload inside benign-looking text.
4. **Variation selectors** (``U+FE00-U+FE0F``, ``U+E0100-U+E01EF``) —
   can alter the rendering of adjacent glyphs on some terminals.
5. **Combining marks bounded** — the « zalgo » attack that stacks
   diacritics until the terminal renders garbage.  Cap runs of
   combining characters at ``_MAX_COMBINING`` (4) consecutive.

Replacement char : ``�`` (U+FFFD REPLACEMENT CHARACTER) — safe
in every locale, visible « something got stripped here » signal.
"""

from __future__ import annotations

import re


REPLACEMENT = "�"

# Bounded combining runs — 4 stacked marks is enough for legit
# diacritics (é̀́̈…) but blocks zalgo tail attacks.
_MAX_COMBINING = 4

# Layer 1 : control chars, keep \t (0x09) and \n (0x0a).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Layer 2 : bidirectional format controls.  Explicit code points
# rather than a broad character class so we can point at each one in
# the audit trail.
_BIDI_CONTROLS = re.compile(
    "["
    "‪"  # LRE — LEFT-TO-RIGHT EMBEDDING
    "‫"  # RLE — RIGHT-TO-LEFT EMBEDDING
    "‬"  # PDF — POP DIRECTIONAL FORMATTING
    "‭"  # LRO — LEFT-TO-RIGHT OVERRIDE
    "‮"  # RLO — RIGHT-TO-LEFT OVERRIDE
    "⁦"  # LRI — LEFT-TO-RIGHT ISOLATE
    "⁧"  # RLI — RIGHT-TO-LEFT ISOLATE
    "⁨"  # FSI — FIRST STRONG ISOLATE
    "⁩"  # PDI — POP DIRECTIONAL ISOLATE
    "‎"  # LRM — LEFT-TO-RIGHT MARK
    "‏"  # RLM — RIGHT-TO-LEFT MARK
    "؜"  # ALM — ARABIC LETTER MARK
    "﻿"  # BOM — ZERO WIDTH NO-BREAK SPACE (also stealth marker)
    "⁠"  # WORD JOINER
    "​"  # ZERO WIDTH SPACE
    "‌"  # ZERO WIDTH NON-JOINER
    "‍"  # ZERO WIDTH JOINER
    "]"
)

# Layer 3 : Unicode Tag characters (Plane 14).
_UNICODE_TAGS = re.compile(r"[\U000e0020-\U000e007f]")

# Layer 4 : Variation selectors (BMP + Plane 14 extension).
_VARIATION_SELECTORS = re.compile(
    r"[\U0000fe00-\U0000fe0f\U000e0100-\U000e01ef]"
)

# Combining marks range (Unicode "combining" categories).  The full
# categorization requires ``unicodedata.combining`` but the
# character ranges below cover the common attacks (M* / diacritics)
# without pulling that module in for every call.  We fall back to
# ``unicodedata.combining`` inside the bounding loop for accuracy.
import unicodedata  # noqa: E402


def _bound_combining_runs(text: str) -> str:
    """Cap sequences of combining characters at ``_MAX_COMBINING``."""
    out: list[str] = []
    combining_run = 0
    for ch in text:
        if unicodedata.combining(ch):
            combining_run += 1
            if combining_run > _MAX_COMBINING:
                continue
        else:
            combining_run = 0
        out.append(ch)
    return "".join(out)


def sanitize_scriptlet_output(text: str) -> str:
    """Return ``text`` cleaned of the 5 attack layers above.

    Idempotent (calling it twice yields the same result as once) and
    length-non-increasing.  Strings that only contain safe printable
    ASCII come out unchanged with a fast path via the regex
    ``search`` — avoiding the substitution walk when nothing matches.

    Non-string inputs (``bytes``, ``None``, numbers…) are coerced
    with ``str()`` so callers can pass them raw ; this matches the
    six frontiers' expectations (rpm callback output, JSON leaf
    values, log record fields).  ``None`` becomes the empty string.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    if not text:
        return text

    # Fast path : nothing to clean.
    if (not _CONTROL_CHARS.search(text)
            and not _BIDI_CONTROLS.search(text)
            and not _UNICODE_TAGS.search(text)
            and not _VARIATION_SELECTORS.search(text)):
        # Combining bound still needed if zalgo present ; cheap check.
        if not any(unicodedata.combining(ch) for ch in text):
            return text

    text = _CONTROL_CHARS.sub(REPLACEMENT, text)
    text = _BIDI_CONTROLS.sub(REPLACEMENT, text)
    text = _UNICODE_TAGS.sub("", text)              # invisible → drop
    text = _VARIATION_SELECTORS.sub("", text)       # invisible → drop
    text = _bound_combining_runs(text)
    return text
