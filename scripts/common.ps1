$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$script:SoleviaRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:Runtime = Join-Path $script:SoleviaRoot '.runtime'
$script:Data = Join-Path $script:SoleviaRoot 'user-data'
$script:Release = Get-Content -LiteralPath (Join-Path $script:SoleviaRoot 'release.json') -Raw | ConvertFrom-Json

function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $([IO.Path]::GetFileName($File)). Run Install.cmd again to retry." }
}

function Get-SoleviaFileHash {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Get-VerifiedDownload {
    param([string]$Url, [string]$Destination, [string]$Sha256)
    if ($Url -notmatch '^https://') { throw 'Download requires HTTPS.' }
    if ((Test-Path -LiteralPath $Destination) -and (Get-SoleviaFileHash $Destination) -eq $Sha256) { return }
    $partial = $Destination + '.partial'
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $partial -TimeoutSec 600
    if ((Get-SoleviaFileHash $partial) -ne $Sha256) { throw 'Download checksum mismatch. Nothing was installed.' }
    Move-Item -LiteralPath $partial -Destination $Destination -Force
}

function Set-LocalRuntime {
    New-Item -ItemType Directory -Path $script:Runtime -Force | Out-Null
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $script:Runtime 'python'
    $env:UV_CACHE_DIR = Join-Path $script:Runtime 'uv-cache'
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $script:Runtime 'browsers'
    $env:SOLEVIA_DATA_DIR = $script:Data
    $env:SOLEVIA_ENV_FILE = Join-Path $script:Data '.env'
    $env:HF_HOME = Join-Path $script:Runtime 'models'
    $env:PYTHONUTF8 = '1'
    $env:PATH = (Join-Path $script:Runtime 'node') + ';' + $env:PATH
}

function Get-InstalledPython { return (Join-Path $script:Runtime 'venv\Scripts\python.exe') }
