---
tags: [model, settings, licenses, auth]
aliases: [PlatformSettings, platform_settings, platform-settings, singleton, invite_only, password policy]
---

# PlatformSettings

A one-row table holding platform-wide policy — the **django-solo analogue**. Four families of knobs live on it today: data licensing, registration policy, password policy, and password-reset throttling.

Table `platform_settings`, model `app/core/platform_settings/models.py`, accessor/updater `app/core/platform_settings/service.py`, endpoints `app/api/v1/platform_settings.py`.

> **Moved out of `licenses/`.** The model and its service used to live inside `app/core/licenses/`, back when the only knob was the default data license. They are now their own bounded context — `app/core/platform_settings/` — because the knobs stopped being about licensing.

## The single-row invariant

There is no `organization`-style CRUD — exactly one row may exist, pinned by a fixed sentinel primary key:

```python
# app/core/platform_settings/models.py
PLATFORM_SETTINGS_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

class PlatformSettings(BaseModel, table=True):
    __tablename__ = "platform_settings"
    default_license_id: uuid.UUID = Field(foreign_key="data_licenses.id", nullable=False, index=True)
    invite_only: bool = Field(default=False, ...)
    ...
```

The service reads/writes at this id exclusively, so a second row can never exist. The singleton has **no soft-delete semantics** — the row, when present, is authoritative.

## The knobs

| Column | Bounds (PATCH) | Meaning |
|---|---|---|
| `default_license_id` | a live licence, not `No license` | platform default [data licence](data-license.md); the broadest layer of the cascade |
| `invite_only` | — | disables open self-signup; see below |
| `email_verification_ttl_hours` | 1 – 8760 | lifetime of an email-verification link |
| `password_min_length` | 8 – 128 | length floor for a newly set password |
| `password_require_uppercase` / `_digit` / `_symbol` | — | character-class requirements, all defaulting to **off** (the shipped behaviour) |
| `password_reset_cooldown_seconds` | 0 – 3600 (`0` disables) | minimum gap between an account's own reset requests |
| `password_reset_max_per_day` | 1 – 100 | daily cap on an account's reset tokens |

The knob list is **derived from the model**, not hand-maintained:

```python
# app/core/platform_settings/service.py
SETTINGS_KNOBS: tuple[str, ...] = tuple(
    name for name in PlatformSettings.model_fields if name not in BaseModel.model_fields
)
```

Both the upsert's merge base and the route's audit snapshot iterate `SETTINGS_KNOBS`, so a knob added to the model cannot silently vanish from either — no "materialized at its DDL default on a sibling's first write", no missing audit diff.

> The password *defaults* here repeat the floor `app/core/auth/password_policy.py` owns. That is deliberate: `PasswordPolicy.from_settings` takes a settings row, so importing the policy module here would close an import cycle. A test (`test_the_knob_default_tracks_the_schema_floor`) holds the two together instead of proximity.

### Invite-only

`invite_only` disables open self-signup as a blanket, **enumeration-safe 403** in `register_user`, and refuses OIDC first-login provisioning. Existing accounts keep logging in; invited/pending accounts still activate. See [Authentication](../components/authentication.md).

### Password-reset throttling

The window is per account and measured against **that account's own reset tokens**, so the limit bounds per-inbox volume, not aggregate outbound (there is no edge limiter). An **admin-triggered** reset is not throttled at all — it ignores both the cooldown and the cap — **but its sends count towards them**. Enough of them silently disable that account's own "forgot password" for the rest of the window: deliberate (same inbox, same flood), and separating the two would need a source column on `password_reset_tokens`.

## Lazy materialization (a read never persists)

`get_platform_settings` returns the persisted row, or a **transient instance** seeded from the shipped defaults when none exists:

```python
# app/core/platform_settings/service.py
row = await session.get(PlatformSettings, PLATFORM_SETTINGS_ID)
if row is not None:
    return row
settings = get_settings()
return PlatformSettings(
    id=PLATFORM_SETTINGS_ID,
    default_license_id=curated_license_id(settings.platform_default_data_license),
    email_verification_ttl_hours=settings.email_verification_ttl_hours,
)
```

The transient default points at the **deterministic** curated row id (`curated_license_id(spdx) = uuid5(...)`), so it resolves without a DB lookup and without env-specific UUIDs.

`get_db` doesn't commit read handlers, so lazily inserting on a GET would only roll back. A transient (never-overridden) instance has no `updated_at`, so the response surfaces `updated_at: null` to signal "still the shipped defaults".

No process-local cache: it couldn't be invalidated across worker processes after a PATCH elsewhere. The cost is one indexed PK lookup per request (django-solo does the same).

## The upsert

`update_platform_settings(session, **changes)` materializes the row on the first *effective* write, as an atomic `INSERT ... ON CONFLICT DO UPDATE` on the sentinel id:

- the **INSERT arm** carries every knob at its current *effective* value merged with `changes` — so a first write of one knob materializes the others at their effective values, not DDL defaults;
- the **DO UPDATE arm** writes only `changes`, so concurrent PATCHes of different knobs can't clobber each other;
- `updated_at` is set explicitly — a Core upsert bypasses the ORM `onupdate` — and the row is `refresh`ed, because `RETURNING` won't overwrite an identity-map-pinned instance.

It does **not** commit: the caller owns the boundary and validates licence refs first.

## Endpoints

| Method + path | Permission | Notes |
|---|---|---|
| `GET /api/v1/platform-settings` | `platform_settings:read` (admin) | every knob; `updated_at: null` before any override |
| `PATCH /api/v1/platform-settings` | `platform_settings:update` (admin) | any subset; omitted fields unchanged |
| `GET /api/v1/platform-settings/public` | **none — unauthenticated** | `signup_enabled` (the `invite_only` inverse) + the nested `password_policy` |

The public route backs the login/registration screens. Disclosure is deliberately minimal and deliberately *not* an enumeration surface: `signup_enabled` is derivable by attempting a registration anyway, and the password policy is revealed by one rejected attempt — while every set-password screen here is anonymous (register, invitation accept, reset confirm), so without it the rules could only be delivered as an error. It carries `Cache-Control: public, max-age=30`, because it is anonymous, hit by every login-screen load, and the app has no rate-limit middleware; a 30s-stale flag is harmless since the register endpoint enforces the mode regardless.

The PATCH:

- rejects the **`No license`** sentinel as the platform default (**400**) — it is a live curated row, so the generic licence-ref guard would pass it, but as the default it unlicenses every inheriting group;
- validates any other requested `default_license_id` via `validate_license_ref` (400 if not live);
- on the row-materializing **first** write with no licence change, validates the *shipped* default too — otherwise an unsynced catalog (`make synclicenses` not run) surfaces as a raw `IntegrityError` 500 instead of a legible 400;
- audits only the **actually-changed** fields (`changed_fields` over two `SETTINGS_KNOBS` snapshots), snapshotting *before* the upsert because the refresh mutates the pinned row in place;
- a **no-op PATCH writes nothing**: no row materialization, no audit row, and `updated_at` keeps signalling "shipped defaults".

## Columns

Inherits `BaseModel` (`id`, `created_at`, `updated_at`, `deleted_at`, `deleted_by_id`), of which only `updated_at` carries meaning here.

`default_license_id` is a NOT NULL FK with **no `ON DELETE`** — the default must always resolve, and deleting the referenced licence is refused by the licence service (409). `get_default_license` loads it **by PK regardless of `deleted_at`**: the default is a curated row `sync_licenses` keeps live, and lineage doesn't lapse. A missing row is an invariant break (`RuntimeError`: run `synclicenses`).

There is **no `server_default`** on it: the shipped default resolves from `Settings.platform_default_data_license` (env) to the matching curated row at write time, not baked into DDL, so it can change without a migration. `Settings` refuses at boot if that SPDX id is not a *selectable* catalog entry — which excludes the `No license` sentinel.

## Related

- [Data licensing & platform settings](../components/licenses.md) — the catalog, the cascade, and the licence API
- [DataLicense](data-license.md) — the row `default_license_id` points at
- [Authentication (auth)](../components/authentication.md) — invite-only, the password policy, verification TTL, reset throttling
- [User management](../components/user-management.md) — the admin-triggered reset that bypasses the throttle
- [Evaluation](evaluation.md) / [EvaluationGroup](evaluation-group.md) — the `data_license_id` overrides above this default
- [Configuration (Settings)](../components/configuration-settings.md) — the env seeds
- [RBAC - global roles](../components/rbac-global-roles.md) — the admin-only `platform_settings:read|update`
- [Audit log](../components/audit-log.md) — `platform_settings.update`
- [Data model overview](data-model-overview.md) — the full ERD
