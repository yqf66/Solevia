# Install-only networking and readable diagnostics; never change machine settings.
function Set-SoleviaInstallNetwork {
    param([string]$Mode='Direct',[string]$Source='China')
    if ($Mode -eq 'Direct') {
        foreach($key in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')) { [Environment]::SetEnvironmentVariable($key,$null,'Process') }
        [Net.WebRequest]::DefaultWebProxy=[Net.GlobalProxySelection]::GetEmptyWebProxy()
        Write-Host '[网络] 直连模式：本次安装不使用系统或环境代理。'
    } else {
        $proxyAddress=@($env:HTTPS_PROXY,$env:HTTP_PROXY,$env:ALL_PROXY) | Where-Object {$_} | Select-Object -First 1
        if($proxyAddress) {
            $proxyUri=$null
            if(-not [Uri]::TryCreate($proxyAddress,[UriKind]::Absolute,[ref]$proxyUri) -or $proxyUri.Scheme -ne 'http') {throw '[NETWORK_PROXY] 当前文件下载代理需要 http:// 格式的 HTTP CONNECT 代理。请调整代理地址，或使用系统 HTTP 代理 / 直连模式。'}
            $proxy=[Net.WebProxy]::new($proxyUri.GetLeftPart([UriPartial]::Authority))
            if($proxyUri.UserInfo) {
                $credentials=$proxyUri.UserInfo.Split(':',2)
                $proxy.Credentials=[Net.NetworkCredential]::new([Uri]::UnescapeDataString($credentials[0]),$(if($credentials.Length -gt 1){[Uri]::UnescapeDataString($credentials[1])}else{''}))
            }
            [Net.WebRequest]::DefaultWebProxy=$proxy
        } else {
            [Net.WebRequest]::DefaultWebProxy=[Net.WebRequest]::GetSystemWebProxy()
            $testUri=[Uri]'https://pypi.org/simple'
            $systemProxy=[Net.WebRequest]::DefaultWebProxy.GetProxy($testUri)
            if($systemProxy -and $systemProxy.Host -ne $testUri.Host) {
                if($systemProxy.Scheme -ne 'http'){throw '[NETWORK_PROXY] 当前系统代理不是 HTTP CONNECT 代理，请改用 HTTP 代理或直连。'}
                $env:HTTPS_PROXY=$systemProxy.AbsoluteUri;$env:HTTP_PROXY=$systemProxy.AbsoluteUri;$env:ALL_PROXY=$systemProxy.AbsoluteUri
            } else {Write-Host '[网络提示] 没有检测到系统代理，本次使用直连。若需要代理，请先配置系统 HTTP 代理或代理环境变量。'}
        }
        Write-Host '[网络] 使用系统 / 环境代理；请确认代理服务已启动。代理凭据不会写入安装提示。'
    }
    # Public downloads do not require provider or Hugging Face account credentials.
    foreach($key in @('HF_TOKEN','HUGGING_FACE_HUB_TOKEN')) {[Environment]::SetEnvironmentVariable($key,$null,'Process')}
    $env:UV_HTTP_TIMEOUT='120';$env:UV_HTTP_RETRIES='3'
    foreach($key in @('UV_INDEX','UV_INDEX_URL','UV_EXTRA_INDEX_URL','PIP_EXTRA_INDEX_URL')) {[Environment]::SetEnvironmentVariable($key,$null,'Process')}
    $env:npm_config_fetch_retries='3';$env:npm_config_fetch_timeout='120000'
    if ($Source -eq 'China') {
        $env:UV_DEFAULT_INDEX='https://pypi.tuna.tsinghua.edu.cn/simple'
        $env:PIP_INDEX_URL=$env:UV_DEFAULT_INDEX
        $env:npm_config_registry='https://registry.npmmirror.com'
        $env:HF_ENDPOINT='https://hf-mirror.com'
        Write-Host '[下载源] 国内镜像：Python 依赖使用清华 PyPI，界面依赖使用 npmmirror，语义模型使用 hf-mirror。'
        Write-Host '[下载源] 镜像由第三方运营；Python 运行环境由 uv 从 Astral 官方发布服务 / GitHub 获取，OCR 模型可能访问其上游。失败后可切换官方源重试。'
    } else {
        $env:UV_DEFAULT_INDEX='https://pypi.org/simple';$env:PIP_INDEX_URL=$env:UV_DEFAULT_INDEX
        $env:npm_config_registry='https://registry.npmjs.org';$env:HF_ENDPOINT='https://huggingface.co'
        Write-Host '[下载源] 官方：Python 依赖 pypi.org，界面依赖 registry.npmjs.org，语义模型 huggingface.co。'
    }
    $script:SoleviaDownloadSource=$Source
}

function Get-SoleviaFailure {
    param([string]$Text)
    $code='INSTALL_STEP';$advice='展开详细日志，查看失败前的步骤和原始原因；保留当前目录后重试，已有缓存会复用。'
    switch -Regex ($Text) {
        '空间|No space|disk full|磁盘已满|112\b' {$code='DISK_SPACE';$advice='检查安装盘和系统临时目录所在盘的可用空间；释放空间或选择其他磁盘后重试。';break}
        '内存|out of memory|MemoryError|paging file|页面文件' {$code='MEMORY';$advice='关闭其他占内存的程序，先只安装基础功能；图片识别和语义查找可稍后补装。';break}
        '拒绝访问|Access.*denied|Permission|UnauthorizedAccess' {$code='PERMISSION';$advice='选择当前账户可写的普通文件夹，并检查安全软件是否拦截；无需关闭安全软件。';break}
        '校验|checksum|hash mismatch' {$code='HASH_MISMATCH';$advice='文件未通过完整性校验，未使用该下载文件。请重试或重新从官方下载完整安装包。';break}
        'proxy|代理|407' {$code='NETWORK_PROXY';$advice='代理可能未启动或需要认证。裸网安装请选择直连；使用代理时确认系统或环境代理可用。';break}
        'SSL|TLS|certificate|证书|安全通道' {$code='NETWORK_TLS';$advice='检查系统日期、证书和网络安全拦截；切换下载源后重试，不要关闭证书校验。';break}
        'timeout|timed out|超时|连接|connect|refused|unreachable|resolve|network|下载|fetch|请求|远程|HTTP|error sending' {$code='NETWORK_DOWNLOAD';$advice='检查联网状态并切换国内镜像 / 官方源。代理模式请确认代理服务已启动。部分运行环境访问官方发布服务 / GitHub；若来源不可达，可使用可用的系统代理后重试。';break}
    }
    [pscustomobject]@{code=$code;advice=$advice}
}

function Assert-SoleviaInstallSpace {
    param([double]$MinimumMiB)
    $rootDrive=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($SoleviaRoot))
    $tempDrive=[IO.DriveInfo]::new([IO.Path]::GetPathRoot([IO.Path]::GetTempPath()))
    $needed=$MinimumMiB*1MB
    if($rootDrive.Name -eq $tempDrive.Name){$needed+=2GB}
    Write-Host ('[空间] 安装盘可用 {0:0.0} GiB；本次预检至少需要 {1:0.0} GiB（按所选能力上限估算，含临时空间）。' -f ($rootDrive.AvailableFreeSpace/1GB),($needed/1GB))
    if($rootDrive.AvailableFreeSpace -lt $needed -or ($rootDrive.Name -ne $tempDrive.Name -and $tempDrive.AvailableFreeSpace -lt 2GB)) {throw ('[DISK_SPACE] 空间不足。安装盘 '+$rootDrive.Name+' 当前可用 '+[Math]::Round($rootDrive.AvailableFreeSpace/1GB,1)+' GiB，需预留 '+[Math]::Round($needed/1GB,1)+' GiB；临时盘 '+$tempDrive.Name+' 需另留 2 GiB（同盘已计入）。请换盘、清理空间或减少可选能力。')}
    try {
        $memory=Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        Write-Host ('[内存] 本机物理内存约 {0:0.0} GiB，当前可用约 {1:0.0} GiB。磁盘预检与内存检查分开进行。' -f ($memory.TotalVisibleMemorySize/1MB),($memory.FreePhysicalMemory/1MB))
        if($memory.TotalVisibleMemorySize -lt 4*1MB -or $memory.FreePhysicalMemory -lt 1*1MB) {Write-Host '[内存提示] 当前内存较紧张，建议关闭其他程序，并先只安装基础功能；图片识别和语义查找可稍后补装。'}
    } catch {Write-Host '[内存提示] 未能读取内存信息；建议基础功能至少 4 GiB，启用图片识别或语义查找建议 8 GiB 以上。'}
}

function Get-VerifiedDownload {
    param([string]$Url,[string]$Destination,[string]$Sha256,[string]$Label='运行组件')
    if($Url -notmatch '^https://'){throw '下载地址必须使用 HTTPS。'}
    if((Test-Path -LiteralPath $Destination) -and (Get-SoleviaFileHash $Destination) -eq $Sha256){Write-Host ('[缓存] '+$Label+'已校验，跳过下载。');return}
    $partial=$Destination+'.partial'
    for($attempt=1;$attempt -le 3;$attempt++) {
        $response=$null;$inputStream=$null;$outputStream=$null
        try {
            Write-Host ('[下载] '+$Label+' · '+([Uri]$Url).Host+' · 第 '+$attempt+'/3 次；连接最长等待 30 秒。')
            $request=[Net.HttpWebRequest]::Create($Url);$request.Timeout=30000;$request.ReadWriteTimeout=30000
            $request.UserAgent='Solevia-Installer';$response=$request.GetResponse()
            if($response.ContentLength -gt 128MB){throw '下载文件大小异常，已停止获取并保留当前安装。'}
            $inputStream=$response.GetResponseStream();$outputStream=[IO.File]::Create($partial)
            $buffer=New-Object byte[] 65536;$total=0L;$last=0L;$clock=[Diagnostics.Stopwatch]::StartNew();$lastTime=0.0;$slow=0
            Write-Host ('[DOWNLOAD_PROGRESS] bytes=0 total='+$response.ContentLength+' speed=0')
            while($true) {
                $pending=$inputStream.BeginRead($buffer,0,$buffer.Length,$null,$null)
                $readStarted=$clock.Elapsed.TotalSeconds
                try {
                    while(-not $pending.AsyncWaitHandle.WaitOne(1000)) {
                        $now=$clock.Elapsed.TotalSeconds
                        if($now-$lastTime -ge 2) {
                            $speed=($total-$last)/($now-$lastTime)/1KB
                            Write-Host ('[DOWNLOAD_PROGRESS] bytes='+$total+' total='+$response.ContentLength+' speed='+[long]($speed*1KB))
                            if($speed -lt 100){$slow+=($now-$lastTime)}else{$slow=0}
                            if($slow -ge 10){Write-Host '[网速提示] 当前下载较慢或暂未收到数据，请耐心等待；连接长时间没有数据会自动重试。';$slow=0}
                            $last=$total;$lastTime=$now
                        }
                        if($now-$readStarted -ge 30){$request.Abort();throw '下载读取超时。'}
                    }
                    $read=$inputStream.EndRead($pending)
                } finally {$pending.AsyncWaitHandle.Close()}
                if($read -eq 0){break}
                $outputStream.Write($buffer,0,$read);$total+=$read;$now=$clock.Elapsed.TotalSeconds
                if($total -gt 128MB){$request.Abort();throw '下载文件大小异常，已停止获取并保留当前安装。'}
                if($now-$lastTime -ge .5) {
                    $speed=($total-$last)/($now-$lastTime)/1KB
                    $extent=if($response.ContentLength -gt 0){' / '+('{0:0.0} MiB' -f ($response.ContentLength/1MB))}else{'（总大小由下载源提供）'}
                    $remaining=if($response.ContentLength -gt 0 -and $speed -gt 0){' · 预计剩余 '+[int](($response.ContentLength-$total)/($speed*1KB))+' 秒'}else{''}
                    Write-Host ('[下载进度] {0}：{1:0.0} MiB{2} · {3:0} KiB/s · 已用 {4:0} 秒{5}' -f $Label,($total/1MB),$extent,$speed,$now,$remaining)
                    Write-Host ('[DOWNLOAD_PROGRESS] bytes='+$total+' total='+$response.ContentLength+' speed='+[long]($speed*1KB))
                    if($speed -lt 100){$slow+=($now-$lastTime)}else{$slow=0}
                    if($slow -ge 10){Write-Host '[网速提示] 当前下载速度较慢，请耐心等待。已有下载仍在继续；可在失败后切换下载源重试。';$slow=0}
                    $last=$total;$lastTime=$now
                }
            }
            $outputStream.Dispose();$outputStream=$null
            if((Get-SoleviaFileHash $partial) -ne $Sha256){throw '下载文件校验失败，本文件尚未安装。'}
            Move-Item -LiteralPath $partial -Destination $Destination -Force
            $average=[long]($total/[Math]::Max(.001,$clock.Elapsed.TotalSeconds))
            Write-Host ('[DOWNLOAD_PROGRESS] bytes='+$total+' total='+$total+' speed='+$average)
            Write-Host ('[下载完成] {0}，完整性校验通过；共 {1:0.0} MiB，用时 {2:0.0} 秒，平均 {3:0} KiB/s。' -f $Label,($total/1MB),$clock.Elapsed.TotalSeconds,($average/1KB));return
        } catch {
            $failure=Get-SoleviaFailure $_.Exception.Message
            Write-Host ('['+$failure.code+'] '+$_.Exception.Message)
            if($attempt -eq 3 -or $failure.code -eq 'HASH_MISMATCH'){throw ('['+$failure.code+'] '+$Label+'下载失败。'+$failure.advice+' 原因：'+$_.Exception.Message)}
            Write-Host '[重试] 等待片刻后重新连接；尚未安装不完整文件。';Start-Sleep -Seconds $attempt
        } finally {if($inputStream){$inputStream.Dispose()};if($outputStream){$outputStream.Dispose()};if($response){$response.Dispose()}}
    }
}
