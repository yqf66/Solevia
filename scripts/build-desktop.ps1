[CmdletBinding()]
param([string]$Output, [string]$Payload, [string]$PayloadHash)
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Output) {$Output=Join-Path $root 'Solevia.exe'}
$framework=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler=Join-Path $framework 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {throw '未找到 Windows .NET Framework 编译组件，请启用或修复 .NET Framework 4.8 后重试。'}
$source=Join-Path $root 'desktop'
$stage=Join-Path ([IO.Path]::GetDirectoryName($Output)) ('.desktop-build-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$temporary=Join-Path $stage ([IO.Path]::GetFileName($Output))
$arguments=@('/nologo','/target:winexe','/platform:x64','/optimize+','/debug-',('/out:'+$temporary),
    ('/win32icon:'+(Join-Path $source 'solevia.ico')),('/win32manifest:'+(Join-Path $source 'app.manifest')),
    '/reference:System.dll','/reference:System.Core.dll','/reference:System.Drawing.dll','/reference:System.Windows.Forms.dll',
    '/reference:System.Web.Extensions.dll','/reference:System.IO.Compression.dll','/reference:System.IO.Compression.FileSystem.dll','/reference:Microsoft.CSharp.dll',
    ('/resource:'+(Join-Path $source 'solevia.ico')+',solevia.icon'),('/resource:'+(Join-Path $source 'solevia-mark.png')+',solevia.mark'))
if ($Payload) {
    if (-not $PayloadHash) {throw '必须提供产品包的校验文件。'}
    $arguments+=@(('/resource:'+$Payload+',solevia.payload'),('/resource:'+$PayloadHash+',solevia.sha256'))
}
$arguments+=@(Get-ChildItem -LiteralPath $source -Filter '*.cs' | ForEach-Object {$_.FullName})
& $compiler @arguments
if ($LASTEXITCODE -ne 0) {throw 'Solevia 桌面入口生成失败。'}
Move-Item -LiteralPath $temporary -Destination $Output -Force
Remove-Item -LiteralPath $stage
Write-Host '桌面入口已准备完成。'
