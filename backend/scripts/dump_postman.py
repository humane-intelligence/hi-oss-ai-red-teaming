"""Generate the Postman mirror collection from ``docs/openapi.yaml``.

A faithful 1:1 view of the API: one folder per OpenAPI tag, one request per operation.
Reads the committed spec produced by ``make openapidump``, so the mirror can never drift
from it. Output is deterministic (sorted) so re-runs are no-ops and CI can gate on drift.

One enrichment the canonical spec deliberately omits is layered in here: the **required
permission** of each gated operation (`annotate_permissions`, which introspects the live
app). It is a Postman-mirror concern, so it stays out of ``docs/openapi.yaml`` — which
means this script imports the app (it is not a pure file transform).

The spec carries no `securityScheme` (auth is a bearer token validated in middleware),
so collection-level bearer auth is injected here and the public routes are marked
`noauth`. Both enrichments come from one live-app introspection (`introspect_routes` —
permission map + the set of routes with no `current_user` dependency), so new routes need
no manual upkeep here.
"""

import json
from pathlib import Path
from typing import Any

import yaml

from scripts.openapi_permissions import annotate_permissions
from scripts.openapi_permissions import introspect_routes

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "openapi.yaml"
OUTPUT = ROOT / "docs" / "postman" / "collections" / "api-mirror.postman_collection.json"

_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
_METHODS = ("get", "post", "put", "patch", "delete")
_METHOD_RANK = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}

_MAX_DEPTH = 6  # cap example expansion so a recursive/self-referential schema can't blow up


def _resolve_ref(ref: str, spec: dict[str, Any]) -> dict[str, Any]:
    node: Any = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _request_body_schema(op: dict[str, Any]) -> dict[str, Any] | None:
    """The operation's `application/json` request-body schema node, or None."""
    return op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")


def _example_for(  # noqa: PLR0911, PLR0912 — irreducible dispatch over JSON-schema node shapes
    schema: dict[str, Any], spec: dict[str, Any], depth: int, seen: frozenset[str]
) -> Any:
    """Best-effort sample value for a JSON schema node, for prefilled request bodies.

    Prefers an explicit `example`/`default`; otherwise synthesizes by type/format. Cycles
    (via `$ref` in `seen`) and `depth` past `_MAX_DEPTH` collapse to a stub so a
    self-referential schema terminates.
    """
    if depth > _MAX_DEPTH:
        return {}
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            return {}
        return _example_for(_resolve_ref(ref, spec), spec, depth, seen | {ref})
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "allOf" in schema:
        # allOf is a combination: merge the object branches; a scalar/enum branch
        # (e.g. allOf:[{$ref → enum}] for a described enum field) is the value itself.
        merged: dict[str, Any] = {}
        for sub in schema["allOf"]:
            value = _example_for(sub, spec, depth, seen)
            if isinstance(value, dict):
                merged.update(value)
            else:
                return value
        return merged
    for combiner in ("oneOf", "anyOf"):
        if combiner in schema:
            # Alternatives, not a combination: take the first non-null variant. So
            # [str, null] yields the string, [object, null] yields the object (not null),
            # and a union of objects yields one variant, not an invalid merge of both.
            variants = [sub for sub in schema[combiner] if sub.get("type") != "null"]
            return _example_for(variants[0] if variants else schema[combiner][0], spec, depth, seen)
    if schema.get("enum"):
        return schema["enum"][0]

    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        return {name: _example_for(prop, spec, depth + 1, seen) for name, prop in schema.get("properties", {}).items()}
    if schema_type == "array":
        items = schema.get("items", {})
        return [_example_for(items, spec, depth + 1, seen)] if items else []
    return _scalar_example(schema)


def _scalar_example(schema: dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    if schema_type in ("integer", "number"):
        # Clamp into the field's own bounds — a bare 0 against a `ge=1` field ships a
        # sample body that the same request's description documents as invalid. The
        # spec serialises those bounds as floats, so an integer field re-narrows.
        value = max(0, schema.get("minimum", 0))
        maximum = schema.get("maximum")
        value = value if maximum is None else min(value, maximum)
        return int(value) if schema_type == "integer" else value
    if schema_type == "boolean":
        return True
    if schema_type == "null":
        return None
    formats = {
        "uuid": "00000000-0000-0000-0000-000000000000",
        "date-time": "2026-01-01T00:00:00Z",
        "date": "2026-01-01",
        "email": "user@example.com",
        "uri": "https://example.com",
    }
    return formats.get(schema.get("format", ""), "string")


def _deref(schema: dict[str, Any], spec: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Follow a single `$ref` and flatten `allOf` so callers see properties/constraints directly."""
    if depth > _MAX_DEPTH:
        return schema
    if "$ref" in schema:
        return _deref(_resolve_ref(schema["$ref"], spec), spec, depth + 1)
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        required: list[Any] = []
        for sub in schema["allOf"]:
            resolved = _deref(sub, spec, depth + 1)
            required += resolved.get("required", [])
            merged.update({k: v for k, v in resolved.items() if k not in ("allOf", "required")})
        # The schema's own keys win over the merged branches; `required` is the one list
        # key that must be *unioned* across branches, not overwritten (each allOf sub
        # contributes its own required fields).
        required += schema.get("required", [])
        merged.update({k: v for k, v in schema.items() if k not in ("allOf", "required")})
        if required:
            merged["required"] = list(dict.fromkeys(required))
        return merged
    return schema


def _type_str(schema: dict[str, Any], spec: dict[str, Any], depth: int = 0) -> str:
    """Human-readable type label for a schema node (e.g. `string<uuid>`, `array<Foo>`, `Bar | null`)."""
    if depth > _MAX_DEPTH:
        return "object"
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    for combiner in ("anyOf", "oneOf"):
        if combiner in schema:
            nullable = any(sub.get("type") == "null" for sub in schema[combiner])
            names = [_type_str(sub, spec, depth + 1) for sub in schema[combiner] if sub.get("type") != "null"]
            label = " | ".join(names) if names else "any"
            return f"{label} | null" if nullable else label
    if "allOf" in schema:
        return _type_str(schema["allOf"][0], spec, depth + 1)
    schema_type = schema.get("type")
    if schema_type == "array":
        return f"array<{_type_str(schema.get('items', {}), spec, depth + 1)}>"
    return f"{schema_type}<{schema['format']}>" if schema.get("format") else (schema_type or "object")


def _constraints(schema: dict[str, Any]) -> str:
    out = []
    pairs = (("maxLength", "maxLen "), ("minLength", "minLen "), ("minimum", "≥"), ("maximum", "≤"))
    out += [f"{label}{schema[key]}" for key, label in pairs if key in schema]
    if schema.get("enum"):
        out.append("one of: " + " | ".join(str(v) for v in schema["enum"][:8]))
    if "default" in schema:
        out.append(f"default {schema['default']!r}")
    return ", ".join(out)


def _schema_fields(schema: dict[str, Any], spec: dict[str, Any]) -> list[tuple[str, str, bool, str, str]]:
    resolved = _deref(schema, spec)
    required = set(resolved.get("required", []))
    rows = []
    for name, prop in resolved.get("properties", {}).items():
        effective = _deref(prop, spec)  # enum/constraints often live behind a $ref
        description = prop.get("description") or effective.get("description") or ""
        constraints = _constraints(effective) or _constraints(prop)
        rows.append((name, _type_str(prop, spec), name in required, constraints, description))
    return rows


def _bullet(name: str, type_label: str, *, required: bool | None, constraints: str, description: str) -> str:
    flag = "" if required is None else f", {'required' if required else 'optional'}"
    extra = f" _[{constraints}]_" if constraints else ""
    tail = f" — {description}" if description else ""
    return f"- `{name}` ({type_label}{flag}){extra}{tail}"


def _render_description(
    method: str, path: str, op: dict[str, Any], spec: dict[str, Any], public: set[tuple[str, str]]
) -> str:
    """Compose the rich Markdown doc shown in Postman from the operation's spec.

    Covers the description, auth note, path/query params, request-body fields, and responses.
    """
    sections: list[str] = []
    body = (op.get("description") or op.get("summary") or "").strip()
    if body:
        sections.append(body)

    # The required permission (when known) is appended to the operation description by
    # `annotate_permissions` (called in `main`), so the Auth line here only states the scheme.
    if (method, path) in public:
        sections.append("**Auth** · 🔓 Public — no token required.")
    else:
        sections.append("**Auth** · 🔒 Bearer token required.")

    params = op.get("parameters", [])
    path_params = [p for p in params if p.get("in") == "path"]
    if path_params:
        lines = ["**Path parameters**"]
        lines += [
            _bullet(
                p["name"],
                _type_str(p.get("schema", {}), spec),
                required=True,
                constraints="",
                description=p.get("description", ""),
            )
            for p in path_params
        ]
        sections.append("\n".join(lines))

    query_params = [p for p in params if p.get("in") == "query"]
    if query_params:
        lines = ["**Query parameters**"]
        lines += [
            _bullet(
                p["name"],
                _type_str(p.get("schema", {}), spec),
                required=p.get("required", False),
                constraints=_constraints(p.get("schema", {})),
                description=p.get("description", ""),
            )
            for p in query_params
        ]
        sections.append("\n".join(lines))

    json_schema = _request_body_schema(op)
    if json_schema is not None:
        ref_name = json_schema.get("$ref", "").split("/")[-1]
        header = f"**Request body** — `{ref_name}`" if ref_name else "**Request body**"
        lines = [header]
        lines += [
            _bullet(name, type_label, required=required, constraints=constraints, description=description)
            for name, type_label, required, constraints, description in _schema_fields(json_schema, spec)
        ]
        sections.append("\n".join(lines))

    responses = op.get("responses", {})
    if responses:
        lines = ["**Responses**", "", "| Status | Meaning |", "|---|---|"]
        lines += [f"| {code} | {responses[code].get('description', '')} |" for code in sorted(responses)]
        sections.append("\n".join(lines))

    sections.append("_Generated from `docs/openapi.yaml` by `make postmandump` — do not hand-edit._")
    return "\n\n".join(sections)


def _build_url(path: str, op: dict[str, Any]) -> dict[str, Any]:
    segments = [seg for seg in path.split("/") if seg]
    postman_path = [f":{seg[1:-1]}" if seg.startswith("{") else seg for seg in segments]
    url: dict[str, Any] = {
        # raw must use Postman's `:param` form, not OpenAPI's `{param}` — the latter is a
        # literal in Postman's URL bar and is sent unsubstituted if a client honours raw.
        "raw": "{{base_url}}/" + "/".join(postman_path),
        "host": ["{{base_url}}"],
        "path": postman_path,
    }
    path_vars = [
        {"key": p["name"], "value": "{{" + p["name"] + "}}", "description": p.get("description", "")}
        for p in op.get("parameters", [])
        if p.get("in") == "path"
    ]
    if path_vars:
        url["variable"] = path_vars
    query = [
        {
            "key": p["name"],
            "value": "",
            "disabled": not p.get("required", False),
            "description": p.get("description", ""),
        }
        for p in op.get("parameters", [])
        if p.get("in") == "query"
    ]
    if query:
        url["query"] = query
    return url


def _build_body(op: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    json_schema = _request_body_schema(op)
    if json_schema is None:
        return None
    example = _example_for(json_schema, spec, depth=0, seen=frozenset())
    return {
        "mode": "raw",
        "raw": json.dumps(example, indent=2, ensure_ascii=False),
        "options": {"raw": {"language": "json"}},
    }


def _build_request(
    method: str, path: str, op: dict[str, Any], spec: dict[str, Any], public: set[tuple[str, str]]
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "method": method,
        "header": [],
        "url": _build_url(path, op),
        "description": _render_description(method, path, op, spec, public),
    }
    body = _build_body(op, spec)
    if body is not None:
        request["header"].append({"key": "Content-Type", "value": "application/json"})
        request["body"] = body
    if (method, path) in public:
        request["auth"] = {"type": "noauth"}
    return {
        "name": op.get("summary") or f"{method} {path}",
        "request": request,
        "response": [],
    }


def build_collection(spec: dict[str, Any], public: set[tuple[str, str]]) -> dict[str, Any]:
    folders: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, ops in spec.get("paths", {}).items():
        for method_lower, op in ops.items():
            if method_lower not in _METHODS:
                continue
            method = method_lower.upper()
            tag = (op.get("tags") or ["untagged"])[0]
            folders.setdefault(tag, []).append((path, method, op))

    # Folder order follows the spec's declared tag order (`OPENAPI_TAGS`); a tag used by
    # an operation but not declared there falls to the end, alphabetically. No hand-kept
    # list — declaring a tag in app/core/openapi.py is the single source of order.
    declared_order = {t["name"]: i for i, t in enumerate(spec.get("tags", []))}
    tag_descriptions = {t["name"]: t.get("description", "") for t in spec.get("tags", [])}
    items = []
    for tag in sorted(folders, key=lambda t: (declared_order.get(t, len(declared_order)), t)):
        requests = sorted(folders[tag], key=lambda t: (t[0], _METHOD_RANK.get(t[1], 9)))
        folder: dict[str, Any] = {
            "name": tag,
            "item": [_build_request(method, path, op, spec, public) for path, method, op in requests],
        }
        if tag_descriptions.get(tag):
            folder["description"] = tag_descriptions[tag]
        items.append(folder)

    info = spec.get("info", {})
    return {
        "info": {
            "name": f"{info.get('title', 'API')} — Mirror",
            "description": (
                "GENERATED from docs/openapi.yaml by scripts/dump_postman.py (`make postmandump`). "
                "Do not hand-edit — changes are overwritten. One folder per tag, one request per operation. "
                "Set `base_url` + `access_token` via the `local` environment."
            ),
            "schema": _SCHEMA,
        },
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}]},
        "variable": [{"key": "base_url", "value": "http://localhost:8000", "type": "string"}],
        "item": items,
    }


def main() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    # One live-app introspection feeds both enrichments (kept out of the canonical spec).
    permissions, public = introspect_routes()
    annotate_permissions(spec, permissions)
    rendered = json.dumps(build_collection(spec, public), indent=2, ensure_ascii=False) + "\n"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # Skip the write on identical content: keeps output idempotent for drift-gating.
    if OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == rendered:
        return
    OUTPUT.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
