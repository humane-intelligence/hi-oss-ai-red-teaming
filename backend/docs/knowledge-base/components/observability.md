---
tags: [component, conventions]
aliases: [Observability, Metrics, Monitoring, Prometheus]
---

# Observability

What the backend emits so the outside world can see what it's doing: structured JSON logs, Prometheus metrics on `GET /metrics`, Celery task events on the broker, and health probes. This note is the convention reference — read it before adding a metric or changing a log field.

## The surface at a glance

| Signal | Where | Consumer |
|---|---|---|
| JSON logs (one object per line, stdout) | every container (`LOG_FORMAT=json`) | log shipper (Promtail → Loki); `docker logs` meanwhile |
| HTTP RED + process metrics | `GET /metrics` on the app | Prometheus scrape |
| AI-gateway call metrics (`redteam_*`) | same `/metrics` | Prometheus scrape |
| Celery task events | Redis broker | celery-exporter / Flower |
| Postgres metrics | `postgres-exporter` (opt-in compose `monitoring` profile, `make upmonitoring`; connects as a read-only `pg_monitor` role created by `make seedlocal`) | Prometheus scrape |
| Liveness / readiness | `GET /health`, `GET /ready` | compose healthchecks, probes |
| Errors | RFC 7807 responses + `logger.exception` tracebacks; unhandled 500s + worker task exceptions reported via the Sentry SDK (opt-in `SENTRY_DSN`) | logs; a self-hosted GlitchTip |

## JSON log line — required fields

Every line carries at least:

| Field | Source | Example |
|---|---|---|
| `timestamp` | `TimeStamper(iso, utc)` | `2026-07-13T10:12:03.220419Z` |
| `level` | `add_log_level` | `info` / `warning` / `error` |
| `service` | `SERVICE_NAME` setting | `ai-red-teaming-api`, `ai-red-teaming-worker` |
| `message` | structlog's `event`, renamed at render time | `http_request` |
| `request_id` | `LoggingMiddleware` contextvars (HTTP only) | `9f3e…` |

The `message` rename and `service` stamp happen only in the JSON render chain — local `console` output keeps structlog's native shape. Pipeline details: [Middleware, logging and request cycle](middleware-logging-and-request-cycle.md). Log shippers add `host`/`container` labels themselves; don't emit them from the app.

## `/metrics` endpoint

`prometheus-fastapi-instrumentator` wired in `app/main.py`: HTTP RED metrics (`http_requests_total`, `http_request_duration_seconds_*`) plus `prometheus_client` process/GC defaults. `/health`, `/ready` and `/metrics` itself are excluded from request metrics (scrape noise). The route is unauthenticated, excluded from the OpenAPI schema, and deliberately **not** routed through the reverse proxy — Prometheus reaches it over the compose network; network posture is the access control.

**Multiprocess gotcha (dormant):** `prometheus_client` keeps metrics per-process. Today the app runs a single uvicorn process, so this is moot — but if `--workers N` ever lands, each worker gets its own counters and the scrape hits a random one. The fix is multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`); don't enable it speculatively.

**Worker gap (dormant):** nothing in the worker calls the AI gateway today — model calls happen in the API process. If a task ever dispatches through the gateway, its counters would increment in a process that exposes no `/metrics` and never be scraped. Worker-side *task* visibility comes from task events instead (below); a worker `/metrics` server is deliberately out of scope until something needs it.

## AI-gateway call metrics

`app/core/ai_gateway/dispatch.py` meters every provider call:

- `redteam_model_calls_total{provider, outcome}` — counter. `provider` is the bounded `ProviderVendor` enum value; `outcome` is one of `ok`, `rate_limit`, `timeout`, `auth`, `context_window`, `bad_request`, `unavailable` (mapped from the [ProviderError taxonomy](ai-gateway-error-taxonomy.md)), `aborted` (consumer walked away mid-stream), `error` (anything else).
- `redteam_model_call_duration_seconds{provider}` — histogram with buckets up to 300 s (LLM calls routinely exceed the default 10 s ceiling). For streams the observation spans first iteration to stream exhaustion (the wrapper is lazy — the timer starts when the consumer starts iterating).

Only actual provider I/O is metered — pre-flight failures (unknown alias, unsupported modality, undecryptable credential) surface as exceptions/logs, not call metrics.

## Auth metrics

- `redteam_auth_revocation_check_failed_total` — counter, no labels (`app/core/auth/services/session_revocation.py`). Session-revocation checks that errored against Redis and **fell open** to "not revoked". A non-zero rate means force-logout is not being enforced for some requests, which is invisible in logs alone and does **not** surface as an HTTP error (the read path degrades silently by design). The fail-**closed** counterpart — the refresh exchange — surfaces as a 503 instead, so it needs no counter. See [Authentication (auth)](authentication.md).

## Celery task events

`worker_send_task_events` + `task_send_sent_event` are on (`app/workers/celery_app.py`), so the broker carries a task lifecycle event stream: consumed by Flower locally and by a Prometheus `celery-exporter` in deployments (task throughput, failures, runtime, queue length). Beat-scheduled tasks (the reapers) ride the same events. See [Celery workers](celery-workers.md).

## Error reporting — Sentry SDK, GlitchTip backend (opt-in)

`app/core/sentry.py` (`init_sentry(settings)`) wires the Sentry SDK; **unset `SENTRY_DSN` makes it a no-op**, so OSS deployments without an error-reporting backend are unaffected. The DSN points at a Sentry-compatible endpoint — in this project a self-hosted **GlitchTip** (deployed with the monorepo `monitoring/` stack). Decisions worth knowing before touching it:

- **Errors only, minimal data.** `traces_sample_rate=0.0` (no performance tracing), `send_default_pii=False`, `max_request_body_size="never"` (red-team prompts must not ride an event; the PII flag does not gate the body), `include_local_variables=False` (per-frame locals across asyncpg/SQLAlchemy internals both blow GlitchTip's envelope limit and can hold secrets — asyncpg keeps the DB password in a local).
- **Only plain 500s.** The Starlette/FastAPI integrations report `failed_request_status_codes={500}` — our RFC 7807 502/503 (flaky target-model endpoints) are expected operational noise, not incidents. `ignore_logger("app.core.error_handlers")` stops the catch-all handler's re-log from double-reporting each 500.
- **Init at import time**, before the FastAPI app is built (`app/main.py` calls it at module scope): the integrations patch the middleware stack at init, and the stack assembles lazily on the first ASGI scope — a lifespan-time init would leave the status-code filter inert. A malformed DSN is caught and degrades to "no error reporting" instead of crashing the process.
- **No outbound trace headers** (`trace_propagation_targets=[]`) — litellm calls reach externally-registered, untrusted target-model endpoints; don't leak `sentry-trace`/`baggage` there.
- **Worker:** `celeryd_init` runs the same `init_sentry` in the worker process (task exceptions reported); beat and the API import the module without sharing that init. See [Celery workers](celery-workers.md).
- **Correlation:** `LoggingMiddleware` sets the `request_id` tag on the Sentry isolation scope, so an event joins the `http_request` access log line. See [Middleware, logging and request cycle](middleware-logging-and-request-cycle.md).

## Adding a business metric — conventions

1. **Name:** `snake_case`, prefixed `redteam_`, unit suffix when there is one (`_seconds`, `_bytes` — never `_ms`), `_total` suffix for counters, no suffix for gauges. Dimensions go in labels, not the name (`redteam_flags_submitted_total{status=…}`, not `…_approved_total`).
2. **Labels:** bounded cardinality only — enums, status sets, provider names. Never user ids, request ids, aliases, or timestamps: Prometheus stores one series per label combination. Don't emit `service`/`host`/`environment` labels from the app — the scrape config attaches them.
3. **Placement:** module-level `Counter`/`Histogram` next to the code that increments it (see `dispatch.py` for the pattern). It lands on `/metrics` automatically via the default registry.
4. **Don't duplicate** what's already collected: container CPU/memory (cAdvisor), HTTP RED (instrumentator), task counts (celery-exporter).
5. **Document it here** — add the metric to this note with what it means and what it does *not* mean.

## Related

- [Middleware, logging and request cycle](middleware-logging-and-request-cycle.md)
- [Celery workers](celery-workers.md)
- [AI Gateway - dispatch](ai-gateway-dispatch.md)
- [Error handling (RFC 7807)](error-handling-rfc-7807.md)
- [Configuration (Settings)](configuration-settings.md)
