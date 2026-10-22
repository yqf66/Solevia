using System;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

namespace Solevia.Desktop {
    public static class Theme {
        public static readonly Color Ground=Color.FromArgb(238,244,249), Ink=Color.FromArgb(30,47,67),
            Muted=Color.FromArgb(91,109,129), Accent=Color.FromArgb(13,112,164), Line=Color.FromArgb(219,228,236);
        public static Label Text(string text,int size=10,bool bold=false) {
            return new Label {Text=text,AutoSize=false,ForeColor=Ink,Font=new Font("Microsoft YaHei UI",size,bold?FontStyle.Bold:FontStyle.Regular)};
        }
        public static Button Button(string text,bool primary=false) {
            var button=new Button {Text=text,AutoSize=false,Width=140,Height=40,FlatStyle=FlatStyle.Flat,
                BackColor=primary?Accent:Color.White,ForeColor=primary?Color.White:Ink,Cursor=Cursors.Hand};
            button.FlatAppearance.BorderColor=primary?Accent:Line;return button;
        }
        public static Icon Icon() {
            using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("solevia.icon"))
                return stream==null?SystemIcons.Application:(Icon)new Icon(stream).Clone();
        }
        public static void Form(Form form,string title,Size size) {
            form.Text=title;form.ClientSize=new Size(size.Width,Math.Min(size.Height,Screen.PrimaryScreen.WorkingArea.Height-90));form.MinimumSize=new Size(Math.Min(size.Width+16,780),Math.Min(size.Height+39,700));
            form.StartPosition=FormStartPosition.CenterScreen;form.BackColor=Ground;form.ForeColor=Ink;
            form.Font=new Font("Microsoft YaHei UI",10);form.Icon=Icon();form.AutoScaleMode=AutoScaleMode.Dpi;
        }
        public static Panel Header(string subtitle) {
            var panel=new Panel {Dock=DockStyle.Top,Height=92,Padding=new Padding(24,18,24,10)};
            var image=new PictureBox {Location=new Point(24,20),Size=new Size(44,48),SizeMode=PictureBoxSizeMode.Zoom};
            using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("solevia.mark"))
                if(stream!=null)using(var bitmap=new Bitmap(stream))image.Image=new Bitmap(bitmap);
            var name=Text("Solevia  足下",18,true);name.SetBounds(82,15,500,32);
            var caption=Text(subtitle,10);caption.ForeColor=Muted;caption.SetBounds(83,52,650,25);
            panel.Controls.AddRange(new Control[]{image,name,caption});return panel;
        }
    }
    public sealed class DetailDialog:Form {
        public DetailDialog(string title,string message,string details) {
            Theme.Form(this,title,new Size(820,540));
            var label=Theme.Text(message,11,true);label.Dock=DockStyle.Top;label.Height=145;label.Padding=new Padding(20);
            var content=new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Both,Dock=DockStyle.Fill,Text=details,WordWrap=false};
            var close=Theme.Button("关闭");close.Dock=DockStyle.Bottom;close.Click+=(s,e)=>Close();
            var copy=Theme.Button("复制排查日志");copy.Dock=DockStyle.Bottom;copy.Click+=(s,e)=>{try{Clipboard.SetText(InstallDiagnostics.Clean(message+Environment.NewLine+details));copy.Text="已复制";}catch{copy.Text="复制失败";}};
            Controls.Add(content);Controls.Add(label);Controls.Add(copy);Controls.Add(close);CancelButton=close;
        }
    }
}
