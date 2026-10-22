using System;
using System.Drawing;
using System.Windows.Forms;

namespace Solevia.Desktop {
    // Paint directly so screenshots and classic Windows themes show the same file progress.
    public sealed class TransferProgress:Control {
        readonly Timer timer=new Timer {Interval=40};
        int amount,phase;
        ProgressBarStyle style;
        public int Value {get{return amount;}set{amount=Math.Max(0,Math.Min(100,value));Invalidate();}}
        public ProgressBarStyle Style {get{return style;}set{style=value;timer.Enabled=value==ProgressBarStyle.Marquee;Invalidate();}}
        public TransferProgress() {
            DoubleBuffered=true;SetStyle(ControlStyles.UserPaint|ControlStyles.AllPaintingInWmPaint,true);
            timer.Tick+=(s,e)=>{phase=(phase+3)%140;Invalidate();};
        }
        protected override void OnPaint(PaintEventArgs args) {
            base.OnPaint(args);args.Graphics.Clear(Theme.Line);
            using(var brush=new SolidBrush(Theme.Accent)) {
                if(style==ProgressBarStyle.Marquee)args.Graphics.FillRectangle(brush,(phase-35)*Width/100,0,Width/3,Height);
                else args.Graphics.FillRectangle(brush,0,0,Width*amount/100,Height);
            }
        }
        protected override void Dispose(bool disposing){if(disposing)timer.Dispose();base.Dispose(disposing);}
    }
}
