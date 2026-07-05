#!/bin/bash
# geturpm.sh — install or upgrade urpm-ng on Mageia.
#
# Options:
#   -y, --yes                 skip confirmation prompts
#   --channel=mgabiz|github   where to fetch urpm-ng from (default: mgabiz)
#   -h, --help                show this header and exit
#
# Non-privileged work (fetch, download) stays user-side. One su -c
# per code path so the root password is asked at most once.

set -euo pipefail

# ── Args ──────────────────────────────────────────────────────────────
YES=0
CHANNEL=""
for arg; do
  case "$arg" in
    -y|--yes)       YES=1 ;;
    --channel=*)    CHANNEL="${arg#*=}" ;;
    -h|--help)      sed -n '2,/^set -eu/p' "$0" | sed 's/^# \{0,1\}//;/^set -eu/d'; exit 0 ;;
    *)              echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

# When piped from curl, our stdin IS the pipe -- ``su -c'' cannot then
# read its password prompt, ``read -p'' cannot prompt the user, and a
# stray byte on the pipe would silently answer for us.  Reopen stdin
# on the controlling terminal whenever one is available.  Bash has
# already finished reading the script from the original stdin by this
# point, so redirecting fd 0 here does not truncate the rest of the
# script.
[[ -r /dev/tty ]] && exec </dev/tty

# Prompt for the channel whenever ``--channel'' was not given AND we
# have a terminal.  ``-y'' skips the "Proceed?" confirmation later,
# not this choice -- the user still gets to decide which repository
# they trust.  If there is no terminal (headless pipe, cron), we fall
# back to mgabiz (signed builds).
if [[ -z "$CHANNEL" ]]; then
  if [[ -t 0 ]]; then
    cat >&2 <<'EOF'
Where do you want to fetch urpm-ng from?
  1) mgabiz  — signed builds hosted on www.mageia.biz (recommended)
  2) github  — release RPMs from github.com/pvi-github/urpm-ng
EOF
    read -r -p "Choice [1]: " reply
    case "${reply:-1}" in
      1|mgabiz) CHANNEL=mgabiz ;;
      2|github) CHANNEL=github ;;
      *) echo "unknown choice: $reply" >&2; exit 1 ;;
    esac
  else
    CHANNEL=mgabiz
  fi
fi

case "$CHANNEL" in
  mgabiz|github) ;;
  *) echo "--channel must be mgabiz or github (got: $CHANNEL)" >&2; exit 1 ;;
esac

# ── Detection ─────────────────────────────────────────────────────────
MGAVER=$(sed -n 's/^Mageia release \([0-9]*\).*/\1/p' /etc/mageia-release 2>/dev/null)
[[ -n "$MGAVER" ]] || { echo "not on Mageia (no /etc/mageia-release)" >&2; exit 1; }
ARCH=$(uname -m)
# ``rpm -q`` returns non-zero if the package is absent — locale-safe,
# unlike parsing the "not installed" message which is translated.
INSTALLED=""
if rpm -q urpm-ng-core >/dev/null 2>&1; then
  INSTALLED=$(rpm -q --qf '%{version}-%{release}' urpm-ng-core)
fi

echo "==> Mageia $MGAVER, $ARCH, channel=$CHANNEL"
[[ -n "$INSTALLED" ]] && echo "==> urpm-ng-core installed: $INSTALLED" \
                      || echo "==> urpm-ng not installed"

# ── Confirmation helper ───────────────────────────────────────────────
confirm() {
  [[ $YES -eq 1 ]] && return 0
  [[ -t 0 ]] || { echo "no TTY; re-run with -y to skip prompts" >&2; exit 1; }
  local r; read -r -p "Proceed? [Y/n] " r
  case "$r" in ''|y|Y|yes|Yes|o|O|oui|Oui) return 0 ;; *) exit 1 ;; esac
}

WORKDIR=$(mktemp -d -t urpm-ng-get.XXXX)
trap 'rm -rf "$WORKDIR"' EXIT

# ── mgabiz channel ────────────────────────────────────────────────────
if [[ "$CHANNEL" == "mgabiz" ]]; then
  MEDIA="https://www.mageia.biz/repo/Mageia/mgabiz/$MGAVER/$ARCH/media"

  # Fast path: urpm-ng-core installed and a media offering urpm-ng-all
  # is already configured -- just upgrade, then remind the user they
  # don't need this script next time.
  if [[ -n "$INSTALLED" ]] && urpm q urpm-ng-all >/dev/null 2>&1; then
    echo "==> mgabiz media already configured -- just upgrading."
    confirm
    su -c "urpm u --auto urpm-ng-all rpmdrake-ng"
    cat <<'HINT'

==================================================
  Next time, this is all you need:

      urpm u -y urpm-ng-all rpmdrake-ng

==================================================
HINT
    exit 0
  fi

  echo "==> Fetching pubkey..."
  curl -fsSL -o "$WORKDIR/pubkey" "$MEDIA/media_info/pubkey"

  if [[ -z "$INSTALLED" ]]; then
    CORE=$(curl -fsSL "$MEDIA/urpm/release/" \
           | grep -oE 'href="urpm-ng-core-[0-9][^"]*\.rpm"' \
           | sed -E 's/^href="([^"]+)"$/\1/' \
           | sort -V -u | tail -1)
    [[ -n "$CORE" ]] || { echo "no urpm-ng-core RPM at $MEDIA/urpm/release/" >&2; exit 1; }
    echo "==> Downloading $CORE..."
    (cd "$WORKDIR" && curl -fsSL -O "$MEDIA/urpm/release/$CORE")
    echo "==> About to run as root (single su prompt):"
    cat <<EOF
    rpm --import $WORKDIR/pubkey
    urpmi --auto $WORKDIR/$CORE
    urpm media discover $MEDIA/
    urpm m u
    urpm i --auto urpm-ng-all rpmdrake-ng
EOF
    confirm
    su -c "set -e
rpm --import '$WORKDIR/pubkey'
urpmi --auto '$WORKDIR/$CORE'
urpm media discover '$MEDIA/'
urpm m u || true    # tolerate partial refresh failures (broken sibling media)
urpm i --auto urpm-ng-all rpmdrake-ng"
  else
    echo "==> urpm-ng-core present but no capable media -- adding mgabiz."
    echo "==> About to run as root (single su prompt):"
    cat <<EOF
    rpm --import $WORKDIR/pubkey
    urpm media discover $MEDIA/
    urpm m u
    urpm i --auto urpm-ng-all rpmdrake-ng
EOF
    confirm
    su -c "set -e
rpm --import '$WORKDIR/pubkey'
urpm media discover '$MEDIA/'
urpm m u || true    # tolerate partial refresh failures (broken sibling media)
urpm i --auto urpm-ng-all rpmdrake-ng"
  fi

# ── github channel ────────────────────────────────────────────────────
else
  API="https://api.github.com/repos/pvi-github/urpm-ng"
  # /releases/latest 404s when every release is marked as prerelease.
  # Drop -f (fail on 4xx): a 404 body then feeds sed harmlessly and
  # TAG stays empty, so the /releases fallback picks it up.
  TAG=$(curl -sSL "$API/releases/latest" 2>/dev/null \
        | sed -n 's/^  "tag_name": *"\([^"]*\)".*/\1/p')
  if [[ -z "$TAG" ]]; then
    TAG=$(curl -fsSL "$API/releases" \
          | sed -n 's/^    "tag_name": *"\([^"]*\)".*/\1/p' \
          | head -1)
  fi
  [[ -n "$TAG" ]] || { echo "could not resolve latest tag on GitHub" >&2; exit 1; }
  echo "==> Latest github release: $TAG"

  URLS=$(curl -fsSL "$API/releases/tags/$TAG" \
         | sed -n 's/^ *"browser_download_url": *"\([^"]*\)".*/\1/p' \
         | grep '\.rpm$' \
         | grep -v '\.src\.rpm$' \
         | grep -v -e '-debuginfo-' -e '-debugsource-' \
         | grep "\.mga${MGAVER}\." \
         | grep -E "\.(${ARCH}|noarch)\.rpm$")
  N=$(printf '%s\n' "$URLS" | grep -c .)
  [[ $N -gt 0 ]] || { echo "no matching RPMs at $TAG for mga${MGAVER}/${ARCH}" >&2; exit 1; }
  echo "==> $N RPM(s) will be downloaded:"
  printf '%s\n' "$URLS" | sed 's|^.*/|    |'
  confirm

  (cd "$WORKDIR" && printf '%s\n' "$URLS" | while read -r u; do
     [[ -n "$u" ]] && curl -fsSL -O "$u"
   done)

  if [[ -z "$INSTALLED" ]]; then
    echo "==> Bootstrapping via urpmi (single su prompt)..."
    su -c "set -e
urpmi --auto $WORKDIR/*.rpm
urpm m u || true    # tolerate partial refresh failures"
  else
    METAS=$(find "$WORKDIR" -maxdepth 1 -type f \
              \( -name 'urpm-ng-all-*.rpm' -o -name 'rpmdrake-ng-*.rpm' \))
    [[ -n "$METAS" ]] || { echo "no meta RPMs in $WORKDIR" >&2; exit 1; }
    echo "==> Reinstalling meta packages (single su prompt)..."
    su -c "set -e
urpm i --auto --reinstall $METAS
urpm m u || true    # tolerate partial refresh failures"
  fi
fi

echo "==> urpm-ng $(rpm -q --qf '%{version}-%{release}' urpm-ng-core) is installed."
