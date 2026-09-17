#!/bin/bash
# Validate the worker and the interface using fatzserver-host's Python, isolated entirely under
# /tmp. It builds no image, writes nothing outside the staging directory, reads no media,
# and contacts no Sonarr.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The source arrives on stdin, so the remote script travels as an argument, not a heredoc.
tar -C "$ROOT" -cf - src tests tools VERSION BUILD | ssh "${TVR_HOST:-fatzserver-host}" '
set -eu
staging=$(mktemp -d /tmp/tv-retention-dev.XXXXXX)
trap "rm -rf $staging" EXIT
tar -xf - -C "$staging"
cd "$staging"
printf "Development staging: %s\n" "$staging"
# Required dependencies must not produce a successful partial gate.
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v node >/dev/null || { echo "Node is required for the release gate" >&2; exit 1; }
export TVR_CONFIG_DIR="$staging/config" TVR_CONFIG="$staging/config/settings.json"
export TVR_RUNTIME="$staging/runtime" TVR_DEVELOPMENT=1
status=0
python3 -m unittest discover -s tests -v || status=1
# Enumerate shipped modules rather than relying on main importing them transitively.
for module in src/worker/*.py; do
  python3 -m py_compile "$module" || status=1
  name=$(basename "$module" .py)
  PYTHONPATH=src/worker python3 -c "import importlib, sys; importlib.import_module(sys.argv[1])" "$name" \
    || status=1
done
# Check ES module syntax, not script-goal syntax; keep checking after a test failure.
for script in src/assets/*.js; do
  node --input-type=module --check < "$script" || status=1
done
node --experimental-vm-modules --test tests/frontend/*.test.js || status=1
if [ "$status" -eq 0 ]; then
  echo "All required checks passed"
else
  echo "Required validation failed" >&2
fi
exit "$status"
'
