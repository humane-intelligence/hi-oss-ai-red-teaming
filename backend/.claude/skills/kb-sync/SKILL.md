---
name: kb-sync
description: Read before synchronizing this repo's knowledge base (the Obsidian-compatible vault in `docs/knowledge-base/`) with the code. Use when changes have landed on `main` and the vault may be stale, or when the user says "update / sync the knowledge base / vault / KB". Mirrors `main` only — never sync from a feature branch. Updates the working tree, then proposes a PR (body carries a table of the mirrored PRs) per repo convention.
---

# kb-sync — syncing the vault with the code

Scope: the `docs/knowledge-base/` vault — the knowledge base for this backend (notes in **English, GFM**: kebab-case, relative `[](…)` links, mermaid, frontmatter; layout: `basics/` / `components/` / `data-models/` / `flows/`). Entry point: `docs/knowledge-base/README.md`. The skill's job: keep the notes in sync with the code **after changes land on `main`**. You don't regenerate everything — you only touch the affected notes.

**Layout.** This repo is the `ai-red-teaming` monorepo; the backend lives under `backend/` and the vault at `backend/docs/knowledge-base/`, alongside the code it documents (`backend/app/`). **Run this skill from `backend/`** (cwd) — every relative path below (`docs/knowledge-base/`, `app/…`, the verification commands) resolves from there, and `git` runs against the monorepo (no `git -C`). Unlike the generated `docs/erd.md` / `docs/openapi.yaml` / `docs/permissions.md`, the vault is **hand-written** — there is no `make` target that regenerates it.

## Hard rule: sync ONLY from `main`

The vault reflects the **merged, canonical** state of the code — not WIP from a branch (a feature branch gets rebased, reworked, or may never land). Before you open anything:

```bash
git branch --show-current
```

Result != `main` → **STOP. Do not touch the vault.** Tell the user which branch they're on and that syncing happens only from `main`. End of task.

**No exceptions:**
- Not "just this one file, it'll change anyway".
- Not "the feature branch is almost merged".
- Not "the user probably wants it". If the user still tells you to sync from a specific branch, they must say so **explicitly** — then it's their deliberate decision, not this rule. Default: main only.

## Procedure

1. **Guard** — branch == `main` (above). Otherwise stop.
2. **Delta** — cursor in `docs/knowledge-base/.kb-sync-state.json` (`last_synced_commit`). Run from `backend/`; `--relative` strips the `backend/` prefix so paths read `app/…` (matching the table below) and frontend-only changes drop out — the vault documents the backend only:
   ```bash
   git rev-parse HEAD                          # where main is now
   git diff --stat --relative <last>..HEAD     # what changed (backend/ only, paths as app/…)
   git log --oneline <last>..HEAD              # why
   ```
   `<last> == HEAD` → "vault is up to date", done. No state file → see **Bootstrap**.
   > After a branch rebase/squash, `<last>` may not be an ancestor of main — the diff will show the same changes again. That's fine: you check the affected notes, and if they already match it's a no-op.

   **Roll call — walk the `#NN` list, don't map by theme.** Extract every PR number in the range up front and carry that list to the end: each number must land either in the PR table (step 8) or in the "Not synced" line with a reason. Grouping the delta by theme instead is how a PR goes missing — and how its work gets credited to the wrong PR.
   ```bash
   git log <last>..HEAD --oneline | grep -oE '#[0-9]+' | tr -d '#' | sort -un
   ```
   A number that reads as "CI/deploy, no vault impact" still gets its file list checked (`gh pr diff <n> --name-only`) before the write-off: the map below routes `Makefile`, `pyproject.toml` and deps to `basics/stack-and-tooling.md`, so *outside `app/`* is not the same as *no vault impact*.
3. **Map changed files → notes** per the table below. `.py`, migrations, config, `Makefile`, deps all count.
4. **Update the affected notes** — prose + mermaid + snippets (**real**, with `path` as inline code) + relative links. Keep the style of the existing notes: plain English, `tags` frontmatter, a `## Related` section. Code paths go as inline code (e.g. `app/core/...`), not links.
   - **New** model/component/integration → add a note in the right folder (`components/`/`data-models/`/`flows/`, kebab-case `.md`) and link it relatively from `README.md`, `basics/architecture-overview.md`, `basics/glossary.md`, and for a model also from `data-models/data-model-overview.md` (ERD).
   - **Removed** → delete the note and every relative link to it.
   - **Changed** → fix the content; check the snippets and diagrams still match the code.
   - Links: relative `[label](relpath.md)`, path computed relative to the source note's location (e.g. from `components/` to a model: `../data-models/x.md`).
5. **Verification** (from `backend/`):
   ```bash
   # code/mermaid fence parity (every ``` is paired)
   while IFS= read -r f; do n=$(grep -c '```' "$f"); [ $((n%2)) -ne 0 ] && echo "ODD: $f"; done < <(find docs/knowledge-base -name '*.md' -not -path '*/.obsidian/*')
   # dead relative links — every [](x.md) must point to an existing file
   python3 - <<'PY'
   import re
   from pathlib import Path
   V = Path("docs/knowledge-base")
   link = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
   for f in V.rglob("*.md"):
       if ".obsidian" in f.parts: continue
       for m in link.finditer(f.read_text(encoding="utf-8")):
           t = m.group(1).split("#", 1)[0]
           if t.startswith("http") or not t.endswith(".md"): continue
           if not (f.parent / t).resolve().exists(): print("DEAD:", f, "->", t)
   PY
   # GFM guard — no Obsidian [[wikilinks]] (the vault is GitHub-Flavored Markdown so it renders on GitHub; CLAUDE.md mentions the literal in inline code, hence the exclude)
   grep -rn '\[\[' docs/knowledge-base --include='*.md' | grep -v '/CLAUDE.md:' && echo 'WIKILINK FOUND — convert to relative [](…)' || echo 'no wikilinks (ok)'
   # Tracker guard — no tracker ids in note prose (the vault ships to readers who cannot resolve
   # them; the team's keys look like BES-123, hence the pattern)
   grep -rn 'BES-[0-9]' docs/knowledge-base --include='*.md' && echo 'TRACKER REF FOUND — drop it; provenance belongs in the commit/PR' || echo 'no tracker refs (ok)'
   ```
   Plus: prose in English (code/identifiers/paths in English).
6. **Save the cursor** — overwrite `docs/knowledge-base/.kb-sync-state.json`: `last_synced_commit` = `main` HEAD, `synced_at` = today's date. Keep `note` to one short line naming the range and the sync's shape; it is a cursor, not a changelog, and it carries no tracker ids.
7. **Don't commit silently.** Leave the updated notes + cursor as **working-tree changes** and summarize what you touched, then propose the PR (next step). Don't `git commit` / `push` without the user's ok.
8. **Propose a PR** — the skill's deliverable. kb-sync is a docs-only chore with no tracker id: branch `docs-kb-sync`, title `docs: sync knowledge base with <short theme>`.
   - Right under **Summary**, add a **Sync range** section stating the window this sync covers: previous cursor → new cursor, as dates and commits — e.g. `2026-06-25 (\`0211d72\`) → 2026-06-29 (\`d261935\`)`. Dates are the old vs new `synced_at` in `.kb-sync-state.json`; commits the old vs new `last_synced_commit`.
   - The body **must carry a table of the merged PRs this sync accounted for** (the ones that drove note changes), columns **PR** (linked title) and **Author**. Build the candidate list from the delta — GitHub squash/merge subjects carry `(#NN)`:
     ```bash
     git log <last>..HEAD --oneline | grep -oE '#[0-9]+' | tr -d '#' | sort -un | while read -r n; do
       gh pr view "$n" --json number,title,author,url --jq '
         "| [#\(.number) \(.title)](\(.url)) | @\(.author.login) |"'
     done
     ```
     Header `| PR | Author |`. **Keep only rows for PRs with vault impact.**
   - **Always also list the PRs in the delta that did *not* enter the sync, with the reason** — every `#NN` from the candidate list that isn't in the table must appear here, so the body accounts for the whole range, not just what changed. One-line "Not synced" note grouped by why, e.g.: `Not synced (no vault impact): test-only PRs (#118, #117, #106, #105), FE/deploy/CI (#107, #103), dependency bumps.`

## Map: code change → notes to touch

Note paths are relative to the vault root `docs/knowledge-base/`.

| Change (`app/…`) | Notes (under `docs/knowledge-base/`) |
|---|---|
| `app/core/ai_gateway/**` | `components/ai-gateway-overview.md` + sub-notes (`ai-gateway-dispatch`, `ai-gateway-litellm-provider`, `ai-gateway-message-types`, `ai-gateway-inference-parameters`, `ai-gateway-key-encryption`, `ai-gateway-error-taxonomy`), `data-models/ai-model.md`, `flows/flow-ai-model-registration-to-invocation.md` |
| `app/core/conversations/**`, `app/api/v1/chat.py`, `app/api/v1/conversation_groups.py` | `components/streaming-sse.md`, `components/conversations.md`, `components/conversation-groups.md`, `components/endpoint-post-chat-stream.md`, `data-models/conversation.md`, `data-models/conversation-group.md`, `flows/flow-ai-message-streaming-end-to-end.md` |
| `app/core/auth/**` (without `object_roles`) | `components/authentication.md`, `components/user-management.md`, `components/rbac-global-roles.md`, `data-models/user-and-role.md`, `data-models/invitation.md`, `flows/flow-request-authentication-and-authorization.md` |
| `app/core/auth/object_roles/**` | `components/object-roles-per-object-permissions.md`, `data-models/object-role-assignment.md` |
| `app/core/evaluations/**`, `app/api/v1/evaluation*`, scenarios/tasks | `components/evaluation-domain.md`, `data-models/{evaluation-group,evaluation,evaluation-ai-model,scenario,task}.md`, `flows/flow-evaluation-group-invitation.md` |
| `app/core/reviews/**`, `app/core/annotations/**` | `components/reviews-reviewer-verdicts.md`, `components/message-flags.md`, `data-models/review.md`, `data-models/message-flag.md` |
| migration in `alembic/versions/`, `app/models.py`, any `models.py` | `data-models/data-model-overview.md` (ERD), the model's note, `basics/glossary.md` |
| `app/api/**` (new/changed endpoint) | `components/api-overview-and-conventions.md`, the component's note |
| `app/core/config.py`, `.env.example` | `components/configuration-settings.md`, `flows/flow-config-env-to-settings.md` |
| `app/workers/**`, `app/core/email/**` | `components/celery-workers.md`, `components/email.md`, `data-models/support-tables-email-verification-reset.md` |
| `app/core/{database,middleware,error_handlers,bulk,pagination}.py` | `components/database-and-sessions.md`, `components/middleware-logging-and-request-cycle.md`, `components/error-handling-rfc-7807.md`, `components/pagination-bulk-and-soft-delete.md` |
| `pyproject.toml`, `Makefile`, `docker-compose.yml`, deps | `basics/stack-and-tooling.md` (and `basics/architecture-overview.md`/`configuration-settings.md` if relevant) |
| new bounded context `app/core/<x>/` | a new note in `components/` + links from `README.md`, `basics/architecture-overview.md`, `basics/glossary.md` |

Source of truth for architecture when mapping: the backend's `CLAUDE.md` (`backend/CLAUDE.md`, Architecture section) — not the thin monorepo-root `CLAUDE.md`.

## Bootstrap (no `.kb-sync-state.json`)

The vault was first built on 2026-06-16. The state file (`docs/knowledge-base/.kb-sync-state.json`) ships with the vault. If it ever disappears: set `last_synced_commit` to the commit whose state the vault actually reflects (don't guess; when in doubt, use `main` HEAD and run a full audit of the notes vs `CLAUDE.md`).

## Red flags — STOP

- You're on a branch != `main` and already opening a note.
- You're editing `docs/erd.md` / `docs/openapi.yaml` / `docs/permissions.md` — those are **generated** `make` artifacts, not the vault.
- You added a relative link to a note that doesn't exist (the dead-link check catches this).
- You left a `[[wikilink]]` instead of a relative `[](…)` link — the vault is GFM.
- You skip the link/fence verification "because it's a small change".
- You leave `.kb-sync-state.json` un-updated after editing.
- You `git commit` / `push` the vault changes **without the user's ok** — propose the PR, then act on approval.

## What you don't do

- Don't sync from a feature branch (see above).
- Don't regenerate the whole vault from scratch for a small change — only the affected notes.
- Don't edit the generated docs (`docs/erd.md`, `docs/openapi.yaml`, `docs/permissions.md`).
- Don't commit `.obsidian/workspace*.json` (it's in the vault's `.gitignore`).
