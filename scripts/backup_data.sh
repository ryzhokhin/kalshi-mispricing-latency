#!/usr/bin/env bash
# Archive the raw recordings and derived data into backups/ with a timestamp.
# The live hour file is copied as-is; the recorder keeps appending to the original.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
stamp=$(date -u +%Y%m%dT%H%MZ)
out="backups/kalshi-data-${stamp}.tar.gz"
tar -czf "$out" data
echo "wrote $out ($(du -h "$out" | cut -f1))"
