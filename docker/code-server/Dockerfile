ARG VERSION=latest
FROM ghcr.io/freva-org/freva-gpt-base:${VERSION}

RUN dnf install -y gcc gcc-c++ make python3-devel \
    && dnf clean all

COPY docker/code-server/pyproject.toml /app/pyproject.toml

# Export requirements from pyproject.toml into a file
RUN conda run -n env uv export --format requirements-txt --no-hashes -o /tmp/requirements.txt
# Install exactly those requirements into the env's interpreter
RUN conda run -n env uv pip install --no-cache-dir --system \
    --python "/opt/conda/envs/env/bin/python" \
    -r /tmp/requirements.txt

# Install Python deps via uv (this replaces `pip install ...`)
RUN conda run -n env uv pip install \
    freva-client dill numpy matplotlib pandas xarray xesmf scipy netcdf4 cartopy contourpy \
    geopy aiohttp requests scikit-learn geopandas healpy easygems astropy tzdata pytz \
    imageio

EXPOSE ${FREVAGPT_MCP_PORT:-8051}

COPY freva_gpt /app/freva_gpt

# Launch CI server
CMD uvicorn freva_gpt.tools.code.server:app \
    --host ${FREVAGPT_MCP_HOST:-0.0.0.0} \
    --port ${FREVAGPT_MCP_PORT:-8051} \
    --proxy-headers
