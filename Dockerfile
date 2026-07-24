FROM python:3.14-bookworm AS base

# Install system deps + Node.js 24 via NodeSource
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg ffmpeg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    HF_HOME=/opt/huggingface

# Install dependencies only (cached unless pyproject.toml or uv.lock changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Install Playwright Chromium + system dependencies (cached, runs before source copy).
# Invoke the installed CLI directly so uv does not try to package source that has
# not been copied into this layer yet.
RUN /opt/venv/bin/playwright install --with-deps chromium

# Copy source and install local package (fast, deps already cached)
COPY . .
RUN uv sync --frozen --dev

# Create runtime data directories (override by volume mount in production)
RUN mkdir -p data/inbox data/outbox data/identity data/logs data/memory data/workspace .coworker/skills "$HF_HOME"

EXPOSE 8000

CMD ["uv", "run", "coworker"]

# Optional release target. Build it with:
#   docker build --target with-embedder -t coworker:with-embedder .
# The default final target below deliberately stays lightweight and downloads this
# model lazily into the persistent Hugging Face cache on first use.
FROM base AS with-embedder

ARG EMBEDDER_MODEL=sentence-transformers/paraphrase-multilingual-mpnet-base-v2
ENV COWORKER_PRELOADED_EMBEDDER_MODEL=${EMBEDDER_MODEL}
RUN uv run python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['COWORKER_PRELOADED_EMBEDDER_MODEL'])"

# Strict Hugging Face offline variant. Set this only after the model download above:
# a cache miss must fail instead of attempting a runtime network request.
FROM with-embedder AS offline
ENV HF_HUB_OFFLINE=1

# Keep the standard image as Docker's default build target.
FROM base AS runtime
