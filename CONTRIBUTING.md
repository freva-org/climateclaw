# Contributing to ClimateClaw

Thanks for helping out! This guide covers the local setup, the day-to-day
commands, and what we expect from a pull request.

## Prerequisites

- **Python 3.13** (`uv python install 3.13` if you don't have it)
- **[uv](https://docs.astral.sh/uv/) ≥ 0.4.27** (needed for dependency-group includes)
- **Docker** with Compose v2, for the dev stack and integration tests
- **make**
- Optional: **Ollama** on the host (`ollama serve`, port `11434`) if you use local models

## Setup

```bash
git clone https://github.com/freva-org/climateclaw.git
cd climateclaw
cp .env.example .env   # then fill in the values you need
make install
```

`make install` syncs the `dev` dependency group and installs the git hooks for
**both** stages:

| Stage        | Hooks                                                        |
|--------------|--------------------------------------------------------------|
| `pre-commit` | ruff check (`--fix`), ruff format, whitespace/YAML/TOML checks |
| `pre-push`   | mypy, unit tests (`pytest -m "not integration"`)             |

If you ran a plain `pre-commit install` earlier, run `make install` again.
Otherwise the pre-push hooks are missing.

## Everyday commands

Run `make` (or `make help`) to see all targets:

| Target             | What it does                                         |
|--------------------|------------------------------------------------------|
| `make lint`        | Run all pre-commit hooks on every file               |
| `make type`        | Run mypy                                             |
| `make test`        | Unit tests, no Docker needed                         |
| `make check`       | `lint` + `type` + `test`; run this before opening a PR |
| `make up`          | Start the dev stack (`./dev.sh up -d --build`)       |
| `make down`        | Stop the dev stack                                   |
| `make integration` | Integration tests against the running dev stack      |

To run a subset of tests, call pytest directly, for example
`uv run pytest tests/test_auth.py -k bearer`.

Ruff is pinned in `.pre-commit-config.yaml` and only runs through pre-commit,
so local runs and CI always use the same version.

## Dev stack and integration tests

```bash
make up            # FastAPI, rag, code, web-search, LiteLLM
make integration   # expects code server on :8051 and web-search on :8052
make down
```

For more control, use `./dev.sh` directly (`./dev.sh --help`). For example,
`./dev.sh up --build` keeps the logs in the foreground, `--debug` enables debug
mode and `--scale` runs the HAProxy setup.

For manual testing, `uv run python scripts/dev_chat.py` starts a REPL that
uses the same orchestrator logic as the backend.

## Dependency groups

| Group  | Contents                                  | Used by            |
|--------|-------------------------------------------|--------------------|
| `test` | pytest + plugins, jupyter-client          | `make test`, CI    |
| `type` | mypy + type stubs                         | `make type`, CI    |
| `dev`  | pre-commit + everything in `test` and `type` | local development |

Add new tooling to the narrowest group that fits.

## Pull requests

1. Branch off `main`.
2. Run `make check` and, if you touched tools or the orchestrator, `make integration`.
3. Open a PR against `main` describing what changed and why.
4. CI must pass (lint, type, full test suite on the dev stack, prod smoke test).
