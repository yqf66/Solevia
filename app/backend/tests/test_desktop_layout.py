"""Exercise the compiled WinForms screens without installing or starting services."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Native Windows UI')


@pytest.fixture(scope='module')
def native_layout_driver(tmp_path_factory):
    root = tmp_path_factory.mktemp('desktop-layout')
    source = ROOT / 'desktop'
    script_root = root / 'scripts'; script_root.mkdir()
    for name in ('install.ps1', 'install-options.ps1', 'common.ps1', 'install-network.ps1'):
        shutil.copyfile(ROOT / 'scripts' / name, script_root / name)
    shutil.copyfile(ROOT / 'release.json', root / 'release.json')
    driver = root / 'LayoutDriver.cs'
    driver.write_text(r'''using System;using System.Drawing;using System.IO;using System.Linq;using System.Reflection;using System.Windows.Forms;using Solevia.Desktop;
class LayoutDriver {
 static T Field<T>(object form,string name){return (T)form.GetType().GetField(name,BindingFlags.Instance|BindingFlags.NonPublic).GetValue(form);}
 static void Check(Control parent){foreach(Control child in parent.Controls){if(!child.Visible)continue;
   if(!parent.ClientRectangle.Contains(child.Bounds)&&!(parent is ScrollableControl&&((ScrollableControl)parent).AutoScroll))throw new Exception("Overflow: "+child.GetType().Name+" "+child.Text+" "+child.Bounds+" in "+parent.ClientRectangle);
   Check(child);}}
 [STAThread]static int Main(string[] args){Console.OutputEncoding=new System.Text.UTF8Encoding(false);Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
  try {string root=args[0];bool repair=args[1]=="repair";float scale=Single.Parse(args[2],System.Globalization.CultureInfo.InvariantCulture);
   if(repair){Directory.CreateDirectory(Path.Combine(root,"user-data"));File.WriteAllText(Path.Combine(root,"user-data/.env"),"SOLEVIA_KERNEL=codex\nLLM_API_KEY=do-not-touch\n");
     Directory.CreateDirectory(Path.Combine(root,".runtime"));File.WriteAllText(Path.Combine(root,".runtime/installed.json"),"{\"features\":{\"web\":true,\"documents\":false,\"ocr\":false,\"semantic\":false}}");
     string saved=Path.Combine(root,"saved");Directory.CreateDirectory(Path.Combine(saved,".runtime"));
     File.WriteAllText(Path.Combine(saved,".runtime/installed.json"),"{\"features\":{\"web\":false,\"documents\":false,\"ocr\":true,\"semantic\":true}}");
     var method=typeof(InstallerForm).GetMethod("ReadPlan",BindingFlags.Static|BindingFlags.NonPublic);
     var task=(System.Threading.Tasks.Task<System.Collections.Generic.Dictionary<string,object>>)method.Invoke(null,new object[]{root,saved});
     var plan=task.GetAwaiter().GetResult();var values=(System.Collections.Generic.Dictionary<string,object>)((System.Collections.Generic.Dictionary<string,object>)plan["plan"])["features"];
     if(Convert.ToBoolean(values["web"])||Convert.ToBoolean(values["documents"])||!Convert.ToBoolean(values["ocr"])||!Convert.ToBoolean(values["semantic"]))throw new Exception("Embedded repair lost saved capabilities");
   }
   using(var form=new InstallerForm(root,false)) {
    form.ConnectionProbe=()=>System.Threading.Tasks.Task.FromResult(true);form.ShowInTaskbar=false;form.Opacity=0;var watch=DateTime.UtcNow;var timer=new Timer{Interval=100};
    timer.Tick+=(s,e)=>{try{if(!form.Ready){if((DateTime.UtcNow-watch).TotalSeconds>20)throw new Exception("Plan timeout");return;}timer.Stop();
      var codex=Field<CheckBox>(form,"codexChoice");var features=Field<CheckBox[]>(form,"features");
      if(!repair&&features.Any(f=>f.Checked))throw new Exception("Fresh install selected optional dependencies");
      if(repair&&!features[0].Checked)throw new Exception("Repair reset capabilities");
      var kernel=form.GetType().GetProperty("KernelArgument",BindingFlags.Instance|BindingFlags.NonPublic);
      if((string)kernel.GetValue(form,null)!="Keep")throw new Exception("Repair needs Keep until explicit choice");
      codex.Checked=false;codex.Checked=true;
      if((string)kernel.GetValue(form,null)!="Codex")throw new Exception("Choice not forwarded");
      if(!repair&&(features[0].Checked||features[3].Checked))throw new Exception("Codex defaults install unnecessary dependencies");
      if(repair&&!File.ReadAllText(Path.Combine(root,"user-data/.env")).Contains("LLM_API_KEY=do-not-touch"))throw new Exception("Preview changed settings");
      Field<Button>(form,"back").PerformClick();form.PerformLayout();Check(form);Field<Button>(form,"advanced").PerformClick();form.PerformLayout();Field<Button>(form,"back").PerformClick();form.PerformLayout();Check(form);Field<Button>(form,"advanced").PerformClick();
      Field<Button>(form,"install").PerformClick();form.PerformLayout();Check(form);
      if(!Field<Panel>(form,"capabilityPage").Visible)throw new Exception("Next did not open capabilities");
      form.Scale(new SizeF(scale,scale));form.PerformLayout();Check(form);
      Field<Panel>(form,"connectionPage").Visible=false;Field<Panel>(form,"capabilityPage").Visible=false;Field<Panel>(form,"progressPage").Visible=true;
      typeof(InstallerForm).GetMethod("Append",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(form,new object[]{"[4/6] 安装所选能力与依赖"});
      typeof(InstallerForm).GetMethod("Append",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(form,new object[]{"[NETWORK_PROGRESS] bytes=2097152 total=-1 speed=1048576"});
      form.PerformLayout();Check(form);
      if(Field<Panel>(form,"logPanel").Visible)throw new Exception("Logs should be collapsed");
      if(!Field<Label>(form,"downloadStatus").Text.Contains("1.0 MiB/s"))throw new Exception("Live download speed missing");
      form.Close();
    }catch(Exception error){Console.WriteLine(error);Environment.Exit(1);}};
    timer.Start();Application.Run(form);timer.Dispose();
   }
   using(var host=new Host(root,false)){host.ShowInTaskbar=false;host.Opacity=0;host.Show();host.PerformLayout();Check(host);host.Scale(new SizeF(scale,scale));host.PerformLayout();Check(host);host.ClosePreview();}
   if(File.Exists(Path.Combine(root,".runtime/services.json")))throw new Exception("Layout check started services");
   Console.WriteLine("Connection, repair, layout and scale passed");return 0;
  }catch(Exception error){Console.WriteLine(error);return 1;}
 }
}''', encoding='utf-8-sig')
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    executable = root / 'LayoutDriver.exe'
    command = [str(compiler), '/nologo', '/target:exe', '/main:LayoutDriver', '/out:' + str(executable)]
    command += ['/reference:' + name for name in ('System.dll', 'System.Core.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.Web.Extensions.dll', 'System.IO.Compression.dll', 'System.IO.Compression.FileSystem.dll', 'Microsoft.CSharp.dll')]
    command += [str(driver)] + [str(path) for path in source.glob('*.cs')]
    result = subprocess.run(command, capture_output=True)
    assert result.returncode == 0, result.stdout.decode(errors='replace')
    return root, executable


@pytest.mark.parametrize('mode', ['fresh', 'repair'])
@pytest.mark.parametrize('scale', ['1', '1.25', '1.5'])
def test_native_connection_wizard_and_host_fit_at_multiple_scales(native_layout_driver, tmp_path, mode, scale):
    fixture, executable = native_layout_driver
    root = tmp_path / 'product'
    shutil.copytree(fixture / 'scripts', root / 'scripts')
    shutil.copyfile(fixture / 'release.json', root / 'release.json')
    result = subprocess.run([str(executable), str(root), mode, scale], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace')
