# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — while the major version is 0 the API may
change between minor versions.

## Unreleased

### Added

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

- The health check that followed a save ran only for models declaring image input; it now follows
  every create, every patch that moves the call, and setting the API key. Bulk create still starts
  none.

## 0.1.0 — 2026-09-10

Initial public release.
