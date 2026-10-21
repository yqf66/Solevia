[CmdletBinding()]
param([int]$ParentId,[string]$Tag,[string]$Package)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
. (Join-Path $PSScriptRoot 'common.ps1')
Set-LocalRuntime
[Windows.Forms.Application]::EnableVisualStyles()
$form=New-Object Windows.Forms.Form
$form.Text='Solevia · 更新'; $form.Size=New-Object Drawing.Size(720,480);$form.StartPosition='CenterScreen'
$form.Font=New-Object Drawing.Font('Microsoft YaHei UI',10)
$form.BackColor=[Drawing.Color]::FromArgb(238,244,249);$form.ForeColor=[Drawing.Color]::FromArgb(30,47,67)
$form.Padding=New-Object Windows.Forms.Padding(24)
$iconPath=Join-Path $SoleviaRoot 'desktop\solevia.ico';if(Test-Path -LiteralPath $iconPath){$form.Icon=New-Object Drawing.Icon($iconPath)}
$heading=New-Object Windows.Forms.Label;$heading.Text='Solevia · 足下';$heading.Font=New-Object Drawing.Font('Microsoft YaHei UI',19,[Drawing.FontStyle]::Bold);$heading.Dock='Top';$heading.Height=54
$label=New-Object Windows.Forms.Label;$label.Text='正在准备更新，请保持此窗口打开。';$label.Dock='Top';$label.Height=65
$label.Font=New-Object Drawing.Font('Microsoft YaHei UI',11)
$explanation=New-Object Windows.Forms.Label;$explanation.Text='更新会保留个人配置、资料和执行记录。完成后自动重新打开 Solevia。';$explanation.Dock='Top';$explanation.Height=48
$bar=New-Object Windows.Forms.ProgressBar;$bar.Dock='Top';$bar.Height=6;$bar.Style='Marquee'
$box=New-Object Windows.Forms.TextBox;$box.Multiline=$true;$box.ReadOnly=$true;$box.ScrollBars='Vertical';$box.Dock='Fill';$box.Visible=$false
$button=New-Object Windows.Forms.Button;$button.Text='更新中…';$button.Enabled=$false;$button.Dock='Bottom';$button.Height=42
$details=New-Object Windows.Forms.Button;$details.Text='查看详细日志';$details.Dock='Bottom';$details.Height=36
$details.Add_Click({$box.Visible=-not $box.Visible})
$form.Controls.AddRange(@($box,$bar,$explanation,$label,$heading,$details,$button))
$script:done=$false;$script:process=$null;$script:readOut=$null;$script:readError=$null
$button.Add_Click({$form.Close()})
$form.Add_FormClosing({param($sender,$eventArgs) if(-not $script:done){$eventArgs.Cancel=$true}})
$form.Add_Shown({
    try {
        if($ParentId -gt 0){$old=Get-Process -Id $ParentId -ErrorAction SilentlyContinue;if($old){$old.WaitForExit(15000)|Out-Null;if(-not $old.HasExited){throw '服务管理窗口尚未退出，请稍后重试。'}}}
        if(-not $Package -and $Tag -notmatch '^v?\d+\.\d+\.\d+$'){throw '无效更新版本号。'}
        $info=New-Object Diagnostics.ProcessStartInfo
        $info.FileName=Get-InstalledPython
        $info.Arguments='"'+(Join-Path $PSScriptRoot 'desktop_worker.py')+'" apply --tag '+$Tag
        if($Package){$info.Arguments='"'+(Join-Path $PSScriptRoot 'desktop_worker.py')+'" local --package "'+$Package+'"'}
        $info.WorkingDirectory=$SoleviaRoot;$info.UseShellExecute=$false;$info.CreateNoWindow=$true
        $info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
        $info.StandardOutputEncoding=[Text.Encoding]::UTF8;$info.StandardErrorEncoding=[Text.Encoding]::UTF8
        $script:process=[Diagnostics.Process]::Start($info)
        $script:readOut=$script:process.StandardOutput.ReadToEndAsync();$script:readError=$script:process.StandardError.ReadToEndAsync()
        $label.Text='正在下载、校验并安装新版。个人资料会保留。'
    } catch {$label.Text='更新未完成';$box.Text=$_.Exception.Message;$box.Visible=$true;$bar.Style='Blocks';$script:done=$true;$button.Text='关闭';$button.Enabled=$true}
})
$timer=New-Object Windows.Forms.Timer;$timer.Interval=400
$timer.Add_Tick({
    if($script:process -and $script:process.HasExited -and -not $script:done){
        $script:done=$true;$bar.Style='Blocks';$bar.Value=100;$text=$script:readOut.Result+"`r`n"+$script:readError.Result;$box.Text=$text
        New-Item -ItemType Directory -Path (Join-Path $Data 'logs') -Force|Out-Null
        [IO.File]::WriteAllText((Join-Path $Data 'logs\update.log'),$text,[Text.Encoding]::UTF8)
        if($script:process.ExitCode -eq 0){
            $label.Text='更新完成，即将重新打开 Solevia。';$button.Text='完成'
            Start-Process -FilePath (Join-Path $SoleviaRoot 'Solevia.exe') -WorkingDirectory $SoleviaRoot -WindowStyle Hidden
        } else {$label.Text='更新未完成，请查看详细日志后重试。';$box.Visible=$true;$button.Text='关闭'}
        $button.Enabled=$true
    }
})
$timer.Start();[void]$form.ShowDialog();$timer.Stop();$timer.Dispose();$form.Dispose()
