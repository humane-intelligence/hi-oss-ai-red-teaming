"""Push the generated Postman collections to a workspace via the Postman API.

The repo is the source of truth; this is a **one-way** push (repo → Postman). Each
collection is matched by `info.name` within `POSTMAN_WORKSPACE_ID` and either created or
replaced — `PUT` keeps the same `uid`/URL so shared links survive a regen. The mirror is
generated (overwrite is intended); scenario collections are pushed the same way.

Dev tooling, not app runtime: secrets are read straight from the environment
(`POSTMAN_API_KEY`, `POSTMAN_WORKSPACE_ID`), never from `Settings`. Standalone by
design — stdlib only, no imports from `app`, no new dependency, no async (a one-shot CLI
that must import cleanly even without an `.env`).
"""

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("push_postman")

# Colour only when writing to an interactive terminal; honour the NO_COLOR convention.
_USE_COLOR = sys.stderr.isatty() and "NO_COLOR" not in os.environ
_BOLD, _DIM, _GREEN, _CYAN, _YELLOW = "1", "2", "32", "36", "33"


def _style(text: str, *codes: str) -> str:
    """Wrap `text` in ANSI SGR codes, dropped entirely when output isn't a TTY."""
    if not _USE_COLOR or not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


ROOT = Path(__file__).resolve().parent.parent
COLLECTIONS = ROOT / "docs" / "postman" / "collections"
MIRROR = COLLECTIONS / "api-mirror.postman_collection.json"
SCENARIOS = COLLECTIONS / "scenarios"

_API_BASE = "https://api.getpostman.com"


def _request(method: str, path: str, api_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = _API_BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(  # noqa: S310 — path is an internal constant, never user input
        url,
        data=data,
        method=method,
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310  # nosec B310 — path is an internal constant
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Postman API {method} {path} failed: {exc.code} {exc.reason}\n{body}") from exc


def _workspace_uids(api_key: str, workspace: str) -> dict[str, str]:
    """Map `info.name -> uid` for the workspace's collections, in **one** listing call.

    Listed once and reused across every file pushed (instead of re-listing per file).
    Raises on a duplicate name: the push matches by name, so an ambiguous workspace can't
    be resolved safely.
    """
    query = urllib.parse.urlencode({"workspace": workspace})
    listing = _request("GET", f"/collections?{query}", api_key)
    uids: dict[str, str] = {}
    for collection in listing.get("collections", []):
        name = collection.get("name")
        if name is None:
            continue
        if name in uids:
            raise SystemExit(f"ambiguous: two collections named {name!r} in workspace — resolve manually")
        uids[name] = collection["uid"]
    return uids


def _push_collection(api_key: str, workspace: str, file: Path, uids: dict[str, str]) -> None:
    collection = json.loads(file.read_text(encoding="utf-8"))
    name = collection["info"]["name"]
    uid = uids.get(name)
    if uid:
        result = _request("PUT", f"/collections/{uid}", api_key, {"collection": collection})
        action = "updated"
    else:
        query = urllib.parse.urlencode({"workspace": workspace})
        result = _request("POST", f"/collections?{query}", api_key, {"collection": collection})
        action = "created"
    info = result.get("collection", {})
    color = _GREEN if action == "created" else _CYAN
    badge = _style(f"  {action:>7}", color)
    uid = _style(f"uid={info.get('uid', '?')}", _DIM)
    logger.info("%s  %s  %s", badge, name, uid)


def _collection_files() -> list[Path]:
    files = [MIRROR] if MIRROR.exists() else []
    if SCENARIOS.is_dir():
        files += sorted(SCENARIOS.glob("*.postman_collection.json"))
    return files


def _dry_run(files: list[Path], api_key: str | None, workspace: str | None) -> None:
    """Report what a push would do without mutating anything.

    With creds it issues only read-only `GET /collections` to resolve each name to
    create-vs-update (and the existing `uid`); without them it still lists the local
    files + their `info.name`, since the dry-run is also a "what's in the repo" check.
    """
    resolve = bool(api_key and workspace)
    meta = _style(f"workspace={workspace or '(unset)'} · resolve={'on' if resolve else 'off'}", _DIM)
    logger.info("%s  %s", _style(f"dry-run · {len(files)} collection(s)", _BOLD), meta)
    uids = _workspace_uids(api_key, workspace) if resolve else {}
    for file in files:
        name = json.loads(file.read_text(encoding="utf-8"))["info"]["name"]
        if resolve:
            uid = uids.get(name)
            action, color, tail = ("update", _CYAN, f"uid={uid}") if uid else ("create", _GREEN, "new")
        else:
            action, color, tail = "create-or-update", _YELLOW, "set POSTMAN_API_KEY + POSTMAN_WORKSPACE_ID to resolve"
        badge = _style(f"  {action:>16}", color)
        logger.info("%s  %s  %s", badge, name, _style(tail, _DIM))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Push generated Postman collections to a workspace (repo → Postman).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be pushed and make no changes (read-only; works without creds).",
    )
    args = parser.parse_args()

    api_key = os.environ.get("POSTMAN_API_KEY")
    workspace = os.environ.get("POSTMAN_WORKSPACE_ID")
    files = _collection_files()
    if not files:
        raise SystemExit("no collections to push — run `make postmandump` first")

    if args.dry_run:
        _dry_run(files, api_key, workspace)
        return

    if not api_key or not workspace:
        raise SystemExit(
            "POSTMAN_API_KEY and POSTMAN_WORKSPACE_ID must be set (export them or add to .env). "
            "Get an API key at https://go.postman.co/settings/me/api-keys."
        )
    logger.info("%s", _style(f"→ pushing {len(files)} collection(s) to workspace {workspace}", _BOLD))
    uids = _workspace_uids(api_key, workspace)  # one listing call, reused for every file
    for file in files:
        _push_collection(api_key, workspace, file, uids)


if __name__ == "__main__":
    main()
