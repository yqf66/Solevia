[CmdletBinding()]
param([switch]$Stop,[switch]$NoBrowser,[switch]$SkipUpdateCheck,[switch]$CheckOnly)
. (Join-Path $PSScriptRoot 'common.ps1')
Set-LocalRuntime
if (-not (Test-Path -LiteralPath (Join-Path $Runtime 'installed.json'))) {
    if ($Stop -or $CheckOnly) { throw 'Solevia is not installed. Run Install.cmd first.' }
    & (Join-Path $PSScriptRoot 'install.ps1')
    if (-not (Test-Path -LiteralPath (Join-Path $Runtime 'installed.json'))) { throw 'Installation did not finish.' }
}
$launcherArgs = @((Join-Path $PSScriptRoot 'launcher.py'))
if ($Stop) {$launcherArgs += '--stop'}
if ($NoBrowser) {$launcherArgs += '--no-browser'}
if ($SkipUpdateCheck) {$launcherArgs += '--skip-update-check'}
if ($CheckOnly) {$launcherArgs += '--check-only'}
Invoke-Checked (Get-InstalledPython) $launcherArgs
