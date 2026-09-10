# Frontend — operator console for the AI red-teaming backend

A local console for the Python red-teaming backend — used by the team for local testing (a client of
the backend, not a hosted product). Coupling to the backend is **runtime-only**: the Vite dev proxy
forwards to it, and `make gen` regenerates the typed client from its live OpenAPI. We never read backend source.

## Running (host needs only Docker + make)

Everything runs in Docker; there is no Node on the host. Always go through `make`.

| Command | Does |
|---|---|
| `make setup` | First run: install deps into `./node_modules` |
| `make dev` | Start the Vite dev server at http://localhost:5173 |
| `make gen` | Regenerate the typed API client from the backend OpenAPI (backend must be up) |
| `make build` | Type-check + production bundle into `./dist` |
| `make lint` | ESLint **+** type-check (tsc) |
| `make eslint` | ESLint only, no type-check |
| `make typecheck` / `make format` | tsc only / Prettier (rewrites) |
| `make format-check` | Prettier in report-only mode (`--check`) |
| `make test` | Run tests (Vitest, single run). Narrow with `make test ARGS='src/features/x/y.test.tsx'` |
| `make sh` | Shell in the container |

`git push` runs `make eslint` and `make format-check` through the monorepo's prek pre-push hooks
whenever the push carries a frontend code or config change (`ts/tsx/js/jsx`, plus `css/html/json/yml`
for the format check; markdown sits outside the filters) — `tsc` and Vitest stay CI-only (too slow to
gate a push). Unformatted code is rejected locally and again by CI, so run `make format` before pushing.

Port is **5173** (override via `FE_PORT`).
Backend defaults to `host.docker.internal:8000`; override via `BACKEND_URL`. See `.env.example`.

## Stack

React 19 + Vite + TypeScript (strict) · Tailwind v4 + shadcn/ui · TanStack Query · React Router v7 ·
typed client via **openapi-typescript** (types) + **openapi-fetch** (~6 KB typed fetch).

Two heavy runtime dependencies, treated differently:

- **Recharts** for the metrics charts (~357 kB raw / ~103 kB gzip, 35 packages incl. a Redux
  runtime), imported behind `lazy()` so it never reaches the entry chunk; **keep it that way.**
- **`radix-ui`**: the umbrella package, pulling 61 `@radix-ui/*` plus 13 support packages
  (`@floating-ui/*`, `react-remove-scroll`, `aria-hidden`, `use-sidecar`). It backs the `ui/`
  primitives that need real interaction: `Select`, `DropdownMenu`, `ToggleGroup`, `Checkbox`,
  `Separator`.

  Measured against the commit that added it: **the entry chunk grew 0.56 kB raw / 0.38 kB gzip**,
  because Vite code-splits each primitive into its own chunk (`select`, `dropdown-menu`, `button`,
  `checkbox`, `toggle`) that loads with the routes using it. The cost is **total** JS, not startup:
  **+131 kB raw / +45 kB gzip (+10%)** across 11 more chunks. So the thing to protect is not the
  entry chunk here but the count: prefer an existing primitive over pulling a new Radix package.

## Conventions

- **Layout.** Feature-folders: one domain per `src/features/<domain>/` (components + query hooks colocated).
  Cross-cutting code in `src/lib/` (`api/`, `auth/`, `query.ts`), shared UI in `src/components/` (`ui/` = shadcn).
- **Typed client.** `src/lib/api/schema.d.ts` is **generated — never edit it.** Change the backend
  contract → `make gen` → fix the call sites the compiler flags (the [fe-sync-backend](.claude/skills/fe-sync-backend/SKILL.md) skill).
- **API calls.** Go through `apiClient` (`src/lib/api/client.ts`). Wrap results with `unwrap()`
  (`src/lib/api/fetcher.ts`) so failures throw a typed `ApiError`. Errors follow RFC 7807
  (`application/problem+json`); a 422 carries a Pydantic-shaped `errors[]`, and a 400 may carry the
  same `errors[]` shape (e.g. a password-policy rejection points at the `password` field).
- **Server state** lives in TanStack Query, never in component state. One query hook per resource.
- **Config** is via `VITE_*` env vars only; never hardcode hosts. `.env.example` is the contract.
- **shadcn components** are added with `npx shadcn@latest add <name>` (config in `components.json`);
  they land in `src/components/ui/` and are ours to edit. `--dry-run` first: `add` offers to
  overwrite, and several primitives here diverge from upstream on purpose (each divergence carries a
  comment saying why). Two names are already taken by files that are *not* the registry component:
  `ui/plain-select.tsx` (a bare styled `<select>`, deliberately not named `native-select`) and
  `ui/dropdown.tsx` (an absolutely-positioned disclosure panel, not `dropdown-menu`; the
  native-`<dialog>` primitives are `ui/drawer.tsx` and `ui/modal.tsx`).
- **`tsconfig.json` carries a `baseUrl`/`paths` block for the CLI only.** It reads
  `compilerOptions.paths` from that file and follows no project reference, so without it `@`
  resolves to a literal `@/` directory; `baseUrl` is required alongside `paths` or the CLI finds no
  components path at all. Inert for `tsc` (`files` is empty); the compiler's alias is in
  `tsconfig.app.json`, which drops `baseUrl` as deprecated in TS6.
- **Generic upstream skills stay user-level.** Install the shadcn skill with
  `npx skills add shadcn-ui/ui -g`; don't vendor it into this repository. Repo-local skills are for
  project-specific workflows such as `fe-add-view` and `fe-sync-backend`.
- **Shared composites** live in `src/components/shared/` — `DataTable`, `Pagination`, `PageHeader`,
  `FormField`, `DetailSkeleton`, plus three that own the responsive behaviour:
  - **`PageActions`**: a detail page's action row. `primary` is JSX (status-dependent clusters);
    `secondary` is data, rendered inline from `sm` and as a `DropdownMenu` below it. Don't hand-roll
    a row of buttons in a page header: a `shrink-0` row sets a min-width wider than a phone.
  - **`RowActions`**: a table row's action cell. Icon buttons from `lg`, one kebab below. Both the
    trigger and the items stop the click, because the row itself navigates.
  - **`FilterSelect`**: an "All …" list filter. Wraps `Select` with the empty-value sentinel and
    the width rules the toolbars depend on.

  Build list/detail views from these, never hand-roll a table. New domain view → the
  [fe-add-view](.claude/skills/fe-add-view/SKILL.md) skill.
- **Create/edit surface — page vs dialog.** Top-level entities (evaluation groups, AI models, users)
  get a full form *page* at `/new` and `/:id/edit`. Things that live *inside* another entity
  (scenarios, tasks, group members, model assignments, API keys) are created/edited in a *dialog*
  opened from the parent's detail page. Pick by ownership, not by size.
- **Navigation.** List → detail → nested edit. Detail/form pages carry a breadcrumb trail and a
  one-step ghost "Back" button; lists don't. Outside an evaluation group that trail is a single
  `Section / leaf` `<Breadcrumbs>` in the page body — the two-trail split below applies only inside
  a group. Resolve ids to names in the UI (`useUserLookup`, `useEvaluationLookup`) —
  never show a raw UUID.
- **Two trails inside an evaluation group.** Pages under a group carry an *identity* line
  (`Evaluation Groups / [Organization /] Group`, `<GroupIdentity>` from
  `features/evaluation-groups/group-trail.tsx`) and a *path* breadcrumb that opens with the group
  and ends at the current page — the split GitHub uses for `org / repo` and the file path. The
  identity line portals into the app header (`NavTrailSlotContext`) rather than sitting on the page,
  so it stays in one place across the subtree; the path breadcrumb stays in the page body. **The
  header copy renders from `lg` up, and below that the same line renders in the page instead** — the
  two are complementary, never both on screen. The handover is at `lg` rather than `md` because
  narrower than that the header has no width to give it: measured at 768px the organization and
  group each came out 0px wide, so the bar would announce a section root and nothing else.
  Rendered without the shell (every page test) there is no slot and the identity line renders in
  place at every width.
  One written exception: on the conversation pages the path trail opens with the group even though
  the identity line ends with it — that repetition is the approved GitHub split (`org / repo` in the
  bar, then the path starting at the repo), not drift.
  The path is rendered only where it carries a segment that is not already on the page: on the
  evaluation detail and create pages it would name just the group (which the identity line already names,
  and the group link in the meta line carries below `lg`) and the page itself (the heading), so
  there is none.
  On the group's own page the identity line **is** the `<h1>` (`<GroupIdentityHeading>`), so the name
  is not printed twice. The organization appears only when the group has one and the lookup can name
  it. Everywhere else a single `Section / leaf` breadcrumb is the whole hierarchy.
- **Loading / empty / error.** `DataTable` covers all three for lists (skeleton / `emptyLabel`
  +optional `emptyHint` + `emptyAction` / inline error). Detail pages use `DetailSkeleton` while
  pending and `humanizeError` for the inline error. Empty states say what the thing is and how to
  create it, not just "No X.".
- **Errors** surface as a toast (global TanStack Query `QueryCache`/`MutationCache` handlers) and
  inline in `DataTable`; don't add per-call error toasts. A 401 clears the token and routes to `/login`.
  A surface that renders *every* failure itself (a dialog showing the field reason and the curated
  copy) opts out with `meta: { suppressErrorToast: true }` on the mutation, so one failure is reported
  once — and then owes a fallback: if its own surface can be gone when the failure lands (unmounted by
  a route change, or closed by the parent), it toasts instead of writing into nothing.
- **Writes** go through a `useMutation` hook (one per action) that invalidates the affected query
  keys and toasts on success. Forms use React Hook Form + Zod; map field-level errors (a 422, or a
  400 carrying `errors[]`) onto fields with `applyApiError` (`src/lib/api/form.ts`). Confirmations
  and reason prompts use `ConfirmDialog`
  (`src/components/shared/confirm-dialog.tsx`).
- **Streaming** (chat replies) uses `streamSse` (`src/lib/api/stream.ts`) — raw fetch + an SSE
  reader, since openapi-fetch is request/response only. It parses `delta`/`done`/`error` events
  and mirrors the client's bearer header and 401 handling. After a stream ends, refetch the
  persisted transcript (invalidate the messages query) rather than trusting local state.
- **TypeScript is strict** (`strict` + `noUncheckedIndexedAccess`, `verbatimModuleSyntax`). Use
  `import type` for type-only imports. No enums (`erasableSyntaxOnly`).
- **Comments:** minimal, why-only.

## Docs

Setup, commands, and project layout: [README.md](README.md). The conventions above are the
contributor guide — read them before adding a view or touching the typed client.

## Deployment

Primarily a local testing console; the same image is also what a deployment serves. The
`Dockerfile` is multi-stage: `dev` (the bind-mount dev server `make dev` uses) and `prod` (the
vite build served by Caddy, no Node). It deploys **same-origin** with the backend — the box's
edge Caddy routes API path-prefixes to the backend and everything else to this SPA, so the client
calls `/api/...` with no CORS. The stack lives in `deploy/` (root `CLAUDE.md` has the map). CI
builds + Trivy-scans the prod image on every PR; CD publishes it to GHCR and rolls the box.
