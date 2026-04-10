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
    # Query urpm's SQLite cache directly. The `media` table stores one
    # row per configured medium, whose name may contain spaces (e.g.
    # "Core Release"), which ruled out the old text-scraping approach.
    local cache_db="${URPM_DEV_MODE:+/var/lib/urpm-dev}/var/lib/urpm/packages.db"
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
        _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
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

# ── Additional top-level commands ────────────────────────────

# Common Mageia release values for --release on init / download.
_URPM_RELEASE_CHOICES="8 9 10 cauldron"
# Container runtime choices for mkimage / build.
_URPM_RUNTIME_CHOICES="docker podman"

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
    esac

    local download_opts="--release -r --arch --auto -y --without-recommends
        --no-peers --only-peers --nodeps --allow-arch
        --buildrequires --builddeps --br -b"

    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$download_opts" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
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
    : # No flags, no positionals — relies on --urpm-root from global flags.
}

_urpm_hold() {
    # `urpm hold` with no package lists current holds; with packages,
    # adds them. Takes installed packages only.
    case "$prev" in
        --reason|-r)
            return  # Free text.
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--reason -r --list -l" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
    fi
}

_urpm_unhold() {
    # unhold releases held packages — no command-specific flags.
    if [[ "$cur" != -* ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
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
    # mkimage builds a root chroot / OCI image.
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
        --tag|-t|--packages|-p)
            return  # Free text.
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--release -r --tag -t --profile --arch \
            --packages -p --runtime --keep-chroot --workdir -w" -- "$cur"))
    fi
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
        --with-rpms|-w)
            return  # Free text glob pattern.
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--image -i --output -o --with-rpms -w \
            --runtime --parallel -j --keep-container" -- "$cur"))
    else
        _filedir '@(spec|src.rpm)'
    fi
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
        COMPREPLY=($(compgen -W "--installed --unavailable" -- "$cur"))
    fi
    # Pattern is free text (FTS query) — no name completion.
}

_urpm_show() {
    # show / sh / info — take a package and expose --files / --changelog.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--files --changelog" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_list() {
    # list takes a single positional filter from a fixed choice list.
    # No command-specific flags beyond display_parent.
    if [[ $cword -eq 2 ]]; then
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
        COMPREPLY=($(compgen -W "--available -a --installed -i --limit -l" -- "$cur"))
    fi
}

_urpm_provides() {
    # provides / p — takes a package name.
    if [[ "$cur" != -* ]]; then
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_whatprovides() {
    # whatprovides / wp — takes a capability string or a file path.
    : # Free text, no completion.
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
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "--tree --all -a --legacy --prefer \
            --pager --no-libs --depth" -- "$cur"))
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
        COMPREPLY=($(compgen -W "--tree --all -a --depth --hide-uninstalled" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
    fi
}

_urpm_recommends_family() {
    # recommends / whatrecommends / suggests / whatsuggests — each is a
    # distinct parser in main.py (no aliases) but none expose any
    # command-specific flag beyond display_parent. All take a package.
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
    local media_subcmds="list l ls add a remove r enable e disable d update u \
set s import discover disc autoconfig auto ac seed-info link"

    if [[ $cword -eq 2 ]]; then
        COMPREPLY=($(compgen -W "$media_subcmds" -- "$cur"))
        return
    fi

    local sub="${words[2]}"
    case "$sub" in
        list|l|ls)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--all -a $_URPM_DISPLAY_FLAGS" -- "$cur"))
            fi
            ;;
        add|a)
            case "$prev" in
                --name|--custom|--version) return ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--name --custom --update --disabled \
--auto -y --import-key --allow-unsigned --version" -- "$cur"))
            fi
            ;;
        remove|r|enable|e|disable|d|seed-info)
            _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            ;;
        update|u)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--files -f --no-appstream" -- "$cur"))
            else
                _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            fi
            ;;
        import)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--replace --auto -y" -- "$cur"))
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
                    COMPREPLY=($(compgen -W "none on_demand seed" -- "$cur"))
                    return
                    ;;
                --seeds|--quota|--retention|--priority)
                    return
                    ;;
            esac
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--all -a --shared --replication \
--seeds --quota --retention --priority --sync-files --no-sync-files" -- "$cur"))
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
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--with --without --sources --debug \
--dry-run -n" -- "$cur"))
            fi
            ;;
        link)
            if [[ $cword -eq 3 ]]; then
                _urpm_compreply_from_lines "$cur" < <(_urpm_media_names)
            fi
            # Subsequent positionals are +server/-server — no dynamic list.
            ;;
    esac
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
        download|dl)                            _urpm_download ;;
        init)                                   _urpm_init ;;
        cleanup)                                _urpm_cleanup ;;
        autoremove|ar)                          _urpm_autoremove ;;
        cleandeps|cd)                           _urpm_cleandeps ;;
        hold)                                   _urpm_hold ;;
        unhold)                                 _urpm_unhold ;;
        progress)                               _urpm_progress ;;
        readme)                                 _urpm_readme ;;
        mkimage)                                _urpm_mkimage ;;
        build)                                  _urpm_build ;;

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
