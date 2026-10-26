using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace Solevia.Desktop {
    public sealed partial class Host:Form {
        readonly string root;
        readonly Label status=Theme.Text("正在检查本地服务…",18,true), note=Theme.Text("",10);
        readonly Button open=Theme.Button("打开工作台",true),stop=Theme.Button("停止服务"),start=Theme.Button("启动服务"),
            update=Theme.Button("检查更新"),features=Theme.Button("补装与管理能力"),logs=Theme.Button("打开日志"),quit=Theme.Button("退出 Solevia"),localUpdate=Theme.Button("本地包更新");
        readonly NotifyIcon tray=new NotifyIcon();
        readonly System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer {Interval=3000};
        readonly TextBox detail=new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Dock=DockStyle.Fill};
        Process supervisor,installerBridge;StreamWriter output;bool exiting,busy,updating,starting,managing;string url;DateTime started;int generation;
        internal Func<bool> ConfirmCapabilityChange;
        public Host(string folder,bool automatic=true) {
            root=folder;BuildHostLayout();
            ConfirmCapabilityChange=()=>MessageBox.Show(this,"补装期间会暂停本地服务，资料与对话保留。请先完成正在进行的操作。是否继续？","补装与管理能力",MessageBoxButtons.OKCancel,MessageBoxIcon.Question)==DialogResult.OK;
            tray.Icon=Theme.Icon();tray.Text="Solevia · 足下";tray.Visible=true;
            var menu=new ContextMenuStrip();menu.Items.Add("打开工作台",null,async(s,e)=>await Open());
            menu.Items.Add("服务管理",null,(s,e)=>Restore());menu.Items.Add("检查更新",null,async(s,e)=>{Restore();await CheckUpdate(true);});
            menu.Items.Add(new ToolStripSeparator());menu.Items.Add("退出 Solevia",null,async(s,e)=>await Exit());tray.ContextMenuStrip=menu;
            tray.DoubleClick+=async(s,e)=>await Open();
            open.Click+=async(s,e)=>await Open();start.Click+=async(s,e)=>await Start(false);stop.Click+=async(s,e)=>await Stop();
            quit.Click+=async(s,e)=>await Exit();update.Click+=async(s,e)=>await CheckUpdate(true);
            logs.Click+=(s,e)=>{string path=Path.Combine(root,@"user-data\logs");Directory.CreateDirectory(path);ProcessRunner.Open(path);};
            localUpdate.Click+=(s,e)=>{
                if(busy||updating)return;
                using(var dialog=new OpenFileDialog {Title="选择已下载的 Solevia ZIP 安装包",Filter="Solevia 安装包 (*.zip)|*.zip"})
                    if(dialog.ShowDialog(this)==DialogResult.OK&&MessageBox.Show(this,"将校验所选安装包，停止服务并更新，保留个人资料。是否继续？","确认本地更新",MessageBoxButtons.OKCancel)==DialogResult.OK)
                        LaunchUpdater("-Package '"+dialog.FileName.Replace("'","''")+"'");
            };
            features.Click+=async(s,e)=>await ManageCapabilities();
            FormClosing+=(s,e)=>{if(!exiting){e.Cancel=true;Hide();tray.ShowBalloonTip(2500,"Solevia 正在后台运行","双击托盘图标打开工作台；右键可更新或退出。",ToolTipIcon.Info);}};
            timer.Tick+=async(s,e)=>{RefreshState();WriteHeartbeat();EnsureInstallerBridge();await HandleDesktopRequest();};timer.Start();WriteHeartbeat();EnsureInstallerBridge();
            if(automatic)Shown+=async(s,e)=>{await Start(true);if(!exiting)await CheckUpdate(false);};
        }
        string DesktopFile(string name){return Path.Combine(root,".runtime",name);}
        bool WebInstalling {get {try {var state=Product.Read(DesktopFile("installer-progress.json"));string value=Convert.ToString(state["status"]);return installerBridge!=null&&!installerBridge.HasExited&&(value=="INSTALLING"||value=="RESTORING");}catch{return false;}}}
        void EnsureInstallerBridge() {
            if(exiting||(installerBridge!=null&&!installerBridge.HasExited)||!File.Exists(Product.Python(root))||!File.Exists(Path.Combine(root,@"scripts\installer_bridge.py")))return;
            try {
                var info=ProcessRunner.Info(Product.Python(root),ProcessRunner.Quote(Path.Combine(root,@"scripts\installer_bridge.py"))+" --parent "+Process.GetCurrentProcess().Id,root);
                installerBridge=new Process {StartInfo=info};
                installerBridge.OutputDataReceived+=(s,e)=>{};installerBridge.ErrorDataReceived+=(s,e)=>{if(e.Data!=null)Append("安装服务："+e.Data);};
                installerBridge.Start();installerBridge.StandardInput.Close();installerBridge.BeginOutputReadLine();installerBridge.BeginErrorReadLine();
            }catch(Exception error){Append("网页补装服务暂不可用："+error.Message);}
        }
        void WriteHeartbeat() {
            if(exiting)return;
            try {string path=DesktopFile("desktop-host.json");Package.RejectLinks(path);Directory.CreateDirectory(Path.GetDirectoryName(path));File.WriteAllText(path,"{}");}catch{}
        }
        async Task HandleDesktopRequest() {
            if(busy||updating||managing||WebInstalling)return;
            string path=DesktopFile("desktop-request.json");if(!File.Exists(path))return;
            try {
                Package.RejectLinks(path);var request=Product.Read(path);File.Delete(path);
                double now=(DateTime.UtcNow-new DateTime(1970,1,1)).TotalSeconds;
                if(Convert.ToString(request["action"])!="manage-capabilities"||Convert.ToDouble(request["expires"])<now)return;
                Restore();await ManageCapabilities();
            }catch(Exception error){Append("无法打开能力管理："+error.Message);}
        }
        async Task ManageCapabilities() {
            if(updating||busy||managing||WebInstalling)return;
            if(url!=null&&!ConfirmCapabilityChange())return;
            managing=true;bool resume=url!=null;
            try {
                await Stop();if(Product.RunningUrl(root)!=null)return;
                using(var installer=new InstallerForm(root,false)){installer.ShowDialog(this);resume=resume||installer.Completed;}
            }finally{managing=false;RefreshState();}
            if(resume)await Start(true);
        }
        void Append(string line) {
            if(IsDisposed)return;if(InvokeRequired){BeginInvoke(new Action<string>(Append),line);return;}
            if(detail.TextLength>40000)detail.Text=detail.Text.Substring(detail.TextLength-20000);
            detail.AppendText(line+Environment.NewLine);
        }
        void Restore(){Show();WindowState=FormWindowState.Normal;Activate();}
        void RefreshState() {
            if(updating||IsDisposed)return;
            if(WebInstalling){status.Text="正在补装能力";note.Text="可在网页查看下载速度与安装进度。\r\n完成后自动恢复服务，资料与对话保留。";start.Enabled=stop.Enabled=open.Enabled=update.Enabled=features.Enabled=localUpdate.Enabled=false;return;}
            localUpdate.Enabled=true;
            url=Product.RunningUrl(root);
            bool alive=supervisor!=null&&!supervisor.HasExited;
            if(url!=null){status.Text="Solevia 已就绪";note.Text="版本 "+Product.Version(root)+" · 服务仅在本机运行\r\n关窗后在托盘继续运行。";starting=false;}
            else if(alive||starting){status.Text="正在启动 Solevia…";note.Text="正在准备数据库与本地服务，请稍候。";}
            else {status.Text="Solevia 已停止";note.Text="点击「启动服务」继续使用，资料与执行记录仍在原位。";}
            if(starting&&(DateTime.UtcNow-started).TotalSeconds>200){starting=false;Append("启动等待超时，请查看日志，或通过「管理能力」修复安装。");}
            serviceSignal.ForeColor=url!=null?Theme.Accent:Theme.Muted;start.Visible=url==null&&!alive;stop.Visible=!start.Visible;RefreshConnection();
            start.Enabled=url==null&&!alive&&!busy;stop.Enabled=(url!=null||alive)&&!busy;open.Enabled=!busy;update.Enabled=!busy;features.Enabled=!busy&&!managing;
        }
        async Task Start(bool openWhenReady) {
            if(busy||updating||WebInstalling)return;url=await Task.Run(()=>Product.RunningUrl(root));
            if(url!=null){if(openWhenReady)ProcessRunner.Open(url);RefreshState();return;}
            if(supervisor!=null&&!supervisor.HasExited)return;
            if(!File.Exists(Product.Python(root))||!File.Exists(Path.Combine(root,@".runtime\installed.json"))) {
                using(var installer=new InstallerForm(root,false))installer.ShowDialog(this);
                if(!File.Exists(Product.Python(root))||!File.Exists(Path.Combine(root,@".runtime\installed.json")))return;
            }
            try {
                Directory.CreateDirectory(Path.Combine(root,@"user-data\logs"));
                if(output!=null)output.Dispose();output=new StreamWriter(Path.Combine(root,@"user-data\logs\desktop.log"),true,Encoding.UTF8){AutoFlush=true};
                var info=ProcessRunner.Info(Product.Python(root),ProcessRunner.Quote(Path.Combine(root,@"scripts\launcher.py"))+" --no-browser --skip-update-check",root);
                supervisor=new Process {StartInfo=info};
                DataReceivedEventHandler received=(s,e)=>{if(e.Data!=null){lock(output)output.WriteLine(e.Data);Append(e.Data);}};
                supervisor.OutputDataReceived+=received;supervisor.ErrorDataReceived+=received;
                supervisor.Start();supervisor.StandardInput.Close();supervisor.BeginOutputReadLine();supervisor.BeginErrorReadLine();starting=true;started=DateTime.UtcNow;int launchGeneration=++generation;RefreshState();
                if(openWhenReady) {
                    for(int i=0;i<100;i++){await Task.Delay(1500);if(exiting||updating||generation!=launchGeneration)return;url=await Task.Run(()=>Product.RunningUrl(root));if(url!=null){ProcessRunner.Open(url);RefreshState();return;}if(supervisor.HasExited)break;}
                    starting=false;RefreshState();Restore();MessageBox.Show(this,"启动尚未完成。请查看下方记录，或打开日志后重试安装修复。","Solevia 启动提示");
                }
            }catch(Exception error){starting=false;Restore();MessageBox.Show(this,error.Message,"Solevia 启动提示");}
        }
        async Task Open(){if(updating)return;if(url!=null)ProcessRunner.Open(url);else await Start(true);}
        async Task Stop() {
            if(busy||updating||WebInstalling)return;++generation;busy=true;RefreshState();
            try {if(File.Exists(Product.Python(root))) {
                int code=await ProcessRunner.Run(Product.Python(root),ProcessRunner.Quote(Path.Combine(root,@"scripts\launcher.py"))+" --stop",root,Append);
                if(code!=0)throw new IOException("服务尚未停止，请稍后重试或查看日志。");
            }starting=false;}
            catch(Exception error){Restore();MessageBox.Show(this,error.Message,"停止服务");}
            finally{busy=false;RefreshState();}
        }
        async Task Exit() {
            if(updating||busy||WebInstalling){Restore();return;}await Stop();
            if(Product.RunningUrl(root)!=null||(supervisor!=null&&!supervisor.HasExited))return;
            exiting=true;timer.Stop();tray.Visible=false;tray.Dispose();Close();
        }
        async Task CheckUpdate(bool explicitCheck) {
            if(busy||updating||WebInstalling)return;busy=true;update.Enabled=false;
            try {
                var lines=new StringBuilder();int code=await ProcessRunner.Run(Product.Python(root),ProcessRunner.Quote(Path.Combine(root,@"scripts\desktop_worker.py"))+" check",root,line=>lines.AppendLine(line));
                if(code!=0)throw new IOException("暂时无法检查更新，请稍后重试。\r\n"+lines);
                var result=Product.Json.Deserialize<Dictionary<string,object>>(lines.ToString().Trim().TrimStart('\ufeff'));
                if(!Convert.ToBoolean(result["available"])) {if(explicitCheck)MessageBox.Show(this,"当前已是最新正式版。","Solevia 更新");return;}
                Restore();string tag=Convert.ToString(result["tag"]);
                if(MessageBox.Show(this,"发现新版 "+tag+"。\r\n\r\n确认后将从 GitHub 官方仓库下载并校验安装包，停止服务后更新，保留个人配置与资料。\r\n更新过程中请勿关闭更新窗口。","更新 Solevia",MessageBoxButtons.OKCancel,MessageBoxIcon.Information)!=DialogResult.OK)return;
                // Updater runs outside the executable it replaces; it restarts the new desktop host after success.
                LaunchUpdater("-Tag '"+tag.Replace("'", "''")+"'");
            }catch(Exception error){if(explicitCheck){Restore();MessageBox.Show(this,error.Message,"检查更新");}else Append("自动检查更新暂不可用，当前版本照常运行。");}
            finally{busy=false;if(!IsDisposed)RefreshState();}
        }
        void LaunchUpdater(string parameters) {
            string powershell=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),@"WindowsPowerShell\v1.0\powershell.exe");
            var info=new ProcessStartInfo(powershell,ProcessRunner.PowerShell(Path.Combine(root,@"scripts\desktop-update.ps1"),"-ParentId "+Process.GetCurrentProcess().Id+" "+parameters)) {
                UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=root};info.EnvironmentVariables.Remove("PSModulePath");
            try{Process.Start(info);updating=true;exiting=true;timer.Stop();tray.Visible=false;tray.Dispose();Close();}
            catch(Exception error){MessageBox.Show(this,error.Message,"无法启动更新窗口");}
        }
    }
}
