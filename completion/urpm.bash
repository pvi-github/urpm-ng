#!/usr/bin/env bash
# Bash completion for urpm
# Install: cp urpm.bash /etc/bash_completion.d/urpm
#
# Structure (per-command handler refactor):
#   1. Dynamic helpers  — package / media / server / peer name lookups
#   2. Constants        — global flags, display/debug parent flags, command list
#   3. Handlers         — one _urpm_<cmd> function per top-level command
#   4. Dispatcher       — _urpm() routes ${words[1]} to the right handler
#
# Handlers read $cur, $prev, $words, $cword from the caller's scope and
# write their result into COMPREPLY. This keeps each handler small and
# maps 1:1 onto the argparse structure in urpm/cli/main.py.

# ─────────────────────────────────────────────────────────────
# 1. Dynamic helpers
# ─────────────────────────────────────────────────────────────

_urpm_installed_packages() {
    # Fast path: query rpmdb directly.
    rpm -qa --qf '%{NAME}\n' 2>/dev/null | sort -u
}

_urpm_cache_db() {
    # Where urpm keeps its SQLite database, per urpm/core/config.py:
    # ``/var/lib/urpm/packages.db``, or ``/var/lib/urpm-dev/packages.db``
    # in dev mode. One function because three helpers need it, and the
    # expression this replaces appended the production sub-path to the
    # dev base directory, so dev mode never found its database.
    if [[ -n "$URPM_DEV_MODE" ]]; then
        echo "/var/lib/urpm-dev/packages.db"
    else
        echo "/var/lib/urpm/packages.db"
    fi
}

_urpm_available_packages() {
    # Use urpm's SQLite cache when present; fall back to installed set.
    local cache_db
    cache_db="$(_urpm_cache_db)"
    if [[ -f "$cache_db" ]]; then
        sqlite3 "$cache_db" "SELECT DISTINCT name FROM packages" 2>/dev/null
    else
        _urpm_installed_packages
    fi
}

_urpm_held_packages() {
    # The holds the user has placed. Only these can be released, and
    # offering the whole installed set for `hold remove` would be
    # noise: holds are rare and deliberate.
    local cache_db
    cache_db="$(_urpm_cache_db)"
    [[ -f "$cache_db" ]] || return 0
    sqlite3 "$cache_db" 'SELECT package_name FROM held_packages' 2>/dev/null
}

_urpm_skipped_entries() {
    # The masked entries, read from the list itself rather than from
    # urpm: the file is urpmi's and is the single source of truth for
    # both tools. Comments and blank lines are not entries.
    local list="${URPM_SKIP_LIST:-/etc/urpmi/skip.list}"
    [[ -r "$list" ]] || return 0
    sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        -e '/^$/d' "$list" 2>/dev/null
}

_urpm_media_names() {
    # Query urpm's SQLite cache directly. The `media` table stores one
    # row per configured medium, whose name may contain spaces (e.g.
    # "Core Release"), which ruled out the old text-scraping approach.
    local cache_db
    cache_db="$(_urpm_cache_db)"
    [[ -f "$cache_db" ]] || return 0
    sqlite3 "$cache_db" 'SELECT name FROM media' 2>/dev/null
}

_urpm_compreply_from_lines() {
    # Read newline-separated choices from stdin and populate COMPREPLY
    # with shell-escaped matches. Use this whenever a dynamic helper
    # may return values containing spaces (e.g. media names like
    # "Core Release"). `printf %q` produces a form that bash parses
    # back into the original string when inserted on the command line.
    #
    # Usage: _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
    local cur="$1"
    local line
    COMPREPLY=()
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if [[ -z "$cur" || "$line" == "$cur"* ]]; then
            COMPREPLY+=("$(printf '%q' "$line")")
        fi
    done
}

_urpm_extract_table_first_column() {
    # Shared helper for tabular `urpm <cmd> list` outputs. A real table
    # always contains a "----" separator row; if we don't see one, the
    # command printed an error / empty message and we return nothing.
    # Avoids proposing localised error words ("Aucun ...") as names.
    local out="$1"
    [[ -n "$out" ]] || return 0
    echo "$out" | grep -q '^-\{4,\}' || return 0
    echo "$out" | awk '
        /^-/ { next }
        /^$/ { next }
        # Skip the header row (the one before the separator) by
        # requiring the first column not to look like a localised
        # word. Accept alphanumerics with dots, dashes, underscores.
        $1 ~ /^[A-Za-z0-9][A-Za-z0-9._-]*$/ {
            # Drop rows that are clearly headers (single uppercase word
            # followed by more uppercase-starting columns).
            if ($1 ~ /^[A-Z][a-zô]*$/ && NF >= 3) next
            print $1
        }
    '
}

_urpm_server_names() {
    _urpm_extract_table_first_column "$(urpm --quiet server list 2>/dev/null)"
}

_urpm_peer_hosts() {
    _urpm_extract_table_first_column "$(urpm --quiet peer list 2>/dev/null)"
}

_urpm_transaction_ids() {
    # `urpm history` prints an ID-first table. Match data rows by the
    # "<integer> |" pattern so headers and separators are skipped.
    urpm --quiet history 2>/dev/null | awk '
        /^ *[0-9]+ \|/ { print $1 }
    '
}

_urpm_config_dropins() {
    # Config drop-ins live in /etc/urpm/conf.d/*.cfg; completion for
    # `urpm config edit <name>` should propose drop-in basenames.
    local dir="${URPM_DEV_MODE:+/var/lib/urpm-dev}/etc/urpm/conf.d"
    [[ -d "$dir" ]] || dir="/etc/urpm/conf.d"
    [[ -d "$dir" ]] || return 0
    local f
    for f in "$dir"/*.cfg; do
        [[ -f "$f" ]] || continue
        basename "$f" .cfg
    done
}

_urpm_profile_names() {
    # mkimage profiles live as YAML files, system-wide under
    # /usr/share/urpm/profiles/ and overridable under /etc/urpm/profiles/.
    # Return the basename of each (without the .yaml suffix).
    local dir f
    for dir in /usr/share/urpm/profiles /etc/urpm/profiles; do
        [[ -d "$dir" ]] || continue
        for f in "$dir"/*.yaml; do
            [[ -f "$f" ]] || continue
            basename "$f" .yaml
        done
    done | sort -u
}

# ─────────────────────────────────────────────────────────────
# 2. Constants
# ─────────────────────────────────────────────────────────────

# Global flags, accepted before the subcommand.
_URPM_GLOBAL_FLAGS="--help -h --version -V --verbose -v --quiet -q \
    --nocolor --root --urpm-root --rootless --allow-no-root"

# display_parent — the reporting flags.  They sit on the top-level
# reporting commands and on the *parent* of a subcommand group, never
# on the subcommands themselves: ``urpm media --json list`` is
# accepted, ``urpm media list --json`` is not.  Offering them after
# the subcommand, which this file used to do for 45 of them, completes
# straight into "unrecognized arguments".
_URPM_DISPLAY_FLAGS="--json --flat --show-all --no-show-all"

# debug_parent — inherited by install / download / update / upgrade.
_URPM_DEBUG_FLAGS="--debug --watched"

# Real top-level commands, mirroring urpm/cli/main.py subparsers.
# Phantom entries that existed in the old completion (kernel-keep,
# blacklist, redlist, seed) have been removed: they are sub-subcommands
# of `config` / `media`, never real top-level commands.  So was `up`,
# which `update` never declared as an alias; `audit`, `genmedia` and
# `system` were the reverse case, real verbs that were never offered.
# test_completion_matches_cli.py compares this list against argparse
# so neither direction drifts again.
_URPM_COMMANDS="install i erase e upgrade u update \
    distupgrade recover \
    search s query q show sh info list l find f \
    provides p whatprovides wp \
    depends d requires req rdepends rd whatrequires wr \
    recommends whatrecommends suggests whatsuggests why \
    download dl init cleanup autoremove ar cleandeps cd \
    hold unhold skip mark progress readme mkimage build image img \
    history h rollback r undo audit genmedia system \
    media m server srv config cfg peer cache c key k \
    appstream mirror proxy"

# ─────────────────────────────────────────────────────────────
# 3. Per-command handlers
# ─────────────────────────────────────────────────────────────
#
# Each handler is invoked by the dispatcher when ${words[1]} matches.
# They inherit $cur/$prev/$words/$cword from _urpm() and populate
# COMPREPLY. Handlers are intentionally small; subcommand groups
# (media, server, config, …) dispatch internally.

# ── Core transaction commands ────────────────────────────────

# Flags that take a value from a fixed choice list — completed when
# the previous word on the command line matches.
_URPM_CONFIG_POLICY_CHOICES="keep replace ask"
_URPM_ERASE_DEBUG_CHOICES="solver tsrun all"
# Common RPM architectures for --allow-arch. Not a closed choice list
# in argparse — just a convenience shortlist.
_URPM_ARCH_CHOICES="x86_64 i686 i586 aarch64 armv7hl noarch"

_urpm_install() {
    # Value-completion for flags with a fixed / conventional argument.
    case "$prev" in
        --config-policy)
            COMPREPLY=($(compgen -W "$_URPM_CONFIG_POLICY_CHOICES" -- "$cur"))
            return
            ;;
        --allow-arch|--arch)
            COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
            return
            ;;
        --enablemedia|--disablemedia)
            _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            return
            ;;
        --download-dir)
            _filedir -d
            return
            ;;
        --buildrequires|--builddeps|--br|-b)
            _filedir '@(spec|src.rpm)'
            return
            ;;
        --check|--prefer|--debug|--watched)
            return  # Free text.
            ;;
    esac

    local install_opts="--auto -y --test --force --reinstall --download-only
        --nodeps --nosignature --noscripts --without-recommends --with-suggests
        --prefer --all --no-peers --only-peers --allow-arch --arch --check
        --enablemedia --disablemedia --download-dir --sync --no-readme
        --no-atomic --config-policy
        --buildrequires --builddeps --br -b --install-src"

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$install_opts $_URPM_DISPLAY_FLAGS $_URPM_DEBUG_FLAGS" -- "$cur"))
    elif [[ "$cur" == */* || "$cur" == .* ]]; then
        _filedir rpm
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_erase() {
    # erase defines its OWN --debug with a closed choice list, shadowing
    # the store_true --debug from debug_parent.
    case "$prev" in
        --debug)
            COMPREPLY=($(compgen -W "$_URPM_ERASE_DEBUG_CHOICES" -- "$cur"))
            return
            ;;
        --arch)
            COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
            return
            ;;
        --check)
            return  # Free text.
            ;;
    esac

    local erase_opts="--auto -y --test --force --auto-orphans --keep-orphans
        --erase-recommends --keep-suggests --debug --arch --check --sync"

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$erase_opts $_URPM_DISPLAY_FLAGS" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

_urpm_upgrade() {
    # Note the asymmetry with install: upgrade has --with-recommends
    # (opt-in) while install has --without-recommends (opt-out). There
    # is no --reinstall, --nodeps, --noscripts, --prefer, --all,
    # --buildrequires or --install-src on upgrade.
    case "$prev" in
        --config-policy)
            COMPREPLY=($(compgen -W "$_URPM_CONFIG_POLICY_CHOICES" -- "$cur"))
            return
            ;;
        --allow-arch|--arch)
            COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
            return
            ;;
        --enablemedia|--disablemedia)
            _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            return
            ;;
        --download-dir)
            _filedir -d
            return
            ;;
        --check|--debug|--watched)
            return  # Free text.
            ;;
    esac

    local upgrade_opts="--auto -y --test --force --download-only --nosignature
        --noerase-orphans --with-recommends --with-suggests --no-peers
        --only-peers --allow-arch --arch --check --enablemedia --disablemedia
        --download-dir --resume --atomic --sync --config-policy"

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$upgrade_opts $_URPM_DISPLAY_FLAGS $_URPM_DEBUG_FLAGS" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

_urpm_update() {
    # Top-level `update` is a shortcut for `media update`; it takes a
    # media name (optional, default = all) and only one extra flag,
    # which is ``--force`` and never was ``--files``.  Both commands
    # share the short ``-f``, which is how the long name drifted here
    # without anyone noticing.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--force -f $_URPM_DISPLAY_FLAGS $_URPM_DEBUG_FLAGS" -- "$cur"))
    else
        _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
    fi
}

_urpm_autoremove() {
    # No ``--dry-run`` here: the command asks before it removes, and
    # ``--interactive`` is what arbitrates package by package.
    case "$prev" in
        --filter)
            return  # Free text (an expression).
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--auto -y --interactive -i --orphans -o \
            --kernels -k --faildeps -f --all -a --filter \
            --buildrequires --builddeps --br -b \
            $_URPM_DISPLAY_FLAGS" -- "$cur"))
    fi
}

_urpm_cleandeps() {
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--auto -y $_URPM_DISPLAY_FLAGS" -- "$cur"))
    fi
}

# ── Additional top-level commands ────────────────────────────

# Common Mageia release values for --release on init / download.
_URPM_RELEASE_CHOICES="8 9 10 cauldron"
# Container runtime choices for mkimage / build.
_URPM_RUNTIME_CHOICES="docker podman"
# Where an image takes urpm-ng-core from.  A closed choice list in
# argparse, which lost its github arm when the project left GitHub.
_URPM_NG_SOURCE_CHOICES="auto local media"

_urpm_download() {
    # download / dl — install-like flag set minus install-only flags.
    case "$prev" in
        --release|-r)
            COMPREPLY=($(compgen -W "$_URPM_RELEASE_CHOICES" -- "$cur"))
            return
            ;;
        --allow-arch|--arch)
            COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
            return
            ;;
        --buildrequires|--builddeps|--br|-b)
            _filedir '@(spec|src.rpm)'
            return
            ;;
        --from-file)
            _filedir
            return
            ;;
    esac

    local download_opts="--release -r --arch --auto -y --without-recommends
        --no-peers --only-peers --nodeps --allow-arch --from-file
        --buildrequires --builddeps --br -b"

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$download_opts $_URPM_DISPLAY_FLAGS $_URPM_DEBUG_FLAGS" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

# distupgrade — mga N → mga N+1 migration.  Target auto-detected
# (current + 1) when --to is omitted ; flags cover the whole life-
# cycle (dry-run, export-plan, resume, abort).
_urpm_distupgrade() {
    case "$prev" in
        --to)
            COMPREPLY=($(compgen -W "$_URPM_RELEASE_CHOICES" -- "$cur"))
            return
            ;;
        --export-plan)
            _filedir
            return
            ;;
        --debug|--watched)
            return  # Free text.
            ;;
    esac
    # ``--show-all`` without the rest of display_parent, and a
    # ``--debug`` that takes a component rather than being a switch.
    local distupgrade_opts="--to --yes -y --auto --dry-run
        --export-plan --resume --continue --abort
        --show-all --no-show-all
        --debug --watched"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$distupgrade_opts" -- "$cur"))
    fi
}

# recover — reconcile transactions interrupted by crash / SIGKILL.
# No positional args, and no options at all: it inherits neither
# display_parent nor debug_parent.
_urpm_recover() {
    :
}

_urpm_init() {
    # init — bootstrap a urpm root. Does NOT inherit display/debug.
    case "$prev" in
        --release)
            COMPREPLY=($(compgen -W "$_URPM_RELEASE_CHOICES" -- "$cur"))
            return
            ;;
        --arch)
            COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
            return
            ;;
        --mirrorlist)
            _filedir
            return
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--mirrorlist --arch --release --auto -y --no-sync" -- "$cur"))
    fi
}

_urpm_cleanup() {
    # No positionals.  The root to clean up comes from the global
    # --urpm-root, which is why this command looked flagless.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--dry-run --legacy-scriptlets" -- "$cur"))
    fi
}

_urpm_hold() {
    # `urpm hold` is subcommand-based: add / remove / list. The old
    # grammar (packages straight after the verb, --list to enumerate)
    # is gone, and so are the flags that went with it.
    local hold_subcmds="add remove list"
    case "$prev" in
        --reason|-r)
            return  # Free text.
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        if [[ "${words[2]}" == add ]]; then
            COMPREPLY=($(compgen -W "--reason -r $_URPM_DISPLAY_FLAGS" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        fi
    elif [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$hold_subcmds" -- "$cur"))
    elif [[ "${words[2]}" == add ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    elif [[ "${words[2]}" == remove ]]; then
        # Only what is actually held can be released.
        COMPREPLY=($(compgen -W "$(_urpm_held_packages)" -- "$cur"))
    fi
}

_urpm_unhold() {
    # unhold releases held packages — kept as an alias of
    # `hold remove`, so it completes the same list.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_held_packages)" -- "$cur"))
    fi
}

_urpm_skip() {
    # `urpm skip` masks candidates from the media. add / remove take
    # entries (a name or a /regexp/), list takes none; all three accept
    # --file to work on a list other than urpmi's own.
    local skip_subcmds="add remove list"
    case "$prev" in
        --file)
            _filedir
            return
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        # ``--file`` belongs to the three subcommands, not to ``skip``
        # itself, so it is only offered once one is named.
        if [[ $cword -eq 2 ]]; then
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "--file $_URPM_DISPLAY_FLAGS" -- "$cur"))
        fi
    elif [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$skip_subcmds" -- "$cur"))
    elif [[ "${words[2]}" == add ]]; then
        # Any package the media know about can be masked, installed or
        # not; a regular expression cannot be completed.
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    elif [[ "${words[2]}" == remove ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_skipped_entries)" -- "$cur"))
    fi
}

_urpm_progress() {
    # progress — single optional flag, does NOT inherit display/debug.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--watch -w" -- "$cur"))
    fi
}

_urpm_readme() {
    # readme — show post-install READMEs. --transaction/-t takes an ID.
    case "$prev" in
        --transaction|-t)
            COMPREPLY=($(compgen -W "$(_urpm_transaction_ids)" -- "$cur"))
            return
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--last --transaction -t --list -l" -- "$cur"))
    fi
}

_urpm_mkimage() {
    # mkimage builds a root chroot / OCI image. It has no positional
    # argument, so a bare TAB shows the flag list for discoverability.
    case "$prev" in
        --release|-r)
            COMPREPLY=($(compgen -W "$_URPM_RELEASE_CHOICES" -- "$cur"))
            return
            ;;
        --profile)
            COMPREPLY=($(compgen -W "$(_urpm_profile_names)" -- "$cur"))
            return
            ;;
        --arch)
            COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
            return
            ;;
        --runtime)
            COMPREPLY=($(compgen -W "$_URPM_RUNTIME_CHOICES" -- "$cur"))
            return
            ;;
        --workdir|-w)
            _filedir -d
            return
            ;;
        --addmedia|--tag|-t|--packages|-p)
            return  # Free text, two words for --addmedia (NAME URL).
            ;;
        --buildrequires|--br)
            _filedir '@(spec|src.rpm)'
            return
            ;;
    esac
    COMPREPLY=($(compgen -W "--release -r --tag -t --profile --arch \
        --packages -p --runtime --keep-chroot --workdir -w \
        --addmedia --import-key --buildrequires --br" -- "$cur"))
}

_urpm_build() {
    # build — compile .src.rpm or .spec sources inside a container image.
    case "$prev" in
        --image|-i)
            return  # Free text (OCI image reference).
            ;;
        --output|-o)
            _filedir -d
            return
            ;;
        --runtime)
            COMPREPLY=($(compgen -W "$_URPM_RUNTIME_CHOICES" -- "$cur"))
            return
            ;;
        --parallel|-j)
            COMPREPLY=($(compgen -W "1 2 4 6 8 12 16" -- "$cur"))
            return
            ;;
        --with-rpms|-w|--subrel|--with|--without|--build-memory)
            return  # Free text.
            ;;
        --net-isolation)
            COMPREPLY=($(compgen -W "auto strict off" -- "$cur"))
            return
            ;;
        --rpmmacros)
            _filedir
            return
            ;;
        --enablemedia|--disablemedia)
            _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            return
            ;;
        --build-cpus)
            COMPREPLY=($(compgen -W "1 2 4 6 8 12 16" -- "$cur"))
            return
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--image -i --output -o --with-rpms -w \
            --runtime --parallel -j --keep-container --no-update \
            --net-isolation --nocheck --auto -y \
            --enablemedia --disablemedia --subrel --rpmmacros \
            --stop-on-fail --rollback-between-builds --rbb \
            --build-cpus --build-memory --strict-memory --full-throttle \
            --with-network --with --without" -- "$cur"))
    else
        _filedir '@(spec|src.rpm)'
    fi
}

_urpm_image() {
    # image / img — container image management (make, update, list, delete).
    local image_subcmds="make m update u list l ls delete d rm"

    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$image_subcmds" -- "$cur"))
        return
    fi

    local sub="${words[2]}"
    case "$sub" in
        make|m)
            # Same options as mkimage, plus the ones that only make
            # sense for an image urpm-ng itself will live in.
            case "$prev" in
                --release|-r)
                    COMPREPLY=($(compgen -W "$_URPM_RELEASE_CHOICES" -- "$cur"))
                    return
                    ;;
                --profile)
                    COMPREPLY=($(compgen -W "$(_urpm_profile_names)" -- "$cur"))
                    return
                    ;;
                --arch)
                    COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
                    return
                    ;;
                --runtime)
                    COMPREPLY=($(compgen -W "$_URPM_RUNTIME_CHOICES" -- "$cur"))
                    return
                    ;;
                --urpm-ng-source)
                    COMPREPLY=($(compgen -W "$_URPM_NG_SOURCE_CHOICES" -- "$cur"))
                    return
                    ;;
                --urpm-ng-core)
                    _filedir rpm
                    return
                    ;;
                --workdir|-w)
                    _filedir -d
                    return
                    ;;
                --buildrequires|--br)
                    _filedir '@(spec|src.rpm)'
                    return
                    ;;
                --exclude|-x)
                    COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
                    return
                    ;;
                --addmedia|--tag|-t|--packages|-p)
                    return  # Free text, two words for --addmedia.
                    ;;
            esac
            COMPREPLY=($(compgen -W "--release -r --tag -t --profile --arch \
                --packages -p --runtime --keep-chroot --workdir -w \
                --buildrequires --br --addmedia --import-key \
                --urpm-ng-source --urpm-ng-core --allow-disttag-mismatch \
                --forward-proxy --no-forward-proxy --exclude -x" -- "$cur"))
            ;;
        update|u)
            case "$prev" in
                --runtime)
                    COMPREPLY=($(compgen -W "$_URPM_RUNTIME_CHOICES" -- "$cur"))
                    return
                    ;;
                --urpm-ng-source)
                    COMPREPLY=($(compgen -W "$_URPM_NG_SOURCE_CHOICES" -- "$cur"))
                    return
                    ;;
                --urpm-ng-core)
                    _filedir rpm
                    return
                    ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--runtime --urpm-ng-source \
                    --urpm-ng-core --allow-disttag-mismatch" -- "$cur"))
            fi
            # Positional is a tag — free text.
            ;;
        list|l|ls)
            case "$prev" in
                --runtime)
                    COMPREPLY=($(compgen -W "$_URPM_RUNTIME_CHOICES" -- "$cur"))
                    return
                    ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--runtime" -- "$cur"))
            fi
            ;;
        delete|d|rm)
            case "$prev" in
                --runtime)
                    COMPREPLY=($(compgen -W "$_URPM_RUNTIME_CHOICES" -- "$cur"))
                    return
                    ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--force -f --runtime" -- "$cur"))
            fi
            # Positionals are tags — free text.
            ;;
    esac
}

# ── Query / inspection commands ──────────────────────────────

# Positional filter for `urpm list`.
_URPM_LIST_FILTERS="installed available updates upgradable all"
# Common depth values for depends/rdepends tree views.
_URPM_DEPTH_CHOICES="1 2 3 4 5 6 7 8 9"

_urpm_search() {
    # search only exposes --installed and --unavailable; the old
    # completion also offered --available and --all which never
    # existed as argparse flags.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--installed --unavailable $_URPM_DISPLAY_FLAGS" -- "$cur"))
    fi
    # Pattern is free text (FTS query) — no name completion.
}

_urpm_show() {
    # show / sh / info — take a package and expose --files / --changelog.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--files --changelog $_URPM_DISPLAY_FLAGS" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_list() {
    # list takes a single positional filter from a fixed choice list.
    # No command-specific flags beyond display_parent.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
    elif [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$_URPM_LIST_FILTERS" -- "$cur"))
    fi
}

_urpm_find() {
    # find / f — FTS-based file name search. Pattern is free text.
    case "$prev" in
        --limit|-l)
            COMPREPLY=($(compgen -W "10 25 50 100 250 500" -- "$cur"))
            return
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--available -a --installed -i --limit -l \
            --all-versions $_URPM_DISPLAY_FLAGS" -- "$cur"))
    fi
}

_urpm_provides() {
    # provides / p — takes a package name.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_whatprovides() {
    # whatprovides / wp — takes a capability string or a file path.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
    fi
}

_urpm_depends() {
    # depends / d / requires / req — far richer than rdepends.
    case "$prev" in
        --depth)
            COMPREPLY=($(compgen -W "$_URPM_DEPTH_CHOICES" -- "$cur"))
            return
            ;;
        --prefer)
            return  # Free text (CSV of preferred alternatives).
            ;;
    esac
    # Neither depends nor rdepends inherits display_parent: they print
    # a tree of their own and offering --json here only completes into
    # an error.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--tree --all -a --legacy --prefer \
            --pager --no-libs --depth --legend --hide-uninstalled" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_rdepends() {
    # rdepends / rd / whatrequires / wr — narrower flag set than depends
    # (no --pager, --prefer, --no-libs, --legacy); default depth is 3.
    case "$prev" in
        --depth)
            COMPREPLY=($(compgen -W "$_URPM_DEPTH_CHOICES" -- "$cur"))
            return
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--tree --all -a --depth --hide-uninstalled \
            --legend" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_recommends_family() {
    # recommends / whatrecommends / suggests / whatsuggests — each is a
    # distinct parser in main.py (no aliases) but none expose any
    # command-specific flag beyond display_parent. All take a package.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_why() {
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

# ── State management ─────────────────────────────────────────

_urpm_mark() {
    # The three subcommands declare no flag of their own, and
    # display_parent sits on ``mark`` itself, so the reporting flags
    # are only offered where they are accepted.
    local mark_subcmds="manual m explicit auto a dep show s list"
    if [[ "$cur" == -* ]]; then
        if [[ $cword -eq 2 ]]; then
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        fi
    elif [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$mark_subcmds" -- "$cur"))
    elif [[ "${words[2]}" =~ ^(manual|m|explicit|auto|a|dep)$ ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

_urpm_history() {
    # history / h — flag-based (not subcommand-based). The old
    # completion offered "list search show" which were never real
    # subcommands. Positional is an integer count.
    case "$prev" in
        --detail|-d|--delete)
            COMPREPLY=($(compgen -W "$(_urpm_transaction_ids)" -- "$cur"))
            return
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--install -i --remove -r --detail -d \
--delete $_URPM_DISPLAY_FLAGS" -- "$cur"))
    fi
    # Positional COUNT is an integer — no dynamic completion.
}

_urpm_rollback() {
    # rollback / r — optional --auto/-y and a free-form positional
    # ("N", "to N", "to DATE"). Provide transaction IDs for the
    # "to N" form when the user has already typed "to".
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--auto -y $_URPM_DISPLAY_FLAGS" -- "$cur"))
    elif [[ "$prev" == "to" ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_transaction_ids)" -- "$cur"))
    elif [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "to" -- "$cur"))
    fi
}

_urpm_undo() {
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--auto -y $_URPM_DISPLAY_FLAGS" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_transaction_ids)" -- "$cur"))
    fi
}

# ── Subcommand groups ────────────────────────────────────────

_urpm_media() {
    local media_subcmds="list l ls add a add-cdrom remove r enable e disable d \
update u set s import discover disc autoconfig auto ac seed-info link"

    if [[ $cword -eq 2 ]]; then
        if [[ "$cur" == -* ]]; then
            # display_parent sits on `media`, never on a subcommand.
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$media_subcmds" -- "$cur"))
        fi
        return
    fi

    local sub="${words[2]}"
    case "$sub" in
        list|l|ls)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--all -a" -- "$cur"))
            fi
            ;;
        add|a)
            case "$prev" in
                --name|--shortname|--version) return ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--name --shortname --custom \
--update --disabled --auto -y --import-key --allow-unsigned \
--version" -- "$cur"))
            fi
            ;;
        add-cdrom)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--dry-run -n" -- "$cur"))
            fi
            ;;
        remove|r)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--all --distupgraded --auto -y" -- "$cur"))
            else
                _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            fi
            ;;
        enable|e|disable|d|seed-info)
            # These three take a medium and nothing else.
            if [[ "$cur" != -* ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            fi
            ;;
        update|u)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--force -f --no-appstream" -- "$cur"))
            else
                _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            fi
            ;;
        import)
            case "$prev" in
                --release|-r)
                    COMPREPLY=($(compgen -W "$_URPM_RELEASE_CHOICES" -- "$cur"))
                    return
                    ;;
                --arch)
                    COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
                    return
                    ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--replace --release -r --arch \
--auto -y" -- "$cur"))
            else
                _filedir 'cfg'
            fi
            ;;
        set|s)
            case "$prev" in
                --shared)
                    COMPREPLY=($(compgen -W "yes no" -- "$cur"))
                    return
                    ;;
                --replication)
                    # Not a closed list in argparse, just the policies
                    # the code knows about.
                    COMPREPLY=($(compgen -W "none on_demand seed" -- "$cur"))
                    return
                    ;;
                --seeds|--quota|--retention|--priority)
                    return
                    ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--shared --replication \
--seeds --quota --retention --priority" -- "$cur"))
            else
                _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            fi
            ;;
        autoconfig|auto|ac)
            case "$prev" in
                --release|-r)
                    COMPREPLY=($(compgen -W "$_URPM_RELEASE_CHOICES" -- "$cur"))
                    return
                    ;;
                --arch)
                    COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
                    return
                    ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--release -r --arch --dry-run -n \
--no-nonfree --no-tainted" -- "$cur"))
            fi
            ;;
        discover|disc)
            case "$prev" in
                --with|--without) return ;;
            esac
            # NB: `media discover --debug` is a store_true "include debug
            # media" flag that shadows debug_parent's --debug, so we do
            # NOT add $_URPM_DEBUG_FLAGS here.
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--with --without --sources --debug \
--import-keys --dry-run -n" -- "$cur"))
            fi
            ;;
        link)
            if [[ "$cur" != -* && $cword -eq 3 ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            fi
            # Subsequent positionals are +server/-server — no dynamic list.
            ;;
    esac
}

_urpm_server() {
    # ``status``, ``unblacklist`` and ``ack-blacklist`` were accepted by
    # the CLI and never offered here.
    local server_subcmds="list l ls add a remove r rm enable e disable d \
priority test t ip-mode stats status unblacklist ack-blacklist \
autoconfig auto"

    if [[ $cword -eq 2 ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$server_subcmds" -- "$cur"))
        fi
        return
    fi

    local sub="${words[2]}"
    case "$sub" in
        list|l|ls)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--all -a" -- "$cur"))
            fi
            ;;
        add|a)
            case "$prev" in
                --priority|-p) return ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--priority -p --disabled \
--custom" -- "$cur"))
            fi
            # Positionals are NAME URL — no dynamic list possible.
            ;;
        remove|r|rm|enable|e|disable|d|stats|test|t|priority|status|unblacklist|ack-blacklist)
            # None of these declares a flag; they all take a server.
            if [[ "$cur" != -* && $cword -eq 3 ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_server_names)
            fi
            ;;
        ip-mode)
            if [[ "$cur" != -* ]]; then
                if [[ $cword -eq 3 ]]; then
                    _urpm_compreply_from_lines "$cur" < <(_urpm_server_names)
                elif [[ $cword -eq 4 ]]; then
                    COMPREPLY=($(compgen -W "auto ipv4 ipv6 dual" -- "$cur"))
                fi
            fi
            ;;
        autoconfig|auto)
            case "$prev" in
                --release|-r)
                    COMPREPLY=($(compgen -W "$_URPM_RELEASE_CHOICES" -- "$cur"))
                    return
                    ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--dry-run -n --release -r" -- "$cur"))
            fi
            ;;
    esac
}

_urpm_mirror() {
    local mirror_subcmds="status enable disable quota disable-version \
enable-version clean sync rate-limit"

    if [[ $cword -eq 2 ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$mirror_subcmds" -- "$cur"))
        fi
        return
    fi

    local sub="${words[2]}"
    case "$sub" in
        clean)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--dry-run -n" -- "$cur"))
            fi
            ;;
        sync)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--latest-only" -- "$cur"))
            elif [[ $cword -eq 3 ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            fi
            ;;
        rate-limit)
            if [[ "$cur" != -* && $cword -eq 3 ]]; then
                COMPREPLY=($(compgen -W "on off" -- "$cur"))
            fi
            ;;
    esac
    # The remaining subcommands declare no flag of their own, and
    # display_parent is on `mirror` itself, so there is nothing to
    # offer past this point.
}

_urpm_cache() {
    local cache_subcmds="info clean flush rebuild stats"

    if [[ $cword -eq 2 ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$cache_subcmds" -- "$cur"))
        fi
        return
    fi

    case "${words[2]}" in
        clean)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--dry-run -n --auto -y \
--verbose -v" -- "$cur"))
            fi
            ;;
        flush)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--dry-run -n --auto -y" -- "$cur"))
            fi
            ;;
    esac
    # info, rebuild and stats take nothing.
}

_urpm_config() {
    local config_subcmds="blacklist bl redlist rl kernel-keep kk version-mode vm \
show gnome-auto-upgrades gau discover-auto-upgrades dau \
packagekit-auto-upgrades pau show-all sa edit"
    local auto_upgrade_choices="yes no true false on off"
    # The list / add / remove trio that both blacklist and redlist
    # take.  Named so the alignment test can read it.
    local config_list_subcmds="list ls add a remove rm"

    # None of the subcommands declares a flag, and display_parent is on
    # `config` itself, so the reporting flags are offered there only.
    if [[ "$cur" == -* ]]; then
        if [[ $cword -eq 2 ]]; then
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        fi
        return
    fi

    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$config_subcmds" -- "$cur"))
        return
    fi

    local sub="${words[2]}"
    case "$sub" in
        blacklist|bl|redlist|rl)
            if [[ $cword -eq 3 ]]; then
                COMPREPLY=($(compgen -W "$config_list_subcmds" -- "$cur"))
                return
            fi
            local subsub="${words[3]}"
            case "$subsub" in
                add|a|remove|rm)
                    if [[ $cword -eq 4 ]]; then
                        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
                    fi
                    ;;
            esac
            ;;
        kernel-keep|kk)
            # Optional integer positional — no completion.
            ;;
        version-mode|vm)
            if [[ $cword -eq 3 ]]; then
                COMPREPLY=($(compgen -W "system cauldron auto" -- "$cur"))
            fi
            ;;
        gnome-auto-upgrades|gau|discover-auto-upgrades|dau|packagekit-auto-upgrades|pau)
            if [[ $cword -eq 3 ]]; then
                COMPREPLY=($(compgen -W "$auto_upgrade_choices" -- "$cur"))
            fi
            ;;
        show-all|sa)
            # Same yes/no vocabulary as the auto-upgrade verbs.
            if [[ $cword -eq 3 ]]; then
                COMPREPLY=($(compgen -W "$auto_upgrade_choices" -- "$cur"))
            fi
            ;;
        show)
            # No positional, no flags.
            ;;
        edit)
            if [[ $cword -eq 3 ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_config_dropins)
            fi
            ;;
    esac
}

_urpm_key() {
    local key_subcmds="list ls l import i add remove rm del"
    # Same shape as config: the flags are on `key`, not on its verbs.
    if [[ "$cur" == -* ]]; then
        if [[ $cword -eq 2 ]]; then
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        fi
    elif [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$key_subcmds" -- "$cur"))
    elif [[ $cword -eq 3 && "${words[2]}" =~ ^(import|i|add)$ ]]; then
        _filedir
    fi
    # `key remove KEYID` positional is an opaque hex id — no dynamic list.
}

_urpm_peer() {
    local peer_subcmds="list ls downloads dl blacklist bl block \
unblacklist unbl unblock clean"

    if [[ $cword -eq 2 ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$peer_subcmds" -- "$cur"))
        fi
        return
    fi

    local sub="${words[2]}"
    case "$sub" in
        list|ls)
            : # Takes nothing of its own.
            ;;
        downloads|dl)
            case "$prev" in
                --limit|-n) return ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--limit -n" -- "$cur"))
            elif [[ $cword -eq 3 ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_peer_hosts)
            fi
            ;;
        blacklist|bl|block)
            case "$prev" in
                --port|-p|--reason|-r) return ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--port -p --reason -r" -- "$cur"))
            elif [[ $cword -eq 3 ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_peer_hosts)
            fi
            ;;
        unblacklist|unbl|unblock)
            case "$prev" in
                --port|-p) return ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--port -p" -- "$cur"))
            elif [[ $cword -eq 3 ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_peer_hosts)
            fi
            ;;
        clean)
            # NB: peer clean defines its own --show-all (no truncation)
            # and does NOT inherit display_parent — do not add
            # $_URPM_DISPLAY_FLAGS here.
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--yes -y --show-all --no-show-all -a" \
                    -- "$cur"))
            elif [[ $cword -eq 3 ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_peer_hosts)
            fi
            ;;
    esac
}

_urpm_appstream() {
    # appstream inherits no display_parent, unlike the other groups.
    local appstream_subcmds="generate gen status merge init-distro"

    if [[ $cword -eq 2 ]]; then
        if [[ "$cur" != -* ]]; then
            COMPREPLY=($(compgen -W "$appstream_subcmds" -- "$cur"))
        fi
        return
    fi

    case "${words[2]}" in
        status)
            : # Takes nothing.
            ;;
        generate|gen)
            case "$prev" in
                --output|-o) _filedir; return ;;
                --media|-m)
                    _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
                    return
                    ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--output -o --no-compress --media -m" -- "$cur"))
            fi
            ;;
        merge)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--refresh -r" -- "$cur"))
            fi
            ;;
        init-distro)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--force -f" -- "$cur"))
            fi
            ;;
    esac
}

# ── Commands that had no handler at all ──────────────────────
#
# All three were offered as top-level verbs and routed nowhere, so a
# TAB after them completed filenames.  `system` even has subcommands.

_urpm_audit() {
    : # No subcommand, no flag, no positional.
}

_urpm_genmedia() {
    # Builds the metadata of a media tree from the RPMs in a
    # directory, which is its one positional.
    case "$prev" in
        --media-info-dir)
            _filedir -d
            return
            ;;
        --hdlist-filter|--synthesis-filter|--xml-info-filter)
            return  # Free text (a filter expression).
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--media-info-dir --hdlist-filter \
            --synthesis-filter --xml-info-filter --no-hdlist --no-md5sum \
            --xml-info --appstream-info --clean --versioned \
            --allow-empty-media --nolock --no-bad-rpm --mageia-tree \
            --verbose -v" -- "$cur"))
    else
        _filedir -d
    fi
}

_urpm_system() {
    # export / import of a machine's whole urpm state.
    local system_subcmds="export import"

    if [[ $cword -eq 2 ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "$_URPM_DISPLAY_FLAGS" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$system_subcmds" -- "$cur"))
        fi
        return
    fi

    case "${words[2]}" in
        export)
            if [[ "$prev" == "--to" ]]; then
                _filedir
            elif [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--to" -- "$cur"))
            fi
            ;;
        import)
            if [[ "$prev" == "--from" ]]; then
                _filedir
            elif [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--from --yes -y --no-backup \
                    --dry-run --merge-media --force --allow-incoherent \
                    --preserve-hw" -- "$cur"))
            fi
            ;;
    esac
}

# ── Fallback: first word, or unknown command ─────────────────

_urpm_global() {
    # First positional: propose a command or a global flag.
    if [[ $cword -eq 1 ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "$_URPM_GLOBAL_FLAGS" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$_URPM_COMMANDS" -- "$cur"))
        fi
    fi
}

# ─────────────────────────────────────────────────────────────
# 4. Dispatcher
# ─────────────────────────────────────────────────────────────

_urpm() {
    local cur prev words cword
    _init_completion || return

    case "${words[1]}" in
        install|i)                              _urpm_install ;;
        erase|e)                                _urpm_erase ;;
        upgrade|u)                              _urpm_upgrade ;;
        update)                                 _urpm_update ;;
        distupgrade)                            _urpm_distupgrade ;;
        recover)                                _urpm_recover ;;
        download|dl)                            _urpm_download ;;
        init)                                   _urpm_init ;;
        cleanup)                                _urpm_cleanup ;;
        autoremove|ar)                          _urpm_autoremove ;;
        cleandeps|cd)                           _urpm_cleandeps ;;
        hold)                                   _urpm_hold ;;
        unhold)                                 _urpm_unhold ;;
        skip)                                   _urpm_skip ;;
        progress)                               _urpm_progress ;;
        readme)                                 _urpm_readme ;;
        mkimage)                                _urpm_mkimage ;;
        build)                                  _urpm_build ;;
        image|img)                              _urpm_image ;;

        search|s|query|q)                       _urpm_search ;;
        show|sh|info)                           _urpm_show ;;
        list|l)                                 _urpm_list ;;
        find|f)                                 _urpm_find ;;
        provides|p)                             _urpm_provides ;;
        whatprovides|wp)                        _urpm_whatprovides ;;
        depends|d|requires|req)                 _urpm_depends ;;
        rdepends|rd|whatrequires|wr)            _urpm_rdepends ;;
        recommends|whatrecommends|suggests|whatsuggests)
                                                _urpm_recommends_family ;;
        why)                                    _urpm_why ;;

        audit)                                  _urpm_audit ;;
        genmedia)                               _urpm_genmedia ;;
        system)                                 _urpm_system ;;

        mark)                                   _urpm_mark ;;
        history|h)                              _urpm_history ;;
        rollback|r)                             _urpm_rollback ;;
        undo)                                   _urpm_undo ;;

        media|m)                                _urpm_media ;;
        server|srv)                             _urpm_server ;;
        mirror|proxy)                           _urpm_mirror ;;
        cache|c)                                _urpm_cache ;;
        config|cfg)                             _urpm_config ;;
        key|k)                                  _urpm_key ;;
        peer)                                   _urpm_peer ;;
        appstream)                              _urpm_appstream ;;

        *)                                      _urpm_global ;;
    esac
}

complete -F _urpm urpm
