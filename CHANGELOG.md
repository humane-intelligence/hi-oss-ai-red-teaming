# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — while the major version is 0 the API may
change between minor versions.

## Unreleased

### Added

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

- **Breaking:** conversation-group labels take several values per category. `labels` on
  `POST /scenarios/{scenario_id}/conversation-groups` and on the group PATCH is now
  `{category_id: [values]}` instead of `{category_id: value}`; a category left out is cleared. A
  category without values no longer takes free text: it is selected on its own, by listing it with
  an empty list, and reads back with `value: null`. Text stored there earlier stays readable and
  can be kept. The `Labels` export column is `{category: [values]}`. Migration `51936c9ff5f4`
  widens the table's primary key; no data is rewritten.
- Starting a conversation and sending, regenerating or continuing a prompt now need the
  evaluation's group to be open for them, and answer 409 otherwise: a published group takes them
  from anyone who can reach it; before publication only from a caller who may edit the evaluation
  (to try a scenario out); a finished group and a rejected or completed evaluation from nobody.
  Conversations already recorded stay readable.
- `GET /auth/users?search=` also matches a first and last name typed together (`ada love`), not
  only one column at a time.
- The 409 that refuses a model added to a prompted or full conversation group no longer carries
  the group's id in `detail`, and the 409 for a deactivated user no longer names an API route.

- The health check that followed a save ran only for models declaring image input; it now follows
  every create, every patch that moves the call, and setting the API key. Bulk create still starts
  none.

## 0.1.0 — 2026-09-10

Initial public release.
