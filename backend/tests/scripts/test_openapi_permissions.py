"""Unit tests for `scripts.openapi_permissions`."""

import copy

import pytest

from app.main import app
from scripts.openapi_permissions import annotate_permissions
from scripts.openapi_permissions import introspect_routes


@pytest.mark.unit
def test_canonical_schema_carries_no_permission_note() -> None:
    # The required-permission prose is a Postman-mirror concern only; the live schema
    # (what dump_openapi.py writes verbatim) must stay clean. The annotate tests below
    # deepcopy before mutating, so this reads the pristine cached schema regardless of
    # execution order.
    rendered = str(app.openapi())

    assert "Requires permission" not in rendered


@pytest.mark.unit
def test_annotate_appends_permission_note_to_gated_operation() -> None:
    schema = copy.deepcopy(app.openapi())  # annotate mutates in place — keep the cache clean
    permissions, _ = introspect_routes()

    annotate_permissions(schema, permissions)

    # `GET /ai-models` is authorized in-route (global or object-scoped `models:read`),
    # so it carries no static note; use a plainly permission-gated operation instead.
    description = schema["paths"]["/api/v1/ai-models"]["post"]["description"]
    assert "**Requires permission:** `models:create`" in description


@pytest.mark.unit
def test_annotate_leaves_public_operation_unannotated() -> None:
    schema = copy.deepcopy(app.openapi())
    permissions, _ = introspect_routes()

    annotate_permissions(schema, permissions)

    assert "Requires permission" not in (schema["paths"]["/health"]["get"].get("description", "") or "")


@pytest.mark.unit
def test_introspect_routes_derives_public_set_from_dependency_tree() -> None:
    _, public = introspect_routes()

    # No `current_user` in their tree → public.
    assert ("GET", "/health") in public
    assert ("POST", "/api/v1/auth/login") in public
    assert ("POST", "/api/v1/auth/register") in public
    assert ("GET", "/api/v1/auth/oidc/{provider}/callback") in public
    # Token-only (no permission) and permission-gated routes both depend on `current_user`.
    assert ("GET", "/api/v1/auth/me") not in public
    assert ("GET", "/api/v1/ai-models") not in public
