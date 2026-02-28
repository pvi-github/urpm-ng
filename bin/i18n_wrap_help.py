#!/usr/bin/env python3
"""Wrap argparse help= and description= strings with _() for i18n.

Usage:
    python3 bin/i18n_wrap_help.py urpm/cli/main.py [--dry-run]

This script:
  - Wraps help='...' / help="..." with help=_('...') / help=_("...")
  - Wraps description='...' / description="..." / description='''...''' with _()
  - Wraps epilog='...' / epilog="..." with _()
  - Skips strings already wrapped with _()
  - Skips non-string values (e.g., help=argparse.SUPPRESS)

Review the diff carefully before committing!
"""

import re
import sys


def wrap_help_strings(content: str) -> str:
    """Wrap help=, description=, epilog= string arguments with _()."""

    # Triple-quoted FIRST (before single/double, to avoid partial matches)

    # Pattern for triple-single-quoted help='''...'''
    content = re.sub(
        r"""\b(help|description|epilog)=(?!_\()('''[\s\S]*?''')""",
        r'\1=_(\2)',
        content
    )

    # Pattern for triple-double-quoted help=\"\"\"...\"\"\"
    content = re.sub(
        r'''\b(help|description|epilog)=(?!_\()("""[\s\S]*?""")''',
        r'\1=_(\2)',
        content
    )

    # Single-quoted help='...' (negative lookahead (?!') to skip triple-quotes)
    content = re.sub(
        r"""\b(help|description|epilog)=(?!_\()('(?!')(?:[^'\\]|\\.)*')""",
        r'\1=_(\2)',
        content
    )

    # Double-quoted help="..." (negative lookahead (?!") to skip triple-quotes)
    content = re.sub(
        r'''\b(help|description|epilog)=(?!_\()("(?!")(?:[^"\\]|\\.)*")''',
        r'\1=_(\2)',
        content
    )

    return content


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file.py> [--dry-run]")
        sys.exit(1)

    filepath = sys.argv[1]
    dry_run = '--dry-run' in sys.argv

    with open(filepath, 'r') as f:
        original = f.read()

    modified = wrap_help_strings(original)

    if dry_run:
        # Show diff-like output
        orig_lines = original.splitlines()
        mod_lines = modified.splitlines()
        changes = 0
        for i, (o, m) in enumerate(zip(orig_lines, mod_lines), 1):
            if o != m:
                changes += 1
                print(f"L{i}:")
                print(f"  - {o.strip()}")
                print(f"  + {m.strip()}")
                print()
        print(f"Total: {changes} lines changed")
    else:
        with open(filepath, 'w') as f:
            f.write(modified)
        # Count changes
        changes = sum(1 for o, m in zip(original.splitlines(), modified.splitlines()) if o != m)
        print(f"Done: {changes} lines modified in {filepath}")


if __name__ == '__main__':
    main()
