#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'USAGE'
Usage: ENV_FILE=.env ./apptainer/vllm/run.sh ACTION [arguments]

Actions:
  pull, build       Pull the pinned OCI image and convert it to SIF.
  foreground        Run vLLM in the foreground .
  up                Start vLLM in the background.
  down              Stop vLLM.
  status            Show process and image state.
  logs              Follow logs.
  shell             Open a GPU-enabled shell in the SIF.
  exec COMMAND...   Run a command in the configured SIF environment.
  inspect           Show Apptainer image metadata.
  digest            Print the SIF SHA-256 digest.
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APPTAINER_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"

# shellcheck source=../lib/common.sh
source "$APPTAINER_DIR/lib/common.sh"

ACTION="${1:-}"
[[ -n "$ACTION" ]] || { usage; exit 64; }
shift || true

ENV_FILE="${ENV_FILE:-$APPTAINER_DIR/.env}"

[[ -f "$ENV_FILE" ]] \
    || common_die "environment file not found: $ENV_FILE"

# shellcheck disable=SC1090
source "$ENV_FILE"

APPTAINER_BIN="${APPTAINER:-apptainer}"
HOST_ARCH="$(common_host_arch)"

image_ref="${VLLM_OCI_IMAGE#*://}"
image_basename="${image_ref##*/}"
image_basename="${image_basename//:/-}"

VLLM_IMAGE_DIR="$APPTAINER_IMAGES_DIR/vllm"
VLLM_SIF="$VLLM_IMAGE_DIR/${image_basename}-${HOST_ARCH}.sif"

HF_CACHE_DIR="$APPTAINER_BASE_DIR/cache/huggingface"
VLLM_CACHE_DIR="$APPTAINER_BASE_DIR/cache/vllm"

VLLM_LOG_DIR="$APPTAINER_LOG_DIR/vllm"
VLLM_RUN_DIR="$APPTAINER_RUN_DIR/vllm"
LOG_FILE="$VLLM_LOG_DIR/vllm.log"
PID_FILE="$VLLM_RUN_DIR/vllm.pid"

prepare_dirs() {
    common_make_dirs \
        "$VLLM_IMAGE_DIR" \
        "$APPTAINER_CACHEDIR" \
        "$APPTAINER_TMPDIR" \
        "$HF_CACHE_DIR" \
        "$VLLM_CACHE_DIR" \
        "$VLLM_LOG_DIR" \
        "$VLLM_RUN_DIR"

    export APPTAINER_CACHEDIR
    export APPTAINER_TMPDIR
}

build_runtime_args() {
    common_validate_cuda_devices "$CUDA_VISIBLE_DEVICES"

    RUNTIME_ARGS=(
        --cleanenv  # prevents Spack/host Python/CUDA environment from leaking in
        --no-eval
        --nv  # exposes NVIDIA libraries/devices

        --bind "$HF_CACHE_DIR:/cache/huggingface"
        --bind "$VLLM_CACHE_DIR:/root/.cache/vllm"

        --env "HF_HOME=/cache/huggingface"
        --env "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
        --env "VLLM_LOGGING_LEVEL=${VLLM_LOGGING_LEVEL:-INFO}"
    )

    if [[ "${APPTAINER_FORCE_USERNS:-1}" == "1" ]]; then
        RUNTIME_ARGS+=(--userns)  # rootless mode
    fi
}

run_vllm() {
    build_runtime_args

    local -a extra_args=()
    common_parse_shell_words extra_args "${EXTRA_VLLM_ARGS:-}"

    common_reject_options extra_args \
        --host \
        --port \
        --api-key

    exec "$APPTAINER_BIN" exec \
        "${RUNTIME_ARGS[@]}" \
        "$VLLM_SIF" \
        vllm serve "$VLLM_MODEL" \
            --host "$VLLM_HOST" \
            --port "$VLLM_PORT" \
            --api-key "$VLLM_API_KEY" \
            "${extra_args[@]}"
}


case "$ACTION" in
    pull|build)
        common_validate_apptainer "$APPTAINER_BIN"
        prepare_dirs

        exec "$APPTAINER_BIN" pull \
            --force \
            "$VLLM_SIF" \
            "$VLLM_OCI_IMAGE"
        ;;
    foreground)
        common_validate_apptainer "$APPTAINER_BIN"
        prepare_dirs

        [[ -f "$VLLM_SIF" ]] \
            || common_die "SIF image not found: $VLLM_SIF"

        run_vllm
        ;;
    up)
        common_validate_apptainer "$APPTAINER_BIN"
        prepare_dirs

        [[ -f "$VLLM_SIF" ]] \
            || common_die "SIF image not found: $VLLM_SIF"

        if pid="$(common_read_pidfile "$PID_FILE" 2>/dev/null)" \
            && common_pid_alive "$pid"; then
            common_die "vLLM is already running with PID $pid"
        fi

        rm -f "$PID_FILE"

        nohup "$0" foreground \
            >>"$LOG_FILE" 2>&1 < /dev/null &

        pid=$!
        printf '%s\n' "$pid" > "$PID_FILE"

        sleep 2

        if ! common_pid_alive "$pid"; then
            printf 'vLLM failed during startup\n' >&2
            tail -n 100 "$LOG_FILE" >&2 || true
            rm -f "$PID_FILE"
            exit 1
        fi

        printf 'vLLM started with PID %s\n' "$pid"
        printf 'log: %s\n' "$LOG_FILE"
        ;;
    down)
        common_stop_pidfile "$PID_FILE" 120
        printf 'vLLM stopped\n'
        ;;
    logs)
        [[ -f "$LOG_FILE" ]] \
            || common_die "log file not found: $LOG_FILE"

        exec tail -n 100 -F "$LOG_FILE"
        ;;
    status)
        printf 'architecture: %s\n' "$HOST_ARCH"
        printf 'OCI image:    %s\n' "$VLLM_OCI_IMAGE"
        printf 'SIF:          %s\n' "$VLLM_SIF"

        if pid="$(common_read_pidfile "$PID_FILE" 2>/dev/null)" \
            && common_pid_alive "$pid"; then

            printf 'status:       running\n'
            printf 'PID:          %s\n' "$pid"

            ps -o pid,ppid,stat,etime,cmd -p "$pid"
        else
            printf 'status:       stopped\n'
        fi
        ;;
    shell)
        common_validate_apptainer "$APPTAINER_BIN"
        prepare_dirs
        build_runtime_args

        [[ -f "$VLLM_SIF" ]] \
            || common_die "SIF image not found: $VLLM_SIF"

        exec "$APPTAINER_BIN" shell \
            "${RUNTIME_ARGS[@]}" \
            "$VLLM_SIF"
        ;;
    exec)
        common_validate_apptainer "$APPTAINER_BIN"
        prepare_dirs

        [[ -f "$VLLM_SIF" ]] \
            || common_die "SIF image not found: $VLLM_SIF"

        (($# > 0)) \
            || common_die "exec requires a command"

        build_runtime_args

        exec "$APPTAINER_BIN" exec \
            "${RUNTIME_ARGS[@]}" \
            "$VLLM_SIF" \
            "$@"
        ;;
    inspect)
        common_validate_apptainer "$APPTAINER_BIN"

        [[ -f "$VLLM_SIF" ]] \
            || common_die "SIF image not found: $VLLM_SIF"

        exec "$APPTAINER_BIN" inspect "$VLLM_SIF"
        ;;

    digest)
        [[ -f "$VLLM_SIF" ]] \
            || common_die "SIF image not found: $VLLM_SIF"

        exec sha256sum "$VLLM_SIF"
        ;;
    *)
    usage
    exit 64
    ;;
esac
