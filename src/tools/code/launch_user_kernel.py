import os
import pwd
import sys
from pathlib import Path

from .kernels import resolve_kernel_user


FREVAGPT_WORKDIR = Path(os.getenv("FREVAGPT_WORKDIR", ""))


def launch_user_kernel(username: str, connection_file: str) -> None:
    pw = resolve_kernel_user(username)

    user_dir = FREVAGPT_WORKDIR / username
    if not user_dir.is_dir():
        raise RuntimeError(f"User kernel directory does not exist: {user_dir}")

    os.chdir(user_dir)

    # Drop group permissions first.
    os.setgid(pw.pw_gid)
    os.initgroups(username, pw.pw_gid)

    # Drop user permissions.
    os.setuid(pw.pw_uid)

    os.execvp(
        sys.executable,
        [
            sys.executable,
            "-m",
            "ipykernel_launcher",
            "-f",
            connection_file,
        ],
    )


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: python -m src.tools.code.launch_user_kernel "
            "<username> <connection_file>"
        )

    username = sys.argv[1]
    connection_file = sys.argv[2]

    launch_user_kernel(username, connection_file)


if __name__ == "__main__":
    main()