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

# ── Colours ───────────────────────────────────────────────────────────
# Only wire ANSI escapes when stdout is a real terminal; piping to a
# file, less, or a CI log stays plain-text.
if [[ -t 1 ]]; then
  _blue=$'\033[0;34m'; _green=$'\033[0;32m'
  _yellow=$'\033[0;33m'; _red=$'\033[0;31m'
  _bold=$'\033[1m';    _reset=$'\033[0m'
else
  _blue='' _green='' _yellow='' _red='' _bold='' _reset=''
fi

log()  { printf '%s==>%s %s\n' "$_blue"   "$_reset" "$*"; }
ok()   { printf '%s==>%s %s\n' "$_green"  "$_reset" "$*"; }
warn() { printf '%s==>%s %s\n' "$_yellow" "$_reset" "$*" >&2; }
die()  { printf '%sxx%s %s\n'  "$_red"    "$_reset" "$*" >&2; exit 1; }

# ── Args ──────────────────────────────────────────────────────────────
YES=0
CHANNEL=""
for arg; do
  case "$arg" in
    -y|--yes)       YES=1 ;;
    --channel=*)    CHANNEL="${arg#*=}" ;;
    -h|--help)      sed -n '2,/^set -eu/p' "$0" | sed 's/^# \{0,1\}//;/^set -eu/d'; exit 0 ;;
    *)              die "unknown option: $arg" ;;
  esac
done

# Piped from curl: our own stdin IS the pipe.  ``su -c'' would then
# read the root password from an empty pipe (auth error), and any
# ``read -p'' would either block or eat a stray byte.  Fix by reading
# every prompt from /dev/tty explicitly, and by redirecting su's
# stdin to /dev/tty at the call site.  Fd 0 is left untouched so
# ``bash -s'' can keep streaming the script from the curl pipe.
if [[ -r /dev/tty && -w /dev/tty ]]; then
  TTY_OK=1
else
  TTY_OK=0
fi

# Prompt for the channel whenever ``--channel'' was not given AND a
# terminal is available.  ``-y'' skips the "Proceed?" confirmation
# later, not this choice -- the user still gets to decide which
# repository they trust.  Without a terminal (headless pipe, cron)
# fall back to mgabiz (signed builds).
if [[ -z "$CHANNEL" ]]; then
  if [[ $TTY_OK -eq 1 ]]; then
    cat >&2 <<'EOF'
Where do you want to fetch urpm-ng from?
  1) mgabiz  — signed builds hosted on www.mageia.biz (recommended)
  2) github  — release RPMs from github.com/pvi-github/urpm-ng
EOF
    read -r -p "Choice [1]: " reply </dev/tty
    case "${reply:-1}" in
      1|mgabiz) CHANNEL=mgabiz ;;
      2|github) CHANNEL=github ;;
      *) die "unknown choice: $reply" ;;
    esac
  else
    CHANNEL=mgabiz
  fi
fi

case "$CHANNEL" in
  mgabiz|github) ;;
  *) die "--channel must be mgabiz or github (got: $CHANNEL)" ;;
esac

# ── Detection ─────────────────────────────────────────────────────────
MGAVER=$(sed -n 's/^Mageia release \([0-9]*\).*/\1/p' /etc/mageia-release 2>/dev/null)
[[ -n "$MGAVER" ]] || die "not on Mageia (no /etc/mageia-release)"
ARCH=$(uname -m)
# ``rpm -q`` returns non-zero if the package is absent — locale-safe,
# unlike parsing the "not installed" message which is translated.
INSTALLED=""
if rpm -q urpm-ng-core >/dev/null 2>&1; then
  INSTALLED=$(rpm -q --qf '%{version}-%{release}' urpm-ng-core)
fi

log "Mageia $MGAVER, $ARCH, channel=$CHANNEL"
[[ -n "$INSTALLED" ]] && log "urpm-ng-core installed: $INSTALLED" \
                      || log "urpm-ng not installed"

# ── Confirmation helper ───────────────────────────────────────────────
confirm() {
  [[ $YES -eq 1 ]] && return 0
  [[ $TTY_OK -eq 1 ]] || die "no TTY; re-run with -y to skip prompts"
  local r; read -r -p "Proceed? [Y/n] " r </dev/tty
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
    log "mgabiz media already configured -- just upgrading."
    confirm
    su -c "urpm u --auto urpm-ng-all rpmdrake-ng" </dev/tty
    cat <<'HINT'

==================================================
  Next time, this is all you need:

      urpm u -y urpm-ng-all rpmdrake-ng

==================================================
HINT
    exit 0
  fi

  log "Fetching pubkey..."
  curl -fsSL -o "$WORKDIR/pubkey" "$MEDIA/media_info/pubkey"

  if [[ -z "$INSTALLED" ]]; then
    CORE=$(curl -fsSL "$MEDIA/urpm/release/" \
           | grep -oE 'href="urpm-ng-core-[0-9][^"]*\.rpm"' \
           | sed -E 's/^href="([^"]+)"$/\1/' \
           | sort -V -u | tail -1)
    [[ -n "$CORE" ]] || die "no urpm-ng-core RPM at $MEDIA/urpm/release/"
    log "Downloading $CORE..."
    (cd "$WORKDIR" && curl -fsSL -O "$MEDIA/urpm/release/$CORE")
    log "About to run as root (single su prompt):"
    cat <<EOF
    rpm --import $WORKDIR/pubkey
    urpmi --auto $WORKDIR/$CORE
    urpm media discover $MEDIA/
    urpm m u
    urpm i --auto urpm-ng-all rpmdrake-ng
EOF
    confirm
    # ``|| true'' on ``urpm m u'' tolerates partial refresh failures
    # (a broken sibling media inherited from an old urpmi.cfg would
    # otherwise abort the install via ``set -e'').
    su -c "set -e
rpm --import '$WORKDIR/pubkey'
urpmi --auto '$WORKDIR/$CORE'
urpm media discover '$MEDIA/'
urpm m u || true
urpm i --auto urpm-ng-all rpmdrake-ng
" </dev/tty
  else
    log "urpm-ng-core present but no capable media -- adding mgabiz."
    log "About to run as root (single su prompt):"
    cat <<EOF
    rpm --import $WORKDIR/pubkey
    urpm media discover $MEDIA/
    urpm m u
    urpm i --auto urpm-ng-all rpmdrake-ng
EOF
    confirm
    # ``|| true'' on ``urpm m u'' tolerates partial refresh failures.
    su -c "set -e
rpm --import '$WORKDIR/pubkey'
urpm media discover '$MEDIA/'
urpm m u || true
urpm i --auto urpm-ng-all rpmdrake-ng
" </dev/tty
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
  [[ -n "$TAG" ]] || die "could not resolve latest tag on GitHub"
  log "Latest github release: $TAG"

  URLS=$(curl -fsSL "$API/releases/tags/$TAG" \
         | sed -n 's/^ *"browser_download_url": *"\([^"]*\)".*/\1/p' \
         | grep '\.rpm$' \
         | grep -v '\.src\.rpm$' \
         | grep -v -e '-debuginfo-' -e '-debugsource-' \
         | grep "\.mga${MGAVER}\." \
         | grep -E "\.(${ARCH}|noarch)\.rpm$")
  N=$(printf '%s\n' "$URLS" | grep -c .)
  [[ $N -gt 0 ]] || die "no matching RPMs at $TAG for mga${MGAVER}/${ARCH}"
  log "$N RPM(s) will be downloaded:"
  printf '%s\n' "$URLS" | sed 's|^.*/|    |'
  confirm

  (cd "$WORKDIR" && printf '%s\n' "$URLS" | while read -r u; do
     [[ -n "$u" ]] && curl -fsSL -O "$u"
   done)

  if [[ -z "$INSTALLED" ]]; then
    log "Bootstrapping via urpmi (single su prompt)..."
    # ``|| true'' on ``urpm m u'' tolerates partial refresh failures.
    su -c "set -e
urpmi --auto $WORKDIR/*.rpm
urpm m u || true
" </dev/tty
  else
    METAS=$(find "$WORKDIR" -maxdepth 1 -type f \
              \( -name 'urpm-ng-all-*.rpm' -o -name 'rpmdrake-ng-*.rpm' \))
    [[ -n "$METAS" ]] || die "no meta RPMs in $WORKDIR"
    log "Reinstalling meta packages (single su prompt)..."
    # ``|| true'' on ``urpm m u'' tolerates partial refresh failures.
    su -c "set -e
urpm i --auto --reinstall $METAS
urpm m u || true
" </dev/tty
  fi
fi

ok "urpm-ng $(rpm -q --qf '%{version}-%{release}' urpm-ng-core) is installed."
