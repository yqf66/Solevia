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
    public sealed class Host:Form {
        readonly string root;
        readonly Label status=Theme.Text("正在检查本地服务…",18,true), note=Theme.Text("",10);
        readonly Button open=Theme.Button("打开工作台",true),stop=Theme.Button("停止服务"),start=Theme.Button("启动服务"),
            update=Theme.Button("检查更新"),features=Theme.Button("管理能力"),logs=Theme.Button("打开日志"),quit=Theme.Button("退出 Solevia"),localUpdate=Theme.Button("本地包更新");
        readonly NotifyIcon tray=new NotifyIcon();
        readonly System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer {Interval=3000};
        readonly TextBox detail=new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Dock=DockStyle.Fill};
        Process supervisor;StreamWriter output;bool exiting,busy,updating,starting;string url;DateTime started;int generation;
        public Host(string folder,bool automatic=true) {
            root=folder;Theme.Form(this,"Solevia · 服务管理",new Size(760,490));
            var content=new TableLayoutPanel {Dock=DockStyle.Fill,Padding=new Padding(28,0,28,22),ColumnCount=1,RowCount=6};
            content.RowStyles.Add(new RowStyle(SizeType.Absolute,44));content.RowStyles.Add(new RowStyle(SizeType.Absolute,58));
            content.RowStyles.Add(new RowStyle(SizeType.Absolute,57));content.RowStyles.Add(new RowStyle(SizeType.Absolute,57));
            content.RowStyles.Add(new RowStyle(SizeType.Absolute,36));content.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            status.Dock=DockStyle.Fill;note.Dock=DockStyle.Fill;note.ForeColor=Theme.Muted;
            var row=new FlowLayoutPanel {Dock=DockStyle.Fill};row.Controls.AddRange(new Control[]{open,start,stop,quit});
            var tools=new FlowLayoutPanel {Dock=DockStyle.Fill};tools.Controls.AddRange(new Control[]{update,features,logs,localUpdate});
            var hint=Theme.Text("关闭此窗口后留在托盘运行。关闭浏览器不会结束服务。",9);hint.Dock=DockStyle.Fill;hint.ForeColor=Theme.Muted;
            content.Controls.Add(status,0,0);content.Controls.Add(note,0,1);content.Controls.Add(row,0,2);content.Controls.Add(tools,0,3);content.Controls.Add(hint,0,4);content.Controls.Add(detail,0,5);
            Controls.Add(content);Controls.Add(Theme.Header("工作台在浏览器中打开，服务由 Solevia 在后台管理。"));
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
            features.Click+=async(s,e)=>{if(updating)return;if(url!=null&&MessageBox.Show(this,"调整能力需要先停止服务，个人资料会保留。是否继续？","管理能力",MessageBoxButtons.OKCancel)!=DialogResult.OK)return;
                await Stop();using(var installer=new InstallerForm(root,false))installer.ShowDialog(this);RefreshState();};
            FormClosing+=(s,e)=>{if(!exiting){e.Cancel=true;Hide();tray.ShowBalloonTip(2500,"Solevia 正在后台运行","双击托盘图标打开工作台；右键可更新或退出。",ToolTipIcon.Info);}};
            timer.Tick+=(s,e)=>RefreshState();timer.Start();
            if(automatic)Shown+=async(s,e)=>{await Start(true);if(!exiting)await CheckUpdate(false);};
        }
        void Append(string line) {
            if(IsDisposed)return;if(InvokeRequired){BeginInvoke(new Action<string>(Append),line);return;}
            if(detail.TextLength>40000)detail.Text=detail.Text.Substring(detail.TextLength-20000);
            detail.AppendText(line+Environment.NewLine);
        }
        void Restore(){Show();WindowState=FormWindowState.Normal;Activate();}
        void RefreshState() {
            if(updating||IsDisposed)return;
            url=Product.RunningUrl(root);
            bool alive=supervisor!=null&&!supervisor.HasExited;
            if(url!=null){status.Text="Solevia 已就绪";note.Text="版本 "+Product.Version(root)+" · 服务仅在本机运行\r\n可以打开工作台，也可以关闭此窗口留在托盘。";starting=false;}
            else if(alive||starting){status.Text="正在启动 Solevia…";note.Text="正在准备数据库与本地服务，请稍候。";}
            else {status.Text="Solevia 已停止";note.Text="点击「启动服务」继续使用，个人资料与执行记录仍在原位。";}
            if(starting&&(DateTime.UtcNow-started).TotalSeconds>200){starting=false;Append("启动等待超时，请查看日志，或通过「管理能力」修复安装。");}
            start.Enabled=url==null&&!alive&&!busy;stop.Enabled=(url!=null||alive)&&!busy;open.Enabled=!busy;update.Enabled=!busy;features.Enabled=!busy;
        }
        async Task Start(bool openWhenReady) {
            if(busy||updating)return;url=await Task.Run(()=>Product.RunningUrl(root));
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
            if(busy||updating)return;++generation;busy=true;RefreshState();
            try {if(File.Exists(Product.Python(root))) {
                int code=await ProcessRunner.Run(Product.Python(root),ProcessRunner.Quote(Path.Combine(root,@"scripts\launcher.py"))+" --stop",root,Append);
                if(code!=0)throw new IOException("服务尚未停止，请稍后重试或查看日志。");
            }starting=false;}
            catch(Exception error){Restore();MessageBox.Show(this,error.Message,"停止服务");}
            finally{busy=false;RefreshState();}
        }
        async Task Exit() {
            if(updating||busy){Restore();return;}await Stop();
            if(Product.RunningUrl(root)!=null||(supervisor!=null&&!supervisor.HasExited))return;
            exiting=true;timer.Stop();tray.Visible=false;tray.Dispose();Close();
        }
        async Task CheckUpdate(bool explicitCheck) {
            if(busy||updating)return;busy=true;update.Enabled=false;
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
