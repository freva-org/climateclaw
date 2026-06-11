ARG VERSION=latest
FROM ghcr.io/freva-org/freva-gpt-base:${VERSION}

COPY docker/web-search-server/pyproject.toml /app/pyproject.toml

# Export requirements from pyproject.toml into a file
RUN conda run -n env uv export --format requirements-txt --no-hashes -o /tmp/requirements.txt
# Install exactly those requirements into the env's interpreter
RUN conda run -n env uv pip install --no-cache-dir --system \
    --python "/opt/conda/envs/env/bin/python" \
    -r /tmp/requirements.txt

EXPOSE ${FREVAGPT_MCP_PORT:-8052}

COPY freva_gpt /app/freva_gpt

# Launch server
CMD uvicorn freva_gpt.tools.web_search.server:app \
    --host ${FREVAGPT_MCP_HOST:-0.0.0.0} \
    --port ${FREVAGPT_MCP_PORT:-8052} \
    --proxy-headers
