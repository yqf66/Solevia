using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace Solevia.Desktop {
    public sealed partial class InstallerForm:Form {
        readonly bool embedded;
        string root;
        readonly TextBox location=new TextBox();
        readonly Button browse=Theme.Button("选择文件夹");
        readonly ComboBox network=new ComboBox {DropDownStyle=ComboBoxStyle.DropDownList};
        readonly ComboBox source=new ComboBox {DropDownStyle=ComboBoxStyle.DropDownList};
        readonly CheckBox[] features=new CheckBox[4];
        readonly CheckBox shortcuts=new CheckBox {Text="创建桌面和开始菜单快捷方式",Checked=true,AutoSize=true};
        readonly Label summary=Theme.Text("正在准备能力说明…",10);
        readonly Label stage=Theme.Text("请选择需要的能力，确认后开始安装。",10,true);
        readonly ProgressBar progress=new ProgressBar {Dock=DockStyle.Bottom,Height=5};
        readonly TransferProgress downloadProgress=new TransferProgress {Dock=DockStyle.Bottom,Height=9,Visible=false};
        readonly Label downloadStatus=Theme.Text("下载开始后显示当前文件进度与速度。",9);
        readonly TextBox log=new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,WordWrap=false,Dock=DockStyle.Fill};
        readonly Button install=Theme.Button("开始安装",true), close=Theme.Button("取消"), details=Theme.Button("查看详细日志");
        readonly TableLayoutPanel rows=new TableLayoutPanel {Dock=DockStyle.Fill,ColumnCount=1,RowCount=4,AutoScroll=true};
        readonly Panel logPanel=new Panel {Dock=DockStyle.Fill,Visible=false};
        readonly Panel progressPage=new Panel {Dock=DockStyle.Fill,Visible=false,Padding=new Padding(0,10,0,0)};
        readonly Label[] stepLabels=new Label[6];
        readonly Label elapsedLabel=Theme.Text("正在准备安装…",10);
        readonly System.Windows.Forms.Timer elapsedTimer=new System.Windows.Forms.Timer {Interval=1000};
        DateTime installStarted;
        bool busy,complete,logWritable;
        internal string DiagnosticPath;
        Dictionary<string,object> plan;
        readonly bool[] selected=new[]{false,false,false,false};
        public InstallerForm(string folder,bool setup) {
            root=folder;embedded=setup;logWritable=!setup;BuildLayout();
        }
        async Task LoadPlan() {
            if(embedded) {
                // Read the same capability source shipped to the command-line installer.
                string preview=Path.Combine(Path.GetTempPath(),"Solevia-preview-"+Guid.NewGuid().ToString("N"));
                try {
                    var files=Package.Verify(Package.Resource("solevia.payload"));
                    Directory.CreateDirectory(Path.Combine(preview,"scripts"));
                    foreach(string name in new[]{"scripts/install.ps1","scripts/install-options.ps1","scripts/common.ps1","scripts/install-network.ps1","release.json"})File.WriteAllBytes(Path.Combine(preview,name.Replace('/',Path.DirectorySeparatorChar)),files[name]);
                    plan=await ReadPlan(preview,root);
                }finally{if(Directory.Exists(preview))Directory.Delete(preview,true);}
            }else plan=await ReadPlan(root);
            var current=(Dictionary<string,object>)((Dictionary<string,object>)plan["plan"])["features"];
            string[] keys={"web","documents","ocr","semantic"};
            var entries=(ArrayList)plan["capabilities"];
            for(int i=0;i<4;i++)AddCapability(i,(Dictionary<string,object>)entries[i],Convert.ToBoolean(current[keys[i]]));
            UpdateSummary();
        }
        static async Task<Dictionary<string,object>> ReadPlan(string folder,string existing=null) {
            var lines=new System.Text.StringBuilder();var errors=new System.Text.StringBuilder();
            string arguments="-PlanOnly -Profile Minimal";
            string state=existing==null?null:Path.Combine(existing,@".runtime\installed.json");
            if(state!=null&&File.Exists(state)) {
                Package.RejectLinks(state);var saved=(Dictionary<string,object>)Product.Read(state)["features"];
                string[] keys={"web","documents","ocr","semantic"},flags={"Web","Documents","Ocr","Semantic"};
                for(int i=0;i<keys.Length;i++)arguments+=" -"+flags[i]+" "+(Convert.ToBoolean(saved[keys[i]])?"1":"0");
            }
            int code=await ProcessRunner.Run("powershell.exe",ProcessRunner.PowerShell(Path.Combine(folder,@"scripts\install.ps1"),arguments),folder,line=>{lock(lines)lines.AppendLine(line);},line=>{lock(errors)errors.AppendLine(line);});
            if(code!=0)throw new IOException("无法读取安装能力说明。\r\n"+errors);
            string text=lines.ToString().Trim().TrimStart('\ufeff');
            if(!text.StartsWith("{"))throw new IOException("安装说明输出无效：["+text+"]");
            return Product.Json.Deserialize<Dictionary<string,object>>(text);
        }
        static string FileSize(double amount) {return amount>=1024?(amount/1024).ToString("0.#")+" GiB":amount.ToString("0")+" MiB";}
        static string Range(object value) {var numbers=(ArrayList)value;return FileSize(Convert.ToDouble(numbers[0]))+"–"+FileSize(Convert.ToDouble(numbers[1]));}
        void UpdateSummary() {
            if(plan==null)return;double minD=150,maxD=350,minS=700,maxS=1500;
            var entries=(ArrayList)plan["capabilities"];
            for(int i=0;i<4;i++)if(selected[i]){var item=(Dictionary<string,object>)entries[i];var d=(ArrayList)item["DownloadMiB"];var s=(ArrayList)item["DiskMiB"];
                minD+=Convert.ToDouble(d[0]);maxD+=Convert.ToDouble(d[1]);minS+=Convert.ToDouble(s[0]);maxS+=Convert.ToDouble(s[1]);}
            bool browser=Product.NeedsBrowser(root);
            if(selected[0]&&browser){minD+=150;maxD+=250;minS+=400;maxS+=700;}
            summary.Text="首次下载约 "+FileSize(minD)+"–"+FileSize(maxD)+"  ·  磁盘约 "+FileSize(minS)+"–"+FileSize(maxS)+"（含缓存）\r\n"+
                "容量为估算；取消已安装能力只会停用，不会删除依赖。";
            try {
                string target=Path.GetFullPath(Environment.ExpandEnvironmentVariables(location.Text.Trim()));
                var drive=new DriveInfo(Path.GetPathRoot(target));var temp=new DriveInfo(Path.GetPathRoot(Path.GetTempPath()));
                double need=maxS*1024*1024+(drive.Name==temp.Name?2L*1024*1024*1024:0);
                bool enough=drive.IsReady&&drive.AvailableFreeSpace>=need&&(drive.Name==temp.Name||temp.AvailableFreeSpace>=2L*1024*1024*1024);
                spaceStatus.Text=drive.Name+" 可用 "+FileSize(drive.AvailableFreeSpace/1024.0/1024)+" · 建议预留 "+FileSize(need/1024/1024)+
                    (enough?" · 空间充足":" · 空间不足，请换盘或减少能力")+(drive.Name!=temp.Name?"\r\n临时盘 "+temp.Name+" 可用 "+FileSize(temp.AvailableFreeSpace/1024.0/1024)+"，需 2 GiB":"");
                spaceStatus.ForeColor=enough?Theme.Muted:Color.Firebrick;
                install.Enabled=enough&&!busy&&!connectionProbeRunning;
            }catch {spaceStatus.Text="无法读取所选磁盘空间，请检查路径和磁盘连接。";spaceStatus.ForeColor=Color.Firebrick;install.Enabled=false;}
        }
        void Append(string line) {
            line=InstallDiagnostics.Clean(line);if(IsDisposed)return;if(InvokeRequired){BeginInvoke(new Action<string>(Append),line);return;}
            var telemetry=System.Text.RegularExpressions.Regex.Match(line,@"^\[(?:DOWNLOAD|NETWORK)_PROGRESS\] bytes=(\d+) total=(-?\d+) speed=(\d+)$");
            if(telemetry.Success) {
                double current=Double.Parse(telemetry.Groups[1].Value),total=Double.Parse(telemetry.Groups[2].Value),speed=Double.Parse(telemetry.Groups[3].Value);
                downloadProgress.Visible=true;downloadProgress.Style=total>0?ProgressBarStyle.Continuous:ProgressBarStyle.Marquee;
                if(total>0)downloadProgress.Value=(int)Math.Min(100,current/total*100);
                string rate=speed>=1024*1024?(speed/1024/1024).ToString("0.0")+" MiB/s":(speed/1024).ToString("0")+" KiB/s";
                downloadStatus.Text=total>0?(current/total*100).ToString("0")+"% · "+FileSize(current/1024/1024)+" / "+FileSize(total/1024/1024):"已传输 "+FileSize(current/1024/1024)+" · 总大小未知";
                downloadStatus.Text+=" · "+(speed>0?rate:total>0&&current>=total?"文件下载完毕，正在校验 / 处理":"0 KiB/s · 等待下载数据");
                if(total>current&&speed>0)downloadStatus.Text+=" · 预计剩余 "+Math.Ceiling((total-current)/speed)+" 秒";
                if(line.StartsWith("[NETWORK_PROGRESS]"))downloadStatus.Text+="\r\n当前步骤网络流量（含加密开销）；使用缓存、校验或构建时网速可为 0。";
                return;
            }
            if(line.StartsWith("[下载]")){downloadProgress.Visible=true;downloadProgress.Style=ProgressBarStyle.Marquee;downloadStatus.Text=line;}
            if(line.StartsWith("[等待]")||line.StartsWith("[下载进度]"))downloadStatus.Text=line;
            if(line.StartsWith("[网速提示]"))downloadStatus.Text=line;
            if(System.Text.RegularExpressions.Regex.IsMatch(line,@"^\[[1-6]/6\]")){downloadProgress.Visible=true;downloadProgress.Style=ProgressBarStyle.Marquee;downloadStatus.Text="正在处理。收到下载数据后显示网速；总大小未知的步骤不显示百分比。";}
            if(log.TextLength>180000)log.Text=log.Text.Substring(log.TextLength-90000);
            log.AppendText(line+Environment.NewLine);
            if(System.Text.RegularExpressions.Regex.IsMatch(line,@"^\[[1-6]/6\]")){stage.Text=line;int step;if(Int32.TryParse(line.Substring(1,1),out step)){progress.Value=Math.Min(95,step*15);UpdateStepLabels(step);}}
            if(logWritable)try{string logFile=Path.Combine(root,@"user-data\logs\install.log");Package.RejectLinks(logFile);Directory.CreateDirectory(Path.GetDirectoryName(logFile));File.AppendAllText(logFile,line+Environment.NewLine,System.Text.Encoding.UTF8);}catch{}
        }
        async Task Install(bool confirmed=false) {
            if(plan==null)return;
            try{root=Path.GetFullPath(Environment.ExpandEnvironmentVariables(location.Text.Trim()));}
            catch(Exception error){MessageBox.Show(this,error.Message,"选择安装位置");return;}
            if(root.Contains("\r")||root.Contains("\n")){MessageBox.Show(this,"安装路径包含不支持的字符，请重新选择。","选择安装位置");return;}
            if(!confirmed&&MessageBox.Show(this,"将在此位置安装所选能力：\r\n"+root+"\r\n\r\n"+network.Text+"\r\n"+source.Text+"\r\n首次安装需要联网。是否继续？","确认安装",MessageBoxButtons.OKCancel,MessageBoxIcon.Question)!=DialogResult.OK)return;
            busy=true;shortcuts.Enabled=false;help.Enabled=false;codexChoice.Enabled=false;detect.Enabled=false;advanced.Enabled=false;logWritable=!embedded;install.Enabled=false;browse.Enabled=false;close.Enabled=false;location.Enabled=false;network.Enabled=false;source.Enabled=false;
            installStarted=DateTime.UtcNow;elapsedTimer.Start();
            foreach(var feature in features)if(feature!=null)feature.Enabled=false;
            try {
                stage.Text="校验产品文件…";progress.Value=5;
                double disk=1500;var entries=(ArrayList)plan["capabilities"];
                for(int i=0;i<4;i++)if(selected[i])disk+=Convert.ToDouble(((ArrayList)((Dictionary<string,object>)entries[i])["DiskMiB"])[1]);
                if(selected[0]&&Product.NeedsBrowser(root))disk+=700;
                var targetDrive=new DriveInfo(Path.GetPathRoot(root));var tempDrive=new DriveInfo(Path.GetPathRoot(Path.GetTempPath()));
                double needed=disk*1024*1024+(targetDrive.Name==tempDrive.Name?2L*1024*1024*1024:0);
                if(targetDrive.AvailableFreeSpace<needed||(targetDrive.Name!=tempDrive.Name&&tempDrive.AvailableFreeSpace<2L*1024*1024*1024))
                    throw new IOException("[DISK_SPACE] 安装盘 "+targetDrive.Name+" 可用 "+FileSize(targetDrive.AvailableFreeSpace/1024.0/1024)+"，需 "+FileSize(needed/1024/1024)+"；临时盘需 2 GiB（同盘已计入）。请换盘、清理空间或减少可选能力。");
                if(embedded)await Task.Run(()=>{Package.Extract(root,message=>{logWritable=true;Append(message);});Bootstrap.Prepare(root,Append);});
                else if(Product.RunningUrl(root)!=null)throw new IOException("请先从托盘退出 Solevia，再修改组件。");
                var drive=new DriveInfo(Path.GetPathRoot(root));if(drive.AvailableFreeSpace<2L*1024*1024*1024)throw new IOException("安装需要额外临时空间，请至少保留 2 GiB 可用磁盘空间。");
                int code=await ProcessRunner.Run("powershell.exe",ProcessRunner.PowerShell(Path.Combine(root,@"scripts\install.ps1"),Product.FeatureArguments(selected)+" -Kernel "+KernelArgument+" -NetworkMode "+(network.SelectedIndex==0?"Direct":"System")+" -DownloadSource "+(source.SelectedIndex==0?"China":"Official")),root,Append,null,true);
                if(code!=0)throw new IOException("安装未完成。下方日志末尾包含失败步骤、错误编号和原始原因。可切换下载源或网络模式后重试；已有文件会复用。日志位置：user-data/logs/install.log");
                if(shortcuts.Checked)try{Product.Shortcuts(root);}catch(Exception error){Append("快捷方式创建失败，可直接打开安装目录中的 Solevia.exe："+error.Message);}
                complete=true;progress.Value=100;downloadProgress.Style=ProgressBarStyle.Continuous;downloadProgress.Value=100;
                if(codexChoice.Checked){stage.Text="正在检查 Codex 连接…";codexReady=await ConnectionSetup.ProbeCodex();}
                downloadStatus.Text=codexChoice.Checked?(codexReady?"Codex 已连接。可直接打开 Solevia 开始对话。":"Codex 连接未完成。打开网页 → 模型连接，按提示处理后点击「检测并连接」。"):
                    "尚未连接模型。打开网页 → 模型连接，可连接 Codex 或填写自己的 API。";
                stage.Text="安装完成，可以打开 Solevia 了。";install.Text="打开 Solevia";close.Text="完成";
                UpdateStepLabels(7);
            }catch(Exception error){Failure(error);}
            finally{busy=false;elapsedTimer.Stop();help.Enabled=true;install.Enabled=true;close.Enabled=true;if(!complete){shortcuts.Enabled=true;codexChoice.Enabled=true;detect.Enabled=true;advanced.Enabled=true;location.Enabled=true;network.Enabled=true;source.Enabled=true;browse.Enabled=embedded;foreach(var check in features)if(check!=null)check.Enabled=true;}}
        }
        void Failure(Exception error) {stage.Text="未完成，请查看原因后重试。";downloadProgress.Style=ProgressBarStyle.Continuous;downloadStatus.Text="当前步骤已停止，请查看错误原因；保留目录可重试。";Append(error.Message);if(DiagnosticPath!=null){File.WriteAllText(DiagnosticPath+".error.txt",error.ToString());Close();return;}using(var dialog=new DetailDialog("Solevia 安装提示",error.Message,log.Text))dialog.ShowDialog(this);}
        internal bool Completed {get{return complete;}}
        internal bool Ready { get {return plan!=null&&features[3]!=null;} }
        internal static void DrawToBitmapAndSave(Form form,string path) {
            using(var image=new Bitmap(form.Width,form.Height)){form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size));image.Save(path,System.Drawing.Imaging.ImageFormat.Png);}
        }
    }
}
