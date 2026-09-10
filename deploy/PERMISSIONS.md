# Permissions — building and deploying

Who (and what) needs which permission to build an image, ship it to the box, and
operate the environment. Written against `dev`; the shape is identical for any
environment stood up from [`terraform/`](terraform/README.md) — only names and
ids differ.

Two rules run through all of it:

- **No AWS API key pairs.** GitHub Actions reaches AWS through OIDC (short-lived
  credentials); the application uses the instance profile. Nothing in the repo or
  in Actions secrets holds an AWS credential. The one static exception is the SES
  **SMTP** pair used by the monitoring stack — an access key id and its SigV4
  derivative, which by protocol have to sit in `monitoring/.env` on the box.
- **No SSH.** Shell access and deploys go through AWS SSM, so port 22 stays
  closed and nobody needs a key pair.

## At a glance

| Actor | Identity it uses | Permission it needs | What that lets it do |
|---|---|---|---|
| Developer merging a PR | GitHub account | `write` on the repo (+ a green `CI passed`) | **Merging to `main` is a deploy** — CI runs on the merge commit and its completion triggers `cd.yml` |
| `publish` / `publish-frontend` job | `secrets.GITHUB_TOKEN` | job-level `packages: write` | push `…-backend` / `…-frontend` images to GHCR |
| `deploy` / `deploy-monitoring` job | GitHub OIDC → AWS role `<name_prefix>-gha-deploy` | job-level `id-token: write` | `ssm:SendCommand` on **one** instance + read the invocation result |
| The box | Instance profile `<name_prefix>-ec2` | see [The box](#the-box-instance-profile) | run the deploy, read secrets, send mail, write exports |
| The box → GHCR | PAT in SSM (`GHCR_USER` / `GHCR_PAT`) | `read:packages`, and its owner must have access to the package | pull the private images |
| Monitoring mail | IAM user `<name_prefix>-smtp` | `ses:SendRawEmail` on the environment's domain identity | Grafana / GlitchTip alert email |
| Operator (a person) | Own AWS principal | see [Operator](#operator-a-person) | `terraform apply`, SSM shell, manual deploy |

## GitHub side

**Merge rights are deploy rights.** `cd.yml` is chained off CI — it fires when the
`CI` workflow completes on `main` (`workflow_run`) — and has no
`workflow_dispatch`, so anyone who can merge a PR can ship to the box. Treat repo
`write` accordingly. Re-running a failed CD run also needs repo `write` (Actions
write).

**Two layers keep a red commit from deploying.** The branch ruleset on `main`
requires the single check `CI passed` (an aggregator job), so a red PR can't merge
at all — that's the gate. On top of it, the **`changes`** job is guarded by
`github.event.workflow_run.conclusion == 'success'`; every other job keys off that
job's outputs, so a red CI skips `changes` and the rest of the chain finds nothing
to do. The run works off `workflow_run.head_sha`, the exact commit CI validated.
That chain is the safety net for anything reaching `main` without passing the
ruleset — a job added without a `needs: changes` dependency does not inherit it.

**Token permissions are per job, not workflow-wide.** `cd.yml` sets
`permissions: contents: read` at the top and widens it only where needed —
`packages: write` in the two publish jobs, `id-token: write` in the two deploy
jobs. Nothing else in the workflow can push a package or mint an OIDC token.

**GHCR packages.** Both packages (backend, frontend) are **Private** and **linked
to the repo**, so pull access follows repo roles. The box is not a repo
collaborator, so it authenticates with the `GHCR_USER` / `GHCR_PAT` pair from SSM
instead. The one-time package setup is in
[`README.md`](README.md#ghcr-packages-one-time-in-the-github-ui).

## AWS side — the CD deploy role

Role `<name_prefix>-gha-deploy`, assumed via `AssumeRoleWithWebIdentity`
against the GitHub OIDC provider. Declared in
[`terraform/envs/dev/main.tf`](terraform/envs/dev/main.tf).

**Trust** — both conditions must hold (`aud` with `StringEquals`, `sub` with
`StringLike`; the value carries no wildcard, so it matches exactly one ref):

- `token.actions.githubusercontent.com:aud` = `sts.amazonaws.com`
- `token.actions.githubusercontent.com:sub` =
  `repo:humane-intelligence/hi-oss-ai-red-teaming:ref:refs/heads/main`

So only this repository can assume the role, even though the workflow file is public.
Note what the `sub` condition does **not** buy: a `workflow_run` job always executes in
the default-branch context, so its token carries `ref:refs/heads/main` whatever produced
the triggering run — including a fork's pull request. Fork code is kept out by the guard
on CD's `changes` job and by `actions/checkout`'s own refusal to check out fork PR code
under `workflow_run`, not by this condition. See the blast-radius note in
[`README.md`](README.md).

**Permissions** — deliberately two statements and nothing more:

- `ssm:SendCommand`, scoped to the `AWS-RunShellScript` document **and** the one
  instance ARN
- `ssm:GetCommandInvocation` / `ssm:ListCommandInvocations` (reading the result
  is not resource-scopeable)

The role cannot read SSM parameters and cannot send a command to any other
instance. One caveat follows from the unscopeable statement: it can read the
output of *any* command invocation in the account, including ones it didn't send.
Otherwise the blast radius of a leaked OIDC exchange is "run a shell command on
the dev box" — which is what a deploy is anyway.

## The box (instance profile)

Instance profile `<name_prefix>-ec2`. The application itself holds no AWS
credentials; everything goes through the role.

| Policy | Grants |
|---|---|
| `AmazonSSMManagedInstanceCore` (AWS-managed) | SSM agent registration, Session Manager, Send-Command execution |
| `ParamStoreRead` (from the `docker-host` module) | `ssm:GetParameter` / `ssm:GetParametersByPath` under `/aibackend/dev/*`, plus `kms:Decrypt` restricted by `kms:ViaService` to SSM — so the role can decrypt SecureStrings, but only through Parameter Store |
| `<name_prefix>-ses-send` | `ses:SendEmail` / `ses:SendRawEmail` on the domain identity only (`mail_domain` — an account typically holds other verified identities too) — application email |
| `<name_prefix>-export-s3` | object access (`PutObject`, `GetObject`, `DeleteObject`, `AbortMultipartUpload`) on the exports bucket only |

Parameter *values* are managed out-of-band, never by Terraform — see the
[secrets boundary](terraform/README.md#secrets-boundary). The list of parameters
lives in [`README.md`](README.md#prerequisites-one-time).

## Operator (a person)

What a human needs to run `terraform plan/apply`, open a shell, or deploy by
hand. Ask the account admin for this shape; the exact policy document is
environment-specific.

| Task | Needs |
|---|---|
| Shell on the box, manual `deploy.sh` | `ssm:StartSession` on the instance (Session Manager) |
| Manage secrets | read/write on the environment's parameter prefix + `kms:Encrypt`/`Decrypt` |
| Terraform state | read/write on the state bucket (native S3 locking, no DynamoDB) |
| `terraform apply` | create/update/delete on EC2, EIP, security groups, S3, SES/SESv2 — plus IAM for roles, policies, instance profiles, OIDC providers, **users and user policies** (the env creates an IAM user for SMTP), and `iam:PassRole`. Add `iam:CreateAccessKey` if the operator is also the one minting the SMTP key, which Terraform deliberately does not manage |

**A PowerUser-class grant alone is not enough.** This stack is IAM-heavy (an
instance profile, an OIDC provider, a federated deploy role, an IAM user, four
role policies and a user policy), and PowerUser doesn't cover IAM writes — you'd
stall on the first role it has to create. On the
other hand `AdministratorAccess` is usually not on the table on a client-owned
account. The workable middle is PowerUser-class for the regional services plus a
narrow IAM addition (roles / policies / instance profiles / OIDC providers / users
/ `PassRole`), fenced by a **permissions boundary** so created roles can't exceed
the grantor's own ceiling, and a region condition. That combination is auditable
enough for most account admins to approve without negotiation.

For a stricter setup, put Terraform behind its own role and give the operator
only `sts:AssumeRole` into it.

## Deliberately absent

- **Static AWS credentials in GitHub** — replaced by OIDC. Nothing to rotate, nothing to leak.
- **SSH keys / open port 22** — Session Manager instead.
- **Secrets in the repo or in the image** — everything sensitive is read from Parameter Store at deploy time into a `0600` `.env`.
- **SecureString values in Terraform state** — Terraform manages IAM *access* to parameters, never the parameters themselves.
- **`set -x` in `deploy.sh`** — tracing would echo secrets into the SSM command log.

## Rotation and what it costs

| Credential | Where it lives | Rotating it means |
|---|---|---|
| `GHCR_PAT` | SSM | update the parameter; the next deploy logs in with it |
| SMTP access key of `<name_prefix>-smtp` | SSM | the SMTP password is a SigV4 derivative of the key — re-derive it, update `GRAFANA_SMTP_USER` / `GRAFANA_SMTP_PASSWORD` / `GLITCHTIP_EMAIL_URL`, then **re-render `monitoring/.env` by hand** and `docker compose up -d`. That project is deliberately not rendered from SSM by any automation ([`../monitoring/README.md`](../monitoring/README.md)), so the containers keep the old credentials until someone does |
| `CONVERSATION_SECRETS_KEY` | SSM or `deploy/conversation_secrets_key` on the box | **destructive to lose.** Seals conversation transcripts; generated by `deploy.sh` on the first rollout when SSM has no parameter, so it may exist **only on the box** — it is not in any backup that skips that file. Losing it makes every sealed transcript permanently unreadable. Rotate through `CONVERSATION_SECRETS_KEY_RETIRED`: promote the new value, move the outgoing one to the retired slot, restart every replica, then run the sweep in a one-off container — `docker compose -f docker-compose.dev.yml run --rm --no-deps app python -m scripts.rewrap_transcripts` from `/opt/aibackend/deploy`, since the box carries no dev toolchain and `make rewraptranscripts` is the dev-machine form — and drop the retired key once that run exits 0. A non-zero `pending` that the run reports as `unreadable` is rows sealed under a key nobody holds any more — dropping the retired key costs them nothing, they are already unopenable. Unlike the model key, this one **does** have an automated re-wrap. Volume is a rotation trigger too, not only the calendar: each sealed message draws a fresh random 96-bit nonce, so the birthday bound sits near 2^32 messages under one key |
| `MODEL_SECRETS_KEY` | SSM | non-destructive **only with the retired slot**: promote the new value to `MODEL_SECRETS_KEY`, move the outgoing one to `MODEL_SECRETS_KEY_RETIRED`. Rows carry a key id, so both keys decrypt. **There is no automated re-wrap** — nothing walks the table and re-encrypts — so the retired key stays configured until every stored model key has been re-saved by hand (saving re-encrypts it under the current key). Dropping it while rows still carry the retired id makes those API keys permanently unreadable; swapping the value *without* the retired slot loses them immediately |
| `SESSION_JWT_SECRET` | SSM | everyone is logged out (session tokens stop verifying) — and outstanding signed media URLs break with it, since URL signing falls back to this secret unless `MEDIA_URL_SIGNING_SECRET` is set, which it isn't here |
| `OAUTH_STATE_SECRET` | SSM | OAuth flows already in flight fail; new ones are fine |
| `DATABASE_PASSWORD` | SSM | Postgres reads it only when the data volume is initialised, so change it in the database too — otherwise the app can't connect |
