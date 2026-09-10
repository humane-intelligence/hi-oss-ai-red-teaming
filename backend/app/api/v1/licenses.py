"""Data-license endpoints — mounted under `/api/v1/licenses`.

The list and detail reads are gated only on authentication (no permission): they back the
licence picker for anyone composing or viewing an evaluation. The write endpoints (create / edit /
delete) are permission-gated and land with the CRUD slice.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Response
from fastapi import status

from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.licenses.models import DataLicense
from app.core.licenses.schemas import DataLicenseResponse
from app.core.licenses.schemas import DataLicenseSummary
from app.core.licenses.schemas import LicenseCreate
from app.core.licenses.schemas import LicenseUpdate
from app.core.licenses.service import create_data_license
from app.core.licenses.service import get_data_license
from app.core.licenses.service import get_restorable_data_license
from app.core.licenses.service import list_data_licenses
from app.core.licenses.service import restore_data_license
from app.core.licenses.service import soft_delete_data_license
from app.core.licenses.service import update_data_license
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import TERMS_REFUSED
from app.core.openapi import problem_response
from app.core.platform_settings.service import get_platform_settings
from app.core.restore import DeletedFilterAuthorNewestFirst
from app.core.restore import assert_may_list_deleted
from app.core.restore import restore_cutoff
from app.core.schemas import Page

router = APIRouter(prefix="/licenses", tags=["licenses"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller may not manage this licence.")
_NOT_FOUND = problem_response("No license with that id.")
_FORBIDDEN_UPDATE = problem_response(
    "Caller may not apply these changes: another user's licence without `licenses:manage`, a curated "
    "licence's code-owned field, a curated licence whose text the catalog ships, or a curated licence's "
    "text without `licenses:manage`."
)
_NOT_RESTORABLE = problem_response(
    "No restorable data license with this id: never deleted, or deleted longer than the restore window ago."
)


def _license_snapshot(lic: DataLicense) -> dict[str, object]:
    """Curated audit snapshot — metadata only; the (large) `content` body is kept out of the trail."""
    return {
        "name": lic.name,
        "version": lic.version,
        "short_description": lic.short_description,
        "reference_url": lic.reference_url,
        "spdx_id": lic.spdx_id,
        "protects_conversation_data": lic.protects_conversation_data,
    }


@router.get(
    "",
    response_model=Page[DataLicenseSummary],
    status_code=status.HTTP_200_OK,
    summary="List data licenses",
    description=(
        "Return the data licenses an evaluation can be set to — the pick-list for the license "
        "selector, plus the curated + user-authored set for the management view. Any authenticated "
        "user may read it. Full licence text is omitted here; fetch a single licence for its body."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: problem_response(
            "Listing deleted licenses requires the 'licenses:delete' permission."
        ),
    },
)
async def list_licenses_endpoint(
    caller: CurrentUserDep,
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
    deleted: DeletedFilterAuthorNewestFirst = False,
) -> Page[DataLicenseSummary]:
    """List data licenses.

    Any authenticated user may read the live list. `deleted=true` is the restore
    surface, so it needs `licenses:delete` and returns the tombstones of licences the
    caller **authored** — matching what the restore will accept, since an admin's delete
    of someone else's licence leaves the author as the one who may restore it. A
    `licenses:manage` holder sees every tombstone, as they may restore any.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — asked for `deleted=true` without `licenses:delete`.
    """
    assert_may_list_deleted(deleted, caller, Permission.LICENSES_DELETE, entity="licenses")
    can_manage = Permission.LICENSES_MANAGE in caller.permissions
    default_id = (await get_platform_settings(db)).default_license_id
    rows, total = await list_data_licenses(
        db,
        deleted=deleted,
        authored_by=None if can_manage else caller.id,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted_cutoff=restore_cutoff(settings),
    )
    items = [DataLicenseSummary.from_model(lic, is_default=lic.id == default_id) for lic in rows]
    return Page[DataLicenseSummary](items=items, total=total, limit=pagination.limit, offset=pagination.offset)


@router.get(
    "/{license_id}",
    response_model=DataLicenseResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a data license",
    description=(
        "Return a single data license by id, including its full legal text. Serves a soft-deleted "
        "license too (its `deleted_at` is set): evaluations and groups keep resolving one after the "
        "delete, so the text behind that reference stays readable. Read-only — editing, deleting and "
        "restoring it are gated elsewhere."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: TERMS_REFUSED,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_license_endpoint(license_id: UUID, _caller: CurrentUserDep, db: DbSession) -> DataLicenseResponse:
    """Get one data license by id, deleted ones included.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **404 Not Found** — no license with that id.
    """
    lic = await get_data_license(db, license_id, include_deleted=True)
    default_id = (await get_platform_settings(db)).default_license_id
    return DataLicenseResponse.from_model(lic, is_default=lic.id == default_id)


@router.post(
    "",
    response_model=DataLicenseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a data license",
    description=(
        "Create a user-authored data license (owned by the caller). `protects_conversation_data` marks "
        "a licence that forbids redistributing raw conversations: message text written under it is "
        "stored encrypted at rest — titles, tags and attached images are not. A conversation takes that "
        "decision when it is created, so it covers conversations started from then on and nothing "
        "already running, including messages added to those later."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the `licenses:create` permission."),
    },
)
@transactional
async def create_license_endpoint(
    payload: LicenseCreate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.LICENSES_CREATE))],
    db: DbSession,
    response: Response,
) -> DataLicenseResponse:
    """Create a data license.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks the `licenses:create` permission.
    """
    lic = await create_data_license(
        db,
        caller_id=caller.id,
        name=payload.name,
        version=payload.version,
        short_description=payload.short_description,
        content=payload.content,
        reference_url=payload.reference_url,
        protects_conversation_data=payload.protects_conversation_data,
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.DATA_LICENSE_CREATE,
        object_type="data_license",
        object_id=lic.id,
        after=_license_snapshot(lic),
    )
    response.headers["Location"] = f"/api/v1/licenses/{lic.id}"
    return DataLicenseResponse.from_model(lic)


@router.patch(
    "/{license_id}",
    response_model=DataLicenseResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a data license",
    description=(
        "Edit a data license. An owner may edit only licenses they created; other users' licenses "
        "require `licenses:manage`. A curated license is managed in code: the one field the API takes "
        "is `content` (the legal text), with `licenses:manage`, and only where the catalog ships none "
        "— there the resync leaves the column alone. A curated license whose text the catalog does "
        "ship is refused outright, since the resync would revert it. `protects_conversation_data` is "
        "part of that code-managed metadata, so it is settable only on a user-authored licence."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: problem_response(
            "A curated license was patched with an empty body (`content` is the only field it takes)."
        ),
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN_UPDATE,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
@transactional
async def update_license_endpoint(
    license_id: UUID,
    payload: LicenseUpdate,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.LICENSES_UPDATE))],
    db: DbSession,
) -> DataLicenseResponse:
    """Update a data license.

    ### Errors

    * **400 Bad Request** — a curated license was patched with an empty body.
    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `licenses:update`, would edit another user's license without
      `licenses:manage`, edits a curated license outside its `content` (the rest is managed in code;
      the text itself needs `licenses:manage`), or edits the text of a curated license the catalog
      ships one for.
    * **404 Not Found** — no license with that id.
    """
    lic = await get_data_license(db, license_id, for_update=True)
    before = _license_snapshot(lic)
    content_before = lic.content
    can_manage = Permission.LICENSES_MANAGE in caller.permissions
    updated = await update_data_license(db, lic, payload, caller_id=caller.id, can_manage=can_manage)
    diff_before, diff_after = changed_fields(before, _license_snapshot(updated))
    # `content` is kept out of the snapshot (large body); flag that it moved so a content-only edit
    # still audits the event.
    context = {"content_changed": True} if updated.content != content_before else None
    if diff_before or diff_after or context:
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.DATA_LICENSE_UPDATE,
            object_type="data_license",
            object_id=updated.id,
            before=diff_before or None,
            after=diff_after or None,
            context=context,
        )
    return DataLicenseResponse.from_model(updated)


@router.post(
    "/{license_id}/restore",
    response_model=DataLicenseResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore a data license",
    description=(
        "Clear a soft-deleted licence's `deleted_at`, returning it to the picker. Restorable for a "
        "fixed window after the delete (set per deployment), and gated exactly like a "
        "delete: an owner may restore only their own, other users' require `licenses:manage`. **Curated "
        "licences are refused (403)** — they are reference data managed in code, and `synclicenses` "
        "revives one on the next resync."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
    },
)
@transactional
async def restore_license_endpoint(
    license_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.LICENSES_DELETE))],
    db: DbSession,
    settings: SettingsDep,
) -> DataLicenseResponse:
    """Restore a soft-deleted data license.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `licenses:delete`, targets another user's licence
      without `licenses:manage`, or targets a curated licence (managed in code).
    * **404 Not Found** — no restorable licence: never deleted, or deleted longer than
      the restore window ago.
    """
    lic = await get_restorable_data_license(db, license_id, deleted_cutoff=restore_cutoff(settings))
    restored = await restore_data_license(
        db, lic, caller_id=caller.id, can_manage=Permission.LICENSES_MANAGE in caller.permissions
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.DATA_LICENSE_RESTORE,
        object_type="data_license",
        object_id=license_id,
        after=_license_snapshot(restored),
    )
    default_id = (await get_platform_settings(db)).default_license_id
    return DataLicenseResponse.from_model(restored, is_default=restored.id == default_id)


@router.delete(
    "/{license_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a data license",
    description=(
        "Soft-delete a data license. Refused (409) for a user-authored platform default. An owner may "
        "delete only their own; other users' require `licenses:manage`. Curated licenses are managed in "
        "code and cannot be deleted here — including the shipped default, which is refused with 403 (not "
        "409) as curated. Groups/evaluations already referencing it keep resolving it."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
        status.HTTP_409_CONFLICT: problem_response("The license is a user-authored current platform default."),
    },
)
@transactional
async def delete_license_endpoint(
    license_id: UUID,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.LICENSES_DELETE))],
    db: DbSession,
) -> Response:
    """Soft-delete a data license.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `licenses:delete`, would delete another user's license without
      `licenses:manage`, or targets a curated license (managed in code, never deletable via the API —
      this includes the shipped default, refused here before the 409 default-guard is reached).
    * **404 Not Found** — no license with that id.
    * **409 Conflict** — the license is a user-authored current platform default.
    """
    lic = await get_data_license(db, license_id, for_update=True)
    before = _license_snapshot(lic)
    can_manage = Permission.LICENSES_MANAGE in caller.permissions
    await soft_delete_data_license(db, lic, caller_id=caller.id, can_manage=can_manage)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.DATA_LICENSE_DELETE,
        object_type="data_license",
        object_id=license_id,
        before=before,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
