"""Logging calls must carry as many placeholders as arguments.

Reported from the field: ``urpm genmedia --appstream-info -v`` printed a
traceback instead of a debug line, because the call read

    logger.debug("File wrotten: ", self.output_path)

with no ``%s`` in the format.  The logging module then tries
``"File wrotten: " % (path,)`` and raises *not all arguments converted
during string formatting*.

What makes this worth a test rather than a one-line fix: the mistake is
invisible until that exact line runs at that exact level.  The one above
had been shipping for releases and only surfaced when a tester passed
``-v``.  Nothing in the type system, the linter or the test suite would
have caught it, since a format string is just a string.
"""

import ast
import re
from pathlib import Path

import pytest

#: Methods that take a %-format string followed by its arguments.
_LOGGING_METHODS = frozenset({
    "debug", "info", "warning", "error", "critical", "exception", "log",
})

#: One %-placeholder.  ``%%`` is an escaped percent and is removed before
#: matching, so it never counts.
_PLACEHOLDER = re.compile(
    r"%(?:\([^)]*\))?[-#0 +]*[\d*]*(?:\.[\d*]+)?[hlL]?[diouxXeEfFgGcrsa]"
)

_SOURCE_ROOTS = ("urpm", "rpmdrake")


def _source_files():
    root = Path(__file__).resolve().parent.parent.parent
    for package in _SOURCE_ROOTS:
        yield from sorted((root / package).rglob("*.py"))


def _is_logger(node: ast.Attribute) -> bool:
    """Is this call made on something that looks like a logger?

    Matching on the name keeps ``warnings.warn`` and any local ``error``
    helper out, which a bare method-name check would sweep in.
    """
    base = node.value
    name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
    return "log" in name.lower()


def _mismatches(path: Path):
    """Yield the calls whose placeholder count does not match its args."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _LOGGING_METHODS:
            continue
        if not _is_logger(node.func):
            continue

        args = node.args
        if node.func.attr == "log":
            args = args[1:]  # the level comes first
        if len(args) < 2:
            continue  # a lone message needs no placeholder

        fmt = args[0]
        if not (isinstance(fmt, ast.Constant) and isinstance(fmt.value, str)):
            continue  # built at run time: nothing to count here

        supplied = args[1:]
        if any(isinstance(a, ast.Starred) for a in supplied):
            # ``*stats()`` expands to a count only known at run time.
            # scheduler.py does this legitimately with a Tuple[int, int].
            continue

        expected = len(_PLACEHOLDER.findall(fmt.value.replace("%%", "")))
        if expected != len(supplied):
            yield (node.lineno, expected, len(supplied),
                   ast.unparse(node)[:90])


def test_every_logging_call_matches_its_arguments():
    failures = []
    for path in _source_files():
        for lineno, expected, supplied, source in _mismatches(path):
            failures.append(
                f"{path.name}:{lineno}: {expected} placeholder(s) for "
                f"{supplied} argument(s)\n      {source}"
            )

    assert not failures, (
        "logging calls that would raise when they run:\n  "
        + "\n  ".join(failures)
    )


def test_the_guard_actually_finds_the_reported_bug(tmp_path):
    """Guard the guard: a walk that matches nothing proves nothing."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        'import logging\n'
        'logger = logging.getLogger(__name__)\n'
        'logger.debug("File wrotten: ", path)\n',
        encoding="utf-8",
    )

    found = list(_mismatches(sample))

    assert len(found) == 1
    assert found[0][1] == 0  # no placeholder
    assert found[0][2] == 1  # one argument


@pytest.mark.parametrize("call", [
    'logger.info("plain message with no argument")',
    'logger.info("one %s here", value)',
    'logger.debug("two %s and %d", a, b)',
    'logger.warning("escaped %% sign only")',
    'logger.log(logging.INFO, "level first, %s", value)',
    'logger.info(built_at_runtime, value)',
    'warnings.warn("not a logger at all", DeprecationWarning)',
])
def test_sound_calls_are_left_alone(tmp_path, call):
    """The guard must not cry wolf, or it will be switched off."""
    sample = tmp_path / "sample.py"
    sample.write_text(call + "\n", encoding="utf-8")

    assert list(_mismatches(sample)) == []
