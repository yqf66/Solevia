using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Windows.Forms;

namespace Solevia.Desktop {
    public sealed partial class InstallerForm {
        readonly Panel pages=new Panel {Dock=DockStyle.Fill};
        readonly Panel connectionPage=new Panel {Dock=DockStyle.Fill,AutoScroll=true};
        readonly Panel capabilityPage=new Panel {Dock=DockStyle.Fill,AutoScroll=true,Visible=false};
        readonly CheckBox codexChoice=new CheckBox {Text="连接 Codex 作为 Solevia 内核（推荐）",AutoSize=true};
        readonly Surface codexCard=new Surface();
        readonly Button detect=Theme.Button("重新检测");
        readonly Label capabilityHint=Theme.Text("",9),spaceStatus=Theme.Text("正在读取安装盘空间…",9);
        bool connectionProbeRunning, codexReady;
        internal Func<System.Threading.Tasks.Task<bool>> ConnectionProbe=ConnectionSetup.ProbeCodex;
        readonly Label connectionHint=Theme.Text("",9),stepHint=Theme.Text("01 / 02   安装准备",9);
        readonly Button back=Theme.Button("上一步"),advanced=Theme.Button("安装位置与网络  ∨"),help=Theme.Button("Codex 连接帮助");
        readonly Panel advancedPanel=new Panel {Visible=false,Height=132,Width=704,Dock=DockStyle.Top};
        readonly ToolTip hints=new ToolTip {AutoPopDelay=18000,InitialDelay=400};
        bool choosing,modeChanged;int page;
        string initialMode;

        void BuildLayout() {
            Theme.Form(this,embedded?"安装 Solevia · 足下":"Solevia · 安装与能力管理",new Size(760,660));
            install.Enabled=false;initialMode=ConnectionSetup.SavedMode(root);
            var frame=new Panel {Dock=DockStyle.Fill,Padding=new Padding(28,0,28,0)};
            pages.Controls.Add(connectionPage);pages.Controls.Add(capabilityPage);pages.Controls.Add(logPanel);pages.Controls.Add(progressPage);
            logPanel.Padding=new Padding(0,0,0,12);logPanel.Controls.Add(log);
            frame.Controls.Add(pages);
            var footer=new Panel {Dock=DockStyle.Bottom,Height=86,Padding=new Padding(28,8,28,14)};
            var actions=new FlowLayoutPanel {Dock=DockStyle.Right,Width=428,FlowDirection=FlowDirection.RightToLeft,WrapContents=false,Padding=new Padding(0,18,0,0)};
            install.Width=174;close.Width=78;back.Width=90;details.Width=116;
            actions.Controls.AddRange(new Control[]{install,close,back});
            stepHint.SetBounds(28,26,240,25);stepHint.ForeColor=Theme.Muted;
            details.SetBounds(28,26,128,36);details.Visible=false;
            footer.Controls.Add(actions);footer.Controls.Add(stepHint);footer.Controls.Add(details);
            BuildProgressPage();
            Controls.Add(frame);Controls.Add(footer);Controls.Add(Theme.Header("足下  /  从这一步，走向想去的地方"));Controls.Add(progress);

            var setup=new Panel {Dock=DockStyle.Top,Height=442,Width=704};
            var title=Theme.Text("准备你的 Solevia 工作台",20);title.SetBounds(0,2,650,42);
            var intro=Theme.Text("先安装基础环境。模型连接和额外能力，都可以稍后完成。",10);intro.ForeColor=Theme.Muted;intro.SetBounds(0,52,690,27);intro.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            codexCard.SetBounds(0,96,704,112);codexCard.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            var question=Theme.Text("发现本机 Codex，是否连接作为 Solevia 内核？",11,true);question.SetBounds(18,15,660,27);
            codexChoice.Font=Font;codexChoice.SetBounds(18,58,650,28);
            codexCard.Controls.AddRange(new Control[]{question,codexChoice});
            connectionHint.SetBounds(0,224,704,66);connectionHint.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;connectionHint.ForeColor=Theme.Muted;
            help.SetBounds(0,306,144,36);detect.SetBounds(156,306,116,36);advanced.SetBounds(284,306,184,36);
            shortcuts.Font=Font;shortcuts.SetBounds(0,368,600,26);
            setup.Height=410;
            setup.Controls.AddRange(new Control[]{title,intro,codexCard,connectionHint,help,detect,advanced,shortcuts});
            connectionPage.Controls.Add(advancedPanel);connectionPage.Controls.Add(setup);
            var pathLabel=Theme.Text("安装到",9);pathLabel.SetBounds(0,4,100,22);
            location.Text=root;location.TextChanged+=(s,e)=>UpdateSummary();location.ReadOnly=!embedded;location.SetBounds(0,29,560,28);location.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            browse.SetBounds(574,24,130,36);browse.Anchor=AnchorStyles.Top|AnchorStyles.Right;browse.Enabled=embedded;
            network.Items.AddRange(new object[]{"直连 · 不使用代理","使用系统 / 环境代理"});network.SelectedIndex=0;
            source.Items.AddRange(new object[]{"国内镜像","官方源"});source.SelectedIndex=0;
            network.SetBounds(0,72,330,28);source.SetBounds(346,72,358,28);source.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            advancedPanel.Controls.AddRange(new Control[]{pathLabel,location,browse,network,source});
            var capabilityContent=new TableLayoutPanel {Dock=DockStyle.Top,Height=444,ColumnCount=1,RowCount=5,Margin=Padding.Empty};
            capabilityContent.RowStyles.Add(new RowStyle(SizeType.Absolute,42));capabilityContent.RowStyles.Add(new RowStyle(SizeType.Absolute,64));
            capabilityContent.RowStyles.Add(new RowStyle(SizeType.Absolute,244));capabilityContent.RowStyles.Add(new RowStyle(SizeType.Absolute,50));capabilityContent.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            var capabilityTitle=Theme.Text("按需添一点能力",20);capabilityTitle.Dock=DockStyle.Fill;
            capabilityHint.Dock=DockStyle.Fill;capabilityHint.ForeColor=Theme.Muted;
            rows.ColumnCount=2;rows.RowCount=2;rows.AutoScroll=false;rows.BackColor=Theme.Ground;
            rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,50));rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,50));
            rows.RowStyles.Add(new RowStyle(SizeType.Percent,50));rows.RowStyles.Add(new RowStyle(SizeType.Percent,50));
            summary.Dock=DockStyle.Fill;summary.Padding=new Padding(0,4,0,0);summary.ForeColor=Theme.Muted;
            capabilityContent.Controls.Add(capabilityTitle,0,0);capabilityContent.Controls.Add(capabilityHint,0,1);capabilityContent.Controls.Add(rows,0,2);capabilityContent.Controls.Add(summary,0,3);spaceStatus.Dock=DockStyle.Fill;capabilityContent.Controls.Add(spaceStatus,0,4);
            capabilityPage.Controls.Add(capabilityContent);
            codexChoice.CheckedChanged+=async(s,e)=>{if(choosing)return;modeChanged=true;UpdateConnection();if(codexChoice.Checked)await ProbeConnection();};
            choosing=true;codexChoice.Checked=initialMode=="codex";choosing=false;UpdateConnection();
            detect.Click+=(s,e)=>UpdateConnection();
            back.Click+=(s,e)=>ShowPage(0);advanced.Click+=(s,e)=>{advancedPanel.Visible=!advancedPanel.Visible;advanced.Text=advancedPanel.Visible?"安装位置与网络  ∧":"安装位置与网络  ∨";};
            help.Click+=(s,e)=>ShowConnectionHelp();
            browse.Click+=(s,e)=>{using(var dialog=new FolderBrowserDialog {Description="选择 Solevia 的安装文件夹",SelectedPath=location.Text})if(dialog.ShowDialog(this)==DialogResult.OK)location.Text=dialog.SelectedPath;};
            details.Click+=(s,e)=>{bool show=!logPanel.Visible;logPanel.Visible=show;progressPage.Visible=!show&&(busy||complete);connectionPage.Visible=!show&&!busy&&!complete&&page==0;capabilityPage.Visible=!show&&!busy&&!complete&&page==1;if(show)logPanel.BringToFront();details.Text=show?"收起详细日志":"查看详细日志";};
            close.Click+=(s,e)=>Close();
            install.Click+=async(s,e)=>{if(complete){if(embedded)ProcessRunner.Open(Path.Combine(root,"Solevia.exe"));Close();}else if(page==0){if(!connectionProbeRunning)ShowPage(1);}else {
                progressPage.Visible=true;progressPage.BringToFront();capabilityPage.Visible=false;logPanel.Visible=false;back.Visible=false;stepHint.Visible=false;details.Visible=true;details.Text="查看详细日志";await Install(true);
                if(!complete){back.Visible=true;ShowPage(1);}else stepHint.Text="准备完成 · 开始你的第一步";
            }};
            FormClosing+=(s,e)=>{if(busy||connectionProbeRunning){e.Cancel=true;stage.Text="正在安装，请等待本步骤完成。";}};
            Shown+=async(s,e)=>{try{await LoadPlan();UpdateConnection();if(codexChoice.Checked)await ProbeConnection();}catch(Exception error){Failure(error);}};
            AcceptButton=install;CancelButton=close;ShowPage(embedded?0:1);
            FormClosed+=(s,e)=>{hints.Dispose();elapsedTimer.Dispose();};
        }
        void BuildProgressPage() {
            var title=Theme.Text("让工作台准备就绪",20);title.SetBounds(0,0,680,42);title.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            elapsedLabel.SetBounds(0,48,680,26);elapsedLabel.ForeColor=Theme.Muted;
            var transfer=new Surface {Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right};transfer.SetBounds(0,88,704,142);
            stage.SetBounds(18,18,666,48);stage.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            downloadStatus.SetBounds(18,72,666,52);downloadStatus.ForeColor=Theme.Muted;downloadStatus.Anchor=stage.Anchor;
            downloadProgress.SetBounds(18,128,666,8);downloadProgress.Dock=DockStyle.None;downloadProgress.Anchor=stage.Anchor;downloadProgress.Visible=true;
            transfer.Controls.AddRange(new Control[]{stage,downloadStatus,downloadProgress});
            string[] names={"校验安装文件","准备 Python 运行环境","准备 Node.js 运行环境","安装所选能力与依赖","构建 Solevia 界面","准备工作区与数据库"};
            for(int i=0;i<6;i++){var label=Theme.Text((i+1).ToString("00")+"   "+names[i],10);label.SetBounds(4,250+i*29,680,27);label.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;stepLabels[i]=label;progressPage.Controls.Add(label);}
            progressPage.Controls.AddRange(new Control[]{title,elapsedLabel,transfer});
            progressPage.Resize+=(s,e)=>{int width=Math.Max(100,progressPage.ClientSize.Width);title.Width=elapsedLabel.Width=width;transfer.Width=width;stage.Width=downloadStatus.Width=downloadProgress.Width=Math.Max(64,width-36);foreach(var label in stepLabels)label.Width=Math.Max(64,width-8);};
            elapsedTimer.Tick+=(s,e)=>elapsedLabel.Text="已用 "+(int)(DateTime.UtcNow-installStarted).TotalMinutes+" 分 "+(DateTime.UtcNow-installStarted).Seconds+" 秒 · 安装仍在进行，请保持此窗口打开";
            UpdateStepLabels(0);
        }
        void UpdateStepLabels(int step){for(int i=0;i<stepLabels.Length;i++)if(stepLabels[i]!=null){stepLabels[i].ForeColor=i+1==step?Theme.Accent:i+1<step?Theme.Ink:Theme.Muted;stepLabels[i].Font=new Font("Microsoft YaHei UI",10,i+1==step?FontStyle.Bold:FontStyle.Regular);}}
        async System.Threading.Tasks.Task ProbeConnection() {
            connectionProbeRunning=true;install.Enabled=false;detect.Enabled=false;codexChoice.Enabled=false;
            connectionHint.Text="正在检查 Codex 登录与本机连接，请稍候…";
            try {codexReady=await ConnectionProbe();}
            finally {connectionProbeRunning=false;if(!IsDisposed){detect.Enabled=true;codexChoice.Enabled=true;UpdateConnection();UpdateSummary();}}
        }
        void UpdateConnection() {
            bool found=ConnectionSetup.FindCodex()!=null;
            codexCard.Selected=codexChoice.Checked;codexCard.Invalidate();
            codexCard.Controls[0].Text=found?"发现本机 Codex，是否连接作为 Solevia 内核？":"未发现本机 Codex，可稍后安装并连接";
            codexChoice.Enabled=found&&!connectionProbeRunning;
            connectionHint.Text=codexChoice.Checked?(codexReady?"Codex 已连接。安装后使用本机登录，不复制密钥。\r\n附件默认只读，修改文件时在对话中确认。":"已选择连接 Codex，但登录 / 连接检查尚未通过。\r\n可按帮助完成安装与登录，或取消勾选，先安装基础工作台。"):
                found?"仅发现程序，尚未连接。勾选后才会检查登录并连接。\r\n也可保持未连接，安装后在「模型连接」配置 Codex 或 API。":"未发现本机有 Codex，建议按需安装对应能力依赖。\r\n可以先安装基础工作台，之后连接 Codex 或填写自己的 API。";
            capabilityHint.Text=(codexReady&&codexChoice.Checked?"Codex 已连接，若暂不使用自配置 API 可稍后安装。":found?"本机存在 Codex；是否连接由你决定。额外能力均可稍后补装。":"未发现本机有 Codex，建议按需安装对应能力依赖。")+
                "\r\n普通 API 对话不需要额外依赖；以下能力均可跳过。基础环境仍会安装。";
        }
        void ShowPage(int index) {
            page=index;connectionPage.Visible=index==0;capabilityPage.Visible=index==1;back.Visible=index==1;
            progressPage.Visible=false;logPanel.Visible=false;details.Visible=log.TextLength>0;stepHint.Visible=!details.Visible;details.Text="查看详细日志";
            install.Text=index==0?"下一步：安装选项":"开始安装";stepHint.Text=index==0?"01 / 02   安装准备":"02 / 02   安装选项";
        }
        void ShowConnectionHelp() {
            using(var dialog=new Form()) {
                Theme.Form(dialog,"连接本机 Codex",new Size(600,400));
                var text=Theme.Text("1. 按官方指南安装 Codex，使用与 Solevia 相同的 Windows 用户。\r\n\r\n2. 打开 Codex 完成登录。命令行用户可运行 codex login。\r\n\r\n3. 发现 Codex 后，可勾选连接；也可稍后在网页「模型连接」点击「检测并连接」。\r\n\r\n打开网页 → 左下角本地用户 → 模型连接，查看状态与处理提示。\r\nSolevia 不复制登录密钥，不修改全局 Codex 配置。",10);text.Dock=DockStyle.Fill;text.Padding=new Padding(24);
                var actions=new FlowLayoutPanel {Dock=DockStyle.Bottom,Height=64,Padding=new Padding(20,10,0,0)};
                var docs=Theme.Button("安装与登录指南");var copy=Theme.Button("复制排查指令");var done=Theme.Button("知道了",true);
                docs.Click+=(s,e)=>ProcessRunner.Open("https://learn.chatgpt.com/docs/cli");copy.Click+=(s,e)=>{try{Clipboard.SetText(ConnectionSetup.RepairPrompt);copy.Text="已复制";}catch{copy.Text="请稍后重试";}};done.Click+=(s,e)=>dialog.Close();
                actions.Controls.AddRange(new Control[]{docs,copy,done});dialog.Controls.Add(text);dialog.Controls.Add(actions);dialog.ShowDialog(this);
            }
        }
        void AddCapability(int index,Dictionary<string,object> item,bool enabled) {
            selected[index]=enabled;
            var card=new Surface {Dock=DockStyle.Fill,Size=new Size(344,116),Margin=new Padding(index%2==0?0:8,0,index%2==0?8:0,12)};
            var check=new CheckBox {Text=Convert.ToString(item["Name"]),Checked=enabled,AutoSize=true,Font=new Font("Microsoft YaHei UI",10)};check.SetBounds(16,13,300,24);features[index]=check;
            var descriptions=new[]{"API 联网搜索与网页阅读","PDF、Word、Excel 与演示文稿","读取图片与扫描件中的文字","资料量大时，按含义查找片段"};
            var benefit=Theme.Text(descriptions[index],9);benefit.SetBounds(16,46,300,24);benefit.ForeColor=Theme.Muted;benefit.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            var capacity=Theme.Text("下载 "+Range(item["DownloadMiB"])+" · 可稍后补装",8);capacity.SetBounds(16,80,300,22);capacity.ForeColor=Theme.Muted;capacity.Anchor=benefit.Anchor;
            hints.SetToolTip(card,Convert.ToString(item["Benefit"])+"\r\n跳过后："+item["Skip"]+"\r\n磁盘 "+Range(item["DiskMiB"])+"；"+item["Memory"]);hints.SetToolTip(check,hints.GetToolTip(card));
            card.Controls.AddRange(new Control[]{check,benefit,capacity});rows.Controls.Add(card,index%2,index/2);
            check.CheckedChanged+=(s,e)=>{selected[index]=check.Checked;UpdateSummary();};
        }
        string KernelArgument {get{return (initialMode!=null||!embedded)&&!modeChanged?"Keep":codexChoice.Checked?"Codex":"Api";}}
        internal void PrepareScreenshot(int step=0){location.Text="%LOCALAPPDATA%\\Programs\\Solevia";ShowPage(step);ActiveControl=install;}
    }
}
