"""Unit tests for `scripts.dump_postman.build_collection`.

`build_collection` is the generic core of the Postman mirror: paths/operations come from
the spec, folder order from the spec's declared tags, and each request's `noauth` vs bearer
from the caller-supplied public set. These tests lock that behaviour with a hand-built
mini-spec (no live app, no I/O) so a regression in the generic logic fails here rather than
silently surfacing as committed-mirror drift.
"""

from typing import Any

import pytest

from scripts.dump_postman import _deref
from scripts.dump_postman import _example_for
from scripts.dump_postman import build_collection

_NO_SPEC: dict[str, Any] = {}

_SPEC: dict[str, Any] = {
    "info": {"title": "Test API"},
    # Declared order is health, then models. `widgets` is intentionally NOT declared.
    "tags": [
        {"name": "health", "description": "probes"},
        {"name": "models", "description": "registry"},
    ],
    "paths": {
        "/health": {
            "get": {"tags": ["health"], "summary": "Health", "responses": {"200": {"description": "ok"}}},
        },
        "/api/v1/ai-models": {
            "post": {
                "tags": ["models"],
                "summary": "Create model",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "example": "gpt-4o"},
                                    "provider": {"type": "string", "enum": ["openai", "anthropic"]},
                                    "id": {"type": "string", "format": "uuid"},
                                },
                            }
                        }
                    }
                },
                "responses": {"201": {"description": "created"}},
            },
            "get": {"tags": ["models"], "summary": "List models", "responses": {"200": {"description": "ok"}}},
        },
        "/api/v1/widgets": {
            "get": {"tags": ["widgets"], "summary": "List widgets", "responses": {"200": {"description": "ok"}}},
        },
    },
}

_PUBLIC: set[tuple[str, str]] = {("GET", "/health")}


@pytest.fixture
def collection() -> dict[str, Any]:
    return build_collection(_SPEC, _PUBLIC)


def _request(collection: dict[str, Any], folder: str, name: str) -> dict[str, Any]:
    folder_item = next(f for f in collection["item"] if f["name"] == folder)
    return next(r for r in folder_item["item"] if r["name"] == name)["request"]


@pytest.mark.unit
def test_collection_envelope_carries_bearer_auth_and_base_url(collection: dict[str, Any]) -> None:
    assert collection["info"]["name"].endswith("Mirror")
    assert collection["auth"] == {
        "type": "bearer",
        "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}],
    }
    assert any(v["key"] == "base_url" for v in collection["variable"])


@pytest.mark.unit
def test_folder_order_follows_declared_tags_then_undeclared_last(collection: dict[str, Any]) -> None:
    # health, models declared in that order; widgets undeclared → appended (alphabetically) last.
    assert [folder["name"] for folder in collection["item"]] == ["health", "models", "widgets"]


@pytest.mark.unit
def test_one_request_per_operation_sorted_by_method_within_a_path(collection: dict[str, Any]) -> None:
    models = next(f for f in collection["item"] if f["name"] == "models")

    methods = [r["request"]["method"] for r in models["item"]]

    assert methods == ["GET", "POST"]  # same path, GET ranks before POST


@pytest.mark.unit
def test_public_route_is_marked_noauth(collection: dict[str, Any]) -> None:
    assert _request(collection, "health", "Health")["auth"] == {"type": "noauth"}


@pytest.mark.unit
def test_gated_route_inherits_collection_bearer(collection: dict[str, Any]) -> None:
    # No per-request auth override → the request inherits the collection-level bearer.
    assert "auth" not in _request(collection, "models", "List models")


@pytest.mark.unit
def test_request_body_prefilled_from_schema(collection: dict[str, Any]) -> None:
    request = _request(collection, "models", "Create model")

    assert {"key": "Content-Type", "value": "application/json"} in request["header"]
    raw = request["body"]["raw"]
    assert '"gpt-4o"' in raw  # explicit example wins
    assert '"openai"' in raw  # enum → first value
    assert "00000000-0000-0000-0000-000000000000" in raw  # uuid format → stub


@pytest.mark.unit
def test_example_for_nullable_object_yields_the_object_not_null() -> None:
    # anyOf:[object, null] is a nullable wrapper — synthesize the object, never collapse to null.
    schema = {"anyOf": [{"type": "object", "properties": {"name": {"type": "string"}}}, {"type": "null"}]}

    assert _example_for(schema, _NO_SPEC, 0, frozenset()) == {"name": "string"}


@pytest.mark.unit
def test_example_for_object_union_picks_one_variant_not_a_merge() -> None:
    # oneOf of two object variants → a single valid variant, not an invalid union of both.
    schema = {
        "oneOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"b": {"type": "integer"}}},
        ]
    }

    assert _example_for(schema, _NO_SPEC, 0, frozenset()) == {"a": "string"}


@pytest.mark.unit
def test_example_for_allof_enum_branch_still_returns_the_enum_value() -> None:
    # Regression guard: a described enum is `allOf:[{enum}]`; the scalar branch must win
    # (the oneOf/anyOf fix must not have changed allOf's merge-or-scalar behaviour).
    schema = {"allOf": [{"enum": ["openai", "anthropic"]}]}

    assert _example_for(schema, _NO_SPEC, 0, frozenset()) == "openai"


@pytest.mark.unit
def test_deref_unions_required_across_allof_branches() -> None:
    schema = {
        "allOf": [
            {"type": "object", "properties": {"a": {}}, "required": ["a"]},
            {"type": "object", "properties": {"b": {}}, "required": ["b"]},
        ]
    }

    assert set(_deref(schema, _NO_SPEC).get("required", [])) == {"a", "b"}


@pytest.mark.unit
def test_untagged_operation_falls_back_to_an_untagged_folder() -> None:
    spec = {"info": {"title": "T"}, "tags": [], "paths": {"/x": {"get": {"summary": "X", "responses": {}}}}}

    collection = build_collection(spec, set())

    assert [f["name"] for f in collection["item"]] == ["untagged"]
