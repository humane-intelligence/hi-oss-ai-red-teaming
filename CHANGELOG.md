# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — while the major version is 0 the API may
change between minor versions.

## Unreleased

### Added

- `GET /evaluation-groups/{group_id}/assignable-roles`: the roles a member of the group can be
  given, for whoever manages its members (the in-group owner or an admin), without the global
  `roles:read` the role catalog needs. Each entry is the role's id, name, label and self-join
  default flag, not its permissions. The console's Add members, Edit roles and Invite by email
  dialogs read it, so an in-group owner who is not a global admin has those three actions again.

- Conversation labels (called custom tags in the console): an evaluation defines up to 20
  categories under `/evaluations/{evaluation_id}/label-categories`, each with up to 50 allowed
  values or none. A conversation group carries `labels` as `{category_id: [values]}` on its create
  (`POST /scenarios/{scenario_id}/conversation-groups`) and PATCH, several values per category; a
  category without values is picked on its own, by an empty list or a blank entry, and reads back
  with `value: null`. A category left out is cleared. Labels are archive metadata, never sent to
  the model; they appear on group reads and in the `Labels` export column as
  `{category: [values]}`. Duplicating an evaluation, or a group with its children, copies the
  categories. Migration `51936c9ff5f4` lets a category hold several values.

- `turn_count` on conversation responses, including the member conversations embedded in a
  conversation group: the number of turns sent so far, `0` for a conversation that was started and
  never used.

- A `provider_model_id` that starts with the routing prefix the `provider` field adds
  (`anthropic/claude-…` under `anthropic`) is refused on save with a 400 pinned to the field —
  only for vendor-hosted rows: with an `inference_endpoint` the id goes through as the proxy
  expects it. The console hints an example id per provider and spells out health-check failure
  reasons; a model the provider does not know now reads `model-not-found` instead of `error`.

- `OIDC_ALLOWED_DOMAINS`: optional comma-separated allowlist of e-mail domains for OIDC sign-in,
  checked against the verified email and Google's `hd` claim. A login outside it is refused with
  `#error=domain_not_allowed` before any account is resolved, existing accounts included, and an
  OIDC session already outside it is refused at its next refresh; a malformed entry fails at
  startup. Unset = any domain.
- Internet access switch on AI models (`web_access`, off by default): chat and stream calls carry
  the provider's native web search (and, on Anthropic, URL fetch) through litellm's
  `web_search_options`. Reads expose `web_access_supported` — Anthropic and Google models always,
  OpenAI per litellm's capability map, other vendors never — and turning the switch on for a model
  without a web option is refused with a 400 pinned to the field, re-checked at dispatch. The
  console offers the switch on the model form (locked up front for vendors that never have a web
  option) and detail page and marks such models where they are picked and in the chat header.

### Changed

- Starting a conversation and sending, regenerating or continuing a prompt now need the
  evaluation's group to be open for them, and answer 409 otherwise: a published group takes them
  from anyone who can reach it; before publication only from a caller who may edit the evaluation
  (to try a scenario out); a finished group and a rejected or completed evaluation from nobody.
  Conversations already recorded stay readable, and a retried send of an already-accepted turn is
  still answered with the stored reply. "May edit the evaluation" is one rule everywhere: the
  global `evaluations:update` plus write access to its group.
- On a masked evaluation, `GET /evaluations/{id}` and `GET /evaluations/{id}/models` show the real
  model names to a caller who may edit the evaluation. Lists, group embeds and write responses stay
  masked.
- The single `POST /evaluation-groups/{group_id}/members` takes a deactivated user only when they
  were removed from that group within the restore window, so Undo after a removal works; any
  other deactivated user is still refused. That 409, on the bulk add and the group invitation as well, now
  carries `type: urn:redteam:error:user-deactivated` and no longer names an API route.
- `GET /auth/users?search=` also matches a first and last name typed together (`ada love`), not
  only one column at a time.
- The 409 that refuses a model added to a prompted or full conversation group no longer carries
  the group's id in `detail`.
- The health check that followed a save ran only for models declaring image input; it now follows
  every create, every patch that moves the call, and setting the API key. Bulk create still starts
  none.

## 0.1.0 — 2026-09-10

Initial public release.
