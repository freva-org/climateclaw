#!/usr/bin/env bash
# Benchmark the backend with instant stand-ins for all its dependencies.
#
# Usage: benchmarks/backend/run.sh [loadgen options...]
#   e.g. benchmarks/backend/run.sh -c 1,16,64 -d 20 --markdown results.md
#
# Env: BENCH_PROJECT_ROOT (checkout whose backend code is benchmarked,
#        default: this repository; used to benchmark another branch),
#      BENCH_WORKERS (backend workers, default 1),
#      BENCH_SHARED_SSL_CONTEXT=1 (experiment, see run_backend.py)
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "${HERE}/../.." && pwd)"
PROJECT="$(cd -- "${BENCH_PROJECT_ROOT:-${ROOT}}" && pwd)"
BACKEND_PORT="${BENCH_BACKEND_PORT:-8502}"
UPSTREAM_PORT="${BENCH_UPSTREAM_PORT:-9100}"
export BENCH_UPSTREAM="http://127.0.0.1:${UPSTREAM_PORT}"

# The backend writes logs and per-thread cache dirs to its working directory.
WORKDIR="$(mktemp -d)"
PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do kill "${pid}" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT

wait_for() {  # url name
  for _ in $(seq 60); do
    curl -s -o /dev/null "$1" && return 0
    sleep 1
  done
  echo "$2 did not start, see logs in ${WORKDIR}" >&2
  exit 1
}

cd "${ROOT}"
uv run python "${HERE}/fake_upstream.py" --port "${UPSTREAM_PORT}" \
  >"${WORKDIR}/upstream.log" 2>&1 &
PIDS+=($!)

(cd "${WORKDIR}" && PYTHONPATH="${PROJECT}/src" exec uv run --project "${PROJECT}" python "${HERE}/run_backend.py" \
  --port "${BACKEND_PORT}" --workers "${BENCH_WORKERS:-1}" >"${WORKDIR}/backend.log" 2>&1) &
PIDS+=($!)

wait_for "${BENCH_UPSTREAM}/v1/chat/completions" "fake upstream"
wait_for "http://127.0.0.1:${BACKEND_PORT}/healthz" "backend"

uv run python "${HERE}/loadgen.py" \
  --backend "http://127.0.0.1:${BACKEND_PORT}" --upstream "${BENCH_UPSTREAM}" "$@"
