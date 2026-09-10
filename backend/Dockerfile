# ===========================================================================
# Builder — resolves and installs production dependencies into /opt/venv
# ===========================================================================
FROM python:3.14.7-slim AS builder

WORKDIR /src

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project


# ===========================================================================
# Production — slim runtime: no uv, no dev deps, no tests, non-root user
# ===========================================================================
FROM python:3.14.7-slim AS runtime

# Pull the latest Debian security patches — the python:slim base lags the
# security repo, and CI gates the build on fixed HIGH/CRITICAL CVEs (Trivy,
# ignore-unfixed). Done as root before dropping to the app user below.
# Never cache this layer: a restored copy applies the patches of whenever it was first built,
# and the scan gating on it passes until the vuln DB catches up. Busting it leaves the builder
# stage above (the slow dependency install) cached.
# Error-Mode=any because a plain `apt-get update` exits 0 when every source is unreachable,
# which would patch nothing and still build green.
RUN apt-get -o Acquire::Retries=3 -o APT::Update::Error-Mode=any update \
    && apt-get -o Acquire::Retries=3 upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# Drop the base image's pip: unreachable from /opt/venv and unused at runtime, while its
# vendored bundle (msgpack, setuptools) is what the CVE gate flags.
RUN python -m pip uninstall -y pip

WORKDIR /src

COPY --from=builder /opt/venv /opt/venv

COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini ./
COPY scripts/ scripts/

# Pre-create the async-export + uploaded-media storage dirs so a fresh named volume mounted
# here inherits `app` ownership (Docker seeds volume perms from the image path), letting
# the non-root runtime user write generated export files / uploaded images.
# chown to app:0 (group root) + make the export dir group-writable so the runtime user
# can write it whatever UID compose runs as (${DOCKER_UID}, = the host's id -u, often != 1000).
# Docker seeds a fresh named volume's perms from this image path, so group-writable is what
# lets a non-1000 host UID write generated export files while staying non-root.
RUN mkdir -p /src/var/exports /src/var/media && useradd -m app && chown -R app:0 /src && chmod -R g+rwX /src/var/exports /src/var/media

USER app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Last layer so the per-build SHA doesn't bust the dependency cache above.
# Build provenance only (OCI label); the running app reads the deployed SHA from
# the runtime env (.env), so no ENV GIT_SHA is baked.
ARG GIT_SHA=
LABEL org.opencontainers.image.source="https://github.com/humane-intelligence/hi-oss-ai-red-teaming" \
      org.opencontainers.image.title="ai-red-teaming-backend" \
      org.opencontainers.image.description="AI red-teaming backend API (FastAPI + Celery)." \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.licenses="Apache-2.0"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
