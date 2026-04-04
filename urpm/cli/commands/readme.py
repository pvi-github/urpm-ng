"""Display README messages from past transactions."""

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from ...i18n import _

if TYPE_CHECKING:
    from ...core.database import PackageDatabase


def _get_readmes(db: "PackageDatabase", transaction_id: int = None,
                 list_only: bool = False) -> list[dict]:
    """Fetch README entries from the database.

    Args:
        db: Package database instance.
        transaction_id: If set, fetch READMEs for this specific transaction.
            Otherwise fetch from the most recent transaction that has READMEs.
        list_only: If True, return minimal data (transaction IDs only).

    Returns:
        List of dicts with keys: transaction_id, package_name, content,
        created_at.
    """
    conn = db._get_connection()

    if list_only:
        cursor = conn.execute("""
            SELECT DISTINCT tr.transaction_id, h.timestamp, h.action,
                   COUNT(*) as readme_count
            FROM transaction_readmes tr
            JOIN history h ON h.id = tr.transaction_id
            GROUP BY tr.transaction_id
            ORDER BY tr.transaction_id DESC
            LIMIT 20
        """)
        return [
            {'transaction_id': row[0], 'timestamp': row[1],
             'action': row[2], 'count': row[3]}
            for row in cursor.fetchall()
        ]

    if transaction_id is not None:
        cursor = conn.execute("""
            SELECT transaction_id, package_name, content, created_at
            FROM transaction_readmes
            WHERE transaction_id = ?
            ORDER BY id
        """, (transaction_id,))
    else:
        # Most recent transaction with READMEs
        cursor = conn.execute("""
            SELECT transaction_id, package_name, content, created_at
            FROM transaction_readmes
            WHERE transaction_id = (
                SELECT MAX(transaction_id) FROM transaction_readmes
            )
            ORDER BY id
        """)

    return [
        {'transaction_id': row[0], 'package_name': row[1],
         'content': row[2], 'created_at': row[3]}
        for row in cursor.fetchall()
    ]


def _show_in_pager(text: str) -> None:
    """Display text in a pager (less) if terminal is interactive."""
    if not sys.stdout.isatty():
        print(text)
        return
    pager = os.environ.get('PAGER', 'less')
    try:
        proc = subprocess.Popen([pager], stdin=subprocess.PIPE)
        proc.communicate(input=text.encode('utf-8', errors='replace'))
    except (FileNotFoundError, BrokenPipeError):
        print(text)


def cmd_readme(args, db: "PackageDatabase") -> int:
    """Entry point for ``urpm readme``."""
    list_mode = getattr(args, 'list', False)
    txn_id = getattr(args, 'transaction', None)

    if list_mode:
        entries = _get_readmes(db, list_only=True)
        if not entries:
            print(_("No README messages stored."))
            return 0
        print(_("Transactions with README messages:"))
        print()
        for e in entries:
            from datetime import datetime
            ts = datetime.fromtimestamp(e['timestamp']).strftime('%Y-%m-%d %H:%M')
            print(f"  #{e['transaction_id']:>5}  {ts}  {e['action']:<10}  "
                  f"({e['count']} README(s))")
        return 0

    readmes = _get_readmes(db, transaction_id=txn_id)
    if not readmes:
        if txn_id:
            print(_("No README messages for transaction #%d.") % txn_id)
        else:
            print(_("No README messages from recent transactions."))
        return 0

    # Build display text
    lines = []
    tid = readmes[0]['transaction_id']
    lines.append(_("README messages from transaction #%d") % tid)
    lines.append("=" * 60)

    for r in readmes:
        lines.append("")
        lines.append(f"── {r['package_name']} ──")
        lines.append("")
        lines.append(r['content'])

    text = "\n".join(lines)

    if sys.stdout.isatty() and not getattr(args, 'no_pager', False):
        _show_in_pager(text)
    else:
        print(text)

    return 0
