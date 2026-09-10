---
tags: [component, media, images, storage]
aliases: [Media, Images, Image upload, Signed URLs, media storage]
---

# Media (image upload & serving)

Generic image upload + serving for covers, avatars, icons and chat attachments, behind a swappable storage backend. A caller uploads an image, gets back a `key`, and the bytes are served straight from a proxy GET. A signed-URL pair layers time-limited access on top of the same proxy, and an upload can be **private** — then it serves via user-bound signed URLs only. This is a self-contained bounded context, grown to cover private assets, the signed-token redesign and an orphan GC.

Code: `app/core/media/` (logic) + `app/api/v1/images.py` (HTTP, mounted under `/api/v1/images`, tag `images`). Table: [MediaAsset](../data-models/media-asset.md).

## Why it exists

The product needs to attach images to things (an evaluation cover, a user avatar, a message attachment for a vision model). Rather than couple each consumer to S3, uploads go through one generic media store: consumers keep only an opaque `key`, the store holds the bytes, and swapping filesystem ↔ S3 is a config switch. A **public** asset (the default) is readable by anyone — the unguessable UUID key is the capability, so it drops straight into `<img src>`. A **private** asset (upload with `is_private=true` — immutable) reads as 404 on the bare-key GET and serves exclusively through signed URLs bound to the requesting user; chat attachments are required to be private, so a red-team prompt image never leaks through a shareable key.

## Storage port (local / s3)

A byte-oriented port `MediaStorage` (`app/core/media/storage/base.py`) — an image is a single ≤20 MB blob, so the port takes/returns whole `bytes` (contrast the export port, which streams text):

```python
async def save(self, key: str, data: bytes, *, content_type: str) -> str
def exists(self, key: str) -> bool          # sync
def open(self, key: str) -> Iterator[bytes] # sync; FileNotFoundError if gone
async def delete(self, key: str) -> None    # idempotent
```

`get_media_storage()` (`storage/__init__.py`) builds a fresh adapter per call, switched by `Settings.media_storage_backend`:

| Backend | Where bytes live | Notes |
|---|---|---|
| `local` (default) | files under `media_storage_dir` | `_resolve(key)` re-resolves against the base dir and rejects path-traversal escapes; blocking FS calls off-loaded via `anyio.to_thread` |
| `s3` | `put_object` under `media_s3_prefix` | creds from the default boto3 chain, region reuses `AWS_REGION`; single PUT (no multipart); missing-object `ClientError` → `FileNotFoundError` to honour the port |

`open` is **sync and eager** — it acquires the handle before returning, so a vanished blob raises `FileNotFoundError` *before* the 200 + headers commit (a clean 404, not a truncated body). A misconfigured `s3` backend (no bucket) fails at **startup** via a `Settings` validator, not on the first upload.

## The upload pipeline (Pillow)

`process_image` (`app/core/media/services/images.py`), run off the event loop via `anyio.to_thread`:

1. `Image.open(...)` — the format is sniffed from the **bytes**, never the request `Content-Type`. Unreadable → 400.
2. Only JPEG / PNG / WebP allowed (MPO — multi-picture JPEG from phones — is folded to JPEG); anything else → 400.
3. `ImageOps.exif_transpose` normalises orientation before resize.
4. `thumbnail((1200, 1200))` downscales (only if larger), preserving aspect ratio and the **source format** (PNG keeps alpha, JPEG stays JPEG).
5. Re-save; a corrupt/truncated body surfaces here as 400, not 500.

The read is capped at 20 MB **mid-stream** (`_read_capped`), so an oversized upload is rejected before it is fully buffered. The row's `id` is minted up front and embedded in the storage key, so key ↔ row is 1:1.

## Signed URLs — backend-agnostic, not S3-presign

A signed-URL pair (`signing.py`, `itsdangerous.URLSafeTimedSerializer`) layers time-limited access on the same proxy GET — deliberately **not** an S3-presigned URL, so behaviour is identical for local and S3:

- `sign_key(key, *, ttl, user_id=None)` wraps a structured payload `{"k": key, "exp": <unix ts>}` signed with `media_url_signing_secret` (falling back to `session_jwt_secret`) under a fixed `media-url` salt. The **absolute expiry is embedded at mint** — the TTL in force when the token was issued is what verification enforces, each mint site may pick its own TTL (`sign_key(ttl=...)`; the endpoint uses `media_signed_url_ttl_seconds`, default 15 min), and a later settings change never re-times an outstanding token. For a **private** asset the payload additionally carries the requesting user's id (`"u"`).
- `verify_token(token)` returns `(key, expires_at, user_id | None)` → `SignatureExpired` / `BadSignature`; a valid signature over an unexpected payload shape (e.g. a pre-`exp` token) is re-raised as `BadSignature` (stays a 403, never a 500).

The token is **signed, not encrypted** — its payload base64-decodes in the clear, so nothing secret may ride it. The `u` claim qualifies: it is the requester's own non-secret id, readable only by whoever already holds the URL.

**Private assets change who can mint and fetch.** Minting for a private asset requires authentication and binds the token to the requesting user; the fetch route then rejects anonymous (401) or foreign (403) callers. A public asset's mint/fetch stays anonymous — so `<img src>` keeps working — and its token grants nothing the bare key doesn't.

## Attachments feed the model as data URLs

Message attachments ([Message](../data-models/message.md), the `message_images` link rows) are re-read from this store on every (re)dispatch: `read_data_url(key)` loads the blob and encodes it as a base64 `data:` URI (content-type derived from the key extension), and `cached_data_url(key)` fronts it with a per-process, byte-bounded (32 MiB), 15-min-TTL cache — history rebuild re-reads every prior image each turn, so this caps it at ~one storage read per key per process. Keys are immutable (they embed the asset id), so a cache entry is always correct; the TTL only bounds how long a since-deleted blob can outlive its storage in memory. `get_assets_by_keys` is the batch lookup the write-path validates attachments with. See [Message persistence (write-path)](message-persistence-write-path.md).

## Endpoints

`/api/v1/images`, tag `images`. Note which routes are unauthenticated.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/images` | **auth-only** (any user) | 201 + `Location`; ≤20 MB, JPEG/PNG/WebP; body flag `is_private` (default false, **immutable**); returns `MediaAssetResponse` |
| GET | `/images/signed-url?key=` | **public** for a public asset; **auth** required for a private one (401/403) | mint a signed URL (`SignedUrlResponse`); 404 if the asset is gone |
| GET | `/images/signed/{token}` | **public**; a private asset's token demands its bound user (anonymous → 401, foreign → 403) | stream by token; expired/forged → 403; `Cache-Control: private, max-age=<until-expiry>` |
| GET | `/images/{key:path}` | **public** | stream by key — **public assets only** (a private asset reads 404 here); `Cache-Control: public, max-age=31536000, immutable`; 404 if gone |
| DELETE | `/images/{key:path}` | **uploader-only** | 204 soft-delete + best-effort blob delete; 403 if not the uploader |

Two design points to remember:

- **Route ordering is load-bearing.** `/signed-url` and `/signed/{token}` are declared **before** the greedy `/{key:path}` catch-all — otherwise the path converter would swallow them. (The token moved from a `?token=` query param into the path.)
- **Serving releases the DB connection before streaming.** `_stream_asset` opens the blob eagerly, then `await db.close()` frees the pooled connection before the (possibly slow) stream — like the export download and chat/stream endpoints. Always sets `X-Content-Type-Options: nosniff`.

## Permissions — none (auth-only)

There is **no dedicated permission**. Upload needs only authentication; public-asset GETs are fully public (the key is the capability); a private asset is gated by the user-bound token instead; delete is uploader-only (`created_by_id == caller`). Rationale: cover/avatar images are non-sensitive, and a UUID key drops straight into an `<img>` tag and caches in the browser — while anything sensitive (chat attachments) rides the private mode.

## Orphan GC — the media reaper

Media is decoupled from its consumers by design (the referencing entity stores the opaque key, no FK), so nothing cleans up an asset whose last reference went away. `reap_orphaned_media` (`app/core/media/tasks.py`, Celery beat every `MEDIA_ORPHAN_REAP_INTERVAL_SECONDS`, default daily) soft-deletes live assets older than `MEDIA_ORPHAN_GRACE_HOURS` (default 7 days) that no known consumer references, then best-effort deletes the blobs (the soft-delete is authoritative — reads already 404, so a failed blob delete leaves only a harmless file).

Two invariants to remember:

- **References count regardless of the referencing row's liveness** — flags/reviews can still reach a soft-deleted conversation's transcript, so its images survive the sweep.
- **The consumer inventory is a convention, not a schema.** Today: `Evaluation.cover_image` + `MessageImage.image_key`. Any new column holding media keys MUST be added to the reaper's query — otherwise it deletes live images.

Accepted race: a reference committed mid-sweep is invisible to the reaper's SELECT — a seconds-wide window against a daily tick, and history degrades gracefully (a missing attachment is dropped with a warning, not an error). See [Celery workers](celery-workers.md).

## Config

`MEDIA_STORAGE_BACKEND` (`local`/`s3`), `MEDIA_STORAGE_DIR`, `MEDIA_S3_BUCKET` (+ startup validator), `MEDIA_S3_PREFIX`, `MEDIA_SIGNED_URL_TTL_SECONDS`, `MEDIA_URL_SIGNING_SECRET` (≥32, falls back to `SESSION_JWT_SECRET`), `MEDIA_ORPHAN_GRACE_HOURS`, `MEDIA_ORPHAN_REAP_INTERVAL_SECONDS`. See [Configuration (Settings)](configuration-settings.md).

## Related

- [MediaAsset](../data-models/media-asset.md) — the `media_assets` table
- [Evaluation](../data-models/evaluation.md) — `cover_image` stores a media `key` (no FK)
- [Message](../data-models/message.md) — `message_images` attachment rows store media keys (no FK)
- [Message persistence (write-path)](message-persistence-write-path.md) — attachment validation + data-URL history rebuild
- [Celery workers](celery-workers.md) — the beat-scheduled orphan reaper
- [Configuration (Settings)](configuration-settings.md) — the `MEDIA_*` settings
- [Exports (CSV / JSON)](exports.md) — the sibling storage port (text streaming, for comparison)
- [API - overview and conventions](api-overview-and-conventions.md)
- [Data model overview](../data-models/data-model-overview.md)
