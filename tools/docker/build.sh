#!/usr/bin/env bash
# Build the Studio test image from a minimal temp context (keeps node_modules etc. out of the build).
set -eu
here="$(cd "$(dirname "$0")" && pwd)"; repo="$here/../.."
ctx="${1:-/tmp/bs-studio-ctx}"
rm -rf "$ctx"; mkdir -p "$ctx"
cp "$here/Dockerfile.studio-test" "$repo/package.json" "$repo/package-lock.json" "$ctx/"
docker build -t bs-studio-test:local -f "$ctx/Dockerfile.studio-test" "$ctx"
