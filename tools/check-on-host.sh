#!/bin/bash
# Validate the worker and the interface using FatzServer's Python, isolated entirely under
# /tmp. It builds no image, writes nothing outside the staging directory, reads no media,
# and contacts no Sonarr.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The source arrives on stdin, so the remote script travels as an argument, not a heredoc.
tar -C "$ROOT" -cf - src tests tools VERSION BUILD | ssh "${TVR_HOST:-FatzServer}" '
set -eu
staging=$(mktemp -d /tmp/tv-retention-dev.XXXXXX)
trap "rm -rf $staging" EXIT
tar -xf - -C "$staging"
cd "$staging"
printf "Development staging: %s\n" "$staging"
python3 -m unittest discover -s tests -v
PYTHONPATH=src/worker python3 -c "import main, actions, server" && echo "worker imports OK"
if command -v node >/dev/null; then
  # Every shipped module, checked as an ES module: `--input-type` governs stdin, and a
  # bare `node --check file.js` would parse with script goal, where import/export is a
  # syntax error and a file without them says nothing about module semantics.
  for script in src/assets/*.js; do
    node --input-type=module --check < "$script" || { echo "syntax check failed: $script" >&2; exit 1; }
  done
  echo "module syntax OK"
  # The runtime test links the page the way the browser does, which needs the vm module
  # API. The flag reaches the children the test runner spawns; its warning is on stderr.
  node --experimental-vm-modules --test tests/frontend/*.test.js \
    && echo "frontend runtime tests OK"
else
  echo "node not present; modules not syntax checked, frontend runtime tests not run"
fi
'
