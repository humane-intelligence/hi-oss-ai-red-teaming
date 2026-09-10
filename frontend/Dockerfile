# Multi-stage. `dev` = thin Node runtime for the local bind-mount workflow (make dev:
# deps live in the bind-mounted ./node_modules). `prod` = built static bundle served by
# Caddy — the deployment artifact, no Node runtime. docker-compose.yml pins `target: dev`;
# CI/CD build `--target prod`.

FROM node:24-alpine AS dev
WORKDIR /app
# Run as an arbitrary host UID (see docker-compose `user:`); give npm a writable HOME.
ENV HOME=/tmp \
    npm_config_cache=/tmp/.npm
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN npm ci
COPY . .
RUN npm run build

FROM caddy:2-alpine AS prod
# caddy:2-alpine lags Alpine's patched apk packages, so the Trivy gate flags fixed base-image
# CVEs (e.g. c-ares CVE-2026-33630); pull the current fixed packages at build.
# Never cache this layer: a restored copy freezes the patch level at whenever it was first
# built while the Trivy gate still passes. (`--no-cache` is apk's index flag, not that.)
RUN apk upgrade --no-cache
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
ARG GIT_SHA=
# Override the labels the caddy base sets — vendor/url/documentation/version otherwise
# describe Caddy, not this console, so docker inspect metadata is self-contradictory.
LABEL org.opencontainers.image.source="https://github.com/humane-intelligence/hi-oss-ai-red-teaming" \
      org.opencontainers.image.url="https://github.com/humane-intelligence/hi-oss-ai-red-teaming" \
      org.opencontainers.image.documentation="https://github.com/humane-intelligence/hi-oss-ai-red-teaming" \
      org.opencontainers.image.vendor="Humane Intelligence" \
      org.opencontainers.image.title="ai-red-teaming-frontend" \
      org.opencontainers.image.description="Operator console (React/Vite, static build) for the AI red-teaming backend, served by Caddy." \
      org.opencontainers.image.version="${GIT_SHA}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.licenses="Apache-2.0"
EXPOSE 80
