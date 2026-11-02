#!/usr/bin/env bash
# EventHub bootstrap: install the bare minimum, fetch the project, create the
# virtualenv and hand over to the Python installer/TUI.  This is the only
# non-Python entry point in the project.
set -Eeuo pipefail

REPO_URL="${EVENTHUB_REPO_URL:-https://github.com/jarrfarr/portapix-v2}"
BRANCH="${EVENTHUB_BRANCH:-master}"
INSTALL_ROOT="${EVENTHUB_INSTALL_ROOT:-/opt/eventhub}"
VENV="${INSTALL_ROOT}/venv"
REPO_DIR="${INSTALL_ROOT}/repo"

log()  { printf '[info] %s\n' "$*" >&2; }
ok()   { printf '[ok]   %s\n' "$*" >&2; }
warn() { printf '[warn] %s\n' "$*" >&2; }
fail() { printf '[fail] %s\n' "$*" >&2; exit 1; }

trap 'fail "bootstrap failed at line ${LINENO}"' ERR

if [ "$(id -u)" -ne 0 ]; then
    fail "bootstrap must run as root (try: curl ... | sudo bash)"
fi

# ---------------------------------------------------------------------------
# Distro / package manager
# ---------------------------------------------------------------------------
detect_pm() {
    if command -v apt-get >/dev/null 2>&1; then echo apt
    elif command -v dnf >/dev/null 2>&1; then echo dnf
    elif command -v pacman >/dev/null 2>&1; then echo pacman
    else echo unknown
    fi
}

PM="$(detect_pm)"
log "package manager: ${PM}"

install_minimum() {
    case "${PM}" in
        apt)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -y
            apt-get install -y --no-install-recommends git curl ca-certificates \
                python3 python3-venv python3-pip
            ;;
        dnf)
            dnf install -y git curl ca-certificates python3 python3-pip
            ;;
        pacman)
            pacman -Sy --noconfirm --needed git curl ca-certificates python python-pip
            ;;
        *)
            fail "unsupported distribution: install git, curl, python3 and venv support manually"
            ;;
    esac
    ok "bootstrap dependencies installed"
}

install_minimum

# ---------------------------------------------------------------------------
# Source checkout
# ---------------------------------------------------------------------------
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/pyproject.toml" ]; then
    SRC="${SCRIPT_DIR}"
    log "installing from local checkout: ${SRC}"
else
    if printf '%s' "${REPO_URL}" | grep -q 'OWNER'; then
        fail "REPO_URL is still the placeholder '${REPO_URL}'. Set EVENTHUB_REPO_URL or run bootstrap.sh from a checkout."
    fi
    mkdir -p "${INSTALL_ROOT}"
    if [ -d "${REPO_DIR}/.git" ]; then
        log "updating ${REPO_DIR}"
        git -C "${REPO_DIR}" fetch --depth 1 origin "${BRANCH}"
        git -C "${REPO_DIR}" checkout -q "${BRANCH}"
        git -C "${REPO_DIR}" pull --ff-only origin "${BRANCH}"
    else
        log "cloning ${REPO_URL} (${BRANCH})"
        git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${REPO_DIR}"
    fi
    SRC="${REPO_DIR}"
fi
ok "source ready at ${SRC}"

# ---------------------------------------------------------------------------
# Virtualenv and Python package
# ---------------------------------------------------------------------------
mkdir -p "${INSTALL_ROOT}"
if [ ! -x "${VENV}/bin/python" ]; then
    log "creating virtualenv at ${VENV}"
    python3 -m venv "${VENV}"
fi
"${VENV}/bin/pip" install --upgrade pip >/dev/null
log "installing EventHub (this can take a few minutes on a Pi)"
"${VENV}/bin/pip" install --upgrade "${SRC}[copyparty]"
ok "EventHub installed"

# ---------------------------------------------------------------------------
# Launch the TUI (or a non-interactive install when no terminal exists)
# ---------------------------------------------------------------------------
NONINTERACTIVE=0
if [ ! -t 0 ]; then
    if [ -r /dev/tty ]; then
        # curl | bash: reattach the controlling terminal so Textual works.
        exec </dev/tty
    else
        NONINTERACTIVE=1
    fi
fi

if [ "${NONINTERACTIVE}" -eq 1 ]; then
    warn "no terminal available; running a non-interactive install"
    exec "${VENV}/bin/eventhub" install --yes "$@"
fi

if [ $# -gt 0 ]; then
    exec "${VENV}/bin/eventhub" "$@"
fi
exec "${VENV}/bin/eventhub"
