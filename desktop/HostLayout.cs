using System;
using System.Drawing;
using System.IO;
using System.Windows.Forms;

namespace Solevia.Desktop {
    public sealed partial class Host {
        readonly Label connection=Theme.Text("",9),serviceSignal=Theme.Text("●",11);
        readonly Panel detailDrawer=new Panel {Dock=DockStyle.Bottom,Height=150,Visible=false,Padding=new Padding(28,0,28,16)};
        void BuildHostLayout() {
            Theme.Form(this,"Solevia · 本地工作台",new Size(680,502));
            var body=new TableLayoutPanel {Dock=DockStyle.Fill,Padding=new Padding(28,0,28,0),ColumnCount=1,RowCount=3};
            body.RowStyles.Add(new RowStyle(SizeType.Absolute,184));body.RowStyles.Add(new RowStyle(SizeType.Absolute,108));body.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            var state=new Surface {Dock=DockStyle.Fill,Size=new Size(624,184),Margin=Padding.Empty};
            var caption=Theme.Text("本地服务",9);caption.ForeColor=Theme.Muted;caption.SetBounds(18,15,300,23);
            serviceSignal.SetBounds(18,46,24,29);serviceSignal.ForeColor=Theme.Muted;
            status.Font=new Font("Microsoft YaHei UI",18);status.SetBounds(43,40,550,40);status.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            note.Font=Font;note.ForeColor=Theme.Muted;note.SetBounds(20,85,570,36);note.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            open.SetBounds(18,130,174,38);start.SetBounds(204,130,114,38);stop.SetBounds(204,130,114,38);
            state.Controls.AddRange(new Control[]{caption,serviceSignal,status,note,open,start,stop});
            var tools=new TableLayoutPanel {Dock=DockStyle.Fill,ColumnCount=2,RowCount=2,Padding=new Padding(0,14,0,0),Margin=Padding.Empty};
            tools.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,50));tools.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,50));
            tools.RowStyles.Add(new RowStyle(SizeType.Percent,50));tools.RowStyles.Add(new RowStyle(SizeType.Percent,50));
            var buttons=new[]{features,update,logs,localUpdate};
            foreach(var button in buttons){button.Dock=DockStyle.Fill;button.Margin=new Padding(0,0,10,8);}
            tools.Controls.Add(features,0,0);tools.Controls.Add(update,1,0);tools.Controls.Add(logs,0,1);tools.Controls.Add(localUpdate,1,1);
            update.Margin=new Padding(0,0,0,8);localUpdate.Margin=update.Margin;
            connection.Dock=DockStyle.Fill;connection.ForeColor=Theme.Muted;connection.Padding=new Padding(0,10,0,0);
            body.Controls.Add(state,0,0);body.Controls.Add(tools,0,1);body.Controls.Add(connection,0,2);
            var footer=new Panel {Dock=DockStyle.Bottom,Width=680,Height=70,Padding=new Padding(28,0,28,18)};
            var hint=Theme.Text("关窗留在托盘，工作台随时回来。",9);hint.ForeColor=Theme.Muted;hint.SetBounds(28,35,360,24);
            var records=Theme.Button("运行记录");records.SetBounds(28,0,106,32);
            quit.SetBounds(522,17,130,36);quit.Anchor=AnchorStyles.Top|AnchorStyles.Right;
            records.Click+=(s,e)=>{detailDrawer.Visible=!detailDrawer.Visible;records.Text=detailDrawer.Visible?"收起记录":"运行记录";ClientSize=new Size(ClientSize.Width,ClientSize.Height+(detailDrawer.Visible?detailDrawer.Height:-detailDrawer.Height));};
            detail.BorderStyle=BorderStyle.FixedSingle;detail.Font=new Font("Consolas",9);detail.BackColor=Color.White;detailDrawer.Controls.Add(detail);
            footer.Controls.AddRange(new Control[]{hint,records,quit});
            Controls.Add(body);Controls.Add(detailDrawer);Controls.Add(footer);Controls.Add(Theme.Header("足下  /  今天，也向前一步"));
            RefreshConnection();
        }
        void RefreshConnection() {
            string mode=ConnectionSetup.SavedMode(root);
            connection.Text=mode=="codex"?"本机 Codex  ·  登录与模型设置在工作台中查看":mode=="api"?"自配置 API  ·  模型与密钥在工作台中管理":"模型连接  ·  打开工作台后选择本机 Codex 或自配置 API";
        }
        internal void ClosePreview(){exiting=true;timer.Stop();tray.Visible=false;tray.Dispose();Close();}
        internal void PreparePreview(){RefreshState();}
    }
}
