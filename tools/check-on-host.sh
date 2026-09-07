#!/bin/bash
# Validate the plugin using FatzServer's Python and PHP, isolated entirely under /tmp.
# It does not install the plugin, touch /boot, read the media library, or contact Sonarr.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tar -C "$ROOT" -cf - src tests tools VERSION | ssh fatzserver-host '
set -eu
staging=$(mktemp -d /tmp/tv-delete-dev.XXXXXX)
trap "rm -rf $staging" EXIT
tar -xf - -C "$staging"
cd "$staging"
printf "Development staging: %s\n" "$staging"
python3 -m unittest discover -s tests -v
python3 tools/build.py
php -l src/tv-delete/include/api.php
sed "1,/^---$/d" src/tv-delete/TVDelete.page > page-lint.php
php -l page-lint.php
if command -v node >/dev/null; then node --check src/tv-delete/assets/app.js && echo "app.js syntax OK"; else echo "node not present; app.js not syntax checked"; fi
'
