"""
CLI command: urpm genmedia

Generates media metadata (hdlist, synthesis, XML info, AppStream, MD5SUM)
from a directory of RPM packages.
"""

import logging
import sys
from pathlib import Path

from ...i18n import _

logger = logging.getLogger(__name__)


def cmd_genmedia(args, db=None):
    """Execute ``urpm genmedia <rpms_dir>``."""
    try:
        from urpm.genmedia import MediaGenerator
    except ImportError:
        print(_("Error: urpm-ng-genmedia is not installed."), file=sys.stderr)
        print(_("Install it with: sudo urpm install urpm-ng-genmedia"), file=sys.stderr)
        return 1

    rpms_dir = Path(args.rpms_dir)
    if not rpms_dir.is_dir():
        print(_("Error: {path} is not a directory.").format(path=rpms_dir),
              file=sys.stderr)
        return 1

    media_info_dir = Path(args.media_info_dir) if args.media_info_dir else None

    gen = MediaGenerator(
        rpms_dir=rpms_dir,
        media_info_dir=media_info_dir,
        lock=not args.nolock,
        verbose=args.verbose,
        no_bad_rpm=args.no_bad_rpm,
    )

    result = gen.generate(
        hdlist=not args.no_hdlist,
        synthesis=True,
        xml_info=args.xml_info,
        appstream=args.appstream_info,
        md5sum=not args.no_md5sum,
        incremental=not args.clean,
        hdlist_filter=args.hdlist_filter,
        synthesis_filter=args.synthesis_filter,
        xml_info_filter=args.xml_info_filter,
        versioned=args.versioned,
        allow_empty=args.allow_empty_media,
    )

    if not result.success:
        for err in result.errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    if args.verbose:
        print(_("{count} packages indexed.").format(count=result.packages_count))

    return 0
