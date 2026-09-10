"""Image upload + serving endpoints (generic media: covers, avatars, icons)."""

from datetime import UTC
from datetime import datetime
from typing import Annotated

from anyio import to_thread
from fastapi import APIRouter
from fastapi import Form
from fastapi import Response
from fastapi import UploadFile
from fastapi import status
from fastapi.responses import StreamingResponse
from itsdangerous import BadSignature
from sqlmodel import col

from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.dependencies import OptionalUserDep
from app.core.dependencies import SettingsDep
from app.core.exceptions import BadRequestError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger
from app.core.media.models import MediaAsset
from app.core.media.schemas import MediaAssetResponse
from app.core.media.schemas import SignedUrlResponse
from app.core.media.services.images import MAX_UPLOAD_BYTES
from app.core.media.services.images import create_image
from app.core.media.signing import sign_key
from app.core.media.signing import verify_token
from app.core.media.storage import get_media_storage
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import TERMS_REFUSED
from app.core.openapi import problem_response
from app.core.terms.service import TermsAcceptanceRequiredError
from app.core.terms.service import acceptance_required_for

router = APIRouter(prefix="/images", tags=["images"])
logger = get_logger(__name__)

_UPLOAD_READ_CHUNK = 1024 * 1024
_CACHE_ONE_YEAR = "public, max-age=31536000, immutable"


async def _read_capped(file: UploadFile) -> bytes:
    """Read the upload in chunks, rejecting once it crosses the size cap (before buffering it all)."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_READ_CHUNK):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise BadRequestError(f"Image exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


async def _get_live_asset(db: DbSession, key: str) -> MediaAsset | None:
    result = await db.execute(MediaAsset.live_select().where(col(MediaAsset.key) == key))
    return result.scalar_one_or_none()


async def _stream_asset(asset: MediaAsset, db: DbSession, *, cache_control: str) -> StreamingResponse:
    """Stream a resolved asset's bytes, closing `db` first (shared by the public + signed GETs)."""
    # Open EAGERLY (offloaded — local open / S3 get_object may block): a vanished blob raises here,
    # before the 200 + headers commit, so it 404s cleanly instead of a truncated body.
    try:
        stream = await to_thread.run_sync(lambda: get_media_storage().open(asset.key))
    except FileNotFoundError:
        raise NotFoundError("Image not found.") from None
    content_type = asset.content_type
    await db.close()  # release the pooled connection before the stream (mirrors exports download)
    return StreamingResponse(
        stream,
        media_type=content_type,
        headers={"Cache-Control": cache_control, "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "",
    response_model=MediaAssetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an image",
    description=(
        "Accept a JPEG/PNG/WebP image (<=20 MB), auto-resize it to fit 1200x1200 (keeping aspect ratio "
        "and source format), store it under a timestamped key, and return the key + URL to reference it "
        "(e.g. as an evaluation cover). Any authenticated user may upload. `is_private=true` makes the "
        "asset signed-URL-only (the public bare-key GET 404s it); the choice is immutable."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_400_BAD_REQUEST: problem_response("File is not a supported image, or exceeds the size limit."),
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: TERMS_REFUSED,
    },
)
async def upload_image(
    caller: CurrentUserDep,
    db: DbSession,
    file: UploadFile,
    response: Response,
    is_private: Annotated[bool, Form()] = False,
) -> MediaAssetResponse:
    """Upload, validate, resize, and store an image.

    ### Errors

    * **400 Bad Request** — the file is not a readable JPEG/PNG/WebP, or exceeds the 20 MB limit.
    * **401 Unauthorized** — bearer token missing/invalid.
    """
    raw = await _read_capped(file)
    asset = await create_image(db, raw=raw, created_by_id=caller.id, is_private=is_private)
    await db.commit()
    response.headers["Location"] = f"/api/v1/images/{asset.key}"
    return MediaAssetResponse.from_asset(asset)


@router.get(
    "/signed-url",
    response_model=SignedUrlResponse,
    status_code=status.HTTP_200_OK,
    summary="Mint a signed URL for an image",
    description=(
        "Return a time-limited signed URL for an existing image. For a `public` image this is open like the "
        "plain GET — the token grants no more than the already-public key does. A `private` image requires "
        "authentication, and the minted URL is bound to the requesting user: only they can fetch with it. "
        "The token embeds the key, so the fetch path carries no key of its own."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Private image — authentication required to mint."),
        status.HTTP_403_FORBIDDEN: TERMS_REFUSED,
        status.HTTP_404_NOT_FOUND: problem_response("No such image (or it was deleted)."),
    },
)
async def mint_signed_url(key: str, db: DbSession, settings: SettingsDep, caller: OptionalUserDep) -> SignedUrlResponse:
    """Mint a signed, expiring URL for a stored image.

    ### Errors

    * **401 Unauthorized** — the image is private and the caller is anonymous.
    * **403 Forbidden** — the image is private and the caller owes a terms-of-service acceptance.
    * **404 Not Found** — no live image for this key.
    """
    asset = await _get_live_asset(db, key)
    if asset is None:
        raise NotFoundError("Image not found.")
    ttl = settings.media_signed_url_ttl_seconds
    if asset.is_private:
        if caller is None:
            raise UnauthorizedError("Authentication required to mint a signed URL for a private image.")
        # Identity authorises here rather than decorating the response, so this carries the consent
        # gate `optional_current_user` skips — as does the fetch below, the other half of the pair.
        if await acceptance_required_for(db, caller.id):
            raise TermsAcceptanceRequiredError
        token, expires_at = sign_key(asset.key, ttl=ttl, user_id=caller.id)
    else:
        token, expires_at = sign_key(asset.key, ttl=ttl)
    return SignedUrlResponse(url=f"/api/v1/images/signed/{token}", expires_at=expires_at)


@router.get(
    "/signed/{token}",
    status_code=status.HTTP_200_OK,
    summary="Fetch an image via a signed URL",
    description=(
        "Stream an image addressed by a signed token (from `/images/signed-url`) rather than its bare key. "
        "The token is the sole path segment — the URL is self-contained, with no query string to assemble. "
        "A user-bound URL (minted for a private image) also requires the same authenticated user. Cached "
        "privately for the token's lifetime — unlike the immutable public GET, since the URL expires."
    ),
    response_class=StreamingResponse,
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_200_OK: {"content": {"image/*": {"schema": {"type": "string", "format": "binary"}}}},
        status.HTTP_401_UNAUTHORIZED: problem_response("User-bound signed URL — authentication required."),
        status.HTTP_403_FORBIDDEN: problem_response(
            "The signed URL is expired, invalid, or bound to another user; or the caller owes a terms acceptance."
        ),
        status.HTTP_404_NOT_FOUND: problem_response("No such image (or it was deleted)."),
    },
)
async def fetch_signed_image(token: str, db: DbSession, caller: OptionalUserDep) -> StreamingResponse:
    """Stream an image referenced by a signed, expiring token.

    ### Errors

    * **401 Unauthorized** — the URL is user-bound and the caller is anonymous.
    * **403 Forbidden** — the token is expired, malformed, forged, bound to another user, or the
      caller owes a terms-of-service acceptance.
    * **404 Not Found** — no live image for the token's key, or its blob is gone.
    """
    try:
        key, expires_at, user_id = verify_token(token)
    except BadSignature as exc:
        raise ForbiddenError("The signed URL is expired or invalid.") from exc
    if user_id is not None:
        if caller is None:
            raise UnauthorizedError("This signed URL requires authentication.")
        if caller.id != user_id:
            raise ForbiddenError("This signed URL is bound to another user.")
        if await acceptance_required_for(db, caller.id):
            raise TermsAcceptanceRequiredError
    asset = await _get_live_asset(db, key)
    if asset is None:
        raise NotFoundError("Image not found.")
    # Cache only until the token expires, never past it — a fetch late in the token's life
    # must not leave a browser copy usable after the URL itself has stopped working.
    max_age = max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
    return await _stream_asset(asset, db, cache_control=f"private, max-age={max_age}")


@router.get(
    "/{key:path}",
    status_code=status.HTTP_200_OK,
    summary="Fetch an image",
    description=(
        "Stream a stored image by its key. Public (no auth) — the key is an unguessable UUID, so the URL "
        "is the capability; this lets it be used directly in an `<img>` tag and cached by the browser. "
        "Serves public assets only: a private asset 404s here and is fetched via its signed URL."
    ),
    response_class=StreamingResponse,
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_200_OK: {"content": {"image/*": {"schema": {"type": "string", "format": "binary"}}}},
        status.HTTP_404_NOT_FOUND: problem_response("No such image (or it was deleted)."),
    },
)
async def get_image(key: str, db: DbSession) -> StreamingResponse:
    """Stream a stored image, cached long-term (the key is immutable).

    ### Errors

    * **404 Not Found** — no live image for this key, or its blob is gone.
    """
    asset = await _get_live_asset(db, key)
    # A private asset reads as absent here — 404, not 403, so the public GET leaks no existence.
    if asset is None or asset.is_private:
        raise NotFoundError("Image not found.")
    return await _stream_asset(asset, db, cache_control=_CACHE_ONE_YEAR)


@router.delete(
    "/{key:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an image",
    description="Delete an uploaded image (soft-deletes the row and removes the blob). Uploader-only.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: problem_response("Missing or invalid bearer token."),
        status.HTTP_403_FORBIDDEN: problem_response("Only the uploader may delete this image."),
        status.HTTP_404_NOT_FOUND: problem_response("No such image."),
    },
)
async def delete_image(key: str, caller: CurrentUserDep, db: DbSession) -> Response:
    """Delete an uploaded image — uploader-only.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — the caller is not the uploader.
    * **404 Not Found** — no live image for this key.
    """
    asset = await _get_live_asset(db, key)
    if asset is None:
        raise NotFoundError("Image not found.")
    if asset.created_by_id != caller.id:
        raise ForbiddenError("Only the uploader may delete this image.")
    # No back-reference check: media is a generic store with no FK from its consumers. A
    # still-referenced key (e.g. an evaluation cover) resolves to a clean 404 after delete —
    # renderers tolerate it like any external image URL.
    asset.soft_delete(caller.id)
    await db.commit()
    # Best-effort: the soft-delete is authoritative (GET 404s via live_select). A failed blob
    # delete just leaves a harmless orphan — never resurrect a committed delete as a 500. Log it
    # so the deferred orphan GC has a signal to reconcile against.
    try:
        await get_media_storage().delete(asset.key)
    except Exception as exc:
        logger.warning("images.blob_delete_failed", key=asset.key, error=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
