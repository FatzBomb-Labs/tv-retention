# Windows entry point for the validation that tools/check-on-host.sh performs.
#
# There is no bash and no working fcntl on the development box, so the suite cannot run
# here: `worker/main.py` imports fcntl at module scope, which takes server.py and every
# test that reaches it down with it, and `core.normalise` calls os.path.normpath, which
# rewrites "/tv/x" to "\tv\x" on Windows and fails a mapping test that is correct in the
# container. Both are artifacts of the platform rather than faults, and neither is worth
# working around in the source: the container is Linux, so validation belongs on Linux.
#
# This runs the same pipeline as the shell script — stage the source under /tmp on the
# host, run it there, delete it — by reusing the script's own remote half rather than
# restating it, so the two can never drift apart.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$target = if ($env:TVR_HOST) { $env:TVR_HOST } else { 'FatzServer' }

$script = Get-Content (Join-Path $PSScriptRoot 'check-on-host.sh') -Raw
# The remote half is the single-quoted argument to ssh: everything between the first
# quote on the ssh line and the closing quote at column 0.
$remote = [regex]::Match($script, "(?ms)\|\s*ssh[^']*'(.*)^'\s*$").Groups[1].Value
if (-not $remote) { throw 'Could not read the remote half of check-on-host.sh' }
$remote = $remote -replace "`r`n", "`n"

# The remote half travels base64-encoded. Passed as a literal argument it would cross
# PowerShell's native-command quoting, which strips the double quotes around
# `trap "rm -rf $staging"` and leaves bash reading `-rf` as a signal name. Base64 is
# alphanumerics, `+`, `/` and `=`, so nothing on the command line needs quoting at all.
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
$remotePath = '/tmp/tvr-check.sh'

# The tarball travels as a file rather than through a pipe. Under `powershell -File`,
# which is how this is invoked, a native-to-native pipe is not a byte stream: PowerShell
# decodes the producer's output as text and re-encodes it for the consumer, so `tar -cf -
# | ssh` delivers a corrupt archive — "tar: Skipping to next header" — while the same
# pipeline typed at an interactive prompt works. Writing the archive, then handing the
# whole file to ssh's stdin, keeps PowerShell out of the middle of it.
$archive = Join-Path ([IO.Path]::GetTempPath()) ("tvr-check-{0}.tar" -f [guid]::NewGuid())

Push-Location $root
try {
    tar -cf $archive src tests tools VERSION BUILD
    if ($LASTEXITCODE -ne 0) { throw 'Could not stage the source archive' }

    ssh -n $target ('printf %s ' + $encoded + ' | base64 -d > ' + $remotePath)
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the remote script on $target" }

    $run = 'bash ' + $remotePath + '; rc=$?; rm -f ' + $remotePath + '; exit $rc'
    & $env:ComSpec /c "ssh $target `"$run`" < `"$archive`""
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    Remove-Item $archive -ErrorAction SilentlyContinue
}
if ($code -ne 0) { Write-Error "Validation failed on $target (exit $code)" }
exit $code
