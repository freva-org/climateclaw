#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] [COMPOSE_ARGS...]

Deploy the climateclaw stack via podman-compose.

Generates a scaled compose file from docker-compose.yml, tears down any
previous deployment, optionally rebuilds images, and starts the services.

Options:
  --project NAME  Deployment project name (required; can also be set in .env)
  --build       Build all images before starting (runs as a separate step)
  -h, --help    Show this help message and exit

Any additional arguments are forwarded to the compose 'up' command.
If no compose arguments are given, defaults to 'up -d'.

Examples:
  $(basename "$0") --project freva-prod --build              Build images, then start detached
  $(basename "$0") --project=freva-prod --build up -d        Same as above (explicit)
  $(basename "$0") --project freva-prod                      Start without rebuilding
  $(basename "$0") --project freva-prod up --force-recreate  Recreate containers without rebuild

Requires:
  CLIMATECLAW_PROJECT_NAME                 Project name in .env or --project
  podman                                Container engine
  podman compose  OR  podman-compose    Compose tool (plugin preferred)
EOF
    exit 0
}

COMPOSE_FILE="docker-compose.yml"
SCALED_FILE="docker-compose.scaled.yml"
ENV_FILE=".env"
PROJECT="${CLIMATECLAW_PROJECT_NAME:-}"
do_build=false
args=()

# If project var is not set in the shell, read from .env
# Setting it with flag, overrides both shell and .env
if [ -z "${PROJECT}" ] && [ -f "${ENV_FILE}" ]; then
    PROJECT="$(
        sed -n 's/^[[:space:]]*CLIMATECLAW_PROJECT_NAME[[:space:]]*=[[:space:]]*//p' "${ENV_FILE}" \
            | tail -n 1 \
            | sed 's/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//'
    )"
fi

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            ;;
        --project)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "[prod.sh] ERROR: --project requires a value" >&2
                exit 1
            fi
            PROJECT="$2"
            shift 2
            ;;
        --project=*)
            PROJECT="${1#*=}"
            if [ -z "${PROJECT}" ]; then
                echo "[prod.sh] ERROR: --project requires a value" >&2
                exit 1
            fi
            shift
            ;;
        --build)
            do_build=true
            shift
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done

if [ -z "${PROJECT}" ]; then
    echo "[prod.sh] ERROR: project is required. Set CLIMATECLAW_PROJECT_NAME in .env or pass --project NAME." >&2
    exit 1
fi

export CLIMATECLAW_PROJECT_NAME="${PROJECT}"

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
    echo "[prod.sh] ERROR: neither 'podman-compose' nor 'podman compose' are available" >&2
    exit 1
fi

# --- Generate scaled compose file ---
echo "[prod.sh] Generating scaled compose file from ${COMPOSE_FILE}"
echo "[prod.sh] Using project: ${CLIMATECLAW_PROJECT_NAME}"
./gen_compose.py "${COMPOSE_FILE}" "${PROJECT}"

# --- Tear down previous deployment ---
${COMPOSE} -f "${SCALED_FILE}" down

echo "[prod.sh] Building climateclaw-base from ${COMPOSE_FILE}"
${COMPOSE} -f "${COMPOSE_FILE}" --profile build-only build climateclaw-base

if [ "$do_build" = true ]; then
    echo "[prod.sh] Building images ..."
    ${COMPOSE} -f "${COMPOSE_FILE}" build
fi

# --- Start ---
if [ ${#args[@]} -eq 0 ]; then
    args=(up -d)
fi
echo "[prod.sh] Starting: ${COMPOSE} -f ${SCALED_FILE} ${args[*]}"
${COMPOSE} -f "${SCALED_FILE}" "${args[@]}"
