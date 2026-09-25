FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The collections netadopt pins for the AVD it is tested on, with the Ansible the venv holds.
RUN .venv/bin/python -c "import sys; from pathlib import Path; \
from netadopt.api import ensure_collections, resolve_ansible; \
sys.exit(ensure_collections(resolve_ansible(), root=Path('/opt/avd-collections')).problem)"

COPY function ./function
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim

# The base image ships the standard library without bytecode, and uid 2000 cannot write it:
# every Python that Ansible starts would compile it again.
RUN python -m compileall -q /usr/local/lib/python3.12

# Changed only when AVD moves, so before the venv.
COPY --from=build /opt/avd-collections /opt/avd-collections
COPY --from=build /app/.venv /app/.venv

# A Fabric's request carries every Device's structured config, up to 68 KiB each in AVD's
# examples: the SDK's 4 MB holds about 60 of them.
ENV AVD_COLLECTIONS=/opt/avd-collections \
    MAX_RECV_MESSAGE_SIZE=64

# Crossplane runs the function as uid 2000, whose home is /: Ansible makes ~/.ansible even for
# --version.
ENV HOME=/tmp

EXPOSE 9443
ENTRYPOINT ["/app/.venv/bin/avd-function"]
