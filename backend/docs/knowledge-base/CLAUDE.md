# knowledge-base — Obsidian-compatible vault for the backend

This directory is the knowledge base documenting this backend. It lives in the `ai-red-teaming`
monorepo at `backend/docs/knowledge-base/`, alongside the code (`backend/app/`) it documents. Notes are written in plain English
as GitHub-Flavored Markdown, so they render on GitHub and in VS Code and still open as an Obsidian
vault. Entry note: `README.md` at the vault root.

## Conventions

- **Format**: GitHub-Flavored Markdown — kebab-case filenames, relative `[](…)` links (no
  `[[wikilinks]]`), mermaid for diagrams, YAML frontmatter (`tags`, `aliases`).
- **Language**: note prose in English; code identifiers, paths, and frontmatter keys stay in
  English. Code paths go as inline code (e.g. `app/core/ai_gateway/`), not as links — they point
  into the source tree, not to other vault notes.
- **No tracker references in note prose.** A note describes how the system works *now*, for a reader
  who cannot resolve a tracker id — one in a heading or a sentence is a dangling pointer to them, and
  headings carrying one also produce anchors that break when the reference is later removed. Provenance
  belongs in the commit and the PR. Write "the create is posted to `/scenarios/…`", never
  "since ABC-123 the create is posted to …". (A stand-in key on purpose: a real one would trip
  the tracker guard in `.claude/skills/kb-sync/SKILL.md`, which no longer excludes this file.)
- **Layout**: `README.md` (entry note) at the root; everything else in `basics/` / `components/` / `data-models/` / `flows/`.
- **Obsidian**: `.obsidian/` is committed (vault config, set to relative Markdown links);
  per-machine UI state (`.obsidian/workspace*.json`, cache, `.trash/`) is gitignored.

## Commits

The vault lives in this repo, so it follows this repo's convention: a one-line imperative subject on a
topic branch + PR. One logical change per commit; the body explains *why*, not *what*.

## kb-sync

The `kb-sync` skill (`backend/.claude/skills/kb-sync/`) keeps the notes in sync with the code after changes
land on `main`. Run it from `backend/` — `git` runs against the monorepo (no `git -C`). Sync from
`main` only, never a feature branch. The sync cursor lives in
`docs/knowledge-base/.kb-sync-state.json` (relative to `backend/`).
