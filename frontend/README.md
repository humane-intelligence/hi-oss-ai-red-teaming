# red-team-console

[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](package.json)
[![TypeScript](https://img.shields.io/badge/TypeScript-strict-3178C6?logo=typescript&logoColor=white)](tsconfig.json)
[![Vite](https://img.shields.io/badge/Vite-8-646CFF?logo=vite&logoColor=white)](vite.config.ts)
[![Tailwind](https://img.shields.io/badge/Tailwind-v4-06B6D4?logo=tailwindcss&logoColor=white)](package.json)
[![Vitest](https://img.shields.io/badge/tests-vitest-6E9F18?logo=vitest&logoColor=white)](package.json)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](../LICENSE)

A local console for the AI red-teaming backend — used by the team to drive and test the backend
during development. It is a **client of that backend**: a thin web UI over its REST + SSE API. Its
primary home is a developer's machine; the same image is also what a deployment serves.

React 19 · Vite · TypeScript (strict) · Tailwind v4 + shadcn/ui · TanStack Query · React Router v7.
The typed API client is generated from the backend's live OpenAPI (openapi-typescript + openapi-fetch).

## Prerequisites

- **Docker** and **make**. That's it — the whole toolchain runs in a container, there is **no Node on the host**.
- A **running backend API** (the red-teaming backend), reachable on `http://localhost:8000` by default.
  This UI does nothing useful without it. Start the backend first.

## Quick start

```bash
make setup                    # first run only: installs node_modules (and writes .env from the example)
make dev                      # Vite dev server → http://localhost:5173
```

`node_modules` is bind-mounted from the host rather than baked into the image, so `make setup` is what
puts the dependencies there — `make dev` alone on a fresh clone starts a container with nothing to run.
Re-run it after any `package.json` change.

Open http://localhost:5173 and log in with credentials from your backend.

If your backend runs somewhere else, set `BACKEND_URL` in `.env` (the dev proxy target) and restart `make dev`.

Testing through a tunnel (e.g. ngrok, for OAuth callbacks that need a public HTTPS URL)? Vite rejects
unrecognized `Host` headers by default — add the tunnel hostname to `ALLOWED_HOSTS` in `.env`
(comma-separated for more than one) and restart `make dev`.

## Regenerating the API client

The typed client (`src/lib/api/schema.d.ts`) is **generated — never edit it by hand**. When the
backend contract changes, regenerate it (backend must be up) and fix the call sites the compiler flags:

```bash
make gen          # pulls the live OpenAPI and rewrites src/lib/api/schema.d.ts
make typecheck    # the compiler points at every stale call site
```

## Commands

Everything goes through `make` (it wraps Docker — you never call `npm` on the host).

| Command | Does |
|---|---|
| `make dev` | Start the Vite dev server at http://localhost:5173 |
| `make gen` | Regenerate the typed API client from the backend OpenAPI (**backend must be up**) |
| `make build` | Type-check + production bundle into `./dist` |
| `make lint` / `make eslint` | ESLint **+** `tsc` / ESLint only |
| `make typecheck` / `make format` / `make format-check` | `tsc` / Prettier (rewrites) / Prettier (`--check`) |
| `make test` | Run the test suite (Vitest, single run) |
| `make setup` | Install dependencies into `./node_modules` (first run, and after a `package.json` change) |
| `make down` | Stop and remove the containers |
| `make sh` | Open a shell in the container |

Override the dev-server port with `FE_PORT` (default `5173`). See `.env.example` for all variables.

## Project layout

```
src/
  features/<domain>/   one folder per domain — components + TanStack Query hooks colocated
  lib/                 cross-cutting: api/ (typed client, fetcher), auth/, query.ts
  components/          shared UI — ui/ (shadcn) + shared/ (DataTable, Pagination, …)
  test/                Vitest + Testing Library + MSW setup
```

The repo conventions (how to add a view, the page-vs-dialog rule, error/loading/empty handling,
the typed-client workflow) live in [CLAUDE.md](CLAUDE.md) — read it before adding a feature.

## Contributing

- Branch off `main`. Commit subjects are one line saying what changed (the team prefixes its tracker id); `main` squash-merges every PR, so the PR title is what lands in the history.
- Run `make lint`, `make test`, and `make build` before pushing — CI runs them as independent checks on every PR and must be green to merge.
- A push carrying frontend changes is gated locally by the prek pre-push hooks: `make eslint` and `make format-check`. Run `make format` first if the formatter has anything to say; `tsc` and Vitest stay CI-only.
- Don't hand-edit generated files (`src/lib/api/schema.d.ts`); change the backend and `make gen`.

## Deployment

The console is built and shipped as well as run locally. The `Dockerfile` is multi-stage: `dev` is the
bind-mount dev server `make dev` uses, and `prod` is the Vite build served by Caddy with no Node
runtime. CI builds and Trivy-scans the `prod` image on every PR; CD publishes it to GHCR and rolls the
deployment target, where it is served **same-origin** with the backend — the edge Caddy routes the API path
prefixes to the backend and everything else here, so the client calls `/api/...` with no CORS. The
stack lives in [`deploy/`](../deploy/README.md).

## Status / known gaps

Deliberately scoped to being an operator console, and not hardened beyond that. Worth knowing:

- Some navigation for overlapping personas (red-teamer ⇄ reviewer) is still pending a product decision.
- It is a client of one backend at a time — no multi-environment switcher; point `BACKEND_URL` at the
  one you mean and restart.
