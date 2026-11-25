# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:24.22.3-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${NODE_IMAGE} AS javascript

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    HOME=/tmp
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 transcriptor \
    && useradd --uid 10001 --gid 10001 --no-create-home transcriptor \
    && install -d -o 10001 -g 10001 /data /app
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
COPY --from=javascript /usr/local/LICENSE /usr/share/doc/node-LICENSE
WORKDIR /app
COPY deploy/requirements-runtime.txt deploy/ytdlp-version.txt /opt/transcriptor/
RUN python -m pip install --no-cache-dir -r /opt/transcriptor/requirements-runtime.txt \
    && python -m pip install --no-cache-dir "yt-dlp[default]==$(cat /opt/transcriptor/ytdlp-version.txt)" \
    && python -m pip check \
    && python -m pip freeze > /opt/transcriptor/installed-packages.txt
COPY deploy/yt-dlp.conf /etc/yt-dlp.conf
COPY app/ /app/app/
COPY static/ /app/static/
COPY scripts/ /app/scripts/
COPY deploy/ /app/deploy/
RUN test -s static/assets/vendor/transformers.min.js \
    && test -s static/assets/vendor/ort-wasm-simd-threaded.wasm \
    && test -s static/assets/vendor/ort-wasm-simd-threaded.mjs \
    && test -s static/assets/vendor/ort-wasm-simd-threaded.asyncify.wasm \
    && test -s static/assets/vendor/ort-wasm-simd-threaded.asyncify.mjs \
    && node --version && yt-dlp --version && ffmpeg -version >/dev/null
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3); assert r.status == 200"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers", "--timeout-graceful-shutdown", "30"]

FROM runtime AS tests
USER root
COPY deploy/requirements-test.txt /opt/transcriptor/requirements-test.txt
RUN python -m pip install --no-cache-dir -r /opt/transcriptor/requirements-test.txt
COPY tests/ /app/tests/
COPY pyproject.toml /app/pyproject.toml
USER 10001:10001
ENV ENV=development TRUSTED_PROXY=false DATA_DIR=/tmp/transcriptor-tests
CMD ["python", "-m", "pytest", "-q", "-W", "error", "-rs", "--tb=short", "-p", "no:cacheprovider"]

# Keep the default final target free of pytest and lint tools.
FROM runtime AS production
