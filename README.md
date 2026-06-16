# ClimateClaw
ClimateClaw is a Python service for building AI-assisted climate-data workflows. It provides the API, conversation handling, model prompting, persistent thread storage, and tool orchestration needed to support interactive work with climate data.

The project integrates LiteLLM-native prompting, MongoDB-backed conversation state, and MCP-based tool execution for retrieval, code execution, and domain-specific automation.

## Highlights
- FastAPI app with strict auth parity to the production Rust service (`/api/chatbot/*`)
- Streaming responses via LiteLLM/OpenAI-compatible SSE (`application/x-ndjson`) with code + image variants
- Persistent conversation threads in MongoDB and JSONL files (`threads/`), plus per-user scratch space (`cache/`)
- MCP manager that wires the backend to dedicated tool servers (`code-server`, `web-search-server`)
- Docker compose stack that includes LiteLLM, Ollama, the backend, and both MCP servers
- Comprehensive pytest suite covering auth, prompting, storage, litellm client helpers, and route matrices

## Quick Start (deployment)

### Requirements
- `podman` or `docker`
- Credentials & headers for the Freva auth services

### Configure environment
Create `.env` (used by FastAPI, Docker, and MCP servers). See `.env.example` for guidance.

### Full stack via Docker Compose
```bash
podman compose up --build
```
Services that start:
- `climateclaw`: FastAPI app (debugpy toggle via `DEBUG=true` for remote debugging session)
- `code-server`: MCP server running the sandboxed Jupyter kernel and exposing `code_interpreter`
- `web-search-server`: MCP server doing web search via OpenAI API and exposing `web_search`
- `litellm`: LiteLLM proxy that reads `litellm_config.yaml`
- `ollama`: Optional local model runner for LiteLLM backends

Bind mounts expose `/work`, logs, threads, and shared `cache` to other Freva services. Provide GPU access to Ollama via Docker device reservations when needed.

## Quick Start (local dev)

### Requirements
- `podman` or `docker`

<!-- ### Install dependencies (uv)
```bash
pip install uv           # one-time
uv venv                  # create .venv
source .venv/bin/activate
uv sync                  # install lockfile deps
``` -->

### Configure environment
Create `.env` (used by FastAPI, Docker, and MCP servers). See `.env.example` for guidance.

### Start docker containers in DEV mode
```bash
./dev.sh up -d --build
```

## Repository Layout
| Path | Purpose |
| --- | --- |
| `climateclaw/app.py` | FastAPI entrypoint, CORS policy, router registration, app lifespan hooks |
| `climateclaw/api/chatbot/*` | HTTP handlers for chat operations (`availablechatbots`, `streamresponse`, `getthread`, etc.) |
| `climateclaw/services/streaming/` | LiteLLM client, orchestrator, stream variant definitions, heartbeat helpers |
| `climateclaw/services/storage/` | MongoDB + disk-backed persistence (`threads/` JSONL, `cache/` scratch space) |
| `climateclaw/services/mcp/` | MCP manager and MCP client |
| `climateclaw/services/authentication/` | Authentication: DEV mode auth surpassing OIDC requirements |
| `climateclaw/core/` | Settings, prompt assembly, logging, startup checks, available-model parsing |
| `climateclaw/tools/` | MCP servers (code interpreter + RAG), auth helpers, header gate middleware |
| `prompt_library/` | Baseline system prompts, summary prompts, and few-shot examples (JSONL) |
| `resources/` | Documentation corpora used by the RAG tool (`stableclimgen` seed content) |
| `docker/` | Dockerfiles for backend, LiteLLM/Ollama helpers, rag/code/web-search MCP servers |
| `scripts/` | Dev utilities (`dev_chat.py`, `dev_script.py`, `check_kernel_env.py`) |
| `tests/` | Pytest suite covering auth, prompting, streaming, storage, and endpoints |
| `litellm_config.yaml` | Source of truth for model catalog (consumed by `available_chatbots()`) |

Generated artifacts that persist across runs:
- `threads/` (JSONL transcript per thread id)
- `cache/{user_id}/{thread_id}` (LLM-created files, plots, etc.)
- `logs/` (when mounted in Docker)

## Architecture at a Glance
1. **FastAPI layer** enforces auth via `AuthRequired` (Bearer tokens validated against `x-freva-rest-url`), injects usernames, and validates per-request headers.
2. **LiteLLM proxy** (`CLIMATECLAW_LITE_LLM_ADDRESS`) provides OpenAI-compatible chat + embeddings endpoints; completions stream into `StreamVariant` classes that normalize assistant text, code blocks, tool hints, images, and server hints.
3. **Persistence** uses MongoDB for storing threads and user feedback.
4. **MCP Manager** (`climateclaw/services/mcp/mcp_manager.py`) connects to tool servers listed in `CLIMATECLAW_AVAILABLE_MCP_SERVERS` (e.g., `["rag", "code"]`), discovers tools, exposes OpenAI function schemas to LiteLLM, and routes tool invocations with per-thread session ids.
5. **Code + Web-search MCP servers** run as separate ASGI apps (dockerized). Requests flow through `header_gate` so required headers (`mongodb-uri`, `working-dir`) become ContextVars before code executes.
6. **Prompting** loads baseline templates + few-shot examples per model and replays thread history (minus prompts, meta) to LiteLLM, matching the Rust semantics.

## API Surface

| Method | Path | Description | Notes |
| --- | --- | --- | --- |
| `GET` | `/api/chatbot/ping` | Static ping stub | Placeholder |
| `GET` | `/api/chatbot/docs` | Docs payload stub | Placeholder |
| `GET` | `/api/chatbot/help` | Help payload stub | Placeholder |
| `GET` | `/api/chatbot/availablechatbots` | Returns model names from `litellm_config.yaml` | Requires auth |
| `GET` | `/api/chatbot/getthread?thread_id=...` | Fetches thread contents omitting prompts + redundant StreamEnd variants | Requires auth |
| `GET` | `/api/chatbot/getuserthreads` | Returns latest 10 threads for authenticated user | Falls back to query `user_id` only if `ALLOW_FALLBACK_OLD_AUTH` |
| `GET` | `/api/chatbot/streamresponse` | Starts an SSE stream of `StreamVariant` JSON payloads | Query params: `thread_id`, `input` (required), `chatbot` |
| `GET/POST` | `/api/chatbot/stop` | Initiates stopping of an active conversation | Requires auth |

### Streaming contract
- Response type: `application/x-ndjson`
- Each `data:` line is a JSON object with `variant` discriminators (`Assistant`, `Code`, `CodeOutput`, `CodeError`, `Image`, `ServerHint`, `StreamEnd`, etc.).
- Code tool calls stream incremental chunks while LiteLLM emits `tool_calls`. When the MCP tool resolves, results are converted back into JSON events and appended to Mongo/disk storage.
- Server automatically injects `thread_id` hints and records the conversation before returning the SSE chunk, ensuring replay safety.

## Persistence, Prompts, and Assets
- **MongoDB (`mongodb_storage.py`)**: canonical record for threads. Each document stores `user_id`, `thread_id`, ISO timestamp, topic (summarized via LiteLLM), and serialized `StreamVariant` list.
- **Disk mirrors (`thread_storage.py`)**: keep JSONL copies under `threads/{thread_id}.txt`, enabling offline replay and dev tooling. Topic of a thread is saved in `threads/{thread_id}.meta.json`.
- **`cache/` scratch**: `create_dir_at_cache()` ensures each user/thread has a writable directory for generated files (plots, CSVs). Entries are sanitized if user IDs contain unsupported characters.
- **Prompt library**: `prompt_library/baseline` contains `starting_prompt.txt`, `summary_prompt.txt`, and `examples.jsonl`. GPT-5 models currently fall back to baseline prompts (warning logged). Customize by adding new prompt sets and updating `_resolve_baseline_dir()` / `_resolve_gpt5_dir_or_placeholder()`.
- **Resources**: `resources/stableclimgen` seeds the RAG MCP server. Drop additional corpora per library folder and list them in `CLIMATECLAW_AVAILABLE_LIBRARIES` inside `climateclaw/tools/rag/server.py`.

## MCP Tooling
- **RAG server** (`climateclaw/tools/rag/server.py`): indexes documentation with custom loaders + splitters, stores embeddings in MongoDB (`embeddings`), and surfaces a single tool `get_context_from_resources`. LiteLLM requests embed queries through the same proxy (`CLIMATECLAW_LITE_LLM_ADDRESS`).
- **Code interpreter** (`climateclaw/tools/code_interpreter/server.py`): spins up per-session Jupyter kernels, sanitizes input, enforces configurable timeouts, and injects Freva config via environment variables. Outputs include stdout/stderr, display data, and structured errors.
- **Web search server** (`climateclaw/tools/web_search/server.py`): performs OpenAI tool-based web search (Responses API `web_search`) against a small allowlist of documentation domains (DKRZ/HPC + ICON), returning answer text with inline URL citations.
- **Header gate** (`climateclaw/tools/header_gate.py`): wraps each MCP ASGI app so critical headers become ContextVars and requests fail fast when missing/invalid (e.g., missing Mongo URI yields SSE-friendly JSON-RPC errors).
- **Manager** (`climateclaw/services/mcp/mcp_manager.py`): caches clients, discovers tool schemas, exports OpenAI function definitions, and pins MCP session ids to thread ids for deterministic tool contexts.

## Development Workflow
- **Run tests**: `uv run pytest` (or `uv run pytest tests/test_auth.py -k bearer` for focused cases). Tests cover auth flows, prompt assembly, storage, stream variant conversions, and route parameter validation.
- **Interactive chat**: `uv run python scripts/dev_chat.py` starts a REPL that exercises the same orchestrator logic, persisting outputs to disk and optionally pointing at local MCP servers.

## Troubleshooting
- **Auth failures**: verify headers include both `Authorization` and `x-freva-rest-url`. Inspect FastAPI logs for the exact HTTP status.
- **Missing models**: ensure `litellm_config.yaml` is readable and contains `model_name` keys. `available_chatbots()` aborts the process if it cannot find any entries.
- **MCP issues**: backend logs warn but continue when tool discovery fails; LiteLLM will simply not emit tool calls. Use `settings.AVAILABLE_MCP_SERVERS` to enable/disable targets explicitly.
- **File access**: Make sure `/work` is mounted read-only where expected.
