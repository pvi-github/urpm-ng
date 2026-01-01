# Bash completion for urpm
# Install: cp urpm.bash /etc/bash_completion.d/urpm

_urpm_installed_packages() {
    # Get installed package names (fast, from rpm database)
    rpm -qa --qf '%{NAME}\n' 2>/dev/null | sort -u
}

_urpm_available_packages() {
    # Get available package names from urpm cache
    # This uses the SQLite database for speed
    local cache_db="${URPM_DEV_MODE:+/var/lib/urpm-dev}/var/lib/urpm/packages.db"
    if [[ -f "$cache_db" ]]; then
        sqlite3 "$cache_db" "SELECT DISTINCT name FROM packages" 2>/dev/null
    else
        # Fallback: use installed packages
        _urpm_installed_packages
    fi
}

_urpm_media_names() {
    # Get media names from urpm
    urpm media list --quiet 2>/dev/null | awk '{print $1}'
}

_urpm() {
    local cur prev words cword
    _init_completion || return

    # Main commands and aliases
    local commands="install i erase e search s query q show sh info list l
        provides p whatprovides wp find f depends d requires req
        rdepends rd whatrequires wr recommends whatrecommends suggests whatsuggests
        why update up upgrade u autoremove ar mark media m server srv
        mirror proxy cache c history h rollback r undo cleandeps cd
        config cfg blacklist bl redlist rl kernel-keep kk key k peer seed"

    # Commands that take installed packages
    local installed_pkg_cmds="erase e why autoremove ar cleandeps cd"

    # Commands that take available packages
    local available_pkg_cmds="install i depends d requires req rdepends rd whatrequires wr
        recommends whatrecommends suggests whatsuggests show sh info
        provides p whatprovides wp find f search s query q"

    # Commands with subcommands
    local media_subcmds="list l ls add a remove r enable e disable d update u set s"
    local server_subcmds="list l ls add a remove r rm enable e disable d test t autoconfig auto"
    local mirror_subcmds="status enable disable quota"
    local cache_subcmds="info clean rebuild stats"
    local history_subcmds="list search show"
    local key_subcmds="list ls l import i add remove rm del"
    local peer_subcmds="list ls downloads dl blacklist bl block unblacklist unbl unblock"
    local blacklist_subcmds="list ls add a remove rm"
    local redlist_subcmds="list ls add a remove rm"
    local mark_subcmds="manual m explicit auto a dep show s list"

    # Global options
    local global_opts="--help -h --version --dev"

    # Install options
    local install_opts="--auto -y --force --reinstall --download-only --nodeps
        --nosignature --without-recommends --with-suggests --prefer --all"

    # Erase options
    local erase_opts="--auto -y --force --nodeps"

    # Upgrade options
    local upgrade_opts="--auto -y --force --download-only --without-recommends"

    # Update options
    local update_opts="--auto -y --force"

    # Search options
    local search_opts="--installed --available --all"

    # List options
    local list_opts="--installed --available --upgradable --recent --orphans --autoremovable"

    # Depends options
    local depends_opts="--installed --available --recursive --tree"

    case "${words[1]}" in
        install|i)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$install_opts" -- "$cur"))
            else
                # Complete with available packages or file paths
                if [[ "$cur" == */* || "$cur" == .* ]]; then
                    # Path completion for local RPM files
                    _filedir rpm
                else
                    COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
                fi
            fi
            ;;
        erase|e)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$erase_opts" -- "$cur"))
            else
                COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
            fi
            ;;
        search|s|query|q)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$search_opts" -- "$cur"))
            fi
            # Don't complete package names for search (free text)
            ;;
        show|sh|info)
            if [[ "$cur" != -* ]]; then
                COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
            fi
            ;;
        list|l)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$list_opts" -- "$cur"))
            fi
            ;;
        provides|p|find|f)
            # File path or capability - no completion
            ;;
        whatprovides|wp)
            # Capability - no specific completion
            ;;
        depends|d|requires|req)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$depends_opts" -- "$cur"))
            else
                COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
            fi
            ;;
        rdepends|rd|whatrequires|wr)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$depends_opts" -- "$cur"))
            else
                COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
            fi
            ;;
        recommends|whatrecommends|suggests|whatsuggests)
            if [[ "$cur" != -* ]]; then
                COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
            fi
            ;;
        why)
            if [[ "$cur" != -* ]]; then
                COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
            fi
            ;;
        update|up)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$update_opts" -- "$cur"))
            else
                # Media names for selective update
                COMPREPLY=($(compgen -W "$(_urpm_media_names)" -- "$cur"))
            fi
            ;;
        upgrade|u)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$upgrade_opts --all" -- "$cur"))
            else
                # Package names for selective upgrade
                COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
            fi
            ;;
        autoremove|ar)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--auto -y --dry-run" -- "$cur"))
            fi
            ;;
        mark)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "$mark_subcmds" -- "$cur"))
            elif [[ $cword -gt 2 && "${words[2]}" =~ ^(manual|m|explicit|auto|a|dep)$ ]]; then
                COMPREPLY=($(compgen -W "$(_urpm_installed_packages)" -- "$cur"))
            fi
            ;;
        media|m)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "$media_subcmds" -- "$cur"))
            elif [[ $cword -gt 2 ]]; then
                case "${words[2]}" in
                    remove|r|enable|e|disable|d|update|u|set|s)
                        COMPREPLY=($(compgen -W "$(_urpm_media_names)" -- "$cur"))
                        ;;
                esac
            fi
            ;;
        server|srv)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "$server_subcmds" -- "$cur"))
            elif [[ $cword -eq 3 ]]; then
                case "${words[2]}" in
                    remove|r|rm|enable|e|disable|d|test|t)
                        # Could complete with server names, but need media first
                        COMPREPLY=($(compgen -W "$(_urpm_media_names)" -- "$cur"))
                        ;;
                esac
            fi
            ;;
        mirror|proxy)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "$mirror_subcmds" -- "$cur"))
            fi
            ;;
        cache|c)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "$cache_subcmds" -- "$cur"))
            fi
            ;;
        history|h)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "list search show" -- "$cur"))
            fi
            ;;
        rollback|r)
            # Transaction ID - no completion
            ;;
        undo)
            # Transaction ID - no completion
            ;;
        cleandeps|cd)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--auto -y --dry-run" -- "$cur"))
            fi
            ;;
        config|cfg)
            # Config keys - could add completion later
            ;;
        blacklist|bl)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "$blacklist_subcmds" -- "$cur"))
            elif [[ $cword -gt 2 && "${words[2]}" =~ ^(add|a)$ ]]; then
                COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
            fi
            ;;
        redlist|rl)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "$redlist_subcmds" -- "$cur"))
            elif [[ $cword -gt 2 && "${words[2]}" =~ ^(add|a)$ ]]; then
                COMPREPLY=($(compgen -W "$(_urpm_available_packages)" -- "$cur"))
            fi
            ;;
        key|k)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "$key_subcmds" -- "$cur"))
            elif [[ $cword -gt 2 && "${words[2]}" =~ ^(import|i|add)$ ]]; then
                _filedir
            fi
            ;;
        peer)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "$peer_subcmds" -- "$cur"))
            fi
            ;;
        kernel-keep|kk)
            # Number of kernels to keep
            COMPREPLY=($(compgen -W "1 2 3 4 5" -- "$cur"))
            ;;
        *)
            # First argument - complete commands
            if [[ $cword -eq 1 ]]; then
                if [[ "$cur" == -* ]]; then
                    COMPREPLY=($(compgen -W "$global_opts" -- "$cur"))
                else
                    COMPREPLY=($(compgen -W "$commands" -- "$cur"))
                fi
            fi
            ;;
    esac
}

complete -F _urpm urpm
