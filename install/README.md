# Installing TV Delete

`tv-delete.plg` is the complete installer: the Slackware package is embedded in the
manifest and verified by SHA256 during installation, so no repository or internet access
is required.

## Through the WebGUI

Unraid → **Plugins** → **Install Plugin** → paste the path to `tv-delete.plg` → *Install*.

If the file is not already on the server, copy it there first, for example to
`/boot/config/plugins/tv-delete.plg`, and use that path.

## From the command line

```bash
plugin install /boot/config/plugins/tv-delete.plg
```

## After installing

The plugin appears at **Tools → TV Delete**. It starts with **Dry run on** and **no
schedule**, so it cannot delete anything until you configure it and explicitly turn dry
run off.

## Uninstalling

Plugins → TV Delete → *Remove*. This removes the package and the plugin's cron entry.
Settings (`/boot/config/plugins/tv-delete/settings.json`), run history, and journals are
deliberately left in place so a reinstall resumes where you left off. Delete that folder
by hand if you want a clean slate.

## Verifying the download

`SHA256SUMS` holds the checksum of the manifest:

```bash
sha256sum -c SHA256SUMS
```
