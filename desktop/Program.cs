using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

[assembly:AssemblyTitle("Solevia")]
[assembly:AssemblyProduct("Solevia · 足下")]
[assembly:AssemblyCompany("Solevia")]
[assembly:AssemblyCopyright("Solevia contributors")]
[assembly:AssemblyVersion("0.1.6.0")]
namespace Solevia.Desktop {
    static class Program {
        [DllImport("user32.dll")]static extern bool SetProcessDPIAware();
        [STAThread]static int Main(string[] args) {
            if(Environment.OSVersion.Version.Major>=6)SetProcessDPIAware();
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            System.Net.ServicePointManager.SecurityProtocol=System.Net.SecurityProtocolType.Tls12;
            string root=AppDomain.CurrentDomain.BaseDirectory;
            try {
                if(args.Length==2&&args[0]=="--verify-package") {Package.Verify(File.ReadAllBytes(args[1]));return 0;}
                if(args.Length==2&&args[0]=="--verify-target") {Package.RejectLinks(Path.GetFullPath(args[1]));return 0;}
                if(args.Length==3&&args[0]=="--extract-package") {Package.Extract(args[1],s=>{});Bootstrap.Prepare(args[1],s=>{});File.WriteAllText(args[2],"verified");return 0;}
                if(args.Length==2&&args[0]=="--render-installer") {
                    using(var form=new InstallerForm(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","Solevia"),Package.Embedded)) {
                        form.DiagnosticPath=args[1];
                        form.ShowInTaskbar=false;form.Opacity=0;
                        var timer=new System.Windows.Forms.Timer {Interval=150};timer.Tick+=(s,e)=>{if(form.Ready){timer.Stop();form.PrepareScreenshot();form.Opacity=1;InstallerForm.DrawToBitmapAndSave(form,args[1]);form.Close();}};
                        timer.Start();Application.Run(form);timer.Dispose();return 0;
                    }
                }
                if(Package.Embedded){
                    string folder=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","Solevia");
                    Application.Run(new InstallerForm(folder,true));return 0;
                }
                if(args.Length>0&&args[0]=="--install"){Application.Run(new InstallerForm(root,false));return 0;}
                string key="Local\\SoleviaDesktop-"+Package.Hash(System.Text.Encoding.UTF8.GetBytes(root.ToUpperInvariant())).Substring(0,24);
                bool created;
                using(var mutex=new Mutex(true,key,out created)) {
                    if(!created){string url=Product.RunningUrl(root);if(url!=null)ProcessRunner.Open(url);else MessageBox.Show("Solevia 正在启动或管理中，请查看任务栏或托盘图标。","Solevia");return 0;}
                    try{Application.Run(new Host(root));}finally{mutex.ReleaseMutex();}
                }return 0;
            }catch(Exception error){if(args.Length>0&&(args[0].StartsWith("--verify-")||args[0]=="--extract-package"))return 1;
                MessageBox.Show(error.Message,"Solevia 提示",MessageBoxButtons.OK,MessageBoxIcon.Error);return 1;}
        }
    }
}
