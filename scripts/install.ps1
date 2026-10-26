[CmdletBinding()]
param(
    [ValidateSet('Minimal','Standard','Full')][string]$Profile = 'Minimal',
    [switch]$NonInteractive,
    [switch]$ChooseFeatures,
    [switch]$Repair,
    [switch]$PlanOnly,
    [switch]$BootstrapOnly,
    [ValidateSet('Keep','Codex','Api')][string]$Kernel='Keep',
    [ValidateSet('Direct','System')][string]$NetworkMode='Direct',
    [ValidateSet('China','Official')][string]$DownloadSource='China',
    [ValidateRange(-1,1)][int]$Web=-1,
    [ValidateRange(-1,1)][int]$Documents=-1,
    [ValidateRange(-1,1)][int]$Ocr=-1,
    [ValidateRange(-1,1)][int]$Semantic=-1
)
. (Join-Path $PSScriptRoot 'common.ps1')
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'install-options.ps1')
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw '此安装包需要 Windows 64 位（x64），不适用于 ARM64。' }
$features = @{web=($Profile -ne 'Minimal'); documents=($Profile -ne 'Minimal'); ocr=($Profile -eq 'Full'); semantic=($Profile -eq 'Full')}
$stateFile = Join-Path $Runtime 'installed.json'
if (Test-Path -LiteralPath $stateFile) {
    $previous = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    foreach ($key in @('web','documents','ocr','semantic')) { $features[$key] = [bool]$previous.features.$key }
}
foreach($entry in @(@('web',$Web),@('documents',$Documents),@('ocr',$Ocr),@('semantic',$Semantic))) {
    if($entry[1] -ne -1){$features[$entry[0]]=($entry[1] -eq 1)}
}
$browserCandidates = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
    (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
    (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
)
$existingBrowser = $browserCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
$cachedBrowser = Get-ChildItem -Path (Join-Path $Runtime 'browsers\chromium-*\chrome-win*\chrome.exe') -ErrorAction SilentlyContinue | Select-Object -First 1
if ($PlanOnly) {
    [pscustomobject]@{kernel=$Kernel;profile=$Profile; python=$Release.python; node=$Release.node.version; modelKeysIncluded=$false; automaticProgramUpdates=$false; capabilities=@(Get-SoleviaCapabilities); plan=(Get-SoleviaInstallPlan $features (-not ($existingBrowser -or $cachedBrowser)))} | ConvertTo-Json -Depth 6
    exit 0
}
$manifest = Get-Content -LiteralPath (Join-Path $SoleviaRoot 'MANIFEST.json') -Raw | ConvertFrom-Json
foreach ($item in $manifest.files) {
    $candidate = [IO.Path]::GetFullPath((Join-Path $SoleviaRoot $item.path))
    if (-not $candidate.StartsWith($SoleviaRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw '安装清单中存在无效文件路径。' }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf) -or (Get-SoleviaFileHash $candidate) -ne $item.sha256) { throw ('安装文件缺失或已被修改：'+$item.path+'。请重新下载并完整解压安装包。') }
}
Write-Host "`n欢迎安装 Solevia · 足下" -ForegroundColor Cyan
Write-Host '日常对话、规划、任务卡片、日历和番茄钟属于基础功能，始终安装。'
Write-Host '对话与规划连接你配置的模型服务，无需下载本地大语言模型，也无需管理员权限。'
Write-Host '以下是首次安装的粗估：下载量 = 网络流量；磁盘占用 = 文件与缓存；运行内存 = 使用时的 RAM。'
Write-Host '基础环境：下载约 150-350 MiB，磁盘约 700 MiB-1.5 GiB（含构建和下载缓存）。'
Write-Host '可选依赖版本与共享缓存会影响实际大小；已有文件会复用，重试不等于再次完整下载。'
if (-not $NonInteractive -and ($ChooseFeatures -or -not (Test-Path -LiteralPath $stateFile))) {
    foreach ($entry in Get-SoleviaCapabilities) {
        Write-Host ("`n"+$entry.Name) -ForegroundColor Cyan
        Write-Host ('  安装后：'+$entry.Benefit)
        Write-Host ('  不安装：'+$entry.Skip)
        Write-Host ('  额外下载约 '+(Format-SoleviaRange $entry.DownloadMiB)+'；磁盘约 '+(Format-SoleviaRange $entry.DiskMiB)+'。')
        Write-Host ('  内存：'+$entry.Memory+'。')
        if ($entry.Key -eq 'web') {
            if ($existingBrowser -or $cachedBrowser) { Write-Host '  已找到可用浏览器，不再下载浏览器组件。' }
            else { Write-Host '  未找到浏览器，另需下载约 150-250 MiB，额外磁盘约 400-700 MiB。' }
        }
        $features[$entry.Key] = Read-SoleviaChoice ('是否启用「'+$entry.Name+'」？') $features[$entry.Key]
    }
}
$plan = Get-SoleviaInstallPlan $features (-not ($existingBrowser -or $cachedBrowser))
$selected = @((Get-SoleviaCapabilities) | Where-Object { $features[$_.Key] } | ForEach-Object { $_.Name })
Write-Host "`n本次安装内容" -ForegroundColor Cyan
Write-Host ('  基础功能'+ $(if ($selected.Count) {' + '+($selected -join '、')} else {'（未选择可选能力）'}))
Write-Host ('  首次下载粗估：'+(Format-SoleviaRange $plan.downloadMiB))
Write-Host ('  磁盘粗估：'+(Format-SoleviaRange $plan.diskMiB)+'，含缓存；安装临时解压建议另留 2 GiB。')
Write-Host ('  '+$plan.memoryNote)
Write-Host '  这些是容量规划范围，不是已测得的内存值；安装结束将报告实际文件占用。'
Write-Host '  以后可在桌面工作台点击「补装与管理能力」，或用 Models.cmd 补装；取消已安装能力只会停用，不会自动删除已有依赖或释放磁盘。'
if (-not $NonInteractive -and -not (Read-SoleviaChoice '确认开始安装 / 修复？' $true)) {
    Write-Host '已取消，尚未下载或安装组件。'; exit 0
}
Set-SoleviaInstallNetwork $NetworkMode $DownloadSource
Assert-SoleviaInstallSpace $plan.diskMiB[1]
Write-Host '[说明] 下载安装工具、Python、Node.js、依赖、浏览器和模型是不同步骤。下载大小与内存占用不同；出现问题请保留安装目录与详细日志。'
Set-LocalRuntime
$installGuard=$null;$serviceGuard=$null
try {
    try {
        $installGuard=[IO.File]::Open((Join-Path $Runtime 'install.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $serviceGuard=[IO.File]::Open((Join-Path $Runtime 'supervisor.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    } catch {throw '另一个安装任务或 Solevia 服务仍在运行，请先退出后再重试。'}
    Set-Content -LiteralPath (Join-Path $Runtime 'install.in-progress') -Value $PID -Encoding ascii
$downloads = Join-Path $Runtime 'downloads'
New-Item -ItemType Directory -Path $downloads,$Data -Force | Out-Null
$uv = Join-Path $Runtime 'uv\uv.exe'
$uvReady = $false
if (Test-Path -LiteralPath $uv) { $uvReady = ((& $uv --version) -match ('^uv '+[regex]::Escape([string]$Release.uv.version)+'(?:\s|$)')) }
if (-not $uvReady) {
    Write-Host '[1/6] 准备并校验安装工具...'
    $zip = Join-Path $downloads 'uv.zip'
    Get-VerifiedDownload $Release.uv.url $zip $Release.uv.sha256 '安装工具（源码安装）'
    Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $Runtime 'uv') -Force
}
if($BootstrapOnly){Write-Host '[完成] 安装工具已就绪，诊断模式到此结束，未下载运行环境。';exit 0}
Write-Host '[2/6] 准备独立运行环境（Python，下载来源 Astral 官方发布服务 / GitHub；已有环境会复用）...'
Invoke-Checked $uv @('python','install','--no-bin',[string]$Release.python)
$python = Get-InstalledPython
if (-not (Test-Path -LiteralPath $python)) { Invoke-Checked $uv @('venv','--python',[string]$Release.python,(Join-Path $Runtime 'venv')) }
$node = Join-Path $Runtime 'node\node.exe'
$nodeReady = $false
if (Test-Path -LiteralPath $node) { $nodeReady = ((& $node --version) -eq ('v'+$Release.node.version)) }
if (-not $nodeReady) {
    Write-Host '[3/6] 准备界面运行环境（Node.js）...'
    $zip = Join-Path $downloads 'node.zip'
    $nodeUrl=[string]$Release.node.url
    if($DownloadSource -eq 'China') {$nodeUrl=$nodeUrl.Replace('https://nodejs.org/dist/','https://npmmirror.com/mirrors/node/')}
    try {Get-VerifiedDownload $nodeUrl $zip $Release.node.sha256 '界面运行环境'}
    catch {if($DownloadSource -ne 'China'){throw};Write-Host '[切换来源] Node.js 镜像下载失败，改用官方源，仍使用同一官方 SHA-256 校验。';Get-VerifiedDownload $Release.node.url $zip $Release.node.sha256 '界面运行环境（官方备用源）'}
    $nodeStage = Join-Path $Runtime 'node-extract'
    Expand-Archive -LiteralPath $zip -DestinationPath $nodeStage -Force
    $nodeFolder = Join-Path $nodeStage ('node-v'+$Release.node.version+'-win-x64')
    if (Test-Path -LiteralPath (Join-Path $Runtime 'node')) {
        Get-ChildItem -LiteralPath $nodeFolder | Copy-Item -Destination (Join-Path $Runtime 'node') -Recurse -Force
    } else { Move-Item -LiteralPath $nodeFolder -Destination (Join-Path $Runtime 'node') }
}
Write-Host '[4/6] 安装所选能力及依赖（下方第三方工具可能显示英文日志）...'
$backend = Join-Path $SoleviaRoot 'app\backend'
Push-Location $backend
try {
    Invoke-Checked $uv @('pip','install','--python',$python,'-r',(Join-Path $SoleviaRoot 'requirements-core.lock'))
    Invoke-Checked $uv @('pip','install','--python',$python,'--no-deps','-e','.')
    $dependencyArgs=@('pip','install','--python',$python,'-c',(Join-Path $SoleviaRoot 'requirements-core.lock'))
    foreach($capability in @('web','documents','ocr','semantic')) {
        if($features[$capability]) {$dependencyArgs+=@('-r',(Join-Path $SoleviaRoot ('requirements\'+$capability+'.lock')))}
    }
    if($dependencyArgs.Count -gt 6) {Invoke-Checked $uv $dependencyArgs}
} finally { Pop-Location }
$npm = Join-Path $Runtime 'node\npm.cmd'
$pnpm = Join-Path $Runtime 'pnpm\node_modules\pnpm\bin\pnpm.cjs'
$pnpmReady = $false
if (Test-Path -LiteralPath $pnpm) { $pnpmReady = ((& $node $pnpm --version) -eq [string]$Release.pnpm) }
if (-not $pnpmReady) { Invoke-Checked $npm @('install','--prefix',(Join-Path $Runtime 'pnpm'),('--cache='+(Join-Path $Runtime 'npm-cache')),'--no-audit','--no-fund',('pnpm@'+$Release.pnpm)) }
Push-Location (Join-Path $SoleviaRoot 'app\frontend')
try {
    Invoke-Checked $node @($pnpm,'install','--frozen-lockfile')
    Write-Host '[5/6] 准备 Solevia 界面...'
    Invoke-Checked $node @($pnpm,'build')
} finally { Pop-Location }
if ($features.web) {
    Push-Location (Join-Path $SoleviaRoot 'app\services\web-research')
    try { Invoke-Checked $npm @('ci','--no-audit','--no-fund',('--cache='+(Join-Path $Runtime 'npm-cache'))) } finally { Pop-Location }
    if ($existingBrowser -or $cachedBrowser) { Write-Host '联网阅读：复用已有浏览器。' }
    else {
        $env:PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT = '60000'
        Invoke-Checked $python @('-m','playwright','install','chromium')
    }
}
if ($features.ocr -or $features.semantic) {
    Push-Location $backend
    Write-Host '[本地模型] 正在准备识字 / 语义查找模型。首次可能较慢，模型文件与缓存会保留，重试可复用。'
    try {
        $modelArgs = @('tools/setup_material_models.py')
        if ($features.ocr) {$modelArgs += '--ocr'}
        if ($features.semantic) {$modelArgs += '--semantic'}
        Invoke-Checked $python $modelArgs
    } finally { Pop-Location }
}
Write-Host '[6/6] 准备个人工作区与数据库...'
$config = Join-Path $Data '.env'
if (-not (Test-Path -LiteralPath $config)) { Copy-Item -LiteralPath (Join-Path $SoleviaRoot 'backend.env.example') -Destination $config }
if ($Kernel -ne 'Keep') { Invoke-Checked $python @((Join-Path $PSScriptRoot 'connection_setup.py'),'--root',$SoleviaRoot,'--kernel',$Kernel.ToLowerInvariant()) }
$installed = @{version=$Release.version;features=$features;installedAt=[DateTime]::UtcNow.ToString('o')}
$temporaryState = $stateFile+'.new'
$installed | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporaryState -Encoding utf8
$serviceGuard.Dispose();$serviceGuard=$null
Invoke-Checked $python @((Join-Path $PSScriptRoot 'launcher.py'),'--migrate-only','--state-file',$temporaryState)
if ((Test-Path -LiteralPath (Join-Path $PSScriptRoot 'build-desktop.ps1')) -and -not (Test-Path -LiteralPath (Join-Path $SoleviaRoot 'Solevia.exe'))) {
    & (Join-Path $PSScriptRoot 'build-desktop.ps1')
}
Move-Item -LiteralPath $temporaryState -Destination $stateFile -Force
$runtimeBytes = Get-SoleviaFolderBytes $Runtime
$appBytes = Get-SoleviaFolderBytes (Join-Path $SoleviaRoot 'app')
Write-Host ('实际文件占用：运行环境、模型与缓存 '+(Format-SoleviaSize ($runtimeBytes/1MB))+'；应用与依赖 '+(Format-SoleviaSize ($appBytes/1MB))+'。不含个人资料；共享文件按逻辑大小统计。')
Write-Host '安装完成！双击 Solevia.exe 打开工作台。所选连接方式已保存；Codex 使用本机登录，API 在「模型连接」中配置。' -ForegroundColor Green
} catch {
    $failure=Get-SoleviaFailure $_.Exception.Message
    Write-Host ('['+$failure.code+'] '+$failure.advice)
    Write-Host ('[原始原因] '+$_.Exception.Message)
    Write-Host '[排查] EXE 安装日志位于安装目录 user-data/logs/install.log；分享前请检查并遮盖个人信息。可先只选基础功能，再逐项补装。'
    throw
} finally {
    if($serviceGuard){$serviceGuard.Dispose()}
    if($installGuard){Remove-Item -LiteralPath (Join-Path $Runtime 'install.in-progress') -ErrorAction SilentlyContinue;$installGuard.Dispose()}
}
