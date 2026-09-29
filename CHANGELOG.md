# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — while the major version is 0 the API may
change between minor versions.

## Unreleased

### Added

- `OIDC_ALLOWED_DOMAINS`: optional comma-separated allowlist of e-mail domains for OIDC sign-in,
  checked against the verified email and Google's `hd` claim. A login outside it is refused with
  `#error=domain_not_allowed` before any account is resolved, existing accounts included, and an
  OIDC session already outside it is refused at its next refresh; a malformed entry fails at
  startup. Unset = any domain.

## 0.1.0 — 2026-09-10

Initial public release.
