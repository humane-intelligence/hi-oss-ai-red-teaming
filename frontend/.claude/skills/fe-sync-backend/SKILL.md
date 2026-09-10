---
name: fe-sync-backend
description: Use when the backend API contract changed (new/renamed/removed endpoints or fields) and the frontend's typed client must be resynced. Regenerates schema.d.ts from the live OpenAPI and fixes the call sites the compiler flags.
---

# Resync the typed API client with the backend

The client is generated from the backend's OpenAPI. When the contract changes, the generated
types drift from the code and the compiler points at every stale call site. This skill is that
loop: regenerate, read the diff, fix, verify.

## Preconditions

- The backend is running and reachable at `BACKEND_URL` (default `host.docker.internal:8000`).
- You are in the `frontend/` directory of the monorepo (where the `Makefile` lives).

## Steps

1. **Regenerate.**
   ```
   make gen
   ```
   This overwrites `src/lib/api/schema.d.ts` from `${OPENAPI_URL:-http://host.docker.internal:8000/openapi.json}`.

2. **Inspect the diff** — this tells you what actually changed:
   ```
   git diff --stat src/lib/api/schema.d.ts
   git diff src/lib/api/schema.d.ts
   ```
   Look for added/removed paths, renamed operations, changed request/response field names,
   newly required fields, changed enums.

3. **Find the breakage.**
   ```
   make typecheck
   ```
   Every stale `apiClient.GET/POST(...)` path, request body, or response field access will fail
   to type-check. Fix them in the relevant `src/features/<domain>/` and `src/lib/`.

4. **Adjust call sites and hooks** to the new shape. Common fixes: rename a field, handle a now-required
   parameter, update a query hook's return type usage, delete a hook for a removed endpoint.

5. **Verify.**
   ```
   make format && make lint
   ```
   Both clean before you commit (`lint` chains `tsc`). `make format` is not optional housekeeping —
   `git push` is gated on `prettier --check` (a prek pre-push hook, and again in CI). The regenerated
   `schema.d.ts` is prettier-ignored, so formatting never touches it.

## Rules

- **Never hand-edit `schema.d.ts`** — it's regenerated and your edits will be lost. Fix the call sites instead.
- Commit the regenerated `schema.d.ts` together with the call-site fixes in one commit
  (e.g. `Sync API client with backend <change>`), so the generated types and the code that uses
  them never diverge in history.
- If `make gen` can't reach the backend, start it first; do not fall back to hand-writing types.
