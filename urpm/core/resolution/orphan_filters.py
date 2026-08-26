"""Composable filters for the interactive orphans triage.

Filters are expressed as short ``key<op>value`` strings the operator
types on the CLI or in the TUI sub-menu.  Multiple filters combine with
AND — an orphan must match every active predicate to remain visible.

Grammar (loose) :

    key ::= disttag | kind | size | installed | group | name | provides
    op  ::= "=" | "<" | ">" | "~="
    value ::= raw string (interpretation depends on the key)

Examples that must all parse and behave correctly :

    disttag=mga9              # exact match
    disttag=!mga10            # negated match
    kind=sublib               # is_soname_sublib(info) is True
    kind=!sublib              # inverse
    size>100M                 # installed size > 100 MiB
    size<1G
    installed<30d             # installed less than 30 days ago
    installed>1y              # installed more than a year ago
    group=Documentation       # RPMTAG_GROUP equality
    name~=^lib64.*            # regex on name
    provides~=libgtk.*        # regex over the provides list

Multiple filters may be passed either as several ``--filter`` flags
or comma-separated inside a single flag :

    --filter disttag=mga10,kind=!sublib
    --filter kind=userland --filter size>10M
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .orphan_classify import OrphanInfo, is_soname_sublib


# --- Public API -----------------------------------------------------------


Predicate = Callable[[OrphanInfo], bool]


@dataclass
class FilterSpec:
    """Compiled AND-composition of individual filter predicates.

    ``predicates`` holds the callable form ; ``raw`` keeps the source
    strings so the TUI can display « Filtres actifs : X, Y » without
    re-serialising.  An empty spec matches everything.
    """

    predicates: List[Predicate] = field(default_factory=list)
    raw: List[str] = field(default_factory=list)

    def matches(self, info: OrphanInfo) -> bool:
        return all(p(info) for p in self.predicates)

    def apply(self, orphans) -> list:
        """Return the subset of ``orphans`` that satisfies every filter."""
        return [o for o in orphans if self.matches(o)]

    def is_empty(self) -> bool:
        return not self.predicates


class FilterSpecError(ValueError):
    """Raised on malformed filter expressions.

    The message is user-facing (goes straight to the CLI / TUI) so it
    must name the offending token clearly.  The parser stops at the
    first error rather than reporting all of them — better ergonomics
    at the prompt.
    """


def parse_filters(exprs, now: Optional[float] = None) -> FilterSpec:
    """Compile a list of filter expressions into a :class:`FilterSpec`.

    ``exprs`` accepts anything iterable of strings.  Each string may
    itself contain several comma-separated expressions.  ``now`` is a
    unix timestamp override for the ``installed`` filter — tests pass
    a fixed value ; production leaves it ``None`` (uses ``time.time()``
    at parse time so the reference stays stable through a session).
    """
    if now is None:
        now = time.time()

    raw: List[str] = []
    predicates: List[Predicate] = []
    for chunk in exprs or ():
        for expr in _split_comma(chunk):
            expr = expr.strip()
            if not expr:
                continue
            pred = _compile_one(expr, now=now)
            predicates.append(pred)
            raw.append(expr)
    return FilterSpec(predicates=predicates, raw=raw)


# --- Parsing internals ----------------------------------------------------


def _split_comma(s: str) -> List[str]:
    return s.split(",")


# ``key<op>value`` — the operator is a single ``=``, ``<``, ``>`` or the
# two-character ``~=``.  ``re.match`` is anchored, so the key must be a
# bare identifier ; that rejects free-form text before the operator.
_EXPR_RE = re.compile(r"^(?P<key>[a-z_]+)(?P<op>~=|=|<|>)(?P<value>.*)$")


def _compile_one(expr: str, *, now: float) -> Predicate:
    m = _EXPR_RE.match(expr)
    if not m:
        raise FilterSpecError(
            f"malformed filter expression: {expr!r} "
            "(expected key=value, key<value, key>value, or key~=regex)"
        )
    key, op, value = m.group("key"), m.group("op"), m.group("value")
    builder = _BUILDERS.get(key)
    if builder is None:
        raise FilterSpecError(
            f"unknown filter key: {key!r} "
            f"(known: {', '.join(sorted(_BUILDERS))})"
        )
    return builder(op, value, now=now)


# --- Filter builders ------------------------------------------------------


def _build_disttag(op: str, value: str, **_) -> Predicate:
    if op != "=":
        raise FilterSpecError(f"disttag only supports '='; got {op!r}")
    negated = value.startswith("!")
    target = value[1:] if negated else value
    if not target:
        raise FilterSpecError("disttag= expects a value (e.g. disttag=mga10)")

    def pred(info: OrphanInfo) -> bool:
        from .orphan_classify import parse_disttag
        match = (parse_disttag(info.evr) == target)
        return not match if negated else match
    return pred


def _build_kind(op: str, value: str, **_) -> Predicate:
    if op != "=":
        raise FilterSpecError(f"kind only supports '='; got {op!r}")
    negated = value.startswith("!")
    target = value[1:] if negated else value
    if target not in {"sublib", "userland"}:
        raise FilterSpecError(
            f"unknown kind {target!r} (known: sublib, userland)"
        )

    def pred(info: OrphanInfo) -> bool:
        is_sub = is_soname_sublib(info)
        match = is_sub if target == "sublib" else not is_sub
        return not match if negated else match
    return pred


_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _parse_size(s: str) -> int:
    m = re.match(r"^(\d+(?:\.\d+)?)([KMGT]?)$", s.strip().upper())
    if not m:
        raise FilterSpecError(
            f"malformed size {s!r} (expected 100, 100M, 1.5G …)"
        )
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2)])


def _build_size(op: str, value: str, **_) -> Predicate:
    if op not in {"<", ">"}:
        raise FilterSpecError(
            f"size only supports '<' or '>'; got {op!r}"
        )
    threshold = _parse_size(value)
    if op == "<":
        return lambda info: info.size < threshold
    return lambda info: info.size > threshold


_DURATION_UNITS = {
    "s": 1, "m": 60, "h": 3600, "d": 86400,
    "w": 7 * 86400, "y": 365 * 86400,
}


def _parse_duration(s: str) -> int:
    m = re.match(r"^(\d+)([smhdwy])$", s.strip().lower())
    if not m:
        raise FilterSpecError(
            f"malformed duration {s!r} (expected 30d, 1y, 2w, 3h …)"
        )
    return int(m.group(1)) * _DURATION_UNITS[m.group(2)]


def _build_installed(op: str, value: str, *, now: float) -> Predicate:
    if op not in {"<", ">"}:
        raise FilterSpecError(
            f"installed only supports '<' or '>'; got {op!r}"
        )
    delta = _parse_duration(value)
    threshold_ts = now - delta
    if op == "<":
        # installed less than N ago == install_time > threshold
        return lambda info: info.install_time > threshold_ts
    return lambda info: info.install_time < threshold_ts


def _build_group(op: str, value: str, **_) -> Predicate:
    if op != "=":
        raise FilterSpecError(f"group only supports '='; got {op!r}")
    negated = value.startswith("!")
    target = value[1:] if negated else value

    def pred(info: OrphanInfo) -> bool:
        match = (info.group == target)
        return not match if negated else match
    return pred


def _build_regex(field_name: str):
    def _factory(op: str, value: str, **_) -> Predicate:
        if op != "~=":
            raise FilterSpecError(
                f"{field_name} only supports '~='; got {op!r}"
            )
        try:
            pat = re.compile(value)
        except re.error as exc:
            raise FilterSpecError(
                f"invalid regex {value!r} for {field_name}: {exc}"
            ) from exc

        if field_name == "name":
            return lambda info: bool(pat.search(info.name))
        # provides: match if any capability matches
        return lambda info: any(pat.search(p) for p in info.provides)
    return _factory


_BUILDERS = {
    "disttag":   _build_disttag,
    "kind":      _build_kind,
    "size":      _build_size,
    "installed": _build_installed,
    "group":     _build_group,
    "name":      _build_regex("name"),
    "provides":  _build_regex("provides"),
}
