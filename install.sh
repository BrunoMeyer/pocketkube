#!/usr/bin/env bash
# Interactive installer. Keep the main call last so a partial download cannot run it.
set -euo pipefail

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
ask() {
    local answer
    printf '%s [%s]: ' "$2" "$3" >&3
    IFS= read -r answer <&3 || fail 'Input closed; installation cancelled.'
    printf -v "$1" '%s' "${answer:-$3}"
}
choose() {
    local value
    while :; do
        ask value "$2" "$3"
        case "|$4|" in *"|$value|"*) printf -v "$1" '%s' "$value"; return;; esac
        printf 'Choose one of: %s\n' "$4" >&3
    done
}
absolute_path() {
    case "$1" in
        '~') printf '%s' "$HOME";;
        '~/'*) printf '%s/%s' "$HOME" "${1:2}";;
        /*) printf '%s' "$1";;
        *) printf '%s/%s' "$PWD" "$1";;
    esac
}

main() {
    if [[ ${1:-} == --help ]]; then
        printf 'Usage: bash install.sh\nInteractive Termux/Linux installer; requires a terminal.\n'
        return
    fi
    [[ $# == 0 ]] || fail 'Only --help is supported.'
    exec 3<> /dev/tty || fail 'An interactive terminal is required. Download the script and run bash install.sh in a terminal.'
    [[ $(uname -s) == Linux ]] || fail 'This installer supports Linux and Android/Termux.'
    umask 077
    local termux=no runtime=proot metrics=host install_dir ref host port config start answer
    local repo=${POCKETKUBE_INSTALL_REPO:-https://github.com/BrunoMeyer/pocketkube.git}
    if [[ -n ${TERMUX_VERSION:-} || ${PREFIX:-} == /data/*/files/usr ]]; then
        termux=yes
        metrics=visible-processes
    fi
    printf '\nPocketKube installer\n\n' >&3
    ask install_dir 'Installation directory (must be empty)' "$HOME/.local/share/pocketkube"
    install_dir=$(absolute_path "$install_dir")
    [[ ! -e $install_dir && ! -L $install_dir ]] || {
        [[ -d $install_dir && -z $(ls -A -- "$install_dir") ]] || fail "Installation directory is not empty: $install_dir"
    }
    ask ref 'Git branch or release tag' main
    [[ $ref != -* && -n $ref ]] || fail 'Invalid branch or tag.'
    choose runtime 'Runtime: proot or docker' "$runtime" 'proot|docker'
    printf 'The API has no authentication. Use a trusted network if binding beyond localhost.\n' >&3
    ask host 'API listen address' 127.0.0.1
    [[ $host =~ ^[a-zA-Z0-9.:_-]+$ && $host != -* ]] || fail 'Enter an IP address or hostname, without a URL or brackets.'
    ask port 'API port' 8443
    [[ $port =~ ^[0-9]{1,5}$ ]] || fail 'Port must be an integer from 1 to 65535.'
    port=$((10#$port))
    ((port >= 1 && port <= 65535)) || fail 'Port must be from 1 to 65535.'
    printf 'Metrics: host = whole machine; visible-processes = accessible processes only (useful on Android).\n' >&3
    choose metrics 'Metrics scope' "$metrics" 'host|visible-processes'
    ask config 'Kubeconfig path' "$HOME/.kube/pocketkube.kubeconfig"
    config=$(absolute_path "$config")
    [[ ! -d $config ]] || fail 'Kubeconfig path is a directory.'
    if [[ -e $config || -L $config ]]; then
        choose answer 'Replace existing kubeconfig?' no 'yes|no'
        [[ $answer == yes ]] || fail 'Existing kubeconfig preserved; choose another path on your next run.'
    fi
    choose start 'Launch the server after installation? (foreground; Ctrl-C stops it)' yes 'yes|no'

    local -a packages=()
    command -v python3 >/dev/null || command -v python >/dev/null || packages+=(python)
    command -v git >/dev/null || packages+=(git)
    if [[ $runtime == proot ]]; then
        command -v proot >/dev/null || packages+=(proot)
    else
        command -v docker >/dev/null || fail 'Install Docker and configure access to its daemon, then rerun this installer.'
    fi
    if ((${#packages[@]})); then
        if [[ $termux == yes ]] && command -v pkg >/dev/null; then
            choose answer "Install missing Termux packages: ${packages[*]}?" yes 'yes|no'
            [[ $answer == yes ]] || fail 'Required dependencies are missing.'
            pkg install -y "${packages[@]}"
        else
            fail "Install these dependencies with your package manager, then retry: ${packages[*]}. Python must include venv and pip."
        fi
    fi
    local python
    python=$(command -v python3 || command -v python)
    "$python" -c 'import sys; sys.exit("Python 3.8 or newer is required") if sys.version_info < (3,8) else None'
    if [[ $runtime == docker ]]; then
        docker info >/dev/null || fail 'Cannot access the Docker daemon.'
    fi
    printf '\nInstalling %s from %s into %s\n' "$ref" "$repo" "$install_dir"
    mkdir -p -- "$install_dir"
    git clone --depth 1 --branch "$ref" -- "$repo" "$install_dir/source"
    "$python" -m venv "$install_dir/venv" || fail "Cannot create a virtual environment. Install Python venv support. Partial installation remains at $install_dir."
    "$install_dir/venv/bin/python" -m pip install "$install_dir/source"

    # Bash %q preserves literal user input without evaluating it as shell code.
    {
        printf 'export POCKETKUBE_METRICS_SCOPE=%q\n' "$metrics"
        printf 'POCKETKUBE_HOST=%q\nPOCKETKUBE_PORT=%q\nPOCKETKUBE_RUNTIME=%q\n' "$host" "$port" "$runtime"
    } > "$install_dir/config.env"
    {
        printf '#!/usr/bin/env bash\nset -euo pipefail\n'
        printf 'source %q\n' "$install_dir/config.env"
        printf 'exec %q -m pocketkube.cli serve --host "$POCKETKUBE_HOST" --port "$POCKETKUBE_PORT" --runtime "$POCKETKUBE_RUNTIME" "$@"\n' "$install_dir/venv/bin/python"
    } > "$install_dir/start.sh"
    chmod 700 "$install_dir/start.sh"
    local client_host=$host server temporary_config
    case "$host" in 0.0.0.0) client_host=127.0.0.1;; ::) client_host=::1;; esac
    [[ $client_host != *:* ]] || client_host="[$client_host]"
    server="http://$client_host:$port"
    mkdir -p -- "$(dirname -- "$config")"
    temporary_config=$(mktemp "${config}.tmp.XXXXXX")
    if ! "$install_dir/venv/bin/python" -m pocketkube.cli kubeconfig --server "$server" --output "$temporary_config"; then
        rm -f -- "$temporary_config"
        fail 'Could not generate kubeconfig.'
    fi
    mv -f -- "$temporary_config" "$config"
    printf '\nInstalled PocketKube.\nStart server: %q\nConfiguration: %s\n' "$install_dir/start.sh" "$install_dir/config.env"
    printf 'Test from another terminal: kubectl --kubeconfig %q get nodes\n' "$config"
    printf 'For remote clients, copy the kubeconfig and replace its server address with this device’s reachable address.\n'
    if [[ $start == yes ]]; then
        printf '\nStarting PocketKube. Keep this terminal open; Ctrl-C stops the server.\n'
        exec "$install_dir/start.sh" <&3 3>&-
    fi
}

main "$@"
