#!/bin/bash
# Validate the worker and the interface using FatzServer's Python, isolated entirely under
# /tmp. It builds no image, writes nothing outside the staging directory, reads no media,
# and contacts no Sonarr.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The source arrives on stdin, so the remote script travels as an argument, not a heredoc.
tar -C "$ROOT" -cf - src tests tools VERSION | ssh fatzserver-host '
set -eu
staging=$(mktemp -d /tmp/tv-retention-dev.XXXXXX)
trap "rm -rf $staging" EXIT
tar -xf - -C "$staging"
cd "$staging"
printf "Development staging: %s\n" "$staging"
python3 -m unittest discover -s tests -v
python3 -c "import sys; sys.path.insert(0, \"src/tv-retention/worker\"); import main, actions, server" \
  && echo "worker imports OK"
if command -v node >/dev/null; then
  node --check src/tv-retention/assets/app.js && echo "app.js syntax OK"
else
  echo "node not present; app.js not syntax checked"
fi
'
