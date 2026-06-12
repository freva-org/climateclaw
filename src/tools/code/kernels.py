import os
import pwd
import sys
import threading
from pathlib import Path
from queue import Empty

from jupyter_client import KernelManager

from src.core.logging_setup import configure_logging

SERVICE_NAME = os.getenv("HOSTNAME") or "code_server"
FREVAGPT_WORKDIR = Path(os.getenv("FREVAGPT_WORKDIR", ""))

logger = configure_logging(__name__, named_log=SERVICE_NAME)


# ── Kernel persistence ───────────────────────────────────────────────────────

KERNEL_REGISTRY: dict[str, KernelManager] = {} 
KERNEL_LOCKS: dict[str, threading.Lock] = {}
KERNEL_LOCKS_GUARD = threading.Lock()
# TODO use sid locks not single guard


def resolve_kernel_user(username: str) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(username)
    except KeyError:
        if os.getenv("FREVAGPT_DEV") == "1":
            return pwd.getpwuid(os.getuid())
        raise


def prepare_user_kernel_dir(username: str) -> Path:
    pw = resolve_kernel_user(username)

    user_dir = FREVAGPT_WORKDIR / username
    user_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    os.chown(user_dir, pw.pw_uid, pw.pw_gid)
    os.chmod(user_dir, 0o700)

    return user_dir

# ── Kernel lifecycle ─────────────────────────────────────────────────────────

def _kernel_ready_handshake(km: KernelManager, timeout: int = 10) -> None:
    kc = km.client()
    kc.start_channels()
    try:
        kc.wait_for_ready(timeout=timeout)
    finally:
        kc.stop_channels()


def start_kernel_for_user(username: str, session_folder: Path) -> KernelManager:
    user_dir = prepare_user_kernel_dir(username, )
    session_dir = user_dir / session_folder
    session_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()

    km = KernelManager()
    km.kernel_cmd = [
        sys.executable,
        "-m",
        "src.tools.code.launch_user_kernel",
        username,
        "{connection_file}",
    ]
    km.start_kernel(env=env, cwd=str(session_dir))
    return km


def restart_kernel(km: KernelManager) -> None:
    km.restart_kernel(now=True)
    _kernel_ready_handshake(km, timeout=10)


def shutdown_kernel(km: KernelManager) -> None:
    try:
        km.shutdown_kernel(now=True)
    except Exception:
        logger.exception("Failed to shutdown dead kernel cleanly")


def get_or_start_kernel(
    username: str,
    sid: str,
    cwd_str: str,
) -> KernelManager:
    km = KERNEL_REGISTRY.get(sid)

    # Check existing kernel state
    if km is not None and not km.is_alive():
        # Dead kernel, discard it
        # NOTE: This restart may break persistance, it is not handled
        # TODO: maybe we should have a code history registry?
        logger.warning("Kernel for sid=%s is dead; restarting", sid)
        shutdown_kernel(km)
        KERNEL_REGISTRY.pop(sid, None) # discard
        km = None
    elif km and km.is_alive():
        # Report alive kernel
        logger.warning("Kernel for sid=%s is alive", sid)

    if km is None:
        logger.info("Starting new kernel for sid=%s", sid)

        km = start_kernel_for_user(username, cwd_str)

        KERNEL_REGISTRY[sid] = km # register
        try:
            _kernel_ready_handshake(km, timeout=10)
        except Exception:
            KERNEL_REGISTRY.pop(sid, None)
            shutdown_kernel(km)
            raise
    return km

# ── Drain stale messages ─────────────────────────────────────────────────────

def drain_stale_messages(kc, max_msgs: int = 100):
    # best effort to avoid stale messages from earlier runs
    _drain_iopub(kc, max_msgs)
    _drain_shell(kc, max_msgs) 
    _drain_control(kc, max_msgs)


def _drain_iopub(kc, max_msgs: int = 100):
    for _ in range(max_msgs):
        try:
            kc.get_iopub_msg(timeout=0.01)
        except Empty:
            break


def _drain_shell(kc, max_msgs: int = 100) -> None:
    """Drain any pending shell-channel messages."""
    for _ in range(max_msgs):
        try:
            kc.get_shell_msg(timeout=0.01)
        except Empty:
            break
        except Exception:
            break


def _drain_control(kc, max_msgs: int = 100) -> None:
    """Drain any pending control-channel messages."""
    for _ in range(max_msgs):
        try:
            kc.get_control_msg(timeout=0.01)
        except Empty:
            break
        except Exception:
            break
