# Docker-first build. The app is only ever run in a container.
#
# Targets:
#   frontend     - builds the SPA into the Python package's static dir
#   python-base  - runtime deps + app installed (shared base)
#   dev          - adds dev deps + tests; used for lint/type-check/pytest and hot-reload dev
#   runtime      - final production image (non-root, healthcheck, built SPA)
#
# Build production:   docker build --target runtime -t energy-optimizer .
# Build dev/test:     docker build --target dev     -t energy-optimizer:dev .

# --- Stage 1: frontend build ---
FROM node:20-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
# Outputs to /app/src/energy_optimizer/web/static (see vite.config.ts outDir).
RUN npm run build

# --- Stage 2: python base (runtime deps + app) ---
FROM python:3.12-slim AS python-base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Warsaw \
    EO_DB=/data/energy_optimizer.sqlite \
    EO_HTTP_HOST=0.0.0.0 \
    EO_HTTP_PORT=8320
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# --- Stage 3: dev / test (dev deps + tests, editable install for hot reload) ---
FROM python-base AS dev
RUN pip install --no-cache-dir -e ".[dev]"
COPY tests ./tests
EXPOSE 8320
# Default dev command: hot-reloading single worker (scheduler runs in the worker only).
CMD ["python", "-m", "uvicorn", "energy_optimizer.web:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8320", "--reload", "--reload-dir", "/app/src"]

# --- Stage 4: production runtime ---
FROM python-base AS runtime
# Place the built SPA in the source tree and reinstall the package (deps already present)
# so the static assets are bundled into the installed package and served by FastAPI.
COPY --from=frontend /app/src/energy_optimizer/web/static ./src/energy_optimizer/web/static
RUN pip install --no-cache-dir --no-deps --force-reinstall .

RUN useradd --system --uid 10001 --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

VOLUME ["/data"]
EXPOSE 8320

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8320/healthz').status==200 else 1)"

CMD ["python", "-m", "energy_optimizer"]
