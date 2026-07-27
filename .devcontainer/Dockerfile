ARG PYTHON_VERSION=3.13
FROM mcr.microsoft.com/devcontainers/python:1-${PYTHON_VERSION}-bookworm

# Keep the development image aligned with the runtime image for media tooling.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    HF_HOME=/home/vscode/.cache/huggingface

# Cache the large Python and browser dependencies independently from source edits.
WORKDIR /tmp/coworker-dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project \
    && /opt/venv/bin/playwright install --with-deps chromium \
    && chown -R vscode:vscode /opt/venv /opt/ms-playwright
