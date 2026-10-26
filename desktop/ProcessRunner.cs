using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;

namespace Solevia.Desktop {
    public static class ProcessRunner {
        public static string Quote(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }
        public static ProcessStartInfo Info(string file, string arguments, string root) {
            if(String.Equals(file,"powershell.exe",StringComparison.OrdinalIgnoreCase))
                file=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),@"WindowsPowerShell\v1.0\powershell.exe");
            var info = new ProcessStartInfo(file, arguments) { WorkingDirectory=root, UseShellExecute=false,
                CreateNoWindow=true, RedirectStandardOutput=true, RedirectStandardError=true, RedirectStandardInput=true,
                StandardOutputEncoding=Encoding.UTF8, StandardErrorEncoding=Encoding.UTF8 };
            info.EnvironmentVariables.Remove("PSModulePath");
            foreach(string key in new[]{"LLM_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY","DEEPSEEK_API_KEY","GITHUB_TOKEN","GH_TOKEN","NPM_TOKEN"})info.EnvironmentVariables.Remove(key);
            info.EnvironmentVariables["PYTHONUTF8"]="1";
            return info;
        }
        public static string PowerShell(string script, string arguments) {
            string cmd="$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $OutputEncoding=[Console]::OutputEncoding; try { & '"+
                script.Replace("'", "''")+"' "+arguments+" *>&1 | ForEach-Object {[Console]::Out.WriteLine([string]$_)}; if(-not $?){exit 1} } catch {[Console]::Out.WriteLine([string]$_.Exception.Message); exit 1}";
            return "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -OutputFormat Text -EncodedCommand "+Convert.ToBase64String(Encoding.Unicode.GetBytes(cmd));
        }
        public static async Task<int> Run(string file, string arguments, string root, Action<string> output, Action<string> errors=null,bool heartbeat=false) {
            using(var process=new Process { StartInfo=Info(file,arguments,root) }) {
                process.OutputDataReceived+=(s,e)=>{if(e.Data!=null && output!=null) output(e.Data);};
                process.ErrorDataReceived+=(s,e)=>{if(e.Data!=null) {if(errors!=null)errors(e.Data);else if(output!=null)output(e.Data);}};
                process.Start(); process.StandardInput.Close(); process.BeginOutputReadLine();process.BeginErrorReadLine();
                DateTime lastOutput=DateTime.UtcNow;
                process.OutputDataReceived+=(s,e)=>{if(e.Data!=null)lastOutput=DateTime.UtcNow;};
                DateTime started=DateTime.UtcNow;
                while(!process.HasExited) {
                    await Task.Delay(1000);
                    if(heartbeat&&(DateTime.UtcNow-lastOutput).TotalSeconds>=30) {
                        if(output!=null)output("[等待] 当前步骤仍在处理，已用 "+(int)(DateTime.UtcNow-started).TotalSeconds+" 秒。暂无新日志，可能正在下载、解压或构建，请耐心等待。");
                        lastOutput=DateTime.UtcNow;
                    }
                }
                process.WaitForExit();return process.ExitCode;
            }
        }
        public static void Open(string target) { Process.Start(new ProcessStartInfo(target) { UseShellExecute=true }); }
    }
}
