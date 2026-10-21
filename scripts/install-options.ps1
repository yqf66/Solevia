# Capability names and planning estimates shared by interactive and preview modes.
# MiB/GiB use binary units. Ranges include dependencies; they are not download quotas.
function Get-SoleviaCapabilities {
    @(
        [pscustomobject]@{Key='web'; Name='联网查资料'; Benefit='搜索最新资料、阅读网页正文，并在讨论中查看来源。'; Skip='仍可对话和规划，但不提供内置联网搜索与网页阅读。'; DownloadMiB=@(150,450); DiskMiB=@(500,1400); Memory='网页阅读时额外约 0.5-1.5 GiB'; Technical='网页阅读服务与浏览器自动化组件'},
        [pscustomobject]@{Key='documents'; Name='阅读 PDF / Office 文档'; Benefit='导入文字版 PDF、Word、Excel、PowerPoint，围绕自己的资料讨论和规划。'; Skip='仍可手动输入或粘贴文字，常见文档的自动解析能力不完整。'; DownloadMiB=@(40,150); DiskMiB=@(150,500); Memory='解析文档时额外约 0.1-0.5 GiB'; Technical='文档转换和 PDF 解析组件'},
        [pscustomobject]@{Key='ocr'; Name='识别图片与扫描件中的文字'; Benefit='在本机识别图片和扫描版 PDF 中的文字，适合拍照笔记、扫描教材。'; Skip='文字版文档仍可阅读，图片和纯扫描页不能自动识别文字。'; DownloadMiB=@(80,300); DiskMiB=@(200,800); Memory='识别时额外约 0.3-1 GiB'; Technical='RapidOCR / ONNX Runtime 与文字识别模型'},
        [pscustomobject]@{Key='semantic'; Name='按含义查找资料'; Benefit='在资料库中按语义匹配相关片段，换一种说法也能查找关联内容。'; Skip='保留关键词检索，不启用本地语义匹配。'; DownloadMiB=@(600,3072); DiskMiB=@(2048,8192); Memory='模型加载与检索时额外约 1-3 GiB'; Technical='多语言 E5 向量模型与 PyTorch；不包含本地对话大模型'}
    )
}

function Format-SoleviaSize {
    param([double]$MiB)
    if ($MiB -ge 1024) { return ('{0:0.#} GiB' -f ($MiB/1024)) }
    return ('{0:0} MiB' -f $MiB)
}

function Format-SoleviaRange {
    param($Range)
    return ((Format-SoleviaSize $Range[0])+' - '+(Format-SoleviaSize $Range[1]))
}

function Get-SoleviaInstallPlan {
    param([hashtable]$Features, [bool]$NeedsBrowser = $false)
    # Baseline includes the isolated runtimes, core dependencies and frontend build cache.
    $download = @(150,350); $disk = @(700,1500)
    foreach ($item in Get-SoleviaCapabilities) {
        if ($Features[$item.Key]) {
            $download[0] += $item.DownloadMiB[0]; $download[1] += $item.DownloadMiB[1]
            $disk[0] += $item.DiskMiB[0]; $disk[1] += $item.DiskMiB[1]
        }
    }
    if ($Features.web -and $NeedsBrowser) {
        $download[0] += 150; $download[1] += 250
        $disk[0] += 400; $disk[1] += 700
    }
    [pscustomobject]@{
        features=$Features; downloadMiB=$download; diskMiB=$disk
        extraBrowser=([bool]$Features.web -and $NeedsBrowser)
        estimateOnly=$true; includesCaches=$true
        memoryNote='基础服务与界面粗估约 0.3-0.8 GiB；各能力只在使用时增加占用。不同任务不宜简单相加，实际峰值取决于资料大小、网页及并发量。'
    }
}

function Read-SoleviaChoice {
    param([string]$Prompt, [bool]$Default)
    $label = if ($Default) {'是'} else {'否'}
    while ($true) {
        $answer = Read-Host ($Prompt+' [Y=是 / N=否，回车默认：'+$label+']')
        if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
        switch -Regex ($answer.Trim()) {
            '^(y|yes|是|好|1)$' { return $true }
            '^(n|no|否|不|0)$' { return $false }
            default { Write-Host '请输入 Y 或 N；直接回车保留默认选择。' -ForegroundColor Yellow }
        }
    }
}

function Get-SoleviaFolderBytes {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return 0 }
    $size = Get-ChildItem -LiteralPath $Path -File -Recurse -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum
    return [double]$size.Sum
}
