# Runtime image for the AVD Fabric composite function.
# Crossplane runs this image as the function's gRPC server (port 9443), passing
# TLS_SERVER_CERTS_DIR so it serves securely in-cluster.
FROM python:3.12-slim

# uv for reproducible, fast dependency install from the committed lockfile.
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev
COPY function ./function
RUN uv sync --frozen --no-dev

EXPOSE 9443
# Call the installed console script directly. Crossplane runs the function as a
# non-root user, so avoid `uv run` (it would try to write a cache under $HOME).
ENTRYPOINT ["/app/.venv/bin/avd-function", "--address", "0.0.0.0:9443"]
