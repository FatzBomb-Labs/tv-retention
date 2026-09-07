# Validation record

Last run: 2026-09-06, from the Webtop development container against FatzServer
(Unraid 7.3.2, Python 3.11.15, PHP 8).

## Automated

`./tools/check-on-host.sh` — source staged under `/tmp` on the host, removed afterwards.

| Check | Result |
|---|---|
| `python3 -m unittest discover -s tests` | 67 tests, all pass |
| `python3 tools/build.py` | package and manifest built, rebuild is byte-identical |
| `php -l src/tv-delete/include/api.php` | no syntax errors |
| `php -l` on the PHP section of `TVDelete.page` | no syntax errors |
| `node --check src/tv-delete/assets/app.js` | no syntax errors |

Coverage by area:

- **Settings** (17 tests) — cron field validation and shell-injection rejection, path
  traversal rejection, library paths confined to `/mnt`, duplicate-folder rejection,
  unknown-instance rejection, masked API keys preserved on save and refused when nothing
  is stored, redaction, guard bounds, sidecar extensions that would match video files.
- **Retention** (20 tests) — each condition alone; all three combine modes including the
  cases that distinguish *latest* from *any*; air-date precedence (Sonarr → TMDB →
  mtime) and the fallback switched off; specials excluded and included; the minimum file
  age; the per-rule percentage guard both blocking and permitting; every decision
  carrying a reason.
- **Paths** (13 tests) — container-to-host mapping, longest-prefix precedence, partial
  names not treated as prefixes, round trips, Unicode NFC/NFD equivalence, trailing
  slashes; sidecar matching restricted to one episode's own stem; media scanning;
  empty-directory detection that never returns the show folder.
- **Matching** (8 tests) — match by stored series id, by TVDB id after an id change, by
  folder path; ambiguity refused rather than guessed; unknown folders reported.
- **Build** (7 tests) — install paths, executable event scripts, manifest checksum,
  launch target, removal that preserves settings, reproducible rebuilds.

## Live, read-only

Against the running `Sonarr-Series` container (`/tv` → `/mnt/user/media/TV`), from a
`/tmp` staging directory with `TVD_CONFIG` pointed at `/tmp`. GET requests only; no
deletion, no `/boot` write, no state written to appdata.

| Check | Result |
|---|---|
| `test-instance` | Sonarr answered; every mapped series folder resolved on disk |
| `match` | The folder `News & Talk/Daily Show, The (1996) {tvdb-71256}` matched "The Daily Show" by folder path |
| `preview` (180-day rule) | 66 episodes considered, 1 selected, 65 kept, 0 unknown to Sonarr, 0 protected; the selection carried a real Sonarr air date (2026-03-06) and its reason |

## Not yet exercised

- A live deletion (dry run off). See [ACCEPTANCE.md](ACCEPTANCE.md).
- The plugin installed through the WebGUI, and the generated cron entry firing.
- TMDB air-date filling — no API key configured yet.
- The plugin-managed recycle folder and its retention purge.
- Unraid notifications.
