#!/usr/bin/env bash
set -Eeuo pipefail

common_die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

common_validate_apptainer() {
    local apptainer_bin="$1"

    command -v "$apptainer_bin" >/dev/null 2>&1 \
        || common_die "Apptainer executable not found: $apptainer_bin"
}

common_host_arch() {
    local arch

    arch="$(uname -m)" \
        || common_die "could not determine host architecture"

    [[ "$arch" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
        || common_die "unsupported architecture: $arch"

    printf '%s\n' "$arch"
}

common_make_dirs() {
    local path

    for path in "$@"; do
        mkdir -p "$path"
    done
}

common_pid_alive() {
    local pid="$1"

    [[ "$pid" =~ ^[0-9]+$ ]] \
        && kill -0 "$pid" >/dev/null 2>&1
}

common_read_pidfile() {
    local pidfile="$1"

    [[ -f "$pidfile" ]] || return 1

    local pid
    pid="$(tr -d '[:space:]' < "$pidfile")"

    [[ "$pid" =~ ^[0-9]+$ ]] || return 1

    printf '%s\n' "$pid"
}

common_stop_pidfile() {
    local pidfile="$1"
    local timeout_seconds="${2:-120}"

    local pid

    if ! pid="$(common_read_pidfile "$pidfile")"; then
        rm -f "$pidfile"
        return 0
    fi

    if ! common_pid_alive "$pid"; then
        rm -f "$pidfile"
        return 0
    fi

    kill -TERM "$pid" >/dev/null 2>&1 || true

    local elapsed=0

    while common_pid_alive "$pid" && (( elapsed < timeout_seconds )); do
        sleep 1
        elapsed=$((elapsed + 1))
    done

    if common_pid_alive "$pid"; then
        printf 'warning: process %s did not stop; sending SIGKILL\n' "$pid" >&2
        kill -KILL "$pid" >/dev/null 2>&1 || true
    fi

    rm -f "$pidfile"
}


common_parse_shell_words() {
    # Usage: common_parse_shell_words OUTPUT_ARRAY_NAME STRING
    #
    # Parses a trusted shell-style argument string into a Bash array
    # while preserving quoted arguments.

    local output_name="$1" input="$2"
    local -n output_ref="$output_name"
    output_ref=()
    [[ -n "$input" ]] || return 0

    local serialized
    if ! serialized="$(python3 - "$input" <<'PY'
import shlex
import sys

try:
    words = shlex.split(sys.argv[1], posix=True)
except ValueError as exc:
    print(f"EXTRA_VLLM_ARGS parse error: {exc}", file=sys.stderr)
    raise SystemExit(64)

print(" ".join(shlex.quote(word) for word in words))
PY
    )"; then
        common_die "could not parse EXTRA_VLLM_ARGS"
    fi

    # Safe because serialized was generated only by shlex.quote above.
    local -a parsed=()

    eval "parsed=($serialized)"

    output_ref=("${parsed[@]}")
}

common_reject_options() {
    # Usage: common_reject_options ARRAY_NAME OPTION [OPTION ...]
    #
    # Rejects both --option value and --option=value

    local array_name="$1"
    shift
    local -n args_ref="$array_name"
    local arg option normalized

    for arg in "${args_ref[@]}"; do
        [[ "$arg" == --* ]] || continue
        normalized="${arg%%=*}"

        for option in "$@"; do
            if [[ "$normalized" == "$option" ]]; then
                common_die "EXTRA_VLLM_ARGS may not set launcher-owned option '$option'"
            fi
        done
    done
}

common_validate_cuda_devices() {
    local requested="${1:-}"
    [[ -n "$requested" ]] || return 0

    command -v nvidia-smi >/dev/null 2>&1 \
        || common_die "nvidia-smi not found"

    local gpu_count
    gpu_count="$(nvidia-smi -L | wc -l | tr -d '[:space:]')"

    [[ "$gpu_count" =~ ^[0-9]+$ ]] \
        || common_die "could not determine GPU count"

    local -a ids=()
    IFS=',' read -ra ids <<< "$requested"

    for id in "${ids[@]}"; do
        [[ "$id" =~ ^[0-9]+$ ]] \
            || common_die "invalid CUDA_VISIBLE_DEVICES entry: '$id'"

        if (( id >= gpu_count )); then
            common_die \
                "CUDA_VISIBLE_DEVICES requests GPU $id, but only $gpu_count GPU(s) are available"
        fi
    done
}
