#!/bin/bash
# Validate the worker and the interface using fatzserver-host's Python, isolated entirely under
# /tmp. It builds no image, writes nothing outside the staging directory, reads no media,
# and contacts no Sonarr.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
host="${TVR_HOST:-fatzserver-host}"
lease="/tmp/tv-retention-lease.$$.$RANDOM"

# The source arrives on stdin, so the remote script travels as an argument, not a heredoc.
# The lease is refreshed by this process while SSH is alive. The remote watcher removes
# staging when the lease goes stale, including when the local client is killed abruptly.
remote_script='
set -eu
lease="${TVR_LEASE_PATH:?missing validation lease}"
lease_timeout="${TVR_LEASE_TIMEOUT:-30}"
staging=$(mktemp -d /tmp/tv-retention-dev.XXXXXX)
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ -n "${watchdog_pid:-}" ]; then
    kill "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true
  fi
  rm -rf "$staging" "$lease"
  exit "$status"
}
watchdog() {
  while :; do
    sleep 3
    [ -e "$lease" ] || exit 0
    now=$(date +%s)
    updated=$(stat -c %Y "$lease" 2>/dev/null || printf 0)
    if [ "$updated" -eq 0 ] || [ $((now - updated)) -gt "$lease_timeout" ]; then
      printf "Validation lease expired; removing staging\n" >&2
      rm -rf "$staging" "$lease"
      kill -TERM "$$" 2>/dev/null || true
      exit 0
    fi
  done
}
: > "$lease"
watchdog &
watchdog_pid=$!
trap cleanup EXIT HUP INT TERM
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

quote_for_remote_bash() {
  printf '%q' "$1"
}

heartbeat() {
  while kill -0 "$run_pid" 2>/dev/null; do
    ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$host" "test -e $lease && touch $lease" >/dev/null 2>&1 || true
    sleep 3
  done
}

remote_command="TVR_LEASE_PATH=$lease TVR_LEASE_TIMEOUT=30 bash -c $(quote_for_remote_bash "$remote_script")"
tar -C "$ROOT" -cf - src tests tools VERSION BUILD | ssh "$host" "$remote_command" &
run_pid=$!
heartbeat &
heartbeat_pid=$!
set +e
wait "$run_pid"
status=$?
set -e
kill "$heartbeat_pid" 2>/dev/null || true
wait "$heartbeat_pid" 2>/dev/null || true
exit "$status"
