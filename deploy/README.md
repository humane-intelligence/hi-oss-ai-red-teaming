# Deploy — dev EC2 environment

[![Terraform](https://img.shields.io/badge/Terraform-IaC-844FBA?logo=terraform&logoColor=white)](terraform/README.md)
[![AWS](https://img.shields.io/badge/AWS-EC2_+_SSM-FF9900?logo=amazonwebservices&logoColor=white)](PERMISSIONS.md)
[![Caddy](https://img.shields.io/badge/Caddy-TLS_proxy-1F88C0?logo=caddy&logoColor=white)](Caddyfile)
[![Compose](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](docker-compose.dev.yml)

Single EC2 instance running the whole stack (FastAPI + Celery + Postgres + Redis)
behind Caddy, for client demos and FE integration testing. **Not production.**

> **Scope: one environment — `dev` — only.** There is no staging and no prod. This
> is a single, deliberately simple hand-rolled box. Production does not exist yet,
> and when it does it will almost certainly be built **differently** — managed
> Postgres, container orchestration / multiple instances, real secrets +
> observability tooling — not by scaling this EC2 setup. Treat everything here as
> dev-only; don't lift it into a prod pattern as-is.

## Documentation map

| Read this for | Where |
|---|---|
| How the dev environment works, and how to operate it | this file |
| Who (or what) needs which permission to build, deploy, or run Terraform | [`PERMISSIONS.md`](PERMISSIONS.md) |
| Infrastructure as code — module contract, state, adopting or creating an environment | [`terraform/README.md`](terraform/README.md) |
| Observability stack (Prometheus, Loki, Grafana, GlitchTip) | [`../monitoring/README.md`](../monitoring/README.md) |

## Status

**This describes a deployment you stand up yourself.** No environment ships
configured: CD publishes images on every push to `main`, but its deploy jobs skip
unless `DEPLOY_ROLE_ARN` and `INSTANCE_ID` are set as repository variables, so nothing
is rolled anywhere until you point them at a box of your own. Manual deploy is also
available, for an unmerged branch or for debugging (see "Manual deploy"). The AWS
resources are codified in [`terraform/`](terraform/README.md) as a parameterised
environment; SSM parameter *values* and client-side DNS stay out-of-band by design.

## How it fits together

Two GitHub Actions workflows, split by trigger so PRs stay quiet:

| Workflow | Trigger | Jobs | Purpose |
|---|---|---|---|
| `.github/workflows/ci.yml` (`CI`) | every PR + push to `main`/`dev` | per-subsystem `lint`/`test`/`build` (backend + frontend) + `Docs lint` + `Stack config` + `Terraform check` + `DCO sign-off`, aggregated by `CI passed` | quality gate |
| `.github/workflows/cd.yml` (`CD`) | **`workflow_run`: `CI` completed on `main`** | `changes` → `publish` / `publish-frontend` → `deploy` → `deploy-monitoring` | ship both images to the box |

`cd.yml` never runs on a PR, so PRs show only the CI jobs (no skipped publish/deploy
clutter). CD is **chained off CI**: it fires when the `CI` workflow completes on
`main`, and its first job (`changes`) is guarded by
`github.event.workflow_run.conclusion == 'success'` — every later job keys off that
job's outputs, so a red CI leaves nothing to publish or deploy. It works off
`workflow_run.head_sha` — the exact commit CI validated, not the branch HEAD,
which may already have moved on.

That chaining is a safety net, not the gate. The gate is a **branch ruleset** on
`main` whose single required check is **`CI passed`** (an aggregator job that fails
if any leaf job failed), so a PR can't merge red in the first place.

## The deploy artifacts (this directory)

| File | Role |
|---|---|
| `docker-compose.dev.yml` | The stack: `app`, `worker`, `beat`, `db`, `redis`, `caddy`, `frontend`, and the self-hosted `slm` (llama.cpp small model, for warmup testing). Pulls pre-built images from GHCR (backend + frontend); `db`/`redis`/`frontend`/`slm` have no host ports. |
| `Caddyfile` | Reverse proxy + automatic TLS (Let's Encrypt). **Same-origin routing:** backend path prefixes (`/api/*`, `/health`, `/ready`, `/docs`, `/redoc`, `/openapi.json`) → `app:8000`; everything else → `frontend` (the built SPA, which does its own `/index.html` fallback). |
| `deploy.sh` | The deploy itself: render `.env` from SSM, pull, migrate, sync roles + licenses, roll. Run on the box. |
| `ec2-bootstrap.sh` | EC2 user-data: installs Docker + compose + AWS CLI, run once at instance launch. |
| `terraform/` | IaC for the environment's AWS resources (instance, EIP, SG, IAM, OIDC deploy role, exports bucket, SES identity, SMTP user) — see [`terraform/README.md`](terraform/README.md) for the module contract, state backend, and secrets boundary. |

The CD workflow (`cd.yml`) lives under `.github/workflows/`, not here — it only
*invokes* `deploy.sh` on the box.

## Why it looks like this

- **Images from GHCR, not built on the box** — the box has no source or toolchain; CD (or a manual `docker push`) produces the images, the box only pulls them.
- **Caddy terminates TLS** — OAuth needs HTTPS with a trusted cert (`OAUTH_COOKIE_SECURE=true`), and a cert can't be issued for a bare IP. Caddy gets a Let's Encrypt cert for `APP_DOMAIN` automatically.
- **Secrets live in SSM Parameter Store**, never in the repo or in the image. The instance reads them via its IAM role; `deploy.sh` renders them into a local `.env` (mode 0600).
- **No SSH** — shell and deploys go through AWS SSM (Session Manager / Send-Command), so port 22 stays closed. CD also reaches the box this way (see below), so GitHub never connects to it directly.
- **`db`/`redis`/`frontend` are internal-only** — no published host ports; only the compose network (Caddy → app + frontend, app → db/redis) can reach them.

## Automated deploy (CD) — what happens on merge to `main`

1. CI runs on the pushed commit; its completion triggers `cd.yml` (`workflow_run`).
   A **`changes`** job diffs the merge commit; **`publish`**
   (when `backend/` changed) and **`publish-frontend`** (when `frontend/` changed) build and
   push `ghcr.io/humane-intelligence/hi-oss-ai-red-teaming-backend` and `…-frontend` — `:main` and
   `:<sha>`. A subsystem that didn't change isn't rebuilt; its `:main` image stays put.
2. **`deploy`** (runs when anything relevant changed, unless a publish failed) requests a short-lived OIDC token from GitHub and
   does `AssumeRoleWithWebIdentity` into the AWS role `<name_prefix>-gha-deploy` —
   no static AWS keys are stored in GitHub.
3. In that role it calls `aws ssm send-command` against the instance, document
   `AWS-RunShellScript`. The command first syncs the box's checkout to the deployed
   commit (`git fetch origin main && git checkout <sha>`) so the deploy scripts move
   in lockstep with the images, then runs `bash /opt/aibackend/deploy/deploy.sh <backend>:<sha> <frontend>:<sha>`
   — each ref passed only if that subsystem was rebuilt this run, else empty (deploy.sh keeps `:main`).
   (The box doesn't auto-pull, so CD pins `deploy/` to the same commit as the images.)
4. The command goes to the **SSM service** (not directly to the box). The SSM agent
   on the instance pulls it and runs it **as root**.
5. `deploy.sh` rolls the stack (steps below), pinned to that exact `<sha>`.
6. The job waits for the command to finish, prints the invocation output, and goes
   red if the command's exit status isn't `Success`.

The role's trust policy is scoped to this repository and its permissions to Send-Command
on this one instance plus reading the result, so it can't touch anything else in the
account. It does **not** pin the commit — see the blast-radius note under Prerequisites
for what does. The exact trust conditions and both policy statements are in
[`PERMISSIONS.md`](PERMISSIONS.md).

## What `deploy.sh` does (the actual roll, automated or manual)

Run on the box, in order:

1. Reads the AWS region from IMDSv2 (so the script isn't pinned to one region).
2. `docker login ghcr.io` using `GHCR_USER` + `GHCR_PAT` from SSM (private image).
3. Renders `.env` (0600): fixed infra values inline, secrets and env-specific
   config pulled from SSM Parameter Store under `/aibackend/dev/`. One value is not a
   plain read: `CONVERSATION_SECRETS_KEY` takes the SSM parameter when it exists, and
   otherwise reuses — or, on a first rollout, generates and persists — the file
   `deploy/conversation_secrets_key` (0600), so a missing parameter never blocks a roll.
   Only `ParameterNotFound` takes that branch; any other SSM failure aborts the deploy,
   because minting a key over a throttled or denied lookup would seal new rows under a key
   the real parameter cannot open. **That file lives on that one box and is in no backup**
   — create the parameter before the first rollout, and note the instance role can only
   *read* SSM, so a key generated on the box cannot be promoted from there.
4. `docker compose ... pull` — fetch the new images (backend + frontend, and any base image updates).
5. `docker compose ... up -d --wait db redis` — bring data services up healthy
   first (a one-off `run` won't start dependencies, so the DB must already be up).
6. `alembic upgrade head` in a one-off container — migrate **before** the new app
   serves traffic. If it fails, `set -e` aborts and the old stack keeps running.
7. `python -m scripts.sync_roles` in a one-off container — provision the canonical
   system roles (idempotent; the `make syncroles` equivalent, run after migrate).
8. `python -m scripts.sync_licenses` in a one-off container — seed the curated data
   licenses (reference data, like the roles); the group/eval projections resolve the
   platform default from that table, so skipping this 500s those reads — and creating a
   private (`invitation_only`) group, on the `No license` row it derives.
9. `python -m scripts.sync_annotation_labels` in a one-off container — seed the curated
   annotation-label vocabulary (reference data, like the roles and licences). Unlike the
   licences, nothing 500s without it: an empty catalog only leaves the label picker with
   nothing to suggest. Three things do fail the deploy on a healthy schema, all needing a
   decision rather than a retry: a label retired by hand whose key was re-added as a second
   live row (the sync revives the tombstone into a duplicate the unique index rejects); a
   curated wording that a label an annotator typed already holds; and a curated row under a key
   the catalog no longer names still holding a wording the catalog ships — what re-keying an
   entry *without* also rewording it leaves behind. The sync refuses the last two outright,
   since writing them would leave one wording as two ids. Retire or rename that row by
   hand (SQL — the route is read-only), or change the catalog wording, then re-run.
10. `docker compose ... up -d --wait` — roll the containers to the new image and
    block until `app`/`worker` are healthy; a container that starts but never gets
    healthy fails the deploy instead of reporting success on a broken roll.
11. `docker image prune -af` — drop **all** unused images, not just dangling ones;
    a later rollback re-pulls the old image from GHCR instead of finding it locally.

> `deploy.sh` calls `alembic` / the `scripts.sync_*` steps through one-off compose
> containers, **not** via `make` — deliberate: the box has no dev toolchain and the
> `make` targets point at the *local* compose file. Don't "fix" it into `make`.

Two optional positional args pin specific images — `$1` backend, `$2` frontend; an
empty/absent arg keeps that service on the `:main` tag from `docker-compose.dev.yml`,
so a one-sided deploy leaves the other service untouched:

```bash
bash deploy.sh                                                       # both :main
bash deploy.sh ghcr.io/…/hi-oss-ai-red-teaming-backend:<sha>                # pin backend, FE stays :main
bash deploy.sh "" ghcr.io/…/hi-oss-ai-red-teaming-frontend:<sha>            # pin frontend, backend stays :main
bash deploy.sh ghcr.io/…-backend:<sha> ghcr.io/…-frontend:<sha>      # pin both
```

## Prerequisites (one-time)

Provisioned in AWS once. The *AWS resources* below — instance, instance profile, OIDC
provider, deploy role — are codified in [`terraform/envs/dev`](terraform/envs/dev); the
on-box setup (the `/opt/aibackend` checkout, `ec2-bootstrap.sh` as user-data), DNS, and
the SSM parameter **values** are deliberately outside Terraform. To stand up a
*different* environment rather than read how this one is wired, follow
[the runbook](terraform/README.md#standing-up-a-new-environment-bring-your-own-aws)
instead:

- **EC2 instance** with its **instance profile** — SSM core, Parameter Store read on
  the prefix, SES send, exports-bucket access ([`PERMISSIONS.md`](PERMISSIONS.md)).
- **GitHub OIDC provider** (`token.actions.githubusercontent.com`) + deploy role
  `<name_prefix>-gha-deploy` (web-identity). Its ARN reaches `cd.yml` through the
  `DEPLOY_ROLE_ARN` repository variable, along with the instance id and region.
  **Blast radius:** Send-Command
  means `AWS-RunShellScript`, so whatever can make CD run gets a root shell on the box.
  What bounds that is the branch ruleset on `main` plus `cd.yml`'s `branches: [main]`
  trigger filter — *not* the `sub` condition on the trust policy: a `workflow_run` job
  always executes in the default-branch context, so its OIDC token carries
  `ref:refs/heads/main` whichever commit produced the triggering CI run. The trust
  condition pins the **repository**, not the commit. It therefore does *not*
  distinguish a CI run triggered by a fork's pull request from one triggered by a push
  to `main`, which matters now that anyone can fork: what keeps fork code out is that
  `changes` refuses to run unless the triggering CI run came from this repository, and
  `actions/checkout` independently refuses to check out fork PR code under
  `workflow_run`. Do not remove either guard, and do not set
  `allow-unsafe-pr-checkout`. The exact trust conditions and policy statements are in
  [`PERMISSIONS.md`](PERMISSIONS.md).
- This repo checked out at **`/opt/aibackend`** on the box (sparse checkout of
  `deploy/` **and `monitoring/`** over a read-only deploy key is enough — CD's two
  jobs run `deploy/deploy.sh` and `monitoring/`'s compose file from there).
- `ec2-bootstrap.sh` already run as user-data (Docker + AWS CLI installed).
- **DNS already points `APP_DOMAIN` at the instance's Elastic IP** — an A-record, or
  an `api-dev-<ip>.sslip.io` fallback. Caddy provisions the TLS cert via Let's Encrypt
  HTTP-01 on the first `up`, so without working DNS and a reachable port 80, cert
  issuance fails and the site never comes up on HTTPS.
- SSM parameters under `/aibackend/dev/`:

  | Name | Type | Value |
  |---|---|---|
  | `GHCR_PAT` | SecureString | GitHub PAT with `read:packages` |
  | `GHCR_USER` | String | owner of that PAT |
  | `DATABASE_PASSWORD` | SecureString | Postgres password |
  | `SESSION_JWT_SECRET` | SecureString | ≥32 chars |
  | `OAUTH_STATE_SECRET` | SecureString | ≥32 chars |
  | `MODEL_SECRETS_KEY` | SecureString | ≥32 chars — encrypts AI-model API keys at rest (JWE). Rotate through `MODEL_SECRETS_KEY_RETIRED`, not by swapping this value alone — see [`PERMISSIONS.md`](PERMISSIONS.md). |
  | `MODEL_SECRETS_KEY_RETIRED` | SecureString | optional — the outgoing key during a rotation; rows written under it keep decrypting for as long as it stays set. No automated re-wrap exists, so it stays until every stored model key has been re-saved by hand — dropping it earlier is the destructive move ([`PERMISSIONS.md`](PERMISSIONS.md)). |
  | `CONVERSATION_SECRETS_KEY` | SecureString | ≥32 chars — seals conversation transcripts under a `protects_conversation_data` licence, and the app refuses to boot without it. Never the same value as `MODEL_SECRETS_KEY`: separate keys are the point, and the app refuses that too at boot. **Create it before the first rollout** — that is the durable form. Absent is survivable rather than intended: `deploy.sh` then generates one and keeps it in `deploy/conversation_secrets_key` (0600), where it exists on that box alone and in no backup. Set the parameter to manage it centrally instead, using the value from that file — SSM wins when present, so a *different* value there leaves every already-sealed transcript unreadable unless the old one moves to `CONVERSATION_SECRETS_KEY_RETIRED` first. |
  | `CONVERSATION_SECRETS_KEY_RETIRED` | SecureString | optional — the outgoing transcript key during a rotation. Unlike the model one, transcripts **do** have a sweep — `docker compose -f docker-compose.dev.yml run --rm --no-deps app python -m scripts.rewrap_transcripts` from `/opt/aibackend/deploy` (no `make` on the box): drop this only once that run exits 0. |
  | `APP_DOMAIN` | String | `api.example.org` (an `<anything>-<ip>.sslip.io` name works for a quick start) |
  | `CORS_ORIGINS` | String | JSON list; **`[]`** with the FE served same-origin (the browser makes no cross-origin calls, so CORS is unused) |
  | `FRONTEND_BASE_URL` | String | FE base URL, no trailing slash — `https://api.example.org` (same-origin) |
  | `EMAIL_BACKEND` | String | `console`, `ses`, or `smtp` — the box runs `ses` |
  | `EMAIL_FROM` | String | sender address; must be a verified SES identity when the backend is `ses` |
  | `EXPORT_STORAGE_BACKEND` | String | `local` or `s3`. **`s3` on the box:** `app` and `worker` are separate containers with no shared volume here, so a `local` export written by the worker is unreachable for the download. |
  | `EXPORT_S3_BUCKET` | String | the exports bucket (`<name_prefix>-exports-<account_id>`); required when the backend is `s3`, validated at boot |
  | `SLM_API_KEY` | SecureString | optional — credential for the self-hosted `slm` service; absent → compose/app fall back to `local`. Must match the `local-slm` AiModel row's `api_key`. |
  | `SENTRY_DSN` | SecureString | optional — error-reporting DSN (Sentry SDK protocol) pointing at the box's self-hosted GlitchTip; absent → `init_sentry` stays a no-op. The box runs GlitchTip (see [`../monitoring/README.md`](../monitoring/README.md)); set this once a project exists there. |
  | `OIDC_GOOGLE_CLIENT_ID` / `OIDC_GOOGLE_CLIENT_SECRET` | String / SecureString | optional — the Google OAuth client behind "Sign in with Google"; rendered as a pair, since the app refuses to boot with only one of them set. Absent → Google stays out of `GET /api/v1/auth/oidc/providers` and the console shows no Google button. |
  | `MONITORING_ENABLED` | String | optional — `"true"` flips `COMPOSE_PROFILES=monitoring`, starting `postgres-exporter`; absent → the sidecar stays off. See `../monitoring/README.md` "Postgres-exporter box activation". |
  | `POSTGRES_MONITORING_PASSWORD` | SecureString | optional — password for the `monitoring` Postgres role `postgres-exporter` connects as; only read when `MONITORING_ENABLED=true`. |

### GHCR packages (one-time, in the GitHub UI)

The repo publishes **two** packages: `…/hi-oss-ai-red-teaming-backend` and
`…/hi-oss-ai-red-teaming-frontend` (one repo, two artifacts). After each is first pushed
(CD's `publish` / `publish-frontend` job, or a manual push), open its **Package
settings** and:

- **Connect repository** — link the package to the repo so its access follows the
  repo's roles (collaborators with repo access can pull) and it shows in the repo's
  Packages tab. The images carry an `org.opencontainers.image.source` label pointing
  at the repo, so GHCR **auto-links** both — just verify.
- **Visibility = Private** (Danger Zone → Change visibility) — the images contain our
  source, so keep them Private. Pulling then requires a token with `read:packages`
  whose owner has access to the package (granted via the repo link above) — that's
  the `GHCR_USER` / `GHCR_PAT` pair the deploy uses (the same pair covers both packages).

## Manual deploy (unmerged branch, or debugging)

CD covers merges to `main`. To deploy a branch that isn't merged, or to deploy by
hand while debugging:

1. **Build + push the images** (from a machine with the source, at the monorepo root).
   Use a tag that won't clobber `:main` if it's a branch. Build only the subsystem(s)
   you're deploying:

   ```bash
   tag=<branch-or-sha>
   echo "$GHCR_PAT" | docker login ghcr.io -u <gh-username> --password-stdin
   # backend — --no-cache-filter keeps the `apt-get upgrade` layer off the cache, so the
   # pushed image carries today's security patches rather than the build cache's
   docker build --no-cache-filter runtime -t ghcr.io/humane-intelligence/hi-oss-ai-red-teaming-backend:$tag backend/
   docker push  ghcr.io/humane-intelligence/hi-oss-ai-red-teaming-backend:$tag
   # frontend (the prod target = static build served by Caddy; --no-cache-filter for its `apk upgrade`)
   docker build --target prod --no-cache-filter prod -t ghcr.io/humane-intelligence/hi-oss-ai-red-teaming-frontend:$tag frontend/
   docker push  ghcr.io/humane-intelligence/hi-oss-ai-red-teaming-frontend:$tag
   ```

2. **Open a shell on the box** via SSM (no SSH):

   ```bash
   aws ssm start-session --target <instance-id> --region us-east-1
   sudo -i
   ```

3. **Run the deploy**, pinning the image(s) you pushed (`$1` backend, `$2` frontend;
   pass `""` to leave one on `:main`):

   ```bash
   cd /opt/aibackend && bash deploy/deploy.sh \
     ghcr.io/humane-intelligence/hi-oss-ai-red-teaming-backend:<tag> \
     ghcr.io/humane-intelligence/hi-oss-ai-red-teaming-frontend:<tag>
   ```

   (For a branch, check out that branch's `deploy/` first:
   `git -C /opt/aibackend checkout <branch>`.)

4. **Verify** — backend API and the served console (same origin):

   ```bash
   curl -fsS https://<APP_DOMAIN>/health             # {"status":"ok"}
   curl -fsS https://<APP_DOMAIN>/ready
   curl -fsS https://<APP_DOMAIN>/openapi.json | head -c 80   # API reachable through Caddy
   curl -fsS https://<APP_DOMAIN>/ | grep -o '<title>[^<]*'   # SPA index served
   curl -fsS https://<APP_DOMAIN>/login | grep -o '<div id="root"' # deep route → SPA fallback
   ```

   Then open `https://<APP_DOMAIN>/` in a browser and confirm login works (the console
   calls `/api/...` same-origin).

## First admin (manual, for now)

There is no automatic admin seed yet (open decision). **Create the first admin by
hand from a shell on the box** after the first deploy — open a Python shell in the
app container:

```bash
cd /opt/aibackend
docker compose -f deploy/docker-compose.dev.yml exec app python
```

The canonical roles already exist — the deploy runs `scripts.sync_roles`. So you
only need the user: create an `ACTIVE` admin via `app.core.auth.services`
(`hash_password` from `passwords`, `create_user` from `users`, carrying the `admin`
role), or drive it through the registration / invitation API. `make seedlocal` does
**not** work here — it refuses any environment other than `local`.

## Operations & debugging (on the box, via the SSM shell)

All from `cd /opt/aibackend` (commands use the `deploy/` compose file):

```bash
docker compose -f deploy/docker-compose.dev.yml ps              # service status + health
docker compose -f deploy/docker-compose.dev.yml logs -f app     # tail (app / worker / caddy / db / frontend)
docker compose -f deploy/docker-compose.dev.yml restart app     # restart one service
docker compose -f deploy/docker-compose.dev.yml exec app bash   # shell inside a container
```

Debugging a failed deploy:

- **CD run went red** — open the failed `deploy` job in Actions; it prints the full
  SSM `get-command-invocation` output (stdout + stderr of `deploy.sh` on the box).
  That output is the box's deploy log — read it there first.
- **Reproduce on the box** — `sudo bash deploy/deploy.sh <image>` re-runs the exact
  same steps interactively, with full output. `set -x` is intentionally **off**
  (secrets would leak into the SSM command log) — comment it back in locally only.
- **TLS / cert didn't provision** — `... logs caddy | grep -i certificate`. DNS must
  resolve `APP_DOMAIN` to the instance and port 80 must be reachable for the
  Let's Encrypt HTTP-01 challenge.
- **Migration failed** — the roll aborts before `up -d`, so the old `app` keeps
  serving. Fix forward and redeploy.
- **Rollback** — redeploy pinned to a previous image:
  `sudo bash deploy/deploy.sh ghcr.io/humane-intelligence/hi-oss-ai-red-teaming-backend:<old-sha>`
- **Data** lives in the `db_data` volume (Postgres) and `caddy_data` (TLS certs);
  both survive redeploys. Back up by snapshotting the instance's EBS volume.
