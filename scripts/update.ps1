[CmdletBinding()]
param([string]$Package)
. (Join-Path $PSScriptRoot 'common.ps1')
Set-LocalRuntime
$updateArgs = @((Join-Path $PSScriptRoot 'updater.py'))
if ($Package) {$updateArgs += @('--package',$Package)}
Invoke-Checked (Get-InstalledPython) $updateArgs
