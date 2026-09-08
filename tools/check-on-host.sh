#!/bin/bash
# Validate the plugin using FatzServer's Python and PHP, isolated entirely under /tmp.
# It does not install the plugin, touch /boot, read the media library, or contact Sonarr.
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
python3 tools/build.py
php -l src/tv-retention/include/api.php
sed "1,/^---$/d" src/tv-retention/TVRetention.page > page-lint.php
php -l page-lint.php
if command -v node >/dev/null; then
  node --check src/tv-retention/assets/app.js && echo "app.js syntax OK"
else
  echo "node not present; app.js not syntax checked"
fi
# The icon must resolve to a real glyph, not merely to a class name that exists somewhere.
# Two icons shipped that rendered as nothing: one absent from the font, one present as a
# class with no :before{content} rule. Both look correct to a grep for the name alone.
icon=$(sed -n "s/^Icon=\"\(.*\)\"/\1/p" src/tv-retention/TVRetention.page)
awesome=/usr/local/emhttp/webGui/styles/font-awesome.css
if [ -f "$awesome" ] && grep -q "[.]fa-${icon}:before" "$awesome"; then
  echo "icon: fa-${icon} resolves to a glyph"
else
  echo "ICON PROBLEM: fa-${icon} has no glyph in font-awesome.css"
  exit 1
fi
'
