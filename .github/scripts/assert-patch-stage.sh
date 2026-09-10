#!/usr/bin/env bash
# Guards the security-patch layer against silent drift.
#
# `no-cache-filters` no-ops on an unknown stage — buildx exits 0, prints no warning, and the
# step stays CACHED — so the stage name is asserted here. The upgrade command is asserted too:
# pinning only the name leaves the flag busting a stage that no longer patches anything.
set -euo pipefail

stage=${1:?stage name required}
dockerfile=${2:?dockerfile path required}

if [ ! -f "$dockerfile" ]; then
  echo "::error::$dockerfile not found — cannot verify the '$stage' patch stage"
  exit 1
fi

# Leading whitespace is legal on a Dockerfile instruction; anchoring without it would let a
# stage body run past an indented FROM into the next stage — and a later stage's upgrade
# would satisfy the check below for a stage that patches nothing.
from_re='^[[:space:]]*from[[:space:]]'
stage_re="${from_re}.*[[:space:]]as[[:space:]]+${stage}[[:space:]]*$"

if ! grep -qiE "$stage_re" "$dockerfile"; then
  echo "::error::no '$stage' stage in $dockerfile — no-cache-filters would silently no-op"
  exit 1
fi

body=$(awk -v stage_re="$stage_re" -v from_re="$from_re" '
  tolower($0) ~ stage_re { inside = 1; next }
  tolower($0) ~ from_re  { inside = 0 }
  inside && $0 !~ /^[[:space:]]*#/
' "$dockerfile")

if ! grep -qiE 'apt(-get)?[[:space:]].*upgrade|apk[[:space:]].*upgrade' <<<"$body"; then
  echo "::error::the '$stage' stage in $dockerfile runs no upgrade — busting its cache patches nothing"
  exit 1
fi
