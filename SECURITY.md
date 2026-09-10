# Security policy

## Supported versions

There are no tagged releases yet, so `main` is the only supported line and security fixes land
there. Anyone running this in production is running their own deployment from their own commit, and
should track `main` until releases exist.

## Reporting a vulnerability

**Please do not open a public issue, pull request, or discussion for a security problem.** A public
report is a disclosure, and this project handles red-teaming transcripts and provider credentials.

Report privately, by either route:

- **Email** — <info@humane-intelligence.org>, with "security" in the subject line.
- **GitHub private vulnerability reporting** — the *Report a vulnerability* button under the
  repository's Security tab. It becomes the preferred route once it is enabled here; GitHub only
  offers it on public repositories.

Useful things to include, as far as you have them: what an attacker gains, the affected component
(backend API, console, deploy tooling), the version or commit you tested, a minimal reproduction, and
whether the issue is already public somewhere.

## What happens next

We will acknowledge the report, work with you on a fix, and credit you in the release notes unless you
prefer otherwise. Please give us a reasonable window to ship a fix before disclosing publicly.

## Scope notes

Two properties of this system are worth knowing before you report:

- **Conversation text can be encrypted at rest**, under a data licence that declares it. That protects
  the database, not the export artefact — an export deliberately writes the transcript out in the
  clear for whoever is already authorised to read it.
- **Signed media URLs** are app-signed tokens on a proxy endpoint, not S3 presigned URLs; a public
  asset's token grants nothing its bare key does not.

Neither is a vulnerability by itself, and both are documented behaviour.
