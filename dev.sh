#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# Simple dev launcher for ClimateClaw
#
# Custom flags (handled here, NOT passed to docker compose):
#   --debug / --DEBUG          -> DEBUG=1
#   --debug=0 / --DEBUG=0      -> DEBUG=0
#   --no-debug                 -> DEBUG=0
#   --scale                    -> use scaling with load-balancing proxy
#   --build                    -> build images before running compose
#
# Everything else is passed through to `docker compose`.
# ------------------------------------------------------------------

print_usage() {
  cat <<'EOF'
Usage: ./dev.sh [OPTIONS] [DOCKER_COMPOSE_ARGS...]

Custom options:
  -h, --help        Show this help message
  --debug, --DEBUG  Enable debug mode (CLIMATECLAW_DEBUG=1)
  --debug=VALUE     Set debug explicitly, e.g. --debug=0 or --debug=1
  --no-debug        Disable debug mode
  --scale           Generate and use docker-compose.dev.scaled.yml
  --build           Build images before starting

Examples:
  ./dev.sh up
  ./dev.sh up --build -d
  ./dev.sh --debug up --build -d
  ./dev.sh up --build -d --debug
  ./dev.sh --scale up --build

Notes:
  All non-custom arguments are passed through to:
    docker compose -f <compose-file> ...
EOF
}

# Set CLIMATECLAW_DEV flag for everything in this session
export CLIMATECLAW_DEV=1

CLIMATECLAW_DEBUG="${CLIMATECLAW_DEBUG:-0}"
COMPOSE_FILE="docker-compose.dev.yml"
BUILD_COMPOSE_FILE="${COMPOSE_FILE}"
DO_BUILD=0
COMPOSE_ARGS=()

for arg in "$@"; do
  case "$arg" in
    # Enable debug
    --debug|--DEBUG)
      CLIMATECLAW_DEBUG=1
      ;;
    # Explicit value: --debug=0 / --DEBUG=1 etc.
    --debug=*|--DEBUG=*)
      CLIMATECLAW_DEBUG="${arg#*=}"
      ;;
    # Disable debug
    --no-debug)
      CLIMATECLAW_DEBUG=0
      ;;
    # Help
    -h|--help)
      print_usage
      exit 0
      ;;
    # Launch with scaling and proxy
    --scale)
      ./gen_compose.py ${COMPOSE_FILE}
      COMPOSE_FILE="docker-compose.dev.scaled.yml"
      ;;
    # Build images once from the unscaled compose file.
    --build)
      DO_BUILD=1
      ;;
    # Everything else goes to docker compose
    *)
      COMPOSE_ARGS+=("$arg")
      ;;
  esac
done

# Export for docker compose / containers
export CLIMATECLAW_DEBUG

echo "[dev.sh] Using ${COMPOSE_FILE} with DEBUG=${CLIMATECLAW_DEBUG}"
echo "[dev.sh] docker compose -f ${COMPOSE_FILE} ${COMPOSE_ARGS[*]}"

docker compose -f "${BUILD_COMPOSE_FILE}" --profile build-only build climateclaw-base
if [ "${DO_BUILD}" = "1" ]; then
  echo "[dev.sh] Building images from ${BUILD_COMPOSE_FILE}"
  docker compose -f "${BUILD_COMPOSE_FILE}" build
fi
docker compose -f "${COMPOSE_FILE}" "${COMPOSE_ARGS[@]}"
