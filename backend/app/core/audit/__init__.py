"""Audit-log subsystem — a single append-only record of sensitive actions and accesses.

Captures `actor / action / target / before / after / timestamp` for sensitive
operations across identity, events (evaluation groups), evaluations, reviews, and
asset access. Two write paths: mutating handlers call `record_audit(...)` explicitly
(with curated before/after, atomic with the action); read/access events are captured
centrally by `AuditAccessMiddleware` from a route allowlist. Read side is an
admin-only `GET /api/v1/audit-logs`.
"""
