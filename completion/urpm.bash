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

_urpm_available_packages() {
    # Use urpm's SQLite cache when present; fall back to installed set.
    local cache_db="${URPM_DEV_MODE:+/var/lib/urpm-dev}/var/lib/urpm/packages.db"
    if [[ -f "$cache_db" ]]; then
        sqlite3 "$cache_db" "SELECT DISTINCT name FROM packages" 2>/dev/null
    else
        _urpm_installed_packages
    fi
}

_urpm_media_names() {
    urpm media list --quiet 2>/dev/null | awk '{print $1}'
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
    # Placeholder: resolved in C5 when `urpm mkimage --profile` lands.
    # Returns nothing so callers fall back to plain file completion.
    :
}

# ─────────────────────────────────────────────────────────────
# 2. Constants
# ─────────────────────────────────────────────────────────────

# Global flags accepted before or alongside any subcommand.
_URPM_GLOBAL_FLAGS="--help -h --version -V --verbose -v --quiet -q \
    --nocolor --root --urpm-root --dev"

# display_parent — inherited by ~21 read-only / reporting commands.
_URPM_DISPLAY_FLAGS="--json --flat --show-all"

# debug_parent — inherited by install / download / update / upgrade.
_URPM_DEBUG_FLAGS="--debug --watched"

# Real top-level commands, mirroring urpm/cli/main.py subparsers.
# Phantom entries that existed in the old completion (kernel-keep,
# blacklist, redlist, seed) have been removed: they are sub-subcommands
# of `config` / `media`, never real top-level commands.
_URPM_COMMANDS="install i erase e upgrade u update up \
    search s query q show sh info list l find f \
    provides p whatprovides wp \
    depends d requires req rdepends rd whatrequires wr \
    recommends whatrecommends suggests whatsuggests why \
    download dl init cleanup autoremove ar cleandeps cd \
    hold unhold mark progress readme mkimage build \
    history h rollback r undo \
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
        --allow-arch)
            COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
            return
            ;;
        --buildrequires|--builddeps|--br|-b)
            _filedir '@(spec|src.rpm)'
            return
            ;;
    esac

    local install_opts="--auto -y --test --force --reinstall --download-only
        --nodeps --nosignature --noscripts --without-recommends --with-suggests
        --prefer --all --no-peers --only-peers --allow-arch --sync --config-policy
        --buildrequires --builddeps --br -b --install-src"

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$install_opts" -- "$cur"))
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
    esac

    local erase_opts="--auto -y --test --force --auto-orphans --keep-orphans
        --erase-recommends --keep-suggests --debug --sync"

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$erase_opts" -- "$cur"))
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
        --allow-arch)
            COMPREPLY=($(compgen -W "$_URPM_ARCH_CHOICES" -- "$cur"))
            return
            ;;
    esac

    local upgrade_opts="--auto -y --test --force --download-only --nosignature
        --noerase-orphans --with-recommends --with-suggests --no-peers
        --only-peers --allow-arch --sync --config-policy"

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$upgrade_opts" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

_urpm_update() {
    # Top-level `update` is a shortcut for `media update`; it takes a
    # media name (optional, default = all) and only one extra flag.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--files" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_media_names)" -- "$cur"))
    fi
}

_urpm_autoremove() {
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--auto -y --dry-run" -- "$cur"))
    fi
}

_urpm_cleandeps() {
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--auto -y --dry-run" -- "$cur"))
    fi
}

# ── Query / inspection commands ──────────────────────────────

_urpm_search() {
    local search_opts="--installed --available --all"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$search_opts" -- "$cur"))
    fi
    # Package name is free text — no name completion on purpose.
}

_urpm_show() {
    if [[ "$cur" != -* ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_list() {
    local list_opts="--installed --available --upgradable --recent --orphans --autoremovable"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$list_opts" -- "$cur"))
    fi
}

_urpm_find() {
    : # File path or capability — free text, no completion.
}

_urpm_whatprovides() {
    : # Capability string — no specific completion.
}

_urpm_depends() {
    local depends_opts="--installed --available --recursive --tree"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$depends_opts" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_rdepends() {
    local depends_opts="--installed --available --recursive --tree"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$depends_opts" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_recommends_family() {
    # recommends / whatrecommends / suggests / whatsuggests — all take package names.
    if [[ "$cur" != -* ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_why() {
    if [[ "$cur" != -* ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

# ── State management ─────────────────────────────────────────

_urpm_mark() {
    local mark_subcmds="manual m explicit auto a dep show s list"
    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$mark_subcmds" -- "$cur"))
    elif [[ $cword -gt 2 && "${words[2]}" =~ ^(manual|m|explicit|auto|a|dep)$ ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

_urpm_history() {
    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "list search show" -- "$cur"))
    fi
}

_urpm_rollback() {
    : # Transaction ID — dynamic completion added in C2.
}

_urpm_undo() {
    if [[ "$cur" != -* ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_transaction_ids)" -- "$cur"))
    fi
}

# ── Subcommand groups ────────────────────────────────────────

_urpm_media() {
    local media_subcmds="list l ls add a remove r enable e disable d update u set s"
    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$media_subcmds" -- "$cur"))
    elif [[ $cword -gt 2 ]]; then
        case "${words[2]}" in
            remove|r|enable|e|disable|d|update|u|set|s)
                COMPREPLY=($(compgen -W "$(_urpm_media_names)" -- "$cur"))
                ;;
        esac
    fi
}

_urpm_server() {
    local server_subcmds="list l ls add a remove r rm enable e disable d test t autoconfig auto"
    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$server_subcmds" -- "$cur"))
    elif [[ $cword -eq 3 ]]; then
        case "${words[2]}" in
            remove|r|rm|enable|e|disable|d|test|t)
                # TODO(C7): switch to _urpm_server_names once the helper lands.
                COMPREPLY=($(compgen -W "$(_urpm_media_names)" -- "$cur"))
                ;;
        esac
    fi
}

_urpm_mirror() {
    local mirror_subcmds="status enable disable quota"
    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$mirror_subcmds" -- "$cur"))
    fi
}

_urpm_cache() {
    local cache_subcmds="info clean rebuild stats"
    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$cache_subcmds" -- "$cur"))
    fi
}

_urpm_config() {
    : # Entire config dispatch is added in C8.
}

_urpm_key() {
    local key_subcmds="list ls l import i add remove rm del"
    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$key_subcmds" -- "$cur"))
    elif [[ $cword -gt 2 && "${words[2]}" =~ ^(import|i|add)$ ]]; then
        _filedir
    fi
}

_urpm_peer() {
    local peer_subcmds="list ls downloads dl blacklist bl block unblacklist unbl unblock"
    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$peer_subcmds" -- "$cur"))
    fi
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
        update|up)                              _urpm_update ;;
        autoremove|ar)                          _urpm_autoremove ;;
        cleandeps|cd)                           _urpm_cleandeps ;;

        search|s|query|q)                       _urpm_search ;;
        show|sh|info)                           _urpm_show ;;
        list|l)                                 _urpm_list ;;
        find|f|provides|p)                      _urpm_find ;;
        whatprovides|wp)                        _urpm_whatprovides ;;
        depends|d|requires|req)                 _urpm_depends ;;
        rdepends|rd|whatrequires|wr)            _urpm_rdepends ;;
        recommends|whatrecommends|suggests|whatsuggests)
                                                _urpm_recommends_family ;;
        why)                                    _urpm_why ;;

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

        *)                                      _urpm_global ;;
    esac
}

complete -F _urpm urpm
