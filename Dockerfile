ARG COWORKER_BUNDLE_REPOSITORY_URL=https://github.com/VirtualBeingsResearch/CoWorker.git
ARG COWORKER_BUNDLE_REPOSITORY_REF=
ARG COWORKER_IMAGE_REVISION=

FROM python:3.14-bookworm AS repository-bundle

ARG COWORKER_BUNDLE_REPOSITORY_URL
ARG COWORKER_BUNDLE_REPOSITORY_REF
ARG COWORKER_IMAGE_REVISION

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --bare "$COWORKER_BUNDLE_REPOSITORY_URL" /repository.git \
    && requested_ref="$COWORKER_BUNDLE_REPOSITORY_REF" \
    && if [ -z "$requested_ref" ]; then requested_ref="$COWORKER_IMAGE_REVISION"; fi \
    && if [ -z "$requested_ref" ]; then requested_ref=HEAD; fi \
    && git --git-dir=/repository.git rev-parse "$requested_ref^{commit}" \
       > /repository.revision \
    && if [ -z "$COWORKER_BUNDLE_REPOSITORY_REF" ]; then \
         git --git-dir=/repository.git symbolic-ref --short HEAD \
           > /repository.branch; \
       else \
         : > /repository.branch; \
       fi \
    && git --git-dir=/repository.git bundle create /repository.bundle --all

FROM python:3.14-bookworm AS base

ARG COWORKER_BUNDLE_REPOSITORY_URL

# Install system deps + Node.js 24 via NodeSource
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git gnupg ffmpeg openssh-client \
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
    HF_HOME=/opt/huggingface \
    COWORKER_BUNDLED_REPOSITORY_URL=${COWORKER_BUNDLE_REPOSITORY_URL} \
    COWORKER_WORKSPACE_PATH=/workspace/CoWorker \
    COWORKER_STATE_PATH=/var/lib/coworker

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
COPY --from=repository-bundle /repository.bundle /opt/coworker/repository.bundle
COPY --from=repository-bundle /repository.revision /opt/coworker/repository.revision
COPY --from=repository-bundle /repository.branch /opt/coworker/repository.branch
RUN chmod +x /app/scripts/container-entrypoint.sh

# Create runtime data directories (override by volume mount in production)
RUN mkdir -p data/inbox data/outbox data/identity data/logs data/memory data/workspace \
    .coworker/skills "$HF_HOME" "$COWORKER_STATE_PATH" /workspace

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/status || exit 1

ENTRYPOINT ["/app/scripts/container-entrypoint.sh"]
CMD ["/opt/venv/bin/coworker"]

# Optional release target. Build it with:
#   docker build --target with-embedder -t coworker:with-embedder .
# The default final target below deliberately stays lightweight and downloads this
# model lazily into the persistent Hugging Face cache on first use.
FROM base AS with-embedder

ARG EMBEDDER_MODEL=sentence-transformers/paraphrase-multilingual-mpnet-base-v2
ENV COWORKER_PRELOADED_EMBEDDER_MODEL=${EMBEDDER_MODEL}
RUN uv run python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['COWORKER_PRELOADED_EMBEDDER_MODEL'])"
VOLUME ["/workspace", "/var/lib/coworker", "/opt/huggingface"]

# Strict Hugging Face offline variant. Set this only after the model download above:
# a cache miss must fail instead of attempting a runtime network request.
FROM with-embedder AS offline
ENV HF_HUB_OFFLINE=1 \
    COWORKER_REPOSITORY_OFFLINE=1

# Keep the standard image as Docker's default build target.
FROM base AS runtime
VOLUME ["/workspace", "/var/lib/coworker", "/opt/huggingface"]
