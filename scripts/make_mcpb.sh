#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
BUNDLE_DIR="$ROOT/bundle/dealpath-remote"
OUT_FILE="$ROOT/dealpath-remote.mcpb"

if [ ! -d "$BUNDLE_DIR" ]; then
  echo "Bundle directory not found: $BUNDLE_DIR" >&2
  exit 1
fi

cd "$BUNDLE_DIR"
zip -r "$OUT_FILE" .
echo "Wrote $OUT_FILE"

