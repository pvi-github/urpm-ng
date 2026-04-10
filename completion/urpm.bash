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

_urpm_install() {
    local install_opts="--auto -y --force --reinstall --download-only --nodeps
        --nosignature --without-recommends --with-suggests --prefer --all"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$install_opts" -- "$cur"))
    elif [[ "$cur" == */* || "$cur" == .* ]]; then
        _filedir rpm
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_erase() {
    local erase_opts="--auto -y --force --nodeps"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$erase_opts" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

_urpm_upgrade() {
    local upgrade_opts="--auto -y --force --download-only --without-recommends --all"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$upgrade_opts" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

_urpm_update() {
    local update_opts="--auto -y --force"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$update_opts" -- "$cur"))
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
    : # Transaction ID — dynamic completion added in C2.
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
