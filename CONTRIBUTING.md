# Contributing

Thanks for taking the time to contribute! This is a monorepo — a Python/FastAPI backend and a
React/Vite operator console — and the two are coupled by the OpenAPI contract, which shapes most of
the workflow below.

## Getting set up

The host needs Docker, make, and [uv](https://docs.astral.sh/uv/) — the backend's dependencies, git
hooks and linters run on the host through uv, which also provisions Python for you. No Node: the
frontend toolchain runs in its container.

```bash
make setup   # deps, git hooks, a migrated database, a seeded local dataset, both .env files
make dev     # the whole stack, foreground
```

Seeded logins are one per role (`admin@test.com`, `owner@test.com`, `redteamer@test.com`,
`annotator@test.com`, `viewer@test.com`), all with password `password123`. The console is at
<http://localhost:5173>, the API docs at <http://localhost:8000/docs>.

Each subsystem has its own README and its own Makefile; `make be-<target>` and `make fe-<target>`
delegate from the root. Read [`backend/README.md`](backend/README.md) or
[`frontend/README.md`](frontend/README.md) before working in either.

## The contract loop

`backend/docs/openapi.yaml` is the single source of truth for the API. After changing anything the
schema sees — a route, a request or response model, a status code:

```bash
make be-openapidump   # refresh the committed spec
make fe-gen           # regenerate the typed client from the live backend (it must be up)
```

Then fix the call sites the TypeScript compiler flags. CI has a drift gate: a spec change without the
regenerated client fails. `frontend/src/lib/api/schema.d.ts` is generated — never edit it by hand.

## Before you push

```bash
make lint    # both subsystems: ruff, bandit, ty, deptry, ESLint, tsc
make test    # both test suites
make check   # lint + test
```

Git hooks (`prek`, installed by `make setup`) run the backend lint set plus the frontend's ESLint and
`prettier --check` at push time. Markdown is outside the formatter's filters; **run `make fe-format`
before pushing a frontend change**, or the push hook will reject it.

## Pull requests

- **Sign off every commit** with `git commit -s`. What that certifies, and how to fix a
  branch you forgot it on, is under [Licensing and sign-off](#licensing-and-sign-off).
- **Branch off `main`.** Commit subjects are one line saying what changed. `main` squash-merges
  every PR, so the
  PR title is what lands in the history — no `Co-Authored-By` trailers, though the DCO
  sign-off below is required on every commit.
- **Keep production diffs around 400 lines.** Tests, docs, and generated artifacts (`uv.lock`, the
  OpenAPI spec, the ERD, the permissions matrix, Alembic autogenerate output, the typed client) do not
  count. It is a guideline with slack, not a CI check — when a change clearly overshoots, split it into
  stacked PRs by layer rather than by file.
- **Update the docs in the same diff.** Changing the schema, the roles, or the data model means
  running the matching dump target (`make be-openapidump` / `be-erddump` / `be-permissionsdump`) and
  committing the result; CI fails on drift. The knowledge base under `backend/docs/knowledge-base/` is
  the exception — it mirrors merged `main` and is synced afterwards, in its own PR.
- **Say how you verified it.** The PR template asks for this because it is the part reviewers cannot
  reconstruct: what you ran, what it printed, and what you could not check.

## Licensing and sign-off

This project is licensed under [Apache 2.0](LICENSE), and contributions come in under the same
licence. You keep the copyright in what you write; submitting it licenses it to the project on those
terms. There is no CLA to sign and no copyright to assign.

Additionally, the documentation contained in Markdown files in this repo is under a [CC-By-4.0 license](LICENSE-docs). Accordingly, contributions to those files come under CC-By-4.0.

The maintainers of this repo request that every commit carries a Developer Certificate of Origin sign-off. The [DCO](DCO)
is a short statement that written for the patch. Commits should only be made if they are allowed to be submitted under the repo
licenses. DCOs are added per commit by adding a `Signed-off-by` trailer, which git can write:

```bash
git commit -s -m "Reject an export request for a sealed conversation"
```

The trailer has to match the author identity on the commit itself:

```
Signed-off-by: Jane Doe <jane@example.com>
```

Set `user.name` and `user.email` in your git config once and `-s` gets it right every time. If you
forgot on the last commit, `git commit --amend -s --no-edit` fixes it; across a whole branch,
`git rebase --signoff main` and force-push. Every commit in the PR needs one except merge commits,
which are exempt — GitHub's own *Update branch* button produces one, unsigned. The `DCO sign-off`
check lists any that are missing, and it reads the commits as pushed, not the single commit that
squash-merging lands on `main`.

Sign-off is a statement about provenance, not identity: GPG or SSH commit signatures are welcome but
not required.

## Tests

pytest for the backend, Vitest for the frontend. The backend suite mirrors the source tree
(`tests/api/` ↔ `app/api/`) and the tier is a marker, not a directory — `unit`, `integration`, `e2e`.
Narrow a run with `make be-test ARGS='-m unit'` or `make fe-test ARGS='src/features/x/y.test.tsx'`.

There is also a hand-authored Postman suite covering the API as business processes — see
[`backend/docs/postman/README.md`](backend/docs/postman/README.md). If you change a request contract,
the collection that walks it should change with you.

## Reporting things

Bugs and feature requests go through the issue templates. **Security problems do not** — see
[SECURITY.md](SECURITY.md) for the private path.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
