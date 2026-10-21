using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace Solevia.Desktop {
    public sealed class InstallerForm:Form {
        readonly bool embedded;
        string root;
        readonly TextBox location=new TextBox();
        readonly Button browse=Theme.Button("选择文件夹");
        readonly CheckBox[] features=new CheckBox[4];
        readonly CheckBox shortcuts=new CheckBox {Text="创建桌面和开始菜单快捷方式",Checked=true,AutoSize=true};
        readonly Label summary=Theme.Text("正在准备能力说明…",10);
        readonly Label stage=Theme.Text("请选择需要的能力，确认后开始安装。",10,true);
        readonly ProgressBar progress=new ProgressBar {Dock=DockStyle.Bottom,Height=5};
        readonly TextBox log=new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,WordWrap=false,Dock=DockStyle.Fill};
        readonly Button install=Theme.Button("开始安装",true), close=Theme.Button("取消"), details=Theme.Button("查看详细日志");
        readonly TableLayoutPanel rows=new TableLayoutPanel {Dock=DockStyle.Fill,ColumnCount=1,RowCount=4,AutoScroll=true};
        readonly Panel logPanel=new Panel {Dock=DockStyle.Fill,Visible=false};
        bool busy,complete;
        internal string DiagnosticPath;
        Dictionary<string,object> plan;
        readonly bool[] selected=new[]{true,true,false,false};
        public InstallerForm(string folder,bool setup) {
            root=folder;embedded=setup;Theme.Form(this,setup?"安装 Solevia · 足下":"Solevia · 安装与能力管理",new Size(960,900)); install.Enabled=false;
            var body=new TableLayoutPanel {Dock=DockStyle.Fill,Padding=new Padding(24,0,24,16),RowCount=6,ColumnCount=1};
            body.RowStyles.Add(new RowStyle(SizeType.Absolute,83));body.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            body.RowStyles.Add(new RowStyle(SizeType.Absolute,110));body.RowStyles.Add(new RowStyle(SizeType.Absolute,34));
            body.RowStyles.Add(new RowStyle(SizeType.Absolute,28));body.RowStyles.Add(new RowStyle(SizeType.Absolute,54));
            var pathPanel=new Panel {Dock=DockStyle.Fill};var label=Theme.Text("安装位置",10,true);label.SetBounds(0,0,180,24);
            location.SetBounds(0,30,730,28);location.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            location.Text=root;location.ReadOnly=!embedded;browse.SetBounds(742,26,160,36);browse.Anchor=AnchorStyles.Top|AnchorStyles.Right;browse.Enabled=embedded;
            pathPanel.Controls.AddRange(new Control[]{label,location,browse});body.Controls.Add(pathPanel,0,0);
            pathPanel.Resize+=(s,e)=>{location.SetBounds(0,30,Math.Max(200,pathPanel.ClientSize.Width-176),28);browse.SetBounds(pathPanel.ClientSize.Width-164,26,164,36);};
            rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            rows.BackColor=Color.White;for(int i=0;i<4;i++)rows.RowStyles.Add(new RowStyle(SizeType.Absolute,106));
            logPanel.Controls.Add(log);var main=new Panel {Dock=DockStyle.Fill};main.Controls.Add(rows);main.Controls.Add(logPanel);body.Controls.Add(main,0,1);
            summary.Dock=DockStyle.Fill;summary.Padding=new Padding(0,12,0,0);body.Controls.Add(summary,0,2);
            body.Controls.Add(shortcuts,0,3);stage.Dock=DockStyle.Fill;body.Controls.Add(stage,0,4);
            var actions=new FlowLayoutPanel {Dock=DockStyle.Fill,FlowDirection=FlowDirection.RightToLeft,Padding=new Padding(0,8,0,0)};
            actions.Controls.AddRange(new Control[]{install,close,details});body.Controls.Add(actions,0,5);
            Controls.Add(body);Controls.Add(Theme.Header("选择适合你的能力。装好后，在浏览器里开始第一步。"));Controls.Add(progress);
            browse.Click+=(s,e)=>{using(var dialog=new FolderBrowserDialog {Description="选择 Solevia 的安装文件夹",SelectedPath=location.Text})
                if(dialog.ShowDialog(this)==DialogResult.OK)location.Text=dialog.SelectedPath;};
            details.Click+=(s,e)=>{logPanel.Visible=!logPanel.Visible;if(logPanel.Visible)logPanel.BringToFront();details.Text=logPanel.Visible?"返回能力选择":"查看详细日志";};
            close.Click+=(s,e)=>Close();install.Click+=async(s,e)=>{if(complete){ProcessRunner.Open(Path.Combine(root,"Solevia.exe"));Close();}else await Install();};
            FormClosing+=(s,e)=>{if(busy){e.Cancel=true;stage.Text="正在安装，请等待本步骤完成。详细日志可查看当前进度。";}};
            Shown+=async(s,e)=>{try {await LoadPlan();}catch(Exception error){Failure(error);}};
            AcceptButton=install;CancelButton=close;
        }
        async Task LoadPlan() {
            if(embedded) {
                // Read the same capability source shipped to the command-line installer.
                string preview=Path.Combine(Path.GetTempPath(),"Solevia-preview-"+Guid.NewGuid().ToString("N"));
                try {
                    var files=Package.Verify(Package.Resource("solevia.payload"));
                    Directory.CreateDirectory(Path.Combine(preview,"scripts"));
                    foreach(string name in new[]{"scripts/install.ps1","scripts/install-options.ps1","scripts/common.ps1","release.json"})File.WriteAllBytes(Path.Combine(preview,name.Replace('/',Path.DirectorySeparatorChar)),files[name]);
                    plan=await ReadPlan(preview);
                }finally{if(Directory.Exists(preview))Directory.Delete(preview,true);}
            }else plan=await ReadPlan(root);
            var current=(Dictionary<string,object>)((Dictionary<string,object>)plan["plan"])["features"];
            string[] keys={"web","documents","ocr","semantic"};
            var entries=(ArrayList)plan["capabilities"];
            for(int i=0;i<4;i++) {
                int index=i;var item=(Dictionary<string,object>)entries[i];selected[i]=Convert.ToBoolean(current[keys[i]]);
                var row=new Panel {Dock=DockStyle.Fill,Padding=new Padding(16,8,16,8)};
                var check=new CheckBox {Text=Convert.ToString(item["Name"]),Checked=selected[i],AutoSize=true,Font=new Font(Font,FontStyle.Bold)};
                check.SetBounds(16,9,650,25);features[i]=check;
                var benefit=Theme.Text(Convert.ToString(item["Benefit"]),10);benefit.SetBounds(39,34,850,23);benefit.Anchor=AnchorStyles.Left|AnchorStyles.Top|AnchorStyles.Right;
                var skip=Theme.Text("跳过后："+Convert.ToString(item["Skip"]),9);skip.ForeColor=Theme.Muted;skip.SetBounds(39,57,850,23);skip.Anchor=benefit.Anchor;
                var capacity=Theme.Text("下载 "+Range(item["DownloadMiB"])+"　·　磁盘 "+Range(item["DiskMiB"])+"　·　"+item["Memory"],9);
                capacity.ForeColor=Theme.Muted;capacity.SetBounds(39,80,850,24);capacity.Anchor=benefit.Anchor;
                row.Controls.AddRange(new Control[]{check,benefit,skip,capacity});rows.Controls.Add(row,0,i);
                row.Resize+=(s,e)=>{foreach(var text in new[]{benefit,skip,capacity})text.Width=Math.Max(100,row.ClientSize.Width-52);};
                check.CheckedChanged+=(s,e)=>{selected[index]=check.Checked;UpdateSummary();};
            }
            UpdateSummary();install.Enabled=true;
        }
        static async Task<Dictionary<string,object>> ReadPlan(string folder) {
            var lines=new System.Text.StringBuilder();var errors=new System.Text.StringBuilder();
            int code=await ProcessRunner.Run("powershell.exe",ProcessRunner.PowerShell(Path.Combine(folder,@"scripts\install.ps1"),"-PlanOnly"),folder,line=>{lock(lines)lines.AppendLine(line);},line=>{lock(errors)errors.AppendLine(line);});
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
            summary.Text="首次下载粗估 "+FileSize(minD)+"–"+FileSize(maxD)+"　｜　文件与缓存 "+FileSize(minS)+"–"+FileSize(maxS)+"，临时解压另留 2 GiB\r\n"+
                "基础服务、规划、任务、日历和番茄钟始终安装；对话使用你连接的模型服务。\r\n"+
                "内存按使用场景增加，与文件大小不同。容量为估算；取消已装能力会停用，但不会自动卸载。";
        }
        void Append(string line) {
            if(IsDisposed)return;if(InvokeRequired){BeginInvoke(new Action<string>(Append),line);return;}
            if(log.TextLength>180000)log.Text=log.Text.Substring(log.TextLength-90000);
            log.AppendText(line+Environment.NewLine);
            if(line.StartsWith("[")){stage.Text=line;int step;if(line.Length>2&&Int32.TryParse(line.Substring(1,1),out step))progress.Value=Math.Min(95,step*15);}
            try{Directory.CreateDirectory(Path.Combine(root,@"user-data\logs"));File.AppendAllText(Path.Combine(root,@"user-data\logs\install.log"),line+Environment.NewLine,System.Text.Encoding.UTF8);}catch{}
        }
        async Task Install() {
            if(plan==null)return;
            try{root=Path.GetFullPath(Environment.ExpandEnvironmentVariables(location.Text.Trim()));}
            catch(Exception error){MessageBox.Show(this,error.Message,"选择安装位置");return;}
            if(root.Contains("\r")||root.Contains("\n")){MessageBox.Show(this,"安装路径包含不支持的字符，请重新选择。","选择安装位置");return;}
            if(MessageBox.Show(this,"将在此位置安装所选能力：\r\n"+root+"\r\n\r\n首次安装需要联网。是否继续？","确认安装",MessageBoxButtons.OKCancel,MessageBoxIcon.Question)!=DialogResult.OK)return;
            busy=true;install.Enabled=false;browse.Enabled=false;close.Enabled=false;location.Enabled=false;
            foreach(var feature in features)if(feature!=null)feature.Enabled=false;
            try {
                stage.Text="校验产品文件…";progress.Value=5;
                if(embedded)await Task.Run(()=>Package.Extract(root,Append));
                else if(Product.RunningUrl(root)!=null)throw new IOException("请先从托盘退出 Solevia，再修改组件。");
                var drive=new DriveInfo(Path.GetPathRoot(root));if(drive.AvailableFreeSpace<2L*1024*1024*1024)throw new IOException("安装需要额外临时空间，请至少保留 2 GiB 可用磁盘空间。");
                int code=await ProcessRunner.Run("powershell.exe",ProcessRunner.PowerShell(Path.Combine(root,@"scripts\install.ps1"),Product.FeatureArguments(selected)),root,Append);
                if(code!=0)throw new IOException("安装步骤未完成。保留安装目录后重试即可；详细原因见日志。");
                if(shortcuts.Checked)try{Product.Shortcuts(root);}catch(Exception error){Append("快捷方式创建失败，可直接打开安装目录中的 Solevia.exe："+error.Message);}
                complete=true;progress.Value=100;stage.Text="安装完成，可以打开 Solevia 了。";install.Text="打开 Solevia";close.Text="完成";
            }catch(Exception error){Failure(error);}
            finally{busy=false;install.Enabled=true;close.Enabled=true;if(!complete){location.Enabled=true;browse.Enabled=embedded;foreach(var check in features)if(check!=null)check.Enabled=true;}}
        }
        void Failure(Exception error) {stage.Text="未完成，请查看原因后重试。";if(DiagnosticPath!=null){File.WriteAllText(DiagnosticPath+".error.txt",error.ToString());Close();return;}using(var dialog=new DetailDialog("Solevia 安装提示",error.Message,log.Text))dialog.ShowDialog(this);}
        internal bool Ready { get {return plan!=null&&features[3]!=null;} }
        internal void PrepareScreenshot(){location.Text="%LOCALAPPDATA%\\Programs\\Solevia";ActiveControl=install;}
        internal static void DrawToBitmapAndSave(Form form,string path) {
            using(var image=new Bitmap(form.Width,form.Height)){form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size));image.Save(path,System.Drawing.Imaging.ImageFormat.Png);}
        }
    }
}
