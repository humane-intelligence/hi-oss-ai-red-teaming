# Monitoring stack (opt-in)

[![Prometheus](https://img.shields.io/badge/Prometheus-metrics-E6522C?logo=prometheus&logoColor=white)](prometheus/)
[![Loki](https://img.shields.io/badge/Loki-logs-F5A800?logo=grafana&logoColor=white)](loki/)
[![Grafana](https://img.shields.io/badge/Grafana-dashboards-F46800?logo=grafana&logoColor=white)](grafana/)
[![GlitchTip](https://img.shields.io/badge/GlitchTip-errors-6C5FC7)](docker-compose.yml)

Prometheus + Loki + Promtail + Grafana + celery-exporter + node-exporter + cadvisor +
blackbox-exporter + GlitchTip — a self-authored, self-contained overlay for wherever [`../deploy/`](../deploy/)
is deployed (a single box; see `../deploy/README.md` for the specifics of that
environment — nothing here is tied to it). A **separate compose
project** from `../deploy/docker-compose.dev.yml`. Joins the app stack's
network externally (`deploy_default`) to scrape `app`/`redis`, without this
stack ever publishing a port beyond loopback.

**First activation is manual** (below) — deliberately, since this stack adds
~1.5-2 GB RAM on top of the app's own footprint (see "Bring the stack up").
**After that, CD keeps it in sync**: the `deploy-monitoring` job
(`../.github/workflows/cd.yml`) redeploys automatically on every `monitoring/**`
change to `main`, but only runs `docker compose up` if `monitoring/.env`
already exists on the box — the stack itself stays untouched on a box that
never opted in (the checkout still syncs either way, there's just nothing to
start).

One gap this doesn't close: `docker compose up -d` only recreates a container
when its *compose service spec* changes, not when a bind-mounted config file's
*content* does — so a `monitoring/**` change that only edits
`prometheus/prometheus.yml`, `loki/loki-config.yml` or
`promtail/promtail-config.yml` reports CD success without those containers
actually picking up the new config (Prometheus needs an explicit `/-/reload`
hit; Loki/Promtail have no live-reload at all). Grafana dashboards are
unaffected — the file-provider already polls every 5 minutes regardless of
restarts. Same class of gap as `../deploy/Caddyfile` not reloading on a plain
`docker compose up -d`; a manual `docker compose restart <service>` after that
kind of change, until/unless this gets automated.

## One-time setup

### 0. Box checkout

The box checks out this repo sparsely; the required scope — `deploy/` **and**
`monitoring/` — is stated with the other prerequisites in
[`../deploy/README.md`](../deploy/README.md#prerequisites-one-time). A box
provisioned before this directory existed has only `deploy/`, so widen it once
from a shell there: `git sparse-checkout add monitoring` (or equivalent).

### 1. SSM parameters (`/aibackend/dev/`)

| Name | Type | Note |
|---|---|---|
| `GRAFANA_ADMIN_PASSWORD` | SecureString | Grafana `admin` login |
| `GLITCHTIP_SECRET_KEY` | SecureString | Django secret key — any long random string |
| `GLITCHTIP_DB_PASSWORD` | SecureString | `glitchtip-db` password |
| `GLITCHTIP_EMAIL_URL` | SecureString | optional — real SMTP URL for GlitchTip's own notification emails; absent, stays `consolemail://` (see "GlitchTip notifications") |
| `GLITCHTIP_FROM_EMAIL` | String | optional — sender for GlitchTip's own emails; only read alongside `GLITCHTIP_EMAIL_URL`; must be a verified identity when that URL points at SES |
| `GRAFANA_SMTP_HOST` | String | optional — `host:port` for the alerting contact point's transport; absent, `GF_SMTP_ENABLED` stays `false` (see "Alerting") |
| `GRAFANA_SMTP_USER` | SecureString | optional — only read if `GRAFANA_SMTP_HOST` is set |
| `GRAFANA_SMTP_PASSWORD` | SecureString | optional — only read if `GRAFANA_SMTP_HOST` is set |
| `GRAFANA_SMTP_FROM` | String | optional — only read if `GRAFANA_SMTP_HOST` is set; sender for the alert emails, must be a verified identity when the transport is SES; absent, stays the `grafana-alerts@monitoring.local` placeholder |
| `GRAFANA_ALERT_EMAIL` | String | optional — real recipient for the "probe down" alert; absent, stays the `alerts@monitoring.local` placeholder (undeliverable, harmless while SMTP is also disabled) |
| `APP_DOMAIN` | String | reused from the app stack's own param (not a new secret) — feeds Grafana's `GF_SERVER_ROOT_URL` (its own `grafana.<APP_DOMAIN>` subdomain) and GlitchTip's `GLITCHTIP_DOMAIN`/`CSRF_TRUSTED_ORIGINS` (its own `glitchtip.<APP_DOMAIN>` subdomain) — see "Grafana subdomain activation" and "GlitchTip activation" |

### 2. Render `.env` for this project

Unlike the app stack, this project isn't rendered by `deploy.sh` (decision: opt-in,
manual). From the SSM shell, in this directory:

```bash
cd /opt/aibackend/monitoring
prefix=/aibackend/dev   # your terraform ssm_parameter_prefix, if you changed it
ssm() { aws ssm get-parameter --with-decryption --name "$prefix/$1" --query Parameter.Value --output text; }
umask 077
cat > .env <<EOF
GRAFANA_ADMIN_PASSWORD=$(ssm GRAFANA_ADMIN_PASSWORD)
GLITCHTIP_SECRET_KEY=$(ssm GLITCHTIP_SECRET_KEY)
GLITCHTIP_DB_PASSWORD=$(ssm GLITCHTIP_DB_PASSWORD)
CE_BROKER_URL=redis://redis:6379/1
APP_DOMAIN=$(ssm APP_DOMAIN)
EOF
# Optional — only if GLITCHTIP_EMAIL_URL is set in SSM (see "GlitchTip notifications")
if glitchtip_email_url="$(ssm GLITCHTIP_EMAIL_URL 2>/dev/null)"; then
  echo "GLITCHTIP_EMAIL_URL=$glitchtip_email_url" >> .env
  # No 2>/dev/null: with a real SMTP URL a missing From means SES rejects
  # every mail silently — fail visibly at render time instead.
  echo "GLITCHTIP_FROM_EMAIL=$(ssm GLITCHTIP_FROM_EMAIL)" >> .env
fi
# Optional — only if GRAFANA_SMTP_HOST is set in SSM (see "Alerting")
if grafana_smtp_host="$(ssm GRAFANA_SMTP_HOST 2>/dev/null)"; then
  cat >> .env <<EOF2
GF_SMTP_ENABLED=true
GF_SMTP_HOST=$grafana_smtp_host
GF_SMTP_USER=$(ssm GRAFANA_SMTP_USER 2>/dev/null)
GF_SMTP_PASSWORD=$(ssm GRAFANA_SMTP_PASSWORD 2>/dev/null)
GRAFANA_SMTP_FROM=$(ssm GRAFANA_SMTP_FROM)
EOF2
fi
# Optional — real recipient for the "probe down" alert; absent, stays the
# undeliverable placeholder (see "Alerting")
if grafana_alert_email="$(ssm GRAFANA_ALERT_EMAIL 2>/dev/null)"; then
  echo "GRAFANA_ALERT_EMAIL=$grafana_alert_email" >> .env
fi
```

## Bring the stack up

```bash
cd /opt/aibackend/monitoring
df -h                    # Prometheus/Loki volumes grow; retention bounded ~14d
free -h                  # this stack adds ~1.5-2 GB on top of the app's own footprint — snug on a 4 GB box
docker compose up -d --wait
```

## Verify

```bash
# Prometheus targets UP, except `postgresql` until `MONITORING_ENABLED=true`
# (see "Postgres-exporter box activation" below)
docker compose exec prometheus wget -qO- http://localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"' | sort | uniq -c

# Grafana up, all 5 dashboards present
docker compose exec grafana wget -qO- http://localhost:3000/api/health
# ...and reachable through its own subdomain (Caddy route — see
# "Grafana subdomain activation" below)
curl -fsS https://grafana.<APP_DOMAIN>/api/health

# Logs flowing from both app and worker (JSON)
docker compose exec loki wget -qO- 'http://localhost:3100/loki/api/v1/label/container/values'
```

## Grafana subdomain activation

The `grafana.<APP_DOMAIN>` Caddy route (`../deploy/Caddyfile`) ships
code-complete but needs a one-time DNS record (`grafana.<APP_DOMAIN>` → the
same Elastic IP as the main site and `glitchtip.<APP_DOMAIN>`) before Caddy
can issue its own Let's Encrypt cert for it — same mechanics as GlitchTip's
subdomain, see "GlitchTip activation" below; a missing record fails the TLS
handshake for this subdomain only, the main site/app/GlitchTip stay
unaffected.

Also restart (or recreate) the `caddy` container on the box once this change
is deployed — a plain `docker compose up -d` doesn't recreate it, since only
the bind-mounted `Caddyfile`'s *content* changed, not its compose service spec
(same gap as "One gap this doesn't close" above). Until then, Grafana isn't
reachable through Caddy at all — same as before this change (SSM port-forward
only, see "GlitchTip activation" below for the same technique) — the new
subdomain route isn't live yet:

```bash
docker compose -f ../deploy/docker-compose.dev.yml restart caddy
```

Once the DNS record exists and `caddy` has picked up the new config, reachable
at `https://grafana.<APP_DOMAIN>/` — no other step needed.

## Postgres-exporter box activation

`postgres-exporter` (`../deploy/docker-compose.dev.yml`) ships code-complete
but off by default, and needs a one-time Postgres role first — the same
statement `backend/scripts/seed_local.py`'s `ensure_monitoring_role` runs
locally for `make seedlocal`:

```bash
aws ssm start-session --target <instance-id> --region us-east-1
# then, inside the session:
docker exec -i deploy-db-1 psql -U <DATABASE_USER> -d <DATABASE_NAME> <<'SQL'
CREATE ROLE monitoring WITH LOGIN PASSWORD '<matches POSTGRES_MONITORING_PASSWORD below>';
GRANT pg_monitor TO monitoring;
SQL
```

Then set two new SSM params under `/aibackend/dev/` — `POSTGRES_MONITORING_PASSWORD`
(SecureString, matching the role's password above) and `MONITORING_ENABLED=true`
— and redeploy the app stack; `deploy.sh` picks both up and starts the sidecar
(`COMPOSE_PROFILES=monitoring`). Verify: Prometheus's `postgresql` job flips
from down to `up`.

## GlitchTip activation

Reachable at `https://glitchtip.<APP_DOMAIN>/` (its own Caddy site block,
`../deploy/Caddyfile`) once that DNS record exists — GlitchTip gets a
subdomain rather than a sub-path of `APP_DOMAIN`: its `BASE_PATH` support
(`docker-compose.yml`) doesn't cover static asset URLs or every redirect, so
a sub-path route would serve a broken UI. Needs a one-time DNS record
(`glitchtip.<APP_DOMAIN>` → the same Elastic IP) before Caddy can issue its
own Let's Encrypt cert for it; a missing record fails the TLS handshake for
this subdomain only (no cert to present) — the main site and app are
unaffected either way.

`CSRF_TRUSTED_ORIGINS` (`docker-compose.yml`) is load-bearing, not a
convenience: GlitchTip never sets `SECURE_PROXY_SSL_HEADER`, so it always
thinks the request is plain HTTP even behind Caddy's HTTPS termination, and
the same-origin CSRF fast path can never match the browser's real `https://`
Origin — every login POST depends on this list. It carries both the
subdomain and the SSM-tunnel origin below (accepted trade-off: anything
reaching the box's own loopback:8090 can forge a passing Origin for the
public subdomain too — low stakes on a single dev box). `docker-compose.local.yml`
reverts both back to root for local testing (`make mon-up` serves GlitchTip
on `:8090`, no Caddy).

An SSM port-forward tunnel to the fixed port `8090` still works too (both
origins are in `CSRF_TRUSTED_ORIGINS`):

```bash
aws ssm start-session --target <instance-id> --region us-east-1 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8090"],"localPortNumber":["8090"]}'
```

Self-registration is disabled on the deployed stack
(`ENABLE_USER_REGISTRATION: "False"`, `docker-compose.yml` — the endpoint is
public); a fresh box bootstraps its first account from a shell on the box
(`docker compose exec glitchtip ./manage.py createsuperuser`), and teammates
join via invitations afterwards. `docker-compose.local.yml` re-enables
self-signup for local testing.

Open either `https://glitchtip.<APP_DOMAIN>/` or (via the tunnel)
`http://localhost:8090`, log in, create an org + project, copy the DSN it
shows (rewrite the host part to the in-network `glitchtip:8080`, e.g.
`http://<key>@glitchtip:8080/1`), and:

```bash
aws ssm put-parameter --name /aibackend/dev/SENTRY_DSN --type SecureString \
  --value '<dsn>' --overwrite --region us-east-1
```

Next app redeploy (CD or manual) picks it up. Trigger a real worker task
exception to confirm it lands in GlitchTip, and confirm the login flow works
end-to-end through real DNS+TLS.

## GlitchTip notifications

`EMAIL_URL: consolemail://` (`docker-compose.yml`) — GlitchTip's own notification
emails (new issue, etc.) are only ever printed to its container's own logs, never
delivered anywhere. **Locally**, `docker-compose.local.yml` overrides this to
`smtp://mailpit:1025`, so notifications land in the app stack's Mailpit inbox —
`http://localhost:8090` (GlitchTip UI, see "GlitchTip activation" above) triggers
the alert, `http://localhost:8025` (Mailpit) shows the email. **On the box**,
set `GLITCHTIP_EMAIL_URL` in SSM (an SMTP URL — e.g. matching the app's own
`EMAIL_BACKEND=ses` transport; + `GLITCHTIP_FROM_EMAIL` for a verified sender)
— see "1. SSM parameters" above; absent, it
stays `consolemail://` (silent). Links inside those emails (e.g. "view issue")
resolve correctly to `https://glitchtip.<APP_DOMAIN>/` via `GLITCHTIP_DOMAIN`
— see "GlitchTip activation" above.

## Alerting

One Grafana unified-alerting rule (`grafana/provisioning/alerting/rules.yml`),
"Probe down (/health)": fires when the Blackbox exporter's `probe_success` metric
is `0` for 5+ minutes, delivered via a built-in **email** contact point. Also
pages on `NoData` (`noDataState: Alerting`) — a gap in the query (Prometheus or
the exporter itself down, not just the app) looks the same as a probe failure,
which is the right call with one alert and one recipient on a single dev box.
SMTP is disabled by default (`docker-compose.yml`) — same "opt-in, absent is
safe" shape as GlitchTip's own `EMAIL_URL: consolemail://` above. **Locally**,
`docker-compose.local.yml` enables it against Mailpit, so a firing alert lands
at `http://localhost:8025`. **On the box**, set `GRAFANA_SMTP_HOST` (+ user/
password, + `GRAFANA_SMTP_FROM` for a verified sender when the transport is
SES) in SSM to turn transport on, and `GRAFANA_ALERT_EMAIL` for a real
recipient — see "1. SSM parameters"; the contact point reads it via Grafana's
own `$__env{}` provisioning interpolation (`contactpoints.yml`). Either one
left unset is harmless: no transport with a real recipient just logs the send
failure, a transport with the placeholder recipient bounces — nothing pages
until both are set.

Smoke-test (local): stop the `app` container, wait ~5 minutes, confirm the rule
shows "Alerting" at `http://localhost:3000/alerting/list` and an email arrives in
Mailpit; restart `app` and confirm it clears.

## Teardown

```bash
docker compose down          # -v to also drop prometheus/loki/grafana/glitchtip-db volumes
```

Independent of the app stack — `down` here never touches `app`/`worker`/`db`/`redis`.

## Local testing

`make mon-up` / `make mon-down` (repo root) bring this stack up against the
local `make dev`/`make up` stack instead of the box, via `docker-compose.local.yml`
— an override that rebinds the `deploy_default` network to the local project's
real network name (`ai-red-teaming_default`) and serves Grafana directly on
`localhost:3000` (no Caddy subdomain locally, no DNS). Requires the main stack
already running. This is also the one way to test GlitchTip locally (`../backend/docker-compose.yml`
doesn't have its own copy — see `../backend/README.md` "Error reporting").

`postgres-exporter` lives in `../backend/docker-compose.yml` (mirroring how it's
deployed — beside the app project it's monitoring, not this one), behind its own
`monitoring` profile:

```bash
cd ../backend
make upmonitoring   # starts postgres-exporter
make seedlocal      # creates/resets the read-only `monitoring` Postgres role it connects as
```

Everything (Prometheus, Loki, Promtail, Grafana, celery-exporter, node-exporter,
cadvisor, GlitchTip, postgres-exporter) then runs for real against the local app.

```bash
make mon-up      # from the monorepo root
# Grafana: http://localhost:3000 (admin / GRAFANA_ADMIN_PASSWORD, default "admin")
# Prometheus: http://localhost:9090
# GlitchTip: http://localhost:8090
# GlitchTip + alert notification emails: http://localhost:8025 (Mailpit inbox — see "GlitchTip notifications"/"Alerting" below)
make mon-down
```

## Files

| Path | Role |
|---|---|
| `docker-compose.yml` | This project — all 9 services |
| `docker-compose.local.yml` + `Makefile` + `.env.example` | Local-testing overlay only (`make mon-up`/`make mon-down`) — not used on the box |
| `prometheus/prometheus.yml` + `prometheus/targets/<component>/*.json` | Scrape config; drop a target JSON, no restart needed (30s file_sd reload) |
| `blackbox/blackbox.yml` | Blackbox exporter's `http_2xx` probe module |
| `loki/loki-config.yml` | Single-binary, filesystem storage, 14d retention |
| `promtail/promtail-config.yml` | Docker service discovery — ships every container's stdout/stderr except this stack's own |
| `grafana/provisioning/` | Datasources (Prometheus + Loki, fixed `uid`s), the dashboard file-provider, and alerting (contact point + rule, see "Alerting") |
| `grafana/dashboards/*.json` | The 5 provisioned dashboards: FastAPI RED + AI gateway + process + uptime, PostgreSQL, Celery, Host + containers, Logs |
