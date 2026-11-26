ARG VERSION=latest
FROM ghcr.io/freva-org/climateclaw-base:${VERSION}

COPY docker/plugin-code-search-server/pyproject.toml /app/pyproject.toml

# Export requirements from pyproject.toml into a file
RUN conda run -n env uv export --format requirements-txt --no-hashes -o /tmp/requirements.txt
# Install exactly those requirements into the env's interpreter
RUN conda run -n env uv pip install --no-cache-dir --system \
    --python "/opt/conda/envs/env/bin/python" \
    -r /tmp/requirements.txt

EXPOSE ${CLIMATECLAW_MCP_PORT:-8053}

COPY src /app/src

# Launch server
CMD uvicorn climateclaw.tools.plugin_code_search.server:app \
    --host ${CLIMATECLAW_MCP_HOST:-0.0.0.0} \
    --port ${CLIMATECLAW_MCP_PORT:-8053} \
    --proxy-headers
