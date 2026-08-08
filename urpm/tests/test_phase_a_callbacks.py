"""Tests for Phase A callback wiring (SPEC_DISTUPGRADE §3.A).

- TA.2 : script_type propagated from amount through QueueProgressMessage
  → TransactionProgress.
- TA.4 : _color_status fixed to match db/history.py values.
- SCRIPT_TYPE_LABELS mapping frozen empirically in mga10-64 spike C2.
"""

from __future__ import annotations

from urpm.core.transaction_queue import (
    QueueProgressMessage,
    SCRIPT_TYPE_LABELS,
    TransactionPhase,
    TransactionProgress,
    _msg_to_progress,
    script_type_label,
)


class TestScriptTypeLabels:
    def test_all_expected_tags_mapped(self):
        expected = {
            1023: "pre",
            1024: "post",
            1025: "preun",
            1026: "postun",
            1151: "pretrans",
            1152: "posttrans",
            5103: "preuntrans",
            5104: "postuntrans",
            1065: "trigger",
        }
        assert SCRIPT_TYPE_LABELS == expected

    def test_lookup_known(self):
        assert script_type_label(1023) == "pre"
        assert script_type_label(1152) == "posttrans"

    def test_lookup_unknown_returns_empty(self):
        assert script_type_label(0) == ""
        assert script_type_label(999) == ""


class TestScriptTypeRoundtrip:
    """script_type must survive the pipe json roundtrip."""

    def test_to_json_from_json_preserves_script_type(self):
        msg = QueueProgressMessage(
            msg_type="progress", phase="script",
            script="post", script_type=1024,
        )
        rebuilt = QueueProgressMessage.from_json(msg.to_json())
        assert rebuilt.script_type == 1024

    def test_defaults_to_zero_when_absent(self):
        # Old serialized payloads without the field should default 0.
        msg = QueueProgressMessage.from_json('{"type": "progress"}')
        assert msg.script_type == 0

    def test_msg_to_progress_carries_script_type(self):
        msg = QueueProgressMessage(
            msg_type="progress", phase="script",
            script="posttrans", script_type=1152,
        )
        tp = _msg_to_progress(msg)
        assert tp.script_type == 1152
        assert tp.script_name == "posttrans"
        assert tp.phase == TransactionPhase.SCRIPT


class TestColorStatusMatchesWriteback:
    """TA.4 : the writeback in db/history.py writes 'complete' and
    'interrupted' but the display was comparing to 'completed' and
    'aborted' → every transaction rendered uncolored.  Verify the
    ``_color_status`` local function body references the real enum
    values only."""

    def _color_status_source(self) -> str:
        import inspect
        from urpm.cli.commands import history as h
        full = inspect.getsource(h)
        # Extract the ``def _color_status(...)`` block up to the next
        # top-level ``def`` at the same indent.
        marker = "    def _color_status("
        start = full.index(marker)
        # Next inner def or "    # " section at the same indent
        # (blank line + non-indented text ends nested scope, but the
        # function is already a few dozen lines so we just take a fixed
        # chunk : until the next `def ` at indent 4).
        rest = full[start + len(marker):]
        end = rest.find("\n    def ")
        return full[start:start + len(marker) + (end if end > 0 else 400)]

    def test_body_uses_real_enum_values(self):
        body = self._color_status_source()
        assert "'complete'" in body
        assert "'interrupted'" in body
        assert "'running'" in body

    def test_body_does_not_use_buggy_values(self):
        body = self._color_status_source()
        # The docstring intentionally cites the old buggy values ; the
        # comparison LINES must not.  Filter out docstring lines
        # (indented triple-quoted content) first.
        lines = [
            ln for ln in body.splitlines()
            if not ln.lstrip().startswith(("#", '"""', "``"))
            and "docstring" not in ln
        ]
        code = "\n".join(lines)
        # Look for the actual comparison shape, not the token in prose.
        assert "== 'completed'" not in code
        assert "== 'aborted'" not in code
