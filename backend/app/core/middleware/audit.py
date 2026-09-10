"""`AuditAccessMiddleware` — central audit of asset-access (read/egress) events.

Access events live at the HTTP boundary (a read changes no row, so an ORM/DB
trigger can't see them). This middleware audits them centrally from an **allowlist
of routes** — no per-endpoint code. It reads the actor from `request.state.user`
(set by `AuthMiddleware`, which is outer), the target id from the matched route's
path params, and writes in its **own session/transaction**.

Pure ASGI (like `LoggingMiddleware`, not `BaseHTTPMiddleware`): it wraps `send` only
to capture the response status, and — crucially — never buffers the response body, so
it sits cleanly in front of the streaming export download. The audit write runs **after
the inner app has finished sending the response**, so it is off the caller's latency
path, and it is **best-effort**: any failure is swallowed and logged, so it never
changes the outcome of the login/download the caller requested (audit can't block a
login). If the inner app raises before responding, no audit row is written.

Register **innermost** in `app/main.py` so it wraps the router directly (sees the
matched route + path params) and runs inside `AuthMiddleware` (sees the actor).

The allowlist is deliberately narrow — only reads that expose a sensitive payload /
data egress, never non-sensitive listing/browsing (avoids flooding the log). A sensitive
read is audited on *every* access, so a paginated transcript read writes one row per
page — that per-egress granularity is intended (an auditor wants each access), not noise.
Mutating routes are audited by the explicit `record_audit` helper, NOT here — with three
deliberate exceptions that must never let an audit write affect the flow: login *attempts*
(`_LOGIN_ROUTE`), the pre-auth account-lifecycle events (`AUTH_LIFECYCLE_ROUTES`: email
verify, invitation accept), and the OIDC callback (`_OIDC_CALLBACK_ROUTE`, which also picks
its own action since every outcome its own logic reaches is the same 302 — an unknown
provider 404s earlier and is simply unaudited). Those are recorded here,
best-effort after the response, so the audit can never block auth; the handler only
stashes the subject id (and, for the OIDC callback, the action) on `request.state`.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from starlette.requests import Request

from app.core.audit.enums import AuditAction
from app.core.audit.service import record_audit
from app.core.auth.schemas import SessionUser
from app.core.database import standalone_session
from app.core.logging import get_logger

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _AccessSpec:
    action: AuditAction
    object_type: str
    object_id_param: str | None = None  # path param holding the target id; None for list/singleton views


# Keyed by (HTTP method, route name = endpoint function name — FastAPI's stable handle; a
# path template would be brittle to the /api/v1 prefix + path params). Deliberately narrow:
# ONLY reads that expose a sensitive payload / egress data — never non-sensitive
# listing/browsing. A sensitive read fires per access, so a paginated transcript writes one
# row per page (intended per-egress granularity). Extend as the team confirms further reads.
ACCESS_AUDIT_ROUTES: dict[tuple[str, str], _AccessSpec] = {
    # Data egress — downloading a generated export streams the underlying data out.
    ("GET", "download_export_job_endpoint"): _AccessSpec(AuditAction.EXPORT_DOWNLOAD, "export_job", "job_id"),
    # Sensitive payload — reading a conversation's full message transcript.
    ("GET", "list_conversation_messages_endpoint"): _AccessSpec(
        AuditAction.DATA_READ, "conversation", "conversation_id"
    ),
    # Same transcript payload, reviewer-side: a submission's parent-conversation messages.
    ("GET", "get_submission_messages_endpoint"): _AccessSpec(AuditAction.DATA_READ, "submission", "submission_id"),
}

# The login route is audited as an *attempt* here — never in the auth handler, so the
# audit can never block a login. We record ONLY the attempt + its outcome, derived purely
# from the HTTP status: no email, no actor, no password — the middleware can't see them
# anyway, and must not (OWASP). Kept separate from the asset-access allowlist because it
# fires on 401 too, not just 2xx.
_LOGIN_ROUTE = ("POST", "login")

# Successful account-lifecycle auth events. Pre-auth (no actor), so no login-style outcome
# branching — recorded ONLY on 2xx, and ONLY when the handler stashed the affected account
# on `request.state.audit_subject` (a purely additive line; the auth flow is never touched).
# Scoped to events whose service ALREADY returns the affected `User`, so the handler can
# attribute without any change to the authorization flow. `auth.register` and
# `auth.credential_reset_confirmed` are intentionally NOT here — attributing them would
# require changing their auth-flow services (return the resolved user), which is off-limits.
AUTH_LIFECYCLE_ROUTES: dict[tuple[str, str], AuditAction] = {
    ("POST", "verify_email_endpoint"): AuditAction.AUTH_EMAIL_VERIFIED,
    ("POST", "post_invitation_accept_endpoint"): AuditAction.INVITATION_ACCEPT,
}

# The OIDC callback reuses `_LOGIN_ROUTE`'s taxonomy (`AUTH_LOGIN`/`AUTH_LOGIN_FAILED` —
# it IS a login, just via a different mechanism) but not its status-code-based outcome
# detection: every path that reaches the handler's own logic is a 302 (success and every
# `#error=` failure alike) — an unknown `{provider}` 404s before that logic runs and is
# simply unaudited — so status carries no signal for what IS audited. The handler
# stashes which action fired directly on
# `request.state.audit_action`, plus `audit_subject` (the resolved account, when one
# was) — read unconditionally below, not gated on `_SUCCESS`. Scoped to the one failure
# (`account_inactive`) that resolves a real account and is the strongest detection
# signal for a refused/blocked activation attempt; `invalid_claims`/`email_unverified`/
# `login_conflict` are unaudited, same call as `_audit_login` skipping non-credential-
# outcome statuses.
_OIDC_CALLBACK_ROUTE = ("GET", "auth_callback")

_SUCCESS = range(200, 300)
_UNAUTHORIZED = 401
_UNHANDLED_EXCEPTION_STATUS = 500


class AuditAccessMiddleware:
    """Audit allowlisted read/access routes (and login attempts) after the response is sent."""

    def __init__(self, app: ASGIApp) -> None:
        """Store the wrapped ASGI app.

        Args:
            app: Downstream ASGI callable this middleware delegates to.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Forward the request, capture its status, then best-effort audit if instrumented.

        Non-``http`` lifecycles (websocket, lifespan) pass through untouched. ``send`` is
        wrapped only to record the response status; the body is never buffered. The audit
        write happens after the inner app returns — i.e. after the response is fully sent —
        so it stays off the caller's latency path. An unhandled exception propagates without
        an audit row (the status line is never reached).
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = _UNHANDLED_EXCEPTION_STATUS

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        route_name = getattr(scope.get("route"), "name", None)
        if route_name is None:
            return
        key = (scope["method"], route_name)
        if key == _LOGIN_ROUTE:
            await self._audit_login(status_code)
        elif (spec := ACCESS_AUDIT_ROUTES.get(key)) is not None and status_code in _SUCCESS:
            await self._record_access(scope, spec)
        elif (lifecycle := AUTH_LIFECYCLE_ROUTES.get(key)) is not None and status_code in _SUCCESS:
            await self._record_lifecycle(scope, lifecycle)
        elif key == _OIDC_CALLBACK_ROUTE:
            await self._audit_oidc_callback(scope)

    async def _audit_login(self, status_code: int) -> None:
        """Record a login attempt by outcome (status only) — no actor, no email, no detail."""
        if status_code in _SUCCESS:
            action = AuditAction.AUTH_LOGIN
        elif status_code == _UNAUTHORIZED:
            action = AuditAction.AUTH_LOGIN_FAILED
        else:
            return  # 422/500 etc. — not a credential-verification outcome, not an attempt
        await self._write(action)

    async def _record_access(self, scope: Scope, spec: _AccessSpec) -> None:
        request = Request(scope)
        actor: SessionUser | None = getattr(request.state, "user", None)
        object_id = _as_uuid(request.path_params.get(spec.object_id_param)) if spec.object_id_param else None
        await self._write(
            spec.action,
            actor_id=actor.id if actor else None,
            actor_email=actor.email if actor else None,
            object_type=spec.object_type,
            object_id=object_id,
        )

    async def _record_lifecycle(self, scope: Scope, action: AuditAction) -> None:
        # Subject = the account the handler resolved onto `request.state.audit_subject`.
        # Absent → no attributable account (defensive: a future lifecycle route whose success
        # path resolves no account leaves this unset) → no row.
        subject: UUID | None = getattr(Request(scope).state, "audit_subject", None)
        if subject is None:
            return
        await self._write(action, object_type="user", object_id=subject)

    async def _audit_oidc_callback(self, scope: Scope) -> None:
        # Every outcome the handler's own logic reaches is a 302 (see `_OIDC_CALLBACK_ROUTE`'s
        # comment) — the handler picks the action itself and stashes it, so there's no
        # status-based branching.
        state = Request(scope).state
        action: AuditAction | None = getattr(state, "audit_action", None)
        if action is None:
            # Handler never reached a stash point — either an unhandled exception, or the
            # unknown-provider 404 that never enters the handler's `try` at all.
            return
        subject: UUID | None = getattr(state, "audit_subject", None)
        if subject is None:
            return  # no attributable account (defensive — every current caller sets one)
        await self._write(action, object_type="user", object_id=subject)

    async def _write(
        self,
        action: AuditAction,
        *,
        actor_id: UUID | None = None,
        actor_email: str | None = None,
        object_type: str | None = None,
        object_id: UUID | None = None,
    ) -> None:
        try:
            async with standalone_session() as session:
                await record_audit(
                    session,
                    actor_id=actor_id,
                    actor_email=actor_email,
                    action=action,
                    object_type=object_type,
                    object_id=object_id,
                )
        except Exception:
            logger.warning("audit.access_write_failed", action=action.value, exc_info=True)


def _as_uuid(value: str | None) -> UUID | None:
    # `value` is a non-empty path-param string here, so a malformed id raises ValueError only.
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None
