"""Terms-of-Service endpoints — mounted under `/api/v1/terms`.

The current version is readable **unauthenticated**: the registration screen has to show it
before an account exists, and legal text a platform asks the public to accept is public by
nature. Publishing is admin-only.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Response
from fastapi import status

from app.core.audit.enums import AuditAction
from app.core.audit.service import record_audit
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import transactional
from app.core.exceptions import NotFoundError
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.schemas import Page
from app.core.terms.schemas import TermsDocumentResponse
from app.core.terms.schemas import TermsDocumentSummary
from app.core.terms.schemas import TermsPublish
from app.core.terms.service import get_current_terms
from app.core.terms.service import get_terms
from app.core.terms.service import list_terms
from app.core.terms.service import publish_terms

router = APIRouter(prefix="/terms", tags=["terms"])

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_NO_TERMS = problem_response("The platform has no published terms of service.")
_NOT_FOUND = problem_response("No terms document with that id.")
_VERSION_TAKEN = problem_response("A live terms document already carries that version.")


@router.get(
    "/current",
    response_model=TermsDocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Get the current terms of service",
    description=(
        "Return the version every account is asked to accept, with its full text. Unauthenticated — "
        "the registration screen needs it before an account exists. `404` while the platform has "
        "published none, which is the shipped state: consent then has nothing to point at, and both "
        "the registration checkbox and the acceptance gate stay out of the way."
    ),
    responses=COMMON_ERROR_RESPONSES | {status.HTTP_404_NOT_FOUND: _NO_TERMS},
)
async def get_current_terms_endpoint(db: DbSession, response: Response) -> TermsDocumentResponse:
    """Read the current terms of service.

    ### Errors

    * **404 Not Found** — nothing published yet.

    ### Implementation Notes

    Anonymous and hit on every registration-screen and acceptance-gate render, so the response
    carries a short `max-age` as `/platform-settings/public` does. That caps repeat renders per
    browser and nothing more: nothing in the deploy path caches, so `public` only marks the body
    shareable should a shared cache ever be added. `MAX_TERMS_CONTENT_CHARS` bounds the worst-case
    body one anonymous hit can pull. `POST /auth/me/terms` refuses a stale id rather than recording
    consent against the wrong version, so the cache cannot corrupt a consent record.

    It can, however, strand a client: a consent surface that re-reads this endpoint to recover from
    that refusal would be answered from its own browser cache with the very version that was
    superseded, and loop there until the entry expires. A client acting on this document (rather
    than merely displaying it) must therefore bypass its cache (`cache: 'no-store'`) — keep that in
    mind before pointing a new consent flow at this route.
    """
    response.headers["Cache-Control"] = "public, max-age=30"
    current = await get_current_terms(db)
    if current is None:
        raise NotFoundError("The platform has no published terms of service.")
    return TermsDocumentResponse.from_model(current)


@router.get(
    "",
    response_model=Page[TermsDocumentSummary],
    status_code=status.HTTP_200_OK,
    summary="List terms of service versions",
    description=(
        "Return the published versions, newest first — the admin's history view. Bodies are "
        "omitted; fetch a single version for its text."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: problem_response("Caller lacks the `platform_settings:read` permission."),
    },
)
async def list_terms_endpoint(
    _admin: Annotated[SessionUser, Depends(require_permission(Permission.PLATFORM_SETTINGS_READ))],
    pagination: PaginationDep,
    db: DbSession,
) -> Page[TermsDocumentSummary]:
    """List the published terms versions.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `platform_settings:read`.
    """
    rows, total = await list_terms(db, limit=pagination.limit, offset=pagination.offset)
    return Page[TermsDocumentSummary](
        items=[TermsDocumentSummary.from_model(row) for row in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{terms_id}",
    response_model=TermsDocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Get one terms of service version",
    description=(
        "Return a single live version with its full text. Any authenticated caller may read one — "
        "the current version is public anyway, and this serves the admin history. A version "
        "tombstoned straight in the database reads as missing here, which is why nothing in the API "
        "deletes one."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: _NOT_FOUND,
    },
)
async def get_terms_endpoint(terms_id: UUID, _caller: CurrentUserDep, db: DbSession) -> TermsDocumentResponse:
    """Get one terms version by id.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **404 Not Found** — no live document with that id.
    """
    document = await get_terms(db, terms_id)
    if document is None:
        raise NotFoundError("No terms document with that id.")
    return TermsDocumentResponse.from_model(document)


@router.post(
    "",
    response_model=TermsDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a terms of service version",
    description=(
        "Publish a new version, which immediately becomes the one every account must accept: every "
        "user whose recorded consent points at an older version is refused by the API until they "
        "accept it, the publishing admin included — so publishing a correction means accepting the "
        "flawed version first. Versions are immutable — there is no edit; correcting text means "
        "publishing the next version."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: problem_response(
            "Caller lacks the `platform_settings:update` permission, or has not accepted the current terms."
        ),
        status.HTTP_409_CONFLICT: _VERSION_TAKEN,
    },
)
@transactional
async def publish_terms_endpoint(
    payload: TermsPublish,
    caller: Annotated[SessionUser, Depends(require_permission(Permission.PLATFORM_SETTINGS_UPDATE))],
    db: DbSession,
    response: Response,
) -> TermsDocumentResponse:
    """Publish a new terms version.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `platform_settings:update`, or has not accepted the
      current version (publishing a correction therefore means accepting the flawed one first).
    * **409 Conflict** — a live document already carries that version (`errors[].type` is
      `terms_version_taken`, so a form can show it on the field).
    """
    document = await publish_terms(db, version=payload.version, content=payload.content)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.TERMS_PUBLISH,
        object_type="terms_document",
        object_id=document.id,
        # The body is the point of the row and can run to tens of KB; the version handle is
        # what makes the trail answerable ("who published what, when").
        after={"version": document.version},
    )
    response.headers["Location"] = f"/api/v1/terms/{document.id}"
    return TermsDocumentResponse.from_model(document)
