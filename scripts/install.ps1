[CmdletBinding()]
param(
    [ValidateSet('Minimal','Standard','Full')][string]$Profile = 'Standard',
    [switch]$NonInteractive,
    [switch]$ChooseFeatures,
    [switch]$Repair,
    [switch]$PlanOnly
)
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'This package requires Windows x64.' }
if ($PlanOnly) {
    [pscustomobject]@{profile=$Profile; python=$Release.python; node=$Release.node.version; modelKeysIncluded=$false; automaticProgramUpdates=$false} | ConvertTo-Json
    exit 0
}
$manifest = Get-Content -LiteralPath (Join-Path $SoleviaRoot 'MANIFEST.json') -Raw | ConvertFrom-Json
foreach ($item in $manifest.files) {
    $candidate = [IO.Path]::GetFullPath((Join-Path $SoleviaRoot $item.path))
    if (-not $candidate.StartsWith($SoleviaRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid manifest path.' }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf) -or (Get-SoleviaFileHash $candidate) -ne $item.sha256) { throw ('Package file is missing or changed: '+$item.path+'. Extract a fresh release package.') }
}
Set-LocalRuntime
$stateFile = Join-Path $Runtime 'installed.json'
$features = @{web=($Profile -ne 'Minimal'); documents=($Profile -ne 'Minimal'); ocr=($Profile -eq 'Full'); semantic=($Profile -eq 'Full')}
if ((Test-Path -LiteralPath $stateFile) -and -not $ChooseFeatures) {
    $previous = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    foreach ($key in @('web','documents','ocr','semantic')) { $features[$key] = [bool]$previous.features.$key }
}
if (-not $NonInteractive -and ($ChooseFeatures -or -not (Test-Path -LiteralPath $stateFile))) {
    Write-Host 'Solevia setup - runtime stays inside this folder; administrator access is not needed.' -ForegroundColor Cyan
    foreach ($entry in @(@('web','Web search and browser reader'),@('documents','PDF and Office document support'),@('ocr','Local OCR models'),@('semantic','Local semantic search model (larger download)'))) {
        $default = if ($features[$entry[0]]) {'Y'} else {'N'}
        $answer = Read-Host ($entry[1] + ' [Y/N, default ' + $default + ']')
        if ($answer) { $features[$entry[0]] = ($answer -match '^(y|yes)$') }
    }
}
$downloads = Join-Path $Runtime 'downloads'
New-Item -ItemType Directory -Path $downloads,$Data -Force | Out-Null
$uv = Join-Path $Runtime 'uv\uv.exe'
$uvReady = $false
if (Test-Path -LiteralPath $uv) { $uvReady = ((& $uv --version) -match ('^uv '+[regex]::Escape([string]$Release.uv.version)+'(?:\s|$)')) }
if (-not $uvReady) {
    Write-Host '[1/6] Preparing verified Python installer...'
    $zip = Join-Path $downloads 'uv.zip'
    Get-VerifiedDownload $Release.uv.url $zip $Release.uv.sha256
    Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $Runtime 'uv') -Force
}
Write-Host '[2/6] Preparing project-local Python...'
Invoke-Checked $uv @('python','install',[string]$Release.python)
$python = Get-InstalledPython
if (-not (Test-Path -LiteralPath $python)) { Invoke-Checked $uv @('venv','--python',[string]$Release.python,(Join-Path $Runtime 'venv')) }
$node = Join-Path $Runtime 'node\node.exe'
$nodeReady = $false
if (Test-Path -LiteralPath $node) { $nodeReady = ((& $node --version) -eq ('v'+$Release.node.version)) }
if (-not $nodeReady) {
    Write-Host '[3/6] Preparing verified Node.js...'
    $zip = Join-Path $downloads 'node.zip'
    Get-VerifiedDownload $Release.node.url $zip $Release.node.sha256
    $nodeStage = Join-Path $Runtime 'node-extract'
    Expand-Archive -LiteralPath $zip -DestinationPath $nodeStage -Force
    $nodeFolder = Join-Path $nodeStage ('node-v'+$Release.node.version+'-win-x64')
    if (Test-Path -LiteralPath (Join-Path $Runtime 'node')) {
        Get-ChildItem -LiteralPath $nodeFolder | Copy-Item -Destination (Join-Path $Runtime 'node') -Recurse -Force
    } else { Move-Item -LiteralPath $nodeFolder -Destination (Join-Path $Runtime 'node') }
}
Write-Host '[4/6] Installing application dependencies...'
$backend = Join-Path $SoleviaRoot 'app\backend'
Push-Location $backend
try {
    Invoke-Checked $uv @('pip','install','--python',$python,'-r',(Join-Path $SoleviaRoot 'requirements-core.lock'))
    Invoke-Checked $uv @('pip','install','--python',$python,'--no-deps','-e','.')
    if ($features.web) { Invoke-Checked $uv @('pip','install','--python',$python,'-e','.[web]') }
    if ($features.documents) { Invoke-Checked $uv @('pip','install','--python',$python,'markitdown[docx,pdf,pptx,xls,xlsx]>=0.1.8,<0.2','pdfplumber>=0.11.10,<1') }
    if ($features.ocr) { Invoke-Checked $uv @('pip','install','--python',$python,'-e','.[ocr]') }
    if ($features.semantic) { Invoke-Checked $uv @('pip','install','--python',$python,'-e','.[semantic]') }
} finally { Pop-Location }
$npm = Join-Path $Runtime 'node\npm.cmd'
$pnpm = Join-Path $Runtime 'pnpm\node_modules\pnpm\bin\pnpm.cjs'
$pnpmReady = $false
if (Test-Path -LiteralPath $pnpm) { $pnpmReady = ((& $node $pnpm --version) -eq [string]$Release.pnpm) }
if (-not $pnpmReady) { Invoke-Checked $npm @('install','--prefix',(Join-Path $Runtime 'pnpm'),('--cache='+(Join-Path $Runtime 'npm-cache')),'--no-audit','--no-fund',('pnpm@'+$Release.pnpm)) }
Push-Location (Join-Path $SoleviaRoot 'app\frontend')
try {
    Invoke-Checked $node @($pnpm,'install','--frozen-lockfile')
    Write-Host '[5/6] Building the interface...'
    Invoke-Checked $node @($pnpm,'build')
} finally { Pop-Location }
if ($features.web) {
    Push-Location (Join-Path $SoleviaRoot 'app\services\web-research')
    try { Invoke-Checked $npm @('ci','--no-audit','--no-fund',('--cache='+(Join-Path $Runtime 'npm-cache'))) } finally { Pop-Location }
    $browserCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    $existingBrowser = $browserCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ($existingBrowser) { Write-Host 'Using your installed browser for web reading.' }
    else {
        $env:PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT = '60000'
        Invoke-Checked $python @('-m','playwright','install','chromium')
    }
}
if ($features.ocr -or $features.semantic) {
    Push-Location $backend
    try {
        $modelArgs = @('tools/setup_material_models.py')
        if ($features.ocr) {$modelArgs += '--ocr'}
        if ($features.semantic) {$modelArgs += '--semantic'}
        Invoke-Checked $python $modelArgs
    } finally { Pop-Location }
}
Write-Host '[6/6] Preparing your private workspace...'
$config = Join-Path $Data '.env'
if (-not (Test-Path -LiteralPath $config)) { Copy-Item -LiteralPath (Join-Path $SoleviaRoot 'backend.env.example') -Destination $config }
$installed = @{version=$Release.version;features=$features;installedAt=[DateTime]::UtcNow.ToString('o')}
$temporaryState = $stateFile+'.new'
$installed | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporaryState -Encoding utf8
Invoke-Checked $python @((Join-Path $PSScriptRoot 'launcher.py'),'--migrate-only','--state-file',$temporaryState)
Move-Item -LiteralPath $temporaryState -Destination $stateFile -Force
Write-Host 'Ready. Double-click Start.cmd, then add your model connection in Solevia.' -ForegroundColor Green
