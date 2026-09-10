#!/usr/bin/env bash
# Deploy the dev stack: render .env from SSM, pull the images, migrate, roll.
# Run on the EC2 box (SSM Session Manager or Send-Command). Optional args pin
# image refs: $1 backend, $2 frontend (e.g. ...:<sha>); each empty/absent keeps
# the :main tag from the compose file, so a one-sided deploy leaves the other.
# Never enable `set -x` — secrets would leak into the SSM command log.
set -euo pipefail
cd "$(dirname "$0")"

# Parameter Store path holding this environment's config. Must match the terraform
# `ssm_parameter_prefix` variable, whose default is this value: change one and the
# instance role is scoped to a prefix the script does not read. CD passes SSM_PREFIX
# from a repository variable when it is set.
PREFIX="${SSM_PREFIX:-/aibackend/dev}"
COMPOSE="docker compose -f docker-compose.dev.yml"

# Region from IMDSv2 so the script is portable across regions/accounts.
imds_token=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
	-H "X-aws-ec2-metadata-token-ttl-seconds: 60")
AWS_DEFAULT_REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $imds_token" \
	"http://169.254.169.254/latest/meta-data/placement/region")
export AWS_DEFAULT_REGION

ssm() { aws ssm get-parameter --with-decryption --name "$PREFIX/$1" --query Parameter.Value --output text; }

# Read an optional parameter into `ssm_value`; 0 when it exists, 1 when SSM says it does not.
# Any other failure aborts the deploy: a throttled or denied lookup must not read as "not
# configured", because for a key that opens rows that silently drops it for the whole window.
# Both streams are read back through the command substitution, never a temp file: the box runs
# the aws-cli snap, and it writes nothing at all — value or error — once its stdout or stderr is
# a regular file. The file-based version of this read saw an empty error, matched no
# ParameterNotFound, and aborted the deploy on every absent parameter without printing a word.
ssm_value=""
ssm_optional() {
	local out
	out="$(ssm "$1" 2>&1)" && { ssm_value="$out"; return 0; }
	case "$out" in *ParameterNotFound*) return 1 ;; esac
	printf '%s\n' "$out" >&2
	exit 1
}

[ -n "${1:-}" ] && export BACKEND_IMAGE="$1"
[ -n "${2:-}" ] && export FRONTEND_IMAGE="$2"

ssm GHCR_PAT | docker login ghcr.io -u "$(ssm GHCR_USER)" --password-stdin

# .env written 0600 via umask; infra values inline, secrets + env config from SSM.
app_domain="$(ssm APP_DOMAIN)"   # read once; used for both the redirect base and APP_DOMAIN
git_sha="$(git rev-parse --short HEAD)"   # deployed commit (box HEAD) — surfaced by the app (/version, /docs) and FE
umask 077
cat > .env <<EOF
ENVIRONMENT=dev
GIT_SHA=$git_sha
DATABASE_HOST=db
DATABASE_PORT=5432
DATABASE_USER=appuser
DATABASE_NAME=ai_red_teaming
DATABASE_PASSWORD=$(ssm DATABASE_PASSWORD)
REDIS_HOST=redis
REDIS_PORT=6379
SESSION_JWT_SECRET=$(ssm SESSION_JWT_SECRET)
SESSION_JWT_ALGORITHM=HS256
OAUTH_STATE_SECRET=$(ssm OAUTH_STATE_SECRET)
OAUTH_COOKIE_SECURE=true
OAUTH_REDIRECT_BASE_URL=https://$app_domain
APP_DOMAIN=$app_domain
CORS_ORIGINS=$(ssm CORS_ORIGINS)
FRONTEND_BASE_URL=$(ssm FRONTEND_BASE_URL)
MODEL_SECRETS_KEY=$(ssm MODEL_SECRETS_KEY)
LOG_LEVEL=INFO
LOG_FORMAT=json
EMAIL_BACKEND=$(ssm EMAIL_BACKEND)
EMAIL_FROM=$(ssm EMAIL_FROM)
EXPORT_STORAGE_BACKEND=$(ssm EXPORT_STORAGE_BACKEND)
EXPORT_S3_BUCKET=$(ssm EXPORT_S3_BUCKET)
EOF

# Transcript sealing has its own key and never borrows the model one, so rotating
# or losing one dataset's key cannot reach the other. The app requires it, but a
# rollout is never blocked on it existing: an operator-managed SSM parameter wins,
# otherwise one is generated on the first deploy and kept beside .env. It is
# *persisted*, not regenerated — a fresh key would leave every already-sealed
# transcript unreadable. Back that file up with the box, or promote it to SSM as
# `CONVERSATION_SECRETS_KEY` to make it durable; losing it loses the transcripts.
# Only ParameterNotFound falls through to that file: any other SSM failure aborts,
# because minting a key over a throttled or denied lookup would seal new rows under
# a key the real parameter cannot open.
conversation_key_file=conversation_secrets_key
if ssm_optional CONVERSATION_SECRETS_KEY; then
	conversation_secrets_key="$ssm_value"
elif [ -s "$conversation_key_file" ]; then
	conversation_secrets_key="$(cat "$conversation_key_file")"
else
	conversation_secrets_key="$(openssl rand -base64 48 | tr -d '\n')"
	(umask 077; printf '%s' "$conversation_secrets_key" > "$conversation_key_file")
	echo "No CONVERSATION_SECRETS_KEY in SSM: generated one in $PWD/$conversation_key_file. Back it up — losing it loses every sealed transcript." >&2
fi
echo "CONVERSATION_SECRETS_KEY=$conversation_secrets_key" >> .env

# Only present during a transcript key rotation, same append-if-present rule as
# the model key below. Dropping it before `make rewraptranscripts` exits 0 leaves
# every row still sealed under it unreadable.
if ssm_optional CONVERSATION_SECRETS_KEY_RETIRED; then
	echo "CONVERSATION_SECRETS_KEY_RETIRED=$ssm_value" >> .env
fi

# Only present in SSM during a key-rotation window. Appended (not inline in the
# heredoc) so a missing parameter doesn't abort the deploy under `set -e`, and
# so we never emit an empty `MODEL_SECRETS_KEY_RETIRED=` — pydantic reads that
# as "" and fails the >=32-char check, refusing to boot.
if ssm_optional MODEL_SECRETS_KEY_RETIRED; then
	echo "MODEL_SECRETS_KEY_RETIRED=$ssm_value" >> .env
fi

# Key for the self-hosted `slm` service. Appended (not inline) so a missing
# parameter doesn't abort under `set -e`; absent → the compose `${SLM_API_KEY:-local}`
# default and the app config default apply. Must match the AiModel row's api_key.
if ssm_optional SLM_API_KEY; then
	echo "SLM_API_KEY=$ssm_value" >> .env
fi

# Sentry error reporting — opt-in. Appended (not inline) so a missing
# parameter doesn't abort under `set -e`; absent → `init_sentry` stays a no-op.
if sentry_dsn="$(ssm SENTRY_DSN 2>/dev/null)"; then
	echo "SENTRY_DSN=$sentry_dsn" >> .env
fi

# Google OIDC client — opt-in. Appended (not inline) so missing parameters don't
# abort under `set -e`; absent → Google stays out of the provider registry. Both
# in one guard: the app refuses to boot when only one of the pair is set.
if oidc_google_client_id="$(ssm OIDC_GOOGLE_CLIENT_ID 2>/dev/null)" &&
	oidc_google_client_secret="$(ssm OIDC_GOOGLE_CLIENT_SECRET 2>/dev/null)"; then
	echo "OIDC_GOOGLE_CLIENT_ID=$oidc_google_client_id" >> .env
	echo "OIDC_GOOGLE_CLIENT_SECRET=$oidc_google_client_secret" >> .env
fi

# Opt-in monitoring stack (../monitoring/) postgres-exporter — off by default.
# COMPOSE_PROFILES in the project .env also gates `docker compose`'s own
# profile activation, not just the container env, so this one flag both starts
# the service and supplies its DB password. Flip MONITORING_ENABLED=true only
# once the `monitoring` Postgres role exists (../monitoring/README.md runbook).
if monitoring_enabled="$(ssm MONITORING_ENABLED 2>/dev/null)" && [ "$monitoring_enabled" = "true" ]; then
	echo "COMPOSE_PROFILES=monitoring" >> .env
	# Guarded like the blocks above: a failed read skips the line instead of
	# writing an empty password (inline `$(...)` in an echo arg wouldn't trip `set -e`).
	if postgres_monitoring_password="$(ssm POSTGRES_MONITORING_PASSWORD 2>/dev/null)"; then
		echo "POSTGRES_MONITORING_PASSWORD=$postgres_monitoring_password" >> .env
	fi
fi

# Migrate, run the reference-data syncs (roles, licenses, annotation labels), then roll. alembic and the
# syncs run directly in one-off containers (not via `make migrate` / `make syncroles`):
# the box has no dev toolchain and make targets the dev compose file — don't
# "fix" this into make. Migrate before the new app serves traffic; on failure
# the script aborts and the old stack keeps running.
$COMPOSE pull
# Start data services and wait for health before migrating: `run --no-deps`
# starts no dependencies, so `db` must already be up for alembic to connect
# (mirrors `make migrate`, which depends on the `services` target).
$COMPOSE up -d --wait db redis
$COMPOSE run --rm --no-deps app alembic upgrade head
$COMPOSE run --rm --no-deps app python -m scripts.sync_roles
# Curated data licenses are reference data (like the roles) and MUST be synced right after the
# migration: the license migration creates an empty `data_licenses` table, and every group/eval
# projection resolves the platform default from it — without this the app 500s on those reads, and
# so does a private-group create, on the `No license` row it derives from the access level.
$COMPOSE run --rm --no-deps app python -m scripts.sync_licenses
# The annotation-label vocabulary is reference data too, but nothing 500s on an empty table —
# it only leaves the label picker with nothing to suggest. Fatal anyway (set -e), deliberately:
# a failure here almost always means the database or schema is broken and the roll would
# inherit it, so consistency with the two syncs above beats shipping around the least critical
# one. Two failures are reachable on a healthy schema, both wanting a decision rather than a
# retry: a hand-added live row sharing a key with a tombstoned curated one makes this sync revive
# the tombstone into a duplicate the unique index rejects (pinned by
# `test_a_resync_over_a_hand_re_added_key_aborts`), and a curated wording that a label an
# annotator typed already holds — or that a curated row the catalog no longer names holds — is
# refused outright (`test_sync_refuses_a_curated_wording_a_user_label_already_holds`); see
# `deploy/README.md` for the remedy.
$COMPOSE run --rm --no-deps app python -m scripts.sync_annotation_labels
# --wait blocks until app/worker pass their healthchecks. A container that boots
# but crashes (or never gets healthy) then fails the deploy, instead of reporting
# success on a broken roll.
$COMPOSE up -d --wait
docker image prune -af
