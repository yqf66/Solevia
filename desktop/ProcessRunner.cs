using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;

namespace Solevia.Desktop {
    public static class ProcessRunner {
        public static string Quote(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }
        public static ProcessStartInfo Info(string file, string arguments, string root) {
            var info = new ProcessStartInfo(file, arguments) { WorkingDirectory=root, UseShellExecute=false,
                CreateNoWindow=true, RedirectStandardOutput=true, RedirectStandardError=true, RedirectStandardInput=true,
                StandardOutputEncoding=Encoding.UTF8, StandardErrorEncoding=Encoding.UTF8 };
            info.EnvironmentVariables.Remove("PSModulePath");
            info.EnvironmentVariables["PYTHONUTF8"]="1";
            return info;
        }
        public static string PowerShell(string script, string arguments) {
            string cmd="$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $OutputEncoding=[Console]::OutputEncoding; & '"+
                script.Replace("'", "''")+"' "+arguments+"; if(-not $?){exit 1}";
            return "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -OutputFormat Text -EncodedCommand "+Convert.ToBase64String(Encoding.Unicode.GetBytes(cmd));
        }
        public static async Task<int> Run(string file, string arguments, string root, Action<string> output, Action<string> errors=null) {
            using(var process=new Process { StartInfo=Info(file,arguments,root) }) {
                process.OutputDataReceived+=(s,e)=>{if(e.Data!=null && output!=null) output(e.Data);};
                process.ErrorDataReceived+=(s,e)=>{if(e.Data!=null) {if(errors!=null)errors(e.Data);else if(output!=null)output(e.Data);}};
                process.Start(); process.StandardInput.Close(); process.BeginOutputReadLine();process.BeginErrorReadLine();
                await Task.Run(()=>process.WaitForExit()); return process.ExitCode;
            }
        }
        public static void Open(string target) { Process.Start(new ProcessStartInfo(target) { UseShellExecute=true }); }
    }
}
