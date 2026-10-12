# Protocol-X Tool MCP — hardened profile (F-021)
# Non-root, read-only root FS, tmpfs /tmp. Compute sizing (F-038) is computed
# at container creation by deploy/docker/compute-env.sh -> .env values below.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

# uv for fast, locked installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY server ./server
COPY core ./core
COPY agents ./agents
COPY adapters ./adapters
COPY templates ./templates
COPY migrations ./migrations
COPY policies ./policies
COPY console/dist ./console/dist

RUN uv sync --no-dev --no-editable

# Non-root runtime user; writable data dir only
RUN useradd --create-home --uid 10001 protox \
    && mkdir -p /data/workspace \
    && chown -R protox:protox /data
USER 10001

ENV PROTOX_DATA_DIR=/data \
    PROTOX_WORKSPACE=/data/workspace \
    PROTOX_HOST=0.0.0.0 \
    PROTOX_PORT=7894

EXPOSE 7894
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7894/health', timeout=3).status==200 else 1)"]

CMD ["uv", "run", "--no-sync", "protocol-x-tool-mcp", "serve", "--transport", "http"]
