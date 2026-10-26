$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$script:SoleviaRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:Runtime = Join-Path $script:SoleviaRoot '.runtime'
$script:Data = Join-Path $script:SoleviaRoot 'user-data'
$script:Release = Get-Content -LiteralPath (Join-Path $script:SoleviaRoot 'release.json') -Raw | ConvertFrom-Json

function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    # Meter actual child network traffic once Python exists. No TLS interception,
    # no global proxy change, and no guessed byte total for third-party tools.
    $meterPython=Get-InstalledPython
    if((Test-Path -LiteralPath $meterPython) -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'install_transfer.py'))) {
        $Arguments=@((Join-Path $PSScriptRoot 'install_transfer.py'),$File)+$Arguments
        $File=$meterPython
    }
    $tail=[Collections.Generic.Queue[string]]::new()
    $previousPreference=$ErrorActionPreference
    try {
        $ErrorActionPreference='Continue'
        & $File @Arguments 2>&1 | ForEach-Object {
            $line=$_.ToString()
            if($line -eq 'System.Management.Automation.RemoteException'){return}
            Write-Host $line
            $tail.Enqueue($line);if($tail.Count -gt 16){[void]$tail.Dequeue()}
        }
        $code=$LASTEXITCODE
    } finally {$ErrorActionPreference=$previousPreference}
    if($code -ne 0) {
        $cause=($tail.ToArray() -join "`n")
        $failure=Get-SoleviaFailure $cause
        throw ('['+$failure.code+'] 当前步骤失败（退出码 '+$code+'）。'+$failure.advice+"`n原始原因："+$cause)
    }
}

function Get-SoleviaFileHash {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

. (Join-Path $PSScriptRoot 'install-network.ps1')

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
