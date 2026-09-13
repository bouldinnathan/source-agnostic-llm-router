#!/bin/sh
set -eu

router_repo_url=${LLM_ROUTER_REPO_URL:-https://github.com/bouldinnathan/source-agnostic-llm-router.git}
router_version=${LLM_ROUTER_VERSION:-main}
router_install_dir=${LLM_ROUTER_INSTALL_DIR:-"${HOME}/.local/share/source-agnostic-llm-router"}
router_bin_dir=${LLM_ROUTER_BIN_DIR:-"${HOME}/.local/bin"}
router_config_home=${XDG_CONFIG_HOME:-"${HOME}/.config"}
router_service=0

for router_arg in "$@"; do
    case "$router_arg" in
        --service) router_service=1 ;;
        --help|-h)
            printf '%s\n' 'Usage: sh install.sh [--service]' \
                'Installs/updates from main; set LLM_ROUTER_VERSION to pin a Git revision.' \
                '--service: enable a systemd user service at boot and after logout.' \
                'Run as your normal user. sudo may be needed for prerequisites/lingering.'
            exit 0
            ;;
        *) printf 'Unknown argument: %s\n' "$router_arg" >&2; exit 2 ;;
    esac
done

if [ "$router_service" -eq 1 ]; then
    if [ "$(id -u)" -eq 0 ]; then
        printf '%s\n' 'Run --service as your normal user, without sudo.' >&2
        exit 2
    fi
    if ! command -v systemctl >/dev/null 2>&1 || ! command -v loginctl >/dev/null 2>&1; then
        printf '%s\n' '--service requires Linux with systemd and loginctl.' >&2
        exit 2
    fi
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        printf '%s\n' 'No systemd user session: run this from a normal local or SSH login.' >&2
        exit 2
    fi
fi

find_python() {
    if [ -n "${LLM_ROUTER_PYTHON:-}" ]; then
        if command -v "$LLM_ROUTER_PYTHON" >/dev/null 2>&1; then
            printf '%s\n' "$LLM_ROUTER_PYTHON"
            return 0
        fi
        return 1
    fi
    for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if ! command -v "$candidate" >/dev/null 2>&1; then
            continue
        fi
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
            >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

install_prerequisites() {
    if [ ! -r /etc/os-release ]; then
        return 1
    fi
    . /etc/os-release
    if [ "$(id -u)" -eq 0 ]; then
        privilege=""
    elif command -v sudo >/dev/null 2>&1; then
        privilege="sudo"
    else
        return 1
    fi
    case " ${ID:-} ${ID_LIKE:-} " in
        *" ubuntu "*|*" debian "*)
            $privilege apt-get update
            $privilege apt-get install -y python3 python3-venv python3-pip git ca-certificates
            ;;
        *" arch "*|*" manjaro "*)
            $privilege pacman -S --needed --noconfirm python python-pip git ca-certificates
            ;;
        *) return 1 ;;
    esac
}

router_python=$(find_python || true)
if [ -z "$router_python" ]; then
    install_prerequisites || {
        printf '%s\n' "Python 3.10+ with venv support is required." >&2
        exit 2
    }
    router_python=$(find_python || true)
fi
if [ -z "$router_python" ]; then
    printf '%s\n' 'Python 3.10+ is required; upgrade your distribution or set LLM_ROUTER_PYTHON.' >&2
    exit 2
fi

if ! command -v git >/dev/null 2>&1 && [ -z "${LLM_ROUTER_SOURCE:-}" ]; then
    install_prerequisites || {
        printf '%s\n' "git is required to install from the repository." >&2
        exit 2
    }
fi

mkdir -p "$router_install_dir" "$router_bin_dir"
router_install_dir=$(cd "$router_install_dir" && pwd -P)
router_bin_dir=$(cd "$router_bin_dir" && pwd -P)
if ! "$router_python" -m venv "$router_install_dir/venv"; then
    install_prerequisites
    "$router_python" -m venv "$router_install_dir/venv"
fi

router_venv_python="$router_install_dir/venv/bin/python"
"$router_venv_python" -m pip install --upgrade pip
router_source=${LLM_ROUTER_SOURCE:-"git+${router_repo_url}@${router_version}"}
"$router_venv_python" -m pip install --upgrade --force-reinstall "$router_source"

for command_name in llm-router llm-router-gateway llm-router-mcp; do
    ln -sf "$router_install_dir/venv/bin/$command_name" "$router_bin_dir/$command_name"
done

"$router_bin_dir/llm-router" --help >/dev/null
printf '%s\n' "Installed source-agnostic-llm-router ${router_version}."
printf '%s\n' "Commands are in ${router_bin_dir}; add it to PATH if needed."
if [ "$router_service" -eq 1 ]; then
    "$router_venv_python" -m llm_router.service \
        --install-dir "$router_install_dir" --config-home "$router_config_home"
else
    printf '%s\n' "Run: ${router_bin_dir}/llm-router provision --dry-run"
    printf '%s\n' "Run: ${router_bin_dir}/llm-router serve --host 127.0.0.1 --port 8088"
    printf '%s\n' 'Or rerun this installer with --service for an always-on systemd service.'
fi
