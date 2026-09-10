<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <img src="docs/assets/logo.svg" alt="Humane Intelligence" width="340">
</picture>

# hi-oss-ai-red-teaming

**A platform for running human-centered red teaming evaluations on AI models and systems.**

<!-- Waiting on the repo going public. (1) The workflow badges stay commented out until then:
     GitHub proxies README images anonymously, so a private repo's badge.svg answers 404 and renders as
     a broken image for everyone, collaborators included — uncomment both lines. (2) Turn on private
     vulnerability reporting under Settings > Code security, plus secret scanning with push protection;
     GitHub only offers those on public repos, and SECURITY.md already names private reporting as the
     preferred route.
[![CI](https://github.com/humane-intelligence/hi-oss-ai-red-teaming/actions/workflows/ci.yml/badge.svg)](https://github.com/humane-intelligence/hi-oss-ai-red-teaming/actions/workflows/ci.yml)
[![CD](https://github.com/humane-intelligence/hi-oss-ai-red-teaming/actions/workflows/cd.yml/badge.svg)](https://github.com/humane-intelligence/hi-oss-ai-red-teaming/actions/workflows/cd.yml)
-->
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)](backend/pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](backend/README.md)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](frontend/README.md)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](docker-compose.yml)

</div>

## What it does

This software is a web application designed to red team AI models or systems. AI red teaming is a
semi-structured testing approach to assess and improve the safety and effectiveness of AI models and
systems by identifying vulnerabilities, limitations, and potential areas for improvement. This
platform is the software to connect human red teamers with the AI models and systems, and keep the
records that make the findings reviewable afterwards. This software is designed to:

- **Be a gateway to any provider.** A model can be registered once in a software instance, including
  the endpoint, credentials, inference parameters — and reached through a single adapter, so a
  red-teamer never handles an API key. Keys are encrypted at rest; replies stream back token by token
  over SSE.
- **Structure according to the engagement.** Evaluation groups → evaluations → scenarios → tasks,
  under a publication lifecycle (`draft → pending approval → approved → published`) that refuses to
  open an engagement still carrying gaps.
- **Create a record worth reviewing.** Conversations are captured in full, and message-level flags,
  reviewer verdicts and annotator notes turn a raw transcript into assessed findings.
- **Provide access control that survives an audit.** Global roles plus per-object roles inside a
  single group, an append-only audit trail, and a data licence that can seal conversation content at
  rest.
- **Show a record of the system.** Asynchronous exports, plus metrics and dashboards over a whole
  event or one evaluation.

This is a monorepo: a Python/FastAPI red-teaming **backend** and its React/Vite operator
**frontend**, coupled by the OpenAPI contract.

| Directory | What lives there |
|---|---|
| [`backend/`](backend/README.md) | The API, the Celery workers, and the data model |
| [`frontend/`](frontend/README.md) | The operator console, on a typed client generated from the backend contract |
| [`deploy/`](deploy/README.md) | The dev environment — CD, the box, infrastructure as code, and who needs which permission to ship |
| [`monitoring/`](monitoring/README.md) | The observability stack — Prometheus, Loki, Grafana, GlitchTip |

## Quickstart

The host needs Docker, make, and [uv](https://docs.astral.sh/uv/) — uv provisions Python and runs the
backend's tooling; the frontend toolchain stays in its container, so no Node.

```bash
make setup   # first run: deps, hooks, migrate, seed, FE install, backend/.env
make dev     # bring up the whole stack
```

- Operator console — <http://localhost:5173>
- API docs (Swagger) — <http://localhost:8000/docs>

`make be-<target>` and `make fe-<target>` delegate to the subsystem Makefiles; `make help` lists the
root targets.

## Documentation

This page is the entry point; the depth is under [`backend/docs/`](backend/docs/).

| Where | What |
|---|---|
| [Knowledge base](backend/docs/knowledge-base/README.md) | The backend in plain English — architecture, data models, components, end-to-end flows. Hand-written; renders on GitHub, opens as an Obsidian vault |
| [`openapi.yaml`](backend/docs/openapi.yaml) | The API contract, dumped from the running app and drift-checked in CI. Browsable at `/docs` once the stack is up |
| [`erd.md`](backend/docs/erd.md) | Every table, column and foreign key as a Mermaid ERD, generated from the models |
| [`permissions.md`](backend/docs/permissions.md) | Every role against every permission, generated from the RBAC definitions |
| [Postman collections](backend/docs/postman/README.md) | A generated mirror of the API, plus hand-authored collections that drive whole flows end to end |

Setup and the conventions of each subsystem stay in the subsystem READMEs linked above.

## Contributing

The setup, the contract loop between backend and frontend, and the gates a PR has to pass are in
[CONTRIBUTING.md](CONTRIBUTING.md). Security problems take the private route in
[SECURITY.md](SECURITY.md) — never a public issue. Participation is under the
[Code of Conduct](CODE_OF_CONDUCT.md). The code is licensed under [Apache 2.0](LICENSE).
