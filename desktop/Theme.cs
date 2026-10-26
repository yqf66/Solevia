using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace Solevia.Desktop {
    public static class Theme {
        public static readonly Color Ground=Color.FromArgb(242,244,247), Ink=Color.FromArgb(28,35,45),
            Muted=Color.FromArgb(101,112,129), Accent=Color.FromArgb(38,99,210), Line=Color.FromArgb(224,229,236);
        public static Label Text(string text,int size=10,bool bold=false) {
            return new Label {Text=text,AutoSize=false,ForeColor=Ink,BackColor=Color.Transparent,
                Font=new Font("Microsoft YaHei UI",size,bold?FontStyle.Bold:FontStyle.Regular)};
        }
        public static Button Button(string text,bool primary=false) {return new QuietButton(primary) {Text=text};}
        public static Icon Icon() {
            using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("solevia.icon"))
                return stream==null?SystemIcons.Application:(Icon)new Icon(stream).Clone();
        }
        public static void Form(Form form,string title,Size size) {
            form.AutoScaleDimensions=new SizeF(96,96);form.AutoScaleMode=AutoScaleMode.Dpi;
            form.Text=title;form.ClientSize=size;form.MinimumSize=new Size(Math.Min(size.Width+16,640),Math.Min(size.Height+39,560));
            form.StartPosition=FormStartPosition.CenterScreen;form.BackColor=Ground;form.ForeColor=Ink;
            form.Font=new Font("Microsoft YaHei UI",9);form.Icon=Icon();
            form.FormBorderStyle=FormBorderStyle.None;
            var chrome=new Panel {Dock=DockStyle.Top,Height=34,Width=size.Width,BackColor=Ground};
            var caption=Text(title,8);caption.ForeColor=Muted;caption.SetBounds(18,9,size.Width-140,22);
            var minimize=Button("−");var close=Button("×");minimize.SetBounds(size.Width-90,0,42,32);close.SetBounds(size.Width-46,0,42,32);
            minimize.Anchor=AnchorStyles.Top|AnchorStyles.Right;close.Anchor=minimize.Anchor;
            minimize.AccessibleName="最小化";close.AccessibleName="关闭窗口";
            minimize.Click+=(s,e)=>form.WindowState=FormWindowState.Minimized;close.Click+=(s,e)=>form.Close();
            MouseEventHandler drag=(s,e)=>{if(e.Button==MouseButtons.Left){ReleaseCapture();SendMessage(form.Handle,0xA1,new IntPtr(2),IntPtr.Zero);}};
            chrome.MouseDown+=drag;caption.MouseDown+=drag;chrome.Controls.AddRange(new Control[]{caption,minimize,close});form.Controls.Add(chrome);
            form.Load+=(s,e)=>{chrome.SendToBack();var available=Screen.FromControl(form).WorkingArea;if(form.Height>available.Height-24)form.Height=available.Height-24;};
        }
        [DllImport("user32.dll")]static extern bool ReleaseCapture();
        [DllImport("user32.dll")]static extern IntPtr SendMessage(IntPtr window,int message,IntPtr wParam,IntPtr lParam);
        public static Panel Header(string subtitle) {
            var panel=new Panel {Dock=DockStyle.Top,Width=760,Height=88,Padding=new Padding(28,18,28,12)};
            var image=new PictureBox {Location=new Point(28,22),Size=new Size(35,41),SizeMode=PictureBoxSizeMode.Zoom};
            using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("solevia.mark"))
                if(stream!=null)using(var bitmap=new Bitmap(stream))image.Image=new Bitmap(bitmap);
            var name=Text("Solevia",21);name.Font=new Font("Georgia",21);name.SetBounds(78,13,200,34);
            var caption=Text(subtitle,9);caption.ForeColor=Muted;caption.SetBounds(80,51,600,23);caption.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;
            panel.Controls.AddRange(new Control[]{image,name,caption});return panel;
        }
        public static GraphicsPath Round(Rectangle bounds,int radius) {
            var path=new GraphicsPath();int d=radius*2;
            path.AddArc(bounds.X,bounds.Y,d,d,180,90);path.AddArc(bounds.Right-d,bounds.Y,d,d,270,90);
            path.AddArc(bounds.Right-d,bounds.Bottom-d,d,d,0,90);path.AddArc(bounds.X,bounds.Bottom-d,d,d,90,90);path.CloseFigure();return path;
        }
    }
    public sealed class Surface:Panel {
        public bool Selected;
        public Surface(){DoubleBuffered=true;BackColor=Color.White;Padding=new Padding(18);}
        protected override void OnPaint(PaintEventArgs e) {
            base.OnPaint(e);e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
            using(var path=Theme.Round(new Rectangle(0,0,Width-1,Height-1),8))
            using(var brush=new SolidBrush(BackColor))using(var pen=new Pen(Selected?Theme.Accent:Theme.Line,Selected?1.5f:1)) {
                e.Graphics.FillPath(brush,path);e.Graphics.DrawPath(pen,path);
            }
        }
    }
    public sealed class QuietButton:Button {
        readonly bool primary;bool hover,down;
        public QuietButton(bool main) {
            primary=main;Width=128;Height=38;FlatStyle=FlatStyle.Flat;FlatAppearance.BorderSize=0;
            Cursor=Cursors.Hand;Font=new Font("Microsoft YaHei UI",9);UseVisualStyleBackColor=false;
            SetStyle(ControlStyles.UserPaint|ControlStyles.AllPaintingInWmPaint|ControlStyles.OptimizedDoubleBuffer,true);
        }
        protected override void OnMouseEnter(EventArgs e){hover=true;Invalidate();base.OnMouseEnter(e);}
        protected override void OnMouseLeave(EventArgs e){hover=false;down=false;Invalidate();base.OnMouseLeave(e);}
        protected override void OnMouseDown(MouseEventArgs e){down=true;Invalidate();base.OnMouseDown(e);}
        protected override void OnMouseUp(MouseEventArgs e){down=false;Invalidate();base.OnMouseUp(e);}
        protected override void OnPaint(PaintEventArgs e) {
            e.Graphics.Clear(Parent==null?Theme.Ground:Parent.BackColor);e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
            Color fill=!Enabled?Color.FromArgb(232,235,240):primary?(down?Color.FromArgb(54,63,77):hover?Color.FromArgb(43,51,64):Theme.Ink):down?Color.FromArgb(225,230,237):hover?Color.FromArgb(236,240,245):Color.White;
            using(var path=Theme.Round(new Rectangle(1,1,Width-3,Height-3),6))using(var brush=new SolidBrush(fill))using(var pen=new Pen(primary?fill:Theme.Line)) {
                e.Graphics.FillPath(brush,path);e.Graphics.DrawPath(pen,path);
            }
            TextRenderer.DrawText(e.Graphics,Text,Font,ClientRectangle,!Enabled?Theme.Muted:primary?Color.White:Theme.Ink,TextFormatFlags.HorizontalCenter|TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
            if(Focused&&ShowFocusCues)ControlPaint.DrawFocusRectangle(e.Graphics,new Rectangle(5,5,Width-11,Height-11),primary?Color.White:Theme.Ink,fill);
        }
    }
    public sealed class DetailDialog:Form {
        public DetailDialog(string title,string message,string details) {
            Theme.Form(this,title,new Size(700,460));
            var body=new TableLayoutPanel {Dock=DockStyle.Fill,Padding=new Padding(24),ColumnCount=1,RowCount=3};
            body.RowStyles.Add(new RowStyle(SizeType.Absolute,100));body.RowStyles.Add(new RowStyle(SizeType.Percent,100));body.RowStyles.Add(new RowStyle(SizeType.Absolute,54));
            var label=Theme.Text(message,10);label.Dock=DockStyle.Fill;
            var content=new TextBox {Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Both,Dock=DockStyle.Fill,Text=InstallDiagnostics.Clean(details),WordWrap=false,BorderStyle=BorderStyle.FixedSingle};
            var close=Theme.Button("关闭");var copy=Theme.Button("复制排查日志");
            close.Click+=(s,e)=>Close();copy.Click+=(s,e)=>{try{Clipboard.SetText(InstallDiagnostics.Clean(message+Environment.NewLine+details));copy.Text="已复制";}catch{copy.Text="复制失败";}};
            var actions=new FlowLayoutPanel {Dock=DockStyle.Fill,FlowDirection=FlowDirection.RightToLeft,Padding=new Padding(0,10,0,0)};actions.Controls.AddRange(new Control[]{close,copy});
            body.Controls.Add(label,0,0);body.Controls.Add(content,0,1);body.Controls.Add(actions,0,2);Controls.Add(body);CancelButton=close;
        }
    }
}
