ARG VERSION=latest
FROM ghcr.io/freva-org/freva-gpt-base:${VERSION}

COPY pyproject.toml /app/pyproject.toml

# Export requirements from pyproject.toml into a file
RUN conda run -n env uv export --format requirements-txt --no-hashes -o /tmp/requirements.txt
# Install exactly those requirements into the env's interpreter
RUN conda run -n env uv pip install --no-cache-dir --system \
    --python "/opt/conda/envs/env/bin/python" \
    -r /tmp/requirements.txt

# -------------------------
# Config + source
# -------------------------

# The litellm also needs to be copied over to the backend so that the list of chatbots can be extracted
COPY litellm_config.yaml /app/litellm_config.yaml

# Copy source code
COPY freva_gpt /app/freva_gpt
COPY tests /app/tests

# -------------------------
# Launch
# -------------------------

ENTRYPOINT ["/bin/sh", "-c", "\
  if [ \"${FREVAGPT_DEBUG,,}\" = \"true\" ] || [ \"${FREVAGPT_DEBUG}\" = \"1\" ]; then \
    echo \"[backend] DEBUG on → waiting for debugger on ${FREVAGPT_DEBUG_PORT:-5678}\"; \
    exec python -m debugpy --listen 0.0.0.0:${FREVAGPT_DEBUG_PORT:-5678} --wait-for-client \
         -m uvicorn freva_gpt.app:app --host 0.0.0.0 --port ${FREVAGPT_BACKEND_PORT:-8502} --proxy-headers; \
  else \
    echo \"[backend] DEBUG off → starting plain uvicorn\"; \
    exec uvicorn freva_gpt.app:app --host 0.0.0.0 --port ${FREVAGPT_BACKEND_PORT:-8502} --proxy-headers; \
  fi"]
