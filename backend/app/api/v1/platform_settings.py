"""Platform-settings singleton endpoints — mounted under `/api/v1/platform-settings`.

Admin-only read/override of platform-wide policy: data licensing, registration, password
policy, password-reset throttling. A resource-level `PATCH` with a partial body (not
per-knob sub-paths) so future settings extend the same routes without new endpoints.
"""

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Response
from fastapi import status
from fastapi.encoders import jsonable_encoder

from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import DbSession
from app.core.dependencies import transactional
from app.core.exceptions import BadRequestError
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.service import validate_license_ref
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.platform_settings.models import PLATFORM_SETTINGS_ID
from app.core.platform_settings.models import PlatformSettings
from app.core.platform_settings.schemas import PlatformSettingsResponse
from app.core.platform_settings.schemas import PlatformSettingsUpdate
from app.core.platform_settings.schemas import PublicPasswordPolicyResponse
from app.core.platform_settings.schemas import PublicPlatformSettingsResponse
from app.core.platform_settings.service import SETTINGS_KNOBS
from app.core.platform_settings.service import get_platform_settings
from app.core.platform_settings.service import update_platform_settings

router = APIRouter(prefix="/platform-settings", tags=["platform-settings"])


def _settings_snapshot(settings: PlatformSettings) -> dict[str, object]:
    # Same derived knob list as the writer, so a new knob can't vanish from the audit diff.
    return {knob: jsonable_encoder(getattr(settings, knob)) for knob in SETTINGS_KNOBS}


@router.get(
    "",
    response_model=PlatformSettingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get platform settings",
    description=(
        "Return every platform-wide setting — data licensing, registration, password policy, "
        "password-reset throttling. Before any admin override, this reflects the shipped "
        "defaults with `updated_at: null`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the `platform_settings:read` permission."),
    },
)
async def get_platform_settings_endpoint(
    _admin: Annotated[SessionUser, Depends(require_permission(Permission.PLATFORM_SETTINGS_READ))],
    db: DbSession,
) -> PlatformSettingsResponse:
    """Read platform settings.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `platform_settings:read`.
    """
    settings = await get_platform_settings(db)
    return PlatformSettingsResponse.from_model(settings)


@router.get(
    "/public",
    response_model=PublicPlatformSettingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get public platform settings",
    description=(
        "Unauthenticated subset of platform settings for the login/registration screens — whether "
        "open self-signup is enabled (the inverse of invite-only mode) and the password rules every "
        "set-password screen must satisfy."
    ),
    responses=COMMON_ERROR_RESPONSES,
)
async def get_public_platform_settings_endpoint(db: DbSession, response: Response) -> PublicPlatformSettingsResponse:
    """Read the anonymous-safe platform-settings subset.

    Deliberately minimal disclosure — `signup_enabled` is derivable anyway by attempting a
    registration, so exposing it lets the FE hide the sign-up path without adding an
    enumeration surface. The password policy is disclosed on the same reasoning: one rejected
    attempt reveals it, and every set-password screen here is anonymous (register, invitation
    accept, reset confirm), so without it the rules can only be delivered as an error.
    """
    # Anonymous, hit by every login-screen load, one DB round-trip per hit, and the app has no
    # rate-limit middleware — a short shared cache caps that; a 30s-stale flag is harmless (the
    # FE is fail-open by design and the register endpoint enforces the mode regardless).
    response.headers["Cache-Control"] = "public, max-age=30"
    settings = await get_platform_settings(db)
    return PublicPlatformSettingsResponse(
        signup_enabled=not settings.invite_only,
        password_policy=PublicPasswordPolicyResponse.from_model(settings),
    )


@router.patch(
    "",
    response_model=PlatformSettingsResponse,
    status_code=status.HTTP_200_OK,
    summary="Update platform settings",
    description=(
        "Override platform-wide settings — any subset of the knobs in the request schema; omitted "
        "fields stay unchanged. `default_license_id` must reference a live licence "
        "(`GET /api/v1/licenses`) other than `No license`, which cannot be the platform default. "
        "Materializes the singleton row on the first effective write; a no-op PATCH writes "
        "(and audits) nothing."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: problem_response(
            "`default_license_id` — requested, or the shipped default on the row-materializing "
            "first write — does not reference a live licence, or is the `No license` entry."
        ),
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the `platform_settings:update` permission."),
    },
)
@transactional
async def update_platform_settings_endpoint(
    payload: PlatformSettingsUpdate,
    admin: Annotated[SessionUser, Depends(require_permission(Permission.PLATFORM_SETTINGS_UPDATE))],
    db: DbSession,
) -> PlatformSettingsResponse:
    """Update platform settings (any subset of the knobs).

    The audit trail records state deltas, not attempts — by design, a no-op PATCH (values
    equal to the effective settings) neither materializes the row nor writes an audit entry.

    ### Errors

    * **400 Bad Request** — `default_license_id` (requested, or the shipped default on the
      row-materializing first write) does not reference a live licence, or is the `No license`
      entry.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `platform_settings:update`.
    * **422 Unprocessable Entity** — a field is explicitly null.
    """
    current = await get_platform_settings(db)
    changes = {
        field: value
        for field, value in payload.model_dump(exclude_unset=True).items()
        if getattr(current, field) != value
    }
    # A no-op PATCH (empty body, or values equal to the effective settings) writes nothing:
    # no silent row materialization, no audit row, `updated_at` keeps signalling "shipped
    # defaults" while transient.
    if not changes:
        return PlatformSettingsResponse.from_model(current)

    if "default_license_id" in changes:
        if changes["default_license_id"] == curated_license_id(NO_LICENSE_SPDX_ID):
            # A live curated row, so the licence-ref guard would pass it — but as the platform
            # default it unlicenses every group and evaluation that inherits.
            raise BadRequestError("The 'No license' entry cannot be the platform default.")
        await validate_license_ref(db, changes["default_license_id"])  # 400 if not a live licence
    elif current.updated_at is None:
        # The first-ever write materializes the whole row, shipped-default licence FK included —
        # validate that too, or an unsynced catalog (`make synclicenses` not run) surfaces as a
        # raw IntegrityError 500 instead of a legible 400.
        await validate_license_ref(db, current.default_license_id)
    # Snapshot *before* the upsert: update_platform_settings refreshes the
    # identity-map-pinned row in place, so a live reference would read the new values.
    before = _settings_snapshot(current)
    settings = await update_platform_settings(db, **changes)
    diff_before, diff_after = changed_fields(before, _settings_snapshot(settings))
    await record_audit(
        db,
        actor_id=admin.id,
        actor_email=admin.email,
        action=AuditAction.PLATFORM_SETTINGS_UPDATE,
        object_type="platform_settings",
        object_id=PLATFORM_SETTINGS_ID,
        before=diff_before,
        after=diff_after,
    )
    return PlatformSettingsResponse.from_model(settings)
