import os
import shutil
import subprocess


def _write_executable(path, content):
    path.write_text(content)
    path.chmod(0o755)


def _prod_fixture(tmp_path, monkeypatch):
    repo_root = os.getcwd()
    shutil.copy(repo_root + "/prod.sh", tmp_path / "prod.sh")
    (tmp_path / "prod.sh").chmod(0o755)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "docker-compose.scaled.yml").write_text("services: {}\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "calls.log"
    _write_executable(
        bin_dir / "podman",
        '#!/usr/bin/env bash\necho "podman $*" >> "$PROD_SH_TEST_LOG"\nexit 0\n',
    )
    _write_executable(
        bin_dir / "podman-compose",
        "#!/usr/bin/env bash\n"
        'echo "podman-compose $*" >> "$PROD_SH_TEST_LOG"\n'
        "exit 0\n",
    )
    _write_executable(
        tmp_path / "gen_compose.py",
        "#!/usr/bin/env bash\n"
        'echo "gen_compose $* PROJECT=$CLIMATECLAW_PROJECT_NAME"'
        ' >> "$PROD_SH_TEST_LOG"\n'
        "exit 0\n",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PROD_SH_TEST_LOG", str(log_path))
    monkeypatch.delenv("CLIMATECLAW_PROJECT_NAME", raising=False)
    return log_path


def _run_prod(tmp_path, *args):
    return subprocess.run(
        [str(tmp_path / "prod.sh"), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_prod_sh_missing_project_fails(tmp_path, monkeypatch):
    _prod_fixture(tmp_path, monkeypatch)

    result = _run_prod(tmp_path)

    assert result.returncode == 1
    assert "project is required" in result.stderr


def test_prod_sh_project_flag_sets_project_and_uses_default_up_args(
    tmp_path,
    monkeypatch,
):
    log_path = _prod_fixture(tmp_path, monkeypatch)

    result = _run_prod(tmp_path, "--project", "codes")

    assert result.returncode == 0
    assert "gen_compose docker-compose.yml codes PROJECT=codes" in log_path.read_text()
    assert "podman-compose -f docker-compose.scaled.yml up -d" in log_path.read_text()


def test_prod_sh_project_equals_flag_sets_project(tmp_path, monkeypatch):
    log_path = _prod_fixture(tmp_path, monkeypatch)

    result = _run_prod(tmp_path, "--project=codes")

    assert result.returncode == 0
    assert "gen_compose docker-compose.yml codes PROJECT=codes" in log_path.read_text()


def test_prod_sh_reads_project_from_env_file(tmp_path, monkeypatch):
    log_path = _prod_fixture(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text('CLIMATECLAW_PROJECT_NAME="codes"\n')

    result = _run_prod(tmp_path)

    assert result.returncode == 0
    assert "gen_compose docker-compose.yml codes PROJECT=codes" in log_path.read_text()


def test_prod_sh_project_flag_overrides_env_file(tmp_path, monkeypatch):
    log_path = _prod_fixture(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text("CLIMATECLAW_PROJECT_NAME=eve\n")

    result = _run_prod(tmp_path, "--project", "codes")

    assert result.returncode == 0
    assert "gen_compose docker-compose.yml codes PROJECT=codes" in log_path.read_text()


def test_prod_sh_preserves_compose_args_after_flags(tmp_path, monkeypatch):
    log_path = _prod_fixture(tmp_path, monkeypatch)

    result = _run_prod(tmp_path, "--project", "codes", "up", "--force-recreate")

    assert result.returncode == 0
    log = log_path.read_text()
    assert "podman-compose -f docker-compose.scaled.yml up --force-recreate" in log
    assert "--project codes" not in log
