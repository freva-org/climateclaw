#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] [COMPOSE_ARGS...]

Deploy the freva-gpt stack via podman compose.

Generates a scaled compose file from docker-compose.yml, tears down any
previous deployment, optionally rebuilds images, and starts the services.

Options:
  --build       Build all images before starting (runs as a separate step)
  -h, --help    Show this help message and exit

Any additional arguments are forwarded to the compose 'up' command.
If no compose arguments are given, defaults to 'up -d'.

Examples:
  $(basename "$0") --build              Build images, then start detached
  $(basename "$0") --build up -d        Same as above (explicit)
  $(basename "$0")                      Start without rebuilding
  $(basename "$0") up --force-recreate  Recreate containers without rebuild

Requires:
  podman                                Container engine
  podman compose  OR  podman-compose    Compose tool (plugin preferred)
EOF
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        -h|--help) usage ;;
    esac
done

COMPOSE_FILE="docker-compose.yml"
SCALED_FILE="docker-compose.scaled.yml"

# --- Require podman ---
if ! command -v podman &>/dev/null; then
    echo "[prod.sh] ERROR: podman is not installed" >&2
    exit 1
fi

# --- Pick compose tool: prefer podman-compose, fall back to plugin ---
if command -v podman-compose &>/dev/null; then
    echo "[prod.sh] Found podman-compose" >&2
    COMPOSE="podman-compose"
elif podman compose version &>/dev/null 2>&1; then
    echo "[prod.sh] WARNING: podman-compose not found, falling back to plugin"
    # Ensure the podman socket is up (needed by the compose plugin)
    sock="/run/user/$(id -u)/podman/podman.sock"
    if [ ! -S "$sock" ]; then
        echo "[prod.sh] Starting podman user socket ..."
        # Try systemd first, fall back to manual
        if systemctl --user start podman.socket 2>/dev/null; then
            echo "[prod.sh] podman.socket started via systemd"
        else
            podman system service --time=0 "unix://$sock" &
            sleep 1
            echo "[prod.sh] podman.socket started manually"
        fi
    fi
    COMPOSE="podman compose"
else
    echo "[prod.sh] ERROR: neither 'podman compose' plugin nor 'podman-compose' are available" >&2
    exit 1
fi

# --- Generate scaled compose file ---
echo "[prod.sh] Generating scaled compose file from ${COMPOSE_FILE}"
./gen_compose.py "${COMPOSE_FILE}"

# --- Tear down previous deployment ---
${COMPOSE} -f "${SCALED_FILE}" down

# --- Handle --build: split it out so build and up are separate steps ---
do_build=false
args=()
for arg in "$@"; do
    if [ "$arg" = "--build" ]; then
        do_build=true
    else
        args+=("$arg")
    fi
done

if [ "$do_build" = true ]; then
    echo "[prod.sh] Building images ..."
    ${COMPOSE} -f "${SCALED_FILE}" build
fi

# --- Start ---
if [ ${#args[@]} -eq 0 ]; then
    args=(up -d)
fi
echo "[prod.sh] Starting: ${COMPOSE} -f ${SCALED_FILE} ${args[*]}"
${COMPOSE} -f "${SCALED_FILE}" "${args[@]}"
