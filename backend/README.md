# ai-red-teaming-backend

[![Python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](app/main.py)
[![Celery](https://img.shields.io/badge/Celery-workers-37814A?logo=celery&logoColor=white)](app/workers/)
[![Postgres](https://img.shields.io/badge/Postgres-SQLModel-4169E1?logo=postgresql&logoColor=white)](app/models.py)
[![Redis](https://img.shields.io/badge/Redis-broker-DC382D?logo=redis&logoColor=white)](docker-compose.yml)
[![Ruff](https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=black)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](../LICENSE)

Async FastAPI backend for AI red-teaming workflows. Python 3.14, Celery, Postgres, Redis.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — dependency + venv manager. Install with `curl -LsSf https://astral.sh/uv/install.sh | sh` (or see the link for other methods).
- Python 3.14 — no system install needed; `make install` lets uv provision and pin it from `.python-version`.
- Docker + Docker Compose — required for running tests (Postgres + Redis spin up automatically)

## Setup

First run after cloning the repo — one command brings up a working local environment (deps, git hooks, a migrated DB, and a full local seed). Needs Docker running:

```bash
make setup
```

It runs, in order: `make install` (sync deps from `uv.lock` — also what CI runs), `make install-hooks` (wire `prek` into `.git/hooks/pre-commit` and `pre-push`; the pre-push set is `make lint` plus the two frontend hooks it skips), `make migrate`, and `make seedlocal`. Run those individually if you only need part of it.

## Environment

All settings live in [app/core/config.py](app/core/config.py) and are read via `get_settings()` — do not read env vars elsewhere, add a field to `Settings` instead.

**Resolution order:** process env → `.env` → `Settings` defaults. Exported vars override `.env`, so `DATABASE_HOST=localhost make test` works without editing the file.

**Local dev:** copy [.env.example](.env.example) to `.env`, then `make up`. **Production:** inject via real env vars (orchestrator secrets / runtime / vault) — it is not recommended to ship `.env`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `ENVIRONMENT` | yes | — | Deployment environment — one of `test`, `local`, `dev`, `prod`. |
| `DATABASE_HOST` | yes | — | Postgres hostname. |
| `DATABASE_PORT` | no | `5432` | Postgres TCP port. |
| `DATABASE_USER` | yes | — | Postgres username. |
| `DATABASE_PASSWORD` | yes | — | Password for `DATABASE_USER`. |
| `DATABASE_NAME` | yes | — | Postgres database name. |
| `REDIS_HOST` | yes | — | Redis hostname. |
| `REDIS_PORT` | no | `6379` | Redis TCP port. |
| `CELERY_BROKER_URL` | no | `redis://$REDIS_HOST:$REDIS_PORT/1` | Override Celery broker URL (e.g. to point at RabbitMQ). |
| `STREAMING_REAP_TTL_SECONDS` | no | `900` | Age after which a message stuck in `streaming` (hard-crash orphan) is reaped to `interrupted`. Must exceed the longest real generation. |
| `STREAMING_REAP_INTERVAL_SECONDS` | no | `300` | How often Celery beat runs the orphan-streaming reaper. |
| `MODEL_INACTIVITY_CHECK_INTERVAL_SECONDS` | no | `3600` | How often Celery beat sweeps for warmup-enabled models that have gone unused. The threshold itself is per-model (`inactivity_alert_hours`, opt-in); warmups do not count as use, so an endpoint kept warm by conversation opens still alerts. Must be > 0. |
| `MAX_CONVERSATION_GROUP_SIZE` | no | `4` | Hard cap on conversations per conversation group. Enforced when batch-creating a group (422) and when adding/moving a conversation into one (409). |
| `RESTORE_WINDOW_DAYS` | no | `7` | How long a soft-deleted row stays restorable, in days. Bounds both the `?deleted=true` listings and the `POST .../restore` endpoints — past it a tombstone is a 404. Never published to the API, so it can be retuned without a client change. Nothing purges tombstones past the window; they just stop being addressable. Must be > 0. |
| `PLATFORM_DEFAULT_DATA_LICENSE` | no | `CC-BY-4.0` | Default data license for evaluation/conversation data (the platform default; overridable at runtime via `PATCH /api/v1/platform-settings`). Must be a valid SPDX id from the catalog (`app/core/licenses/catalog.py`). Distinct from the code license (Apache-2.0). |
| `LOG_LEVEL` | no | `INFO` | Root log level (`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`). |
| `LOG_FORMAT` | no | `json` | Log renderer — `json` for production, `console` for dev (pretty + colored when TTY). |
| `SENTRY_DSN` | no | — | Opt-in error reporting to a self-hosted GlitchTip via the Sentry SDK protocol (see "Error reporting" below). Unset disables it entirely. |
| `SERVICE_NAME` | no | `ai-red-teaming-api` | `service` field stamped on JSON log lines. Compose sets `ai-red-teaming-worker` for the worker/beat services. |
| `DB_POOL_SIZE` | no | `5` | Max persistent SQLAlchemy connections per process. |
| `DB_MAX_OVERFLOW` | no | `10` | Burst connections above `DB_POOL_SIZE`. |
| `DB_POOL_TIMEOUT` | no | `30` | Seconds to wait for a free connection. |
| `DB_POOL_RECYCLE` | no | `1800` | Recycle connections older than this many seconds. |
| `DB_ECHO` | no | `false` | Log every SQL statement (development only). |
| `BULK_MAX_ROWS` | no | `1000` | Hard cap on rows accepted by a single bulk endpoint request. Larger imports should go through Celery. |
| `SESSION_JWT_SECRET` | for auth | — | HMAC secret used to sign / verify the app's session JWT. Required for `/auth/*` to work. |
| `SESSION_JWT_ALGORITHM` | no | `HS256` | JWS algorithm for the session JWT. |
| `SESSION_TTL_SECONDS` | no | `86400` | Session JWT lifetime. |
| `REFRESH_TOKEN_TTL_SECONDS` | no | `2592000` | Refresh token lifetime (default: 30 days). |
| `REFRESH_ABSOLUTE_MAX_LIFETIME_SECONDS` | no | `7776000` | Absolute ceiling on a refreshed session, from the original login (default: 90 days). `/auth/refresh` rejects once exceeded; must exceed `REFRESH_TOKEN_TTL_SECONDS`. |
| `OAUTH_STATE_SECRET` | for auth | — | Signs the short-lived cookie that carries OIDC `state`+`nonce` across the IdP redirect. |
| `OAUTH_COOKIE_SECURE` | no | `true` | Set `false` for local HTTP dev. |
| `OAUTH_REDIRECT_BASE_URL` | for auth | — | Public base URL the IdP will redirect back to (must match what's registered with the IdP). Required and pinned so the redirect URI never depends on `Host` / `X-Forwarded-Host`. |
| `OIDC_GOOGLE_CLIENT_ID` | no | — | Google OIDC client id. Unset leaves Google out of `GET /auth/oidc/providers` entirely, so the SPA hides the button. Must be set together with the secret. |
| `OIDC_GOOGLE_CLIENT_SECRET` | no | — | Google OIDC client secret. Setting one of the pair without the other fails at startup rather than silently disabling the provider. |
| `CORS_ORIGINS` | no | `[]` | JSON list of browser origins allowed to call the API. Empty disables CORS. Credentials are allowed, so `"*"` is not a valid entry. |
| `MODEL_SECRETS_KEY` | yes | — | Symmetric key used to encrypt AI-model API keys at rest (JWE `dir`+`A256GCM`). Conversation text is **not** sealed with it — that has its own required key. Must be ≥ 32 characters. New ciphertexts are tagged with this key's id; rotate by promoting a new value here and moving the old one to `MODEL_SECRETS_KEY_RETIRED`. |
| `MODEL_SECRETS_KEY_RETIRED` | no | — | Outgoing key during a rotation window. Set to the previous `MODEL_SECRETS_KEY` so existing ciphertexts keep decrypting. **Nothing re-wraps `ai_models.api_key_encrypted`** — there is no sweep for it, so dropping this key makes every model credential still sealed under it unrecoverable, and each one has to be re-entered by hand first. Transcripts are unaffected — they ride their own key and their own sweep. Must be ≥ 32 characters when set. |
| `CONVERSATION_SECRETS_KEY` | yes | — | Key sealing conversation message text under a `protects_conversation_data` licence. Deliberately never the model one, so the two datasets keep distinct blast radii: rotating or losing one leaves the other readable. Must be ≥ 32 characters. Required rather than falling back to `MODEL_SECRETS_KEY` — a borrowed key would rest the licence's promise on the secret protecting provider credentials, and startup refuses key material shared between the two keyrings, retired slots included. On the boxes an SSM parameter wins; without one `deploy.sh` generates a key on the first rollout and keeps it beside `.env`, where it exists on that box alone: see [`deploy/README.md`](../deploy/README.md). |
| `CONVERSATION_SECRETS_KEY_RETIRED` | no | — | Outgoing transcript key during a rotation window. Rows carry the id of the key that sealed them, so they keep opening while it stays set; run `make rewraptranscripts` and drop it only once that run exits 0 — which means no row is left that a re-run could still move. Must be ≥ 32 characters when set. |
| `HUGGINGFACE_API_KEY` | no | — | Default credential for the `huggingface` provider when an `AiModel` row has no `api_key` of its own. |
| `GOOGLE_API_KEY` | no | — | Default credential for the `google` provider when an `AiModel` row has no `api_key` of its own. |
| `OPENAI_API_KEY` | no | — | Default credential for the `openai` provider when an `AiModel` row has no `api_key` of its own. |
| `ANTHROPIC_API_KEY` | no | — | Default credential for the `anthropic` provider when an `AiModel` row has no `api_key` of its own. |
| `AZURE_API_KEY` | no | — | Default credential for the `azure` provider when an `AiModel` row has no `api_key` of its own. |
| `COHERE_API_KEY` | no | — | Default credential for the `cohere` provider when an `AiModel` row has no `api_key` of its own. |
| `SLM_API_KEY` | no | `local` | Credential for the opt-in self-hosted `slm` compose service (`make upllm`). Non-secret local default; the compose `--api-key`, the seeded `local-slm` row, and the e2e test all read it. |
| `EMAIL_BACKEND` | no | `console` | Email delivery backend: `console` (log-only), `ses` (AWS SESv2), or `smtp`. |
| `EMAIL_FROM` | yes | — | Sender address for all transactional email. |
| `BRAND_COMPANY` | no | `Humane Intelligence` | Operator name shown in the shared email footer. |
| `BRAND_PRODUCT` | no | `AI Red Teaming` | Product name injected into email subjects and the footer as the Jinja `brand.product`. |
| `EMAIL_FOOTER_ADDRESS` | no | — | Optional postal / company line in the email footer; omitted entirely when unset. |
| `FRONTEND_BASE_URL` | yes | — | SPA base URL embedded in transactional mails (e.g. invitation accept link). No trailing slash. |
| `INVITATION_TTL_HOURS` | no | `168` | Platform invitation token lifetime (default: 7 days). |
| `EMAIL_VERIFICATION_TTL_HOURS` | no | `24` | Self-signup email-verification token lifetime. |
| `PASSWORD_RESET_TTL_HOURS` | no | `24` | Password-reset token lifetime. |
| `EXPORT_STORAGE_BACKEND` | no | `local` | Storage adapter for finished export files — `local` or `s3`. Each provider's config coexists below; only the selected one is read. **The deployed box runs `s3`:** `app` and `worker` are separate containers with no shared volume there, so a `local` file written by the worker is unreachable for the download. |
| `EXPORT_STORAGE_DIR` | no | `var/exports` | `local` backend only — base directory the finished export files are written into. |
| `EXPORT_S3_BUCKET` | if `s3` | — | Bucket for exports. Required when `EXPORT_STORAGE_BACKEND=s3`, validated at startup. |
| `EXPORT_S3_PREFIX` | no | `exports/` | Key prefix inside the bucket. |
| `EXPORT_JOB_TTL_SECONDS` | no | `86400` | How long a finished export stays downloadable; the reaper then deletes the file and the job row (default: 24 h). |
| `EXPORT_REAP_INTERVAL_SECONDS` | no | `3600` | How often Celery beat runs the export expiry sweep. |
| `EXPORT_STUCK_TTL_SECONDS` | no | `3600` | A job stuck in `pending`/`running` longer than this (worker crash, or a broker drop with no `acks_late` redelivery) is failed by the reaper. It carries no `expires_at`, so the TTL sweep alone would never touch it. |
| `MEDIA_STORAGE_BACKEND` | no | `local` | Storage adapter for uploaded images (covers, avatars, icons) — `local` or `s3`. Mirrors the export port, for single blobs rather than text streams. |
| `MEDIA_STORAGE_DIR` | no | `var/media` | `local` backend only — base directory for uploaded image bytes. |
| `MEDIA_S3_BUCKET` | if `s3` | — | Bucket for media. Required when `MEDIA_STORAGE_BACKEND=s3`. |
| `MEDIA_S3_PREFIX` | no | `images/` | Key prefix inside the bucket. |
| `MEDIA_SIGNED_URL_TTL_SECONDS` | no | `900` | Lifetime of a signed media URL. The token is app-signed on the proxy-GET, deliberately not S3-presigned, so it stays backend-agnostic. |
| `MEDIA_URL_SIGNING_SECRET` | no | — | Dedicated signing secret for media URLs; unset falls back to `SESSION_JWT_SECRET`. Set it to rotate media-URL signing independently of session tokens. Must be ≥ 32 characters when set. |
| `MEDIA_ORPHAN_GRACE_HOURS` | no | `168` | Orphan GC: a live asset older than this that no known consumer references is soft-deleted and its blob removed. The grace keeps a just-uploaded image alive while its owner attaches it (default: 7 days). |
| `MEDIA_ORPHAN_REAP_INTERVAL_SECONDS` | no | `86400` | How often Celery beat runs that orphan sweep. |
| `HEALTH_CHECK_BACKOFF_SECONDS` | no | `5` | Per-model health check: how long the probe waits between retries while an endpoint is still starting. |
| `HEALTH_CHECK_WAKE_DEADLINE_SECONDS` | no | `120` | How long the probe keeps retrying a cold-starting endpoint before giving up. The task's abandonment horizon is derived from this rather than being its own knob, so the two cannot drift apart. |
| `SMTP_HOST` | if `smtp` | — | SMTP server, used only when `EMAIL_BACKEND=smtp`. |
| `SMTP_PORT` | no | `587` | SMTP port. |
| `SMTP_USERNAME` | no | — | SMTP user. Authentication is skipped entirely when unset (e.g. a local Mailpit). |
| `SMTP_PASSWORD` | no | — | Password for `SMTP_USERNAME`. |
| `SMTP_TLS` | no | `starttls` | `none` (local Mailpit), `starttls` (port 587), or `ssl` (implicit TLS, port 465). |
| `AWS_REGION` | no | `us-east-1` | Region for the `ses` email backend and the S3 storage adapters. boto3 takes credentials from its default chain — no key pair lives in config. |
| `GIT_SHA` | no | — | Deployed commit SHA, written into `.env` by `deploy.sh` and surfaced on `GET /version`. Empty locally, which reads as `dev`. |
| `POSTGRES_MONITORING_PASSWORD` | no | — | Password for the read-only `monitoring` Postgres role (`pg_monitor`: stats views only, no table access) used by the opt-in `postgres-exporter` service. Unset skips role creation in `scripts.seed_local`. |

**Dev-only (not `Settings`):** `POSTMAN_API_KEY` + `POSTMAN_WORKSPACE_ID` are read straight from the environment by `make postmanpush` (the one-way repo→Postman sync), never through `Settings` — so they're intentionally absent from the table above. Leave them unset unless you push collections; both are documented commented-out in [.env.example](.env.example). The Postman `local` environment's own variables (`base_url`, seed creds, `live_*` slots) live in [docs/postman/environments/local.postman_environment.json](docs/postman/environments/local.postman_environment.json), not in `.env`.

**Auth note:** OIDC provider credentials (`client_id` / `client_secret`) come from `Settings` (`OIDC_GOOGLE_CLIENT_ID` / `_SECRET` above); the provider *list* itself still lives in code in [app/core/auth/services/providers.py](app/core/auth/services/providers.py) and will move to a DB-backed registry once schemas land. First login provisions or links a local user via a persisted `(provider, subject)` identity — see [app/core/auth/services/oidc.py](app/core/auth/services/oidc.py).

## Quickstart

```bash
make test             # pytest on the host against db + redis from docker compose
make lint             # prek hooks: ruff + ty + deptry + bandit + housekeeping (frontend hooks skipped)
make docslint         # housekeeping hooks only, repo-wide (what CI runs on non-backend changes)
make format           # ruff format + ruff check --fix
make openapidump      # refresh docs/openapi.yaml
make erddump          # refresh docs/erd.md (Mermaid ERD)
make permissionsdump  # refresh docs/permissions.md (roles/permissions matrix)
make postmandump      # refresh docs/postman mirror collection from docs/openapi.yaml
make postmanpush      # push generated collections to your Postman workspace (POSTMAN_API_KEY + POSTMAN_WORKSPACE_ID)
make postmanpushdry   # preview what postmanpush would create/update (no mutations; works without creds)
make newman COLL=12-licensing       # run one self-contained collection under Newman (live stack required)
make newmanchained COLL=04-models    # same, for a collection that needs 00-bootstrap's exported environment
make services         # start only db + redis (compose), wait for healthy
make up               # start the full stack (app + worker + beat + flower + db + redis + mailpit) detached
make upllm            # start the full stack + the opt-in self-hosted SLM (llama.cpp); first run downloads the model
make upmonitoring     # start postgres-exporter for the local monitoring stack (see ../monitoring/README.md)
make down             # stop docker compose stack (incl. opt-in profiles like the SLM)
make logs             # tail docker compose logs (follow)
make shell            # exec bash in the running app container
make dbshell          # pgcli on host against the compose db (localhost:5432)
make update           # upgrade all uv deps (relock) + prek hook versions
make updatehooks      # bump prek hook versions only (no relock)
make help             # list every target
```

`make test` brings up `db` + `redis` via docker compose (waits for healthchecks),
then runs `pytest` on the host against `localhost`. Same path runs in CI. The
app container is only used for `make up` / production-style runs.

## Email

Transactional mail goes through a pluggable backend selected by `EMAIL_BACKEND` (see [app/core/email/backends/](app/core/email/backends/)): `console` (default — logs each mail as an `email.sent.console` event, no network), `ses` (AWS SESv2, credentials from the default boto3 chain + `AWS_REGION`), or `smtp` (any SMTP server via `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_TLS`).

For local dev the compose stack runs **Mailpit** — a throwaway SMTP server with a web inbox — so you can see rendered mail (verification links, invitations, …) without AWS or a real mailbox. It's off by default (`console` just logs); to route mail to it, set in `backend/.env`:

```bash
EMAIL_BACKEND=smtp
SMTP_HOST=mailpit
SMTP_PORT=1025
SMTP_TLS=none
```

Then `make up` and open the inbox at http://localhost:8025. (Mailpit always runs with the stack; switching `EMAIL_BACKEND` is all that's needed.)

To eyeball every template at once (styling review), `make emailpreview` sends one sample of each registered template straight to Mailpit — no `.env` change needed, it forces the `smtp` backend for that run only.

## Error reporting

Error reporting goes to a self-hosted, open-source **[GlitchTip](https://glitchtip.com)** (MIT) instance. GlitchTip speaks the Sentry SDK wire protocol, so the app uses the standard `sentry-sdk` client unchanged — `SENTRY_DSN` just points at our own GlitchTip rather than a SaaS.

Opt-in via `SENTRY_DSN` — unset (the default) makes [app/core/sentry.py](app/core/sentry.py)'s `init_sentry` a no-op, so a plain OSS deployment is unaffected. Set it and both the API process (`app/main.py`, initialized at import — before the middleware stack is built) and the Celery worker (`app/workers/celery_app.py`, on worker-process startup) report to it — errors only, no performance tracing (`traces_sample_rate=0.0`). The beat scheduler is deliberately out (`celeryd_init` fires only in workers).

FastAPI/Starlette only report a plain `500` — our RFC 7807 `BadGatewayError` (502) / `ServiceUnavailableError` (503) are expected operational noise from flaky target-model endpoints, not incidents. Celery task exceptions are captured automatically (`CeleryIntegration` auto-enables). `send_default_pii=False` and `max_request_body_size="never"` keep request bodies (red-team prompts) out of events; `request_id` is attached as an event tag by `LoggingMiddleware`, while `method` / `path` come from the SDK's own request capture. The catch-all handler re-logs every unhandled exception the Starlette integration already reports, so its logger is excluded from Sentry (`ignore_logger`) — otherwise each 500 would be reported twice.

To enable it, point `SENTRY_DSN` at a Sentry-protocol endpoint (a self-hosted GlitchTip) and restart the app + worker so `init_sentry` picks it up. Trigger any unhandled error (API or a Celery task) and it appears in the project.

Testing against the local monitoring stack (`make mon-up`, a root target): GlitchTip's project settings page shows the DSN using its own `GLITCHTIP_DOMAIN` (`localhost:8090`) — the browser-facing host. That host is unreachable from inside the `app`/`worker` containers, so the SDK's events silently fail to send. Replace the host with GlitchTip's compose service name and internal port instead: `glitchtip:8080`, e.g. `http://<public_key>@glitchtip:8080/<project_id>`.

## Self-hosted models (OpenAI-compatible)

Any self-hosted model that exposes an OpenAI-compatible API (vLLM, llama.cpp's `llama-server`, Ollama, LocalAI, …) is registered as an `AiModel` row with **no code change** — the gateway routes the `generic` provider through litellm's `openai` prefix with a caller-supplied `api_base`:

```jsonc
POST /api/v1/ai-models
{
  "name": "My local model",
  "model_alias": "my-local-slm",
  "provider": "generic",
  "provider_model_id": "qwen2.5-0.5b-instruct",  // the server's model/alias
  "inference_endpoint": "http://my-host:8080/v1", // OpenAI-compatible base URL
  "api_key": "local"                              // see the note below
}
```

Then dispatch by alias — e.g. `POST /api/v1/chat/stream` with `"model_alias": "my-local-slm"`.

> **Auth note:** most self-hosted servers don't require a key, but litellm's `openai` path rejects an *empty* one. Send any non-empty `api_key` (and, if the server enforces auth, set it to match, e.g. `llama-server --api-key local`).

**Local test server.** The compose stack ships an **opt-in** `slm` service (`ghcr.io/ggml-org/llama.cpp` serving Qwen2.5-0.5B-Instruct, CPU-only) behind the `llm` profile, so plain `make up` (or `make dev` from the repo root) skips it. Start it with:

```bash
make upllm   # full stack + the SLM; first run downloads the ~400 MB GGUF into the slm_models volume
```

`make seedlocal` registers a matching `local-slm` row pointing at `http://slm:8080/v1` — inert until `make upllm` is running, then callable via `chat/stream`. The server, that seeded row, and the e2e test share one credential — `SLM_API_KEY` in `.env` (default `local`); change it there to rotate all three. From the repo root, `docker compose --profile llm up` brings up the whole stack (incl. frontend) with the SLM.

## Migrations

Migrations run inside the app container so they use the exact same environment as production.
`alembic/` and `alembic.ini` are volume-mounted, so files generated by `makemigrations` appear on the host automatically.

```bash
make migrate                                # apply all pending migrations (upgrade head)
make syncroles                              # upsert canonical system roles (deploy step, run after migrate)
make synclicenses                           # upsert the curated data licenses (deploy step, run after migrate)
make syncannotationlabels                   # upsert the curated annotation labels (deploy step, run after migrate)
make makemigrations MSG='add users table'   # generate a new autogenerate migration
make undomigration                          # roll back the last migration
make showmigrations                         # show the full migration history
make currentmigration                       # show the currently applied revision
make migrationsql                           # emit pending migrations as raw SQL (no live DB needed)
make checkmigrations                        # fail if the history has more than one head (no live DB needed)
make resetdata                               # local dev: drop ALL compose volumes (db + redis) and re-migrate
```

Migration filenames follow the pattern `YYYYMMDD_HHMM_<rev>_<slug>.py`, e.g. `20260513_1430_a1b2c3d4_add_users_table.py`.

Canonical system roles (`admin`, `owner`, `red_teamer`, `annotator`, `viewer`) and their
permissions are defined in [app/core/auth/roles.py](app/core/auth/roles.py) and synchronized
into the database by `make syncroles` — an idempotent, environment-agnostic deploy step that
runs right after `make migrate`. `make seedlocal` reuses the same sync and additionally
seeds a full local dataset — one login per role (`admin`/`owner`/`redteamer`/`annotator`/`viewer`@test.com,
shared password `password123`) plus a demo evaluation, conversation, flag and pending review, for
exercising the UI under each role (local environment only). The full
role-to-permission breakdown is documented in [docs/permissions.md](docs/permissions.md)
(see [Roles & permissions](#roles--permissions)).

When adding a new model:
1. Subclass `BaseModel` from `app.core.base_model` (provides `id`, timestamps)
2. Import the model module in `app/models.py` — Alembic picks it up automatically
3. Run `make makemigrations MSG='...'` — Alembic detects the schema diff and generates the migration
4. Run `make erddump` and commit the refreshed [docs/erd.md](docs/erd.md) in the same diff (the pre-push hook does this; CI fails on drift)

## API

- **Interactive docs.** Swagger UI at `/docs`, ReDoc at `/redoc`.
- **Raw spec.** `GET /openapi.json` from a running app.
- **Committed schema.** [docs/openapi.yaml](docs/openapi.yaml) — versioned dump of the live spec, used for code review and as input for SDK / Postman / Stoplight tooling. Regenerate with `make openapidump` (pure introspection — no DB / Redis / docker needed). The pre-push hook refreshes it automatically when `app/**.py` changes; CI fails if the committed file drifts from what the app serves.
- **Versioning.** Domain endpoints live under `/api/v1/...`. Probes (`/health`, `/ready`) and Prometheus `/metrics` (HTTP RED + process metrics; excluded from the OpenAPI schema, not proxied publicly) are top-level and unversioned.
- **Errors.** Every non-2xx response uses the RFC 7807 *Problem Details* envelope with `Content-Type: application/problem+json` (`type`, `title`, `status`, `detail`, `instance`, plus `errors[]` for 422). `type` is `about:blank` except where a client must branch on the reason rather than the status: the terms refusal below carries `urn:redteam:error:terms-acceptance-required`, because it shares 403 with a permission denial and means the opposite — that one is a dead end, this one is cleared by accepting — except the OIDC login and callback routes (`/api/v1/auth/oidc/{provider}/login` and `/callback`), which the browser reaches by top-level navigation and which answer an in-flow login failure with a `302` + `#error=<code>` redirect instead, so a JSON body doesn't strand the user on a dead page.
- **Pagination.** List endpoints take `limit` (1..100, default 20) and `offset` (≥ 0, default 0) and return a `Page` wrapper (`items`, `total`, `limit`, `offset`).
- **Filtering & ordering.** List endpoints expose per-resource filters via Pydantic deps; `order_by` (leading `-` for descending) picks the sort column from a typed whitelist.
- **Bulk.** Bulk endpoints wrap [app/core/bulk.py](app/core/bulk.py): `BulkRequest[T]` → `BulkResponse[R]`, always 200 (partial failures inline as RFC 7807), `dry_run=True` previews without committing. Hard cap via `BULK_MAX_ROWS`.
- **Full conventions.** See [.claude/skills/api/SKILL.md](.claude/skills/api/SKILL.md) — the authoritative reference for adding endpoints.

## Database schema (ERD)

[docs/erd.md](docs/erd.md) — a Mermaid entity-relationship diagram of every table, column, and foreign key, rendered straight from the SQLModel metadata. GitHub renders the Mermaid block inline. Regenerate with `make erddump` (pure introspection — no DB / Redis / docker / running app needed). The pre-push hook refreshes it automatically when `app/**.py` changes; CI fails if the committed file drifts from the models.

## Roles & permissions

[docs/permissions.md](docs/permissions.md) — a matrix of every canonical role against every permission, plus a role catalog (display names + descriptions), the per-object **Object scopes** (assignable roles + break-glass permission for the `object_roles` layer), and the assign/revoke elevation map. Rendered straight from the RBAC sources of truth: [app/core/auth/roles.py](app/core/auth/roles.py) and the object-role registry [app/core/auth/object_roles/registry.py](app/core/auth/object_roles/registry.py). Regenerate with `make permissionsdump` (pure introspection — no DB / Redis / docker / running app needed). The pre-push hook refreshes it automatically when either source changes; CI fails if the committed file drifts from the definitions.

### Permission surface for clients

The permission vocabulary has **one source of truth** — the `Permission` enum in [app/core/auth/roles.py](app/core/auth/roles.py) — and the same strings are exposed three ways that never diverge:

- **JWT access token.** The `permissions` claim carries the flattened union of the caller's role permissions as a JSON array of strings, e.g. `"permissions": ["evaluations:read", "flags:create"]` (minted in [app/core/auth/services/jwt.py](app/core/auth/services/jwt.py)). The FE may decode it as a **fast-path** for UI gating without a round-trip. It lags up to the token TTL after a role change, and **roles themselves are not in the token** — fetch them from `/auth/me`.
- **`GET /api/v1/auth/me`.** Returns the caller's `roles` (slim: `id` + `name` + display label) and effective `permissions` (the flattened union across those roles), resolved **live from the DB** so the FE can refetch fresh state right after a role change. Gate the UI on this union, not on a single role's permissions. Session fields (`id`, `email`, `email_verified`, `provider`) still come from the token; names, `has_password` and `organization` are projected from the row. It also carries the caller's consent state — `consent_terms` / `consent_emails` / `terms_accepted_at` plus `accepted_terms` (the version actually accepted, which is not necessarily the current one) and `terms_acceptance_required`, the flag a client blocks the app on until the current terms of service are accepted via `POST /api/v1/auth/me/terms`. **The flag is a convenience, not the enforcement:** once a version is published, every authenticated endpoint answers `403` with `type: urn:redteam:error:terms-acceptance-required` until this account accepts it — `GET /auth/me`, `GET /terms/current`, `GET /terms/{id}` and `POST /auth/me/terms` keep answering, so the state is always clearable. Public endpoints never resolve the gate at all, except the signed-URL mint/fetch pair for a *private* image, which authorises on identity and therefore refuses the same way (see `app/core/terms/`).
- **`GET /api/v1/roles`** (gated on `roles:read`, held by admin + owner) — the role catalog: assignable roles with their full permission set and `is_system` flag, the source for `role_ids` on user update/invite.
- **`GET /api/v1/permissions`** (gated on `roles:read`, like `/roles`) — the permission catalog: every permission key with a human description, for the role-builder/review UI. The same strings as committed `docs/permissions.md`; gated like `/roles` because the same admin/owner role-builder UI consumes both.

## Knowledge base

[docs/knowledge-base/](docs/knowledge-base/) — a hand-written, Obsidian-compatible knowledge base documenting the whole backend in plain English: architecture, data models, components, and end-to-end flows. Entry note: [docs/knowledge-base/README.md](docs/knowledge-base/README.md). It is GitHub-Flavored Markdown (kebab-case files, relative links, Mermaid), so it renders on GitHub and still opens as an [Obsidian](https://obsidian.md) vault (best for the graph + backlinks). Unlike `erd.md` / `openapi.yaml` / `permissions.md` it is **not generated** — keep it current with the `kb-sync` skill ([.claude/skills/kb-sync/SKILL.md](.claude/skills/kb-sync/SKILL.md)), which mirrors merged `main` and detects drift via its cursor.

## Deployment (dev)

A single-instance dev environment runs the whole stack via Docker Compose behind Caddy. On merge to `main`, the `CD` workflow ([.github/workflows/cd.yml](../.github/workflows/cd.yml)) publishes the image to GHCR and rolls the box automatically — OIDC for AWS auth, SSM Send-Command to reach the box (no inbound SSH). Full walkthrough (what/why/how + manual deploy + debugging) in [deploy/README.md](../deploy/README.md). The artifacts live under [deploy/](../deploy/):

- [deploy/docker-compose.dev.yml](../deploy/docker-compose.dev.yml) — pulls a pre-built image from GHCR (no bind mounts, no `--reload`), adds a Caddy reverse proxy, and keeps `db` / `redis` off the host network. `app` runs uvicorn with `--proxy-headers` to trust Caddy's `X-Forwarded-*`.
- [deploy/Caddyfile](../deploy/Caddyfile) — TLS termination + reverse proxy to `app:8000`. `APP_DOMAIN` (from `.env`) drives the automatic Let's Encrypt cert and the HTTP→HTTPS redirect.
- [deploy/ec2-bootstrap.sh](../deploy/ec2-bootstrap.sh) — EC2 user-data: installs Docker + the compose plugin, the AWS CLI, git, and unattended-upgrades, creates `/opt/aibackend`.
- [deploy/deploy.sh](../deploy/deploy.sh) — run on the box: renders `.env` from SSM Parameter Store, logs in to GHCR, pulls, migrates (`alembic upgrade head`), syncs canonical roles (`scripts.sync_roles`), then rolls the stack. Region comes from IMDSv2.

## Project layout

```
app/
├── api/
│   ├── health.py     # /health, /ready (unversioned probes)
│   └── v1/           # /api/v1/* — domain endpoints
├── core/             # config, schemas, exceptions, error_handlers, openapi
├── workers/          # Celery app + sync DB session
├── models.py         # central model registry (Alembic imports this)
└── main.py           # app factory + lifespan
scripts/              # standalone runnable scripts
├── seed_local.py     # full local dataset seed — one login per role (make seedlocal)
├── sync_roles.py     # canonical system roles upsert (make syncroles; deploy step)
├── sync_licenses.py  # curated data-license catalog upsert (make synclicenses; deploy step)
├── rewrap_transcripts.py # re-wrap sealed message text onto the active key (make rewraptranscripts)
├── dump_openapi.py   # backs `make openapidump`
├── preview_emails.py # one sample of every email template into Mailpit (make emailpreview)
├── dump_erd.py       # backs `make erddump`
├── dump_permissions.py  # backs `make permissionsdump`
├── dump_postman.py   # backs `make postmandump`, with openapi_permissions.py for the per-route gates
└── push_postman.py   # backs `make postmanpush` / `postmanpushdry`
tests/
├── conftest.py       # shared fixtures (client, async_client)
├── unit/             # pure logic — no I/O
├── api/              # mirrors app/api — endpoint tests with real DB
├── core/             # mirrors app/core
├── email/            # mirrors app/core/email
├── workers/          # mirrors app/workers
└── scripts/          # mirrors scripts/
alembic/              # migration env + versions/
docs/
├── openapi.yaml      # committed OpenAPI schema (see API section)
├── erd.md            # committed Mermaid ERD (see Database schema section)
├── permissions.md    # committed roles/permissions matrix (see Roles & permissions section)
├── postman/          # generated API mirror + hand-authored scenario collections (docs/postman/README.md)
└── knowledge-base/   # hand-written Obsidian-compatible vault (see Knowledge base section)
deploy/               # dev EC2 deploy: prod compose, Caddyfile, bootstrap + deploy scripts
```

Tests mirror the source tree — when a new source area is added (e.g. `app/services/`), create `tests/services/` alongside it. Tier is a marker (`unit` / `integration` / `e2e`), not a directory — see [.claude/skills/tests/SKILL.md](.claude/skills/tests/SKILL.md).

## Conventions

- All workflows run through `make` — never call `uv run pytest`, `ruff`, `ty` etc. directly.
- Tests in pytest; the tier (`unit` / `integration` / `e2e`) is a marker, not a directory — layout mirrors the source tree. See [.claude/skills/tests/SKILL.md](.claude/skills/tests/SKILL.md).
- API endpoints follow the conventions in [.claude/skills/api/SKILL.md](.claude/skills/api/SKILL.md): `/api/v1` prefix, RFC 7807 errors, `PaginationDep` + `Page[T]` for lists.
- Per-task workflows for AI assistants — adding endpoints, models, migrations, and email templates — live under [.claude/skills/](.claude/skills/) (`new-endpoint`, `new-model`, `new-migration`, `new-email-template`).
- The Postman suite — what each of the 24 scenario collections walks, how to run it under Newman, and
  which two cannot run unattended — is documented in [docs/postman/README.md](docs/postman/README.md).
  Authoring, verifying, and pushing them follows [.claude/skills/postman-scenarios/SKILL.md](.claude/skills/postman-scenarios/SKILL.md): layout + auth/variable conventions, the build → verify-on-live-stack → push workflow (`make postmandump` / `postmanpushdry` / `postmanpush`), and the request-contract gotchas.
- Background tasks (Celery) follow the conventions in [.claude/skills/tasks/SKILL.md](.claude/skills/tasks/SKILL.md): one `tasks.py` per bounded context (auto-discovered), JSON-only payloads, DB via `session_scope`, declared retry policy.
- Soft delete is opt-in per statement: every `BaseModel` subclass gets `live_select()` / `live_update()` classmethods that pre-attach a `with_loader_criteria` filtering rows where `deleted_at IS NOT NULL` (see [app/core/soft_delete.py](app/core/soft_delete.py)). Plain `select(Model)` / `update(Model)` / `delete(Model)` deliberately do NOT filter — that's the escape hatch for ops tooling. Flip the flag on a loaded row with `obj.soft_delete(by_id)` / `obj.restore()` — `by_id` records who deleted it (`None` for system-initiated), which is what scopes a later restore; for bulk soft-delete, chain `.values(deleted_at=func.now(), deleted_by_id=actor_id)` onto `live_update()` so the timestamp is stamped server-side at execute time and the deleter is recorded. Reading tombstones back (the restore window and deleter scoping) lives in [app/core/restore.py](app/core/restore.py). For queries that eager-load a second soft-delete-aware model, attach `with_live(OtherModel)` to `.options(...)` so its rows are filtered too. Full contract in the [`new-model` skill](.claude/skills/new-model/SKILL.md).
- Single source of truth for tool config: [pyproject.toml](pyproject.toml).
- Editor whitespace via [.editorconfig](../.editorconfig).
- Pre-commit hooks: [.pre-commit-config.yaml](../.pre-commit-config.yaml).
- CI runs `make lint`, `make audit` (dependency CVE scan) and `make test-cov` on every PR that touches backend code, and `make docslint` on every PR that does not: [.github/workflows/](../.github/workflows/).
- Weekly grouped dep bumps via Dependabot: [.github/dependabot.yml](../.github/dependabot.yml).
- The hand-written knowledge base lives in [docs/knowledge-base/](docs/knowledge-base/); keep it current with the [`kb-sync` skill](.claude/skills/kb-sync/SKILL.md) (mirrors merged `main`, detects drift via its cursor).
- AI assistant instructions for this repo: [CLAUDE.md](CLAUDE.md).
