#!/bin/bash
#
# geturpm.sh — one-shot installer for urpm-ng on Mageia
#
# USAGE (two patterns; the second is recommended when you don't
# already trust the source):
#
#   # Quick — piped to bash, no local inspection
#   curl -fsSL https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh | URPM_YES=1 bash
#
#   # Verified — download, inspect, then run
#   curl -fsSLO https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh
#   less geturpm.sh                       # inspect it
#   bash geturpm.sh
#
# WHAT IT DOES
#   1. Detects your Mageia release and architecture.
#   2. Pulls urpm-ng from the channel you chose (interactive prompt
#      by default, or via ``URPM_CHANNEL``).
#   3. Installs it — bootstraps with urpmi on a fresh box, uses
#      urpm itself on an upgrade.
#   4. Points you at the first-run commands.
#
# OPTIONS (environment variables)
#   URPM_CHANNEL=mgabiz|github  Download channel.  Default: prompt
#                               interactively; ``mgabiz`` if piped.
#                               ``gitlab`` and ``codeberg`` are
#                               planned but not yet available.
#   URPM_YES=1                  Skip every confirmation prompt.
#                               REQUIRED when this script is piped
#                               from curl (no TTY on stdin).
#   URPM_KEEP=1                 Do not delete the download directory
#                               on success (useful for debugging).
#

set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────
readonly GH_OWNER="pvi-github"
readonly GH_REPO="urpm-ng"
readonly GH_API="https://api.github.com/repos/${GH_OWNER}/${GH_REPO}"

# Trailing slash matters — this is the media root (with
# ``media_info/`` and the RPMs sitting inside).
readonly MGABIZ_ROOT_TMPL="https://www.mageia.biz/repo/Mageia/mgabiz/%s/%s/media/"

# ── Colours (only when stdout is a real terminal) ──────────────────────
if [[ -t 1 ]]; then
  _c_reset=$'\033[0m'
  _c_red=$'\033[0;31m'
  _c_green=$'\033[0;32m'
  _c_yellow=$'\033[0;33m'
  _c_blue=$'\033[0;34m'
  _c_bold=$'\033[1m'
else
  _c_reset='' _c_red='' _c_green='' _c_yellow='' _c_blue='' _c_bold=''
fi

log()  { printf '%s==>%s %s\n' "$_c_blue"   "$_c_reset" "$*"; }
ok()   { printf '%s==>%s %s\n' "$_c_green"  "$_c_reset" "$*"; }
warn() { printf '%s==>%s %s\n' "$_c_yellow" "$_c_reset" "$*" >&2; }
die()  { printf '%sxx%s  %s\n' "$_c_red"    "$_c_reset" "$*" >&2; exit 1; }

# ── Cleanup ────────────────────────────────────────────────────────────
WORKDIR=""
cleanup() {
  local rc=$?
  if [[ -n "$WORKDIR" && -d "$WORKDIR" ]]; then
    if [[ "${URPM_KEEP:-0}" == "1" ]]; then
      warn "Keeping $WORKDIR (URPM_KEEP=1)"
    else
      rm -rf "$WORKDIR"
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

# ── Detection ──────────────────────────────────────────────────────────
detect_mageia_release() {
  local rel
  rel=$(rpm -q --qf '%{version}' mageia-release-Default 2>/dev/null \
        | cut -d. -f1) || true
  if [[ -z "$rel" && -r /etc/mageia-release ]]; then
    rel=$(sed -n 's/^Mageia release \([0-9]*\).*/\1/p' /etc/mageia-release)
  fi
  [[ -n "$rel" ]] || die "Could not detect your Mageia release. Is /etc/mageia-release present?"
  printf '%s\n' "$rel"
}

detect_arch() { uname -m; }

detect_installed_urpm_ng() {
  # Empty output when urpm-ng-core is not installed.
  # NOTE: use rpm's exit code, not string matching -- the "package
  # not installed" message is localised (fr: "le paquet ... n'est
  # pas installé", de: "Paket ... ist nicht installiert", ...), so
  # grepping an English fragment would misclassify every non-English
  # box as "already installed".
  rpm -q urpm-ng-core >/dev/null 2>&1 || return 0
  rpm -q --qf '%{version}-%{release}' urpm-ng-core 2>/dev/null
}

# ``urpm q PKG`` searches configured media catalogues for available
# packages.  If it returns any hit for ``urpm-ng-all``, a media
# offering the meta package is configured and we can just
# ``urpm i`` / ``urpm u`` without re-adding anything.
have_urpm_ng_capable_media() {
  command -v urpm >/dev/null 2>&1 || return 1
  urpm q urpm-ng-all 2>/dev/null | grep -q .
}

# ── Confirmation helpers ───────────────────────────────────────────────
require_tty_or_yes() {
  if [[ "${URPM_YES:-0}" == "1" ]]; then return 0; fi
  if [[ ! -t 0 ]]; then
    die "stdin is not a terminal (script piped from curl?). Re-run with URPM_YES=1 to skip confirmation prompts."
  fi
}

confirm_or_die() {
  local prompt="${1:-Proceed?}"
  if [[ "${URPM_YES:-0}" == "1" ]]; then return 0; fi
  local reply
  read -r -p "$prompt [Y/n] " reply
  case "$reply" in
    ''|y|Y|yes|Yes|YES|o|O|oui|Oui|OUI) return 0 ;;
    *) die "Aborted by user." ;;
  esac
}

# ── Channel selection ──────────────────────────────────────────────────
prompt_channel() {
  # Called only when URPM_CHANNEL is unset and stdin is a TTY.
  cat >&2 <<'EOF'

Where would you like to get urpm-ng from?
  1) www.mageia.biz — signed builds (recommended)
  2) github         — releases from github.com/pvi-github/urpm-ng
     (gitlab and codeberg mirrors are planned)

EOF
  local reply
  read -r -p "Choice [1]: " reply
  case "${reply:-1}" in
    1|mgabiz|mageia.biz|www.mageia.biz) printf 'mgabiz\n' ;;
    2|github|gh)                        printf 'github\n' ;;
    *) die "Unknown choice: $reply" ;;
  esac
}

resolve_channel() {
  local ch="${URPM_CHANNEL:-}"
  if [[ -z "$ch" ]]; then
    if [[ -t 0 && "${URPM_YES:-0}" != "1" ]]; then
      ch=$(prompt_channel)
    else
      ch="mgabiz"
    fi
  fi
  case "$ch" in
    mgabiz|github) printf '%s\n' "$ch" ;;
    gitlab|codeberg)
      die "URPM_CHANNEL=$ch is planned but not yet available. Use mgabiz (default) or github."
      ;;
    *)
      die "Unknown URPM_CHANNEL=$ch (accepted: mgabiz|github; gitlab and codeberg are planned)."
      ;;
  esac
}

# ── mgabiz channel ─────────────────────────────────────────────────────
mgabiz_url() {
  printf "$MGABIZ_ROOT_TMPL\n" "$1" "$2"
}

mgabiz_pubkey_url() {
  printf '%smedia_info/pubkey\n' "$1"
}

mgabiz_pick_latest_core_rpm() {
  # Scrape the Apache/nginx auto-index in the urpm sub-media
  # (RPMs live one level below the catalogue root, per the
  # ``[urpm/release]`` section of media_info/media.cfg).
  # ``sort -V`` handles version-release ordering natively and
  # picks the highest ``-N.mgaXX`` release too.
  local media_url="$1"
  local rpm_dir="${media_url}urpm/release/"
  curl -fsSL "$rpm_dir" \
    | grep -oE 'href="urpm-ng-core-[0-9][^"]*\.rpm"' \
    | sed -E 's/^href="([^"]+)"$/\1/' \
    | sort -V -u \
    | tail -n1
}

mgabiz_install() {
  local mgaver="$1" arch="$2" installed="$3"
  local media_url pubkey_url pubkey_file core_rpm core_url

  media_url=$(mgabiz_url "$mgaver" "$arch")
  pubkey_url=$(mgabiz_pubkey_url "$media_url")
  log "Media URL : ${_c_bold}${media_url}${_c_reset}"

  # Fast-path: urpm-ng already installed AND a media offering
  # urpm-ng-all is already configured -> just upgrade.  This is
  # the ``routine reinstall / periodic update`` case.
  if [[ -n "$installed" ]] && have_urpm_ng_capable_media; then
    log "urpm-ng ${installed} present and a capable media is configured."
    printf '\n%sPlanned:%s\n' "$_c_bold" "$_c_reset"
    printf '  su -c "urpm m u"\n'
    printf '  su -c "urpm u --auto urpm-ng-all rpmdrake-ng"\n\n'
    confirm_or_die
    su -c "urpm m u"
    su -c "urpm u --auto urpm-ng-all rpmdrake-ng"
    return 0
  fi

  # Otherwise: need to bootstrap and/or add the media.
  log "Fetching the mgabiz pubkey..."
  WORKDIR=$(mktemp -d --suffix=-urpm-ng-get)
  pubkey_file="$WORKDIR/pubkey"
  curl -fsSL -o "$pubkey_file" "$pubkey_url" \
    || die "Failed to download pubkey from $pubkey_url"

  # If urpm-ng is absent, bootstrap by downloading urpm-ng-core
  # directly from the media (no urpm to talk to that media yet) and
  # installing it via urpmi.
  if [[ -z "$installed" ]]; then
    log "urpm-ng not installed — bootstrapping from mgabiz."
    core_rpm=$(mgabiz_pick_latest_core_rpm "$media_url")
    [[ -n "$core_rpm" ]] || die "Could not find any urpm-ng-core RPM at $media_url"
    core_url="${media_url}urpm/release/${core_rpm}"
    log "Latest urpm-ng-core : ${_c_bold}${core_rpm}${_c_reset}"

    printf '\n%sPlanned:%s\n' "$_c_bold" "$_c_reset"
    printf '  1. su -c "rpm --import %s"\n' "$pubkey_file"
    printf '  2. curl -O %s          (into %s)\n' "$core_url" "$WORKDIR"
    printf '  3. su -c "urpmi --auto %s/%s"\n' "$WORKDIR" "$core_rpm"
    printf '  4. su -c "urpm media discover %s"\n' "$media_url"
    printf '  5. su -c "urpm m u"\n'
    printf '  6. su -c "urpm i --auto urpm-ng-all rpmdrake-ng"\n\n'
    confirm_or_die

    su -c "rpm --import $pubkey_file"
    log "Downloading $core_rpm..."
    (cd "$WORKDIR" && curl -fsSL -O "$core_url")
    su -c "urpmi --auto $WORKDIR/$core_rpm"
    su -c "urpm media discover $media_url"
    su -c "urpm m u"
    su -c "urpm i --auto urpm-ng-all rpmdrake-ng"
    return 0
  fi

  # urpm-ng is installed but no capable media -> add mgabiz and
  # install the meta packages from it.
  log "urpm-ng ${installed} present but mgabiz media not configured."
  printf '\n%sPlanned:%s\n' "$_c_bold" "$_c_reset"
  printf '  1. su -c "rpm --import %s"\n' "$pubkey_file"
  printf '  2. su -c "urpm media discover %s"\n' "$media_url"
  printf '  3. su -c "urpm m u"\n'
  printf '  4. su -c "urpm i --auto urpm-ng-all rpmdrake-ng"\n\n'
  confirm_or_die

  su -c "rpm --import $pubkey_file"
  su -c "urpm media discover $media_url"
  su -c "urpm m u"
  su -c "urpm i --auto urpm-ng-all rpmdrake-ng"
}

# ── github channel ─────────────────────────────────────────────────────
gh_latest_tag() {
  local tag
  tag=$(curl -fsSL "${GH_API}/releases/latest" 2>/dev/null \
        | sed -n 's/^  "tag_name": *"\([^"]*\)".*/\1/p') || true
  if [[ -z "$tag" ]]; then
    tag=$(curl -fsSL "${GH_API}/releases" \
          | grep -m1 '"tag_name"' | cut -d'"' -f4)
  fi
  [[ -n "$tag" ]] || die "Could not determine the latest release tag on GitHub."
  printf '%s\n' "$tag"
}

gh_release_rpm_urls() {
  local tag="$1" mgaver="$2" arch="$3"
  curl -fsSL "${GH_API}/releases/tags/${tag}" \
    | sed -n 's/^ *"browser_download_url": *"\([^"]*\)".*/\1/p' \
    | grep '\.rpm$' \
    | grep -v '\.src\.rpm$' \
    | grep -v -e '-debuginfo-' -e '-debugsource-' \
    | grep "\.mga${mgaver}\." \
    | grep -E "\.(${arch}|noarch)\.rpm$"
}

github_install() {
  local mgaver="$1" arch="$2" installed="$3"
  local tag urls url_count target_vr meta_url

  log "Querying github for the latest release..."
  tag=$(gh_latest_tag)
  log "Latest release : ${_c_bold}${tag}${_c_reset}"

  log "Resolving RPM URLs for Mageia ${mgaver} / ${arch}..."
  urls=$(gh_release_rpm_urls "$tag" "$mgaver" "$arch")
  url_count=$(printf '%s\n' "$urls" | grep -c . || true)
  [[ -n "$urls" && "$url_count" -gt 0 ]] \
    || die "No RPMs found for Mageia ${mgaver} / ${arch} at github tag ${tag}."

  # Extract VERSION-RELEASE from the meta URL for the idempotency
  # check.  Full VERSION-RELEASE — a mere release bump is still an
  # upgrade the user should get.
  meta_url=$(printf '%s\n' "$urls" | grep -m1 '/urpm-ng-all-' || true)
  if [[ -n "$meta_url" ]]; then
    target_vr=$(basename "$meta_url" \
                | sed -E 's/^urpm-ng-all-(.+)\.(noarch|[^.]+)\.rpm$/\1/')
    log "Release contents : urpm-ng ${_c_bold}${target_vr}${_c_reset}"
    if [[ -n "$installed" && "$installed" == "$target_vr" ]]; then
      ok "urpm-ng ${installed} is already installed at that exact release; nothing to do."
      return 0
    fi
  fi

  printf '\n%sAbout to download and install %d RPM(s):%s\n' \
    "$_c_bold" "$url_count" "$_c_reset"
  printf '%s\n' "$urls" | sed 's|^.*/|  |'
  printf '\n'
  confirm_or_die

  WORKDIR=$(mktemp -d --suffix=-urpm-ng-get)
  log "Downloading into $WORKDIR..."
  (
    cd "$WORKDIR"
    printf '%s\n' "$urls" | while read -r url; do
      [[ -n "$url" ]] || continue
      printf '  %s%s%s... ' "$_c_blue" "$(basename "$url")" "$_c_reset"
      if curl -fsSL -O "$url"; then
        printf '%sOK%s\n' "$_c_green" "$_c_reset"
      else
        printf '%sFAIL%s\n' "$_c_red" "$_c_reset"
        exit 1
      fi
    done
  )
  ok "Downloaded ${url_count} RPM(s)."

  if [[ -z "$installed" ]]; then
    log "Installing (first time — using urpmi to bootstrap)..."
    su -c "urpmi --auto $WORKDIR/*.rpm && \
           urpm mark auto \$(rpm -qa 'urpm-ng-*' | \
                             grep -v urpm-ng-all | sed 's/-[0-9].*//')"
  else
    log "Upgrading over existing $installed..."
    local metas
    metas=$(find "$WORKDIR" -maxdepth 1 -type f \
              \( -name 'urpm-ng-all-*.rpm' -o -name 'rpmdrake-ng-*.rpm' \))
    [[ -n "$metas" ]] || die "No urpm-ng-all / rpmdrake-ng RPMs found in $WORKDIR."
    su -c "urpm i --auto --reinstall $metas"
  fi
}

# ── Main ───────────────────────────────────────────────────────────────
main() {
  require_tty_or_yes

  local mgaver arch installed channel
  mgaver=$(detect_mageia_release)
  arch=$(detect_arch)
  installed=$(detect_installed_urpm_ng | tr -d '\n')

  log "Detected: Mageia ${_c_bold}${mgaver}${_c_reset}, arch ${_c_bold}${arch}${_c_reset}"
  if [[ -n "$installed" ]]; then
    log "Existing urpm-ng-core: ${_c_bold}${installed}${_c_reset}"
  else
    log "urpm-ng is not installed yet."
  fi

  channel=$(resolve_channel)
  log "Channel : ${_c_bold}${channel}${_c_reset}"

  case "$channel" in
    mgabiz) mgabiz_install "$mgaver" "$arch" "$installed" ;;
    github) github_install "$mgaver" "$arch" "$installed" ;;
  esac

  ok "urpm-ng $(rpm -q --qf '%{version}-%{release}' urpm-ng-core) is now installed."
  printf '\n%sNext steps%s\n' "$_c_bold" "$_c_reset"
  printf '  1. Import your existing urpmi media (if any):\n'
  printf '       su -c "urpm media import"\n'
  printf '  2. Attach servers to the mirrorlist-based media:\n'
  printf '       su -c "urpm server autoconfig"\n'
  printf '  3. Refresh package lists:\n'
  printf '       su -c "urpm m u"\n'
}

main "$@"
