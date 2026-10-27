using System;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Diagnostics;
using System.Threading.Tasks;
using System.Collections.Generic;

namespace Solevia.Desktop {
    // Discovery only. Authentication stays inside Codex; finding a binary is not a login check.
    public static class ConnectionSetup {
        public static async Task<bool> ProbeCodex() {
            string executable=FindCodex();if(executable==null)return false;
            using(var process=new Process {StartInfo=ProcessRunner.Info(executable,"app-server --listen stdio:// -c project_doc_max_bytes=0",Path.GetTempPath())}) {
                try {
                    process.Start();process.BeginErrorReadLine();
                    var check=Probe(process);
                    if(await Task.WhenAny(check,Task.Delay(25000))!=check)return false;
                    return await check;
                }catch{return false;}
                finally {try{if(!process.HasExited)process.Kill();process.WaitForExit(3000);}catch{}}
            }
        }
        static async Task<bool> Probe(Process process) {
            string version=System.Reflection.Assembly.GetExecutingAssembly().GetName().Version.ToString(3);
            await process.StandardInput.WriteLineAsync("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"clientInfo\":{\"name\":\"solevia-installer\",\"version\":\""+version+"\"}}}");
            if(await Reply(process,1)==null)return false;
            await process.StandardInput.WriteLineAsync("{\"jsonrpc\":\"2.0\",\"method\":\"initialized\"}");
            await process.StandardInput.WriteLineAsync("{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"account/read\",\"params\":{}}");
            var account=await Reply(process,2);if(account==null)return false;
            object value;
            return account.TryGetValue("account",out value)&&value!=null || account.TryGetValue("requiresOpenaiAuth",out value)&&value is bool&&!(bool)value;
        }
        static async Task<Dictionary<string,object>> Reply(Process process,int id) {
            string line;
            while((line=await process.StandardOutput.ReadLineAsync())!=null) {
                if(line.Length>1024*1024)return null;
                var reply=Product.Json.Deserialize<Dictionary<string,object>>(line);
                object received;
                if(reply.TryGetValue("id",out received)&&Convert.ToString(received)==id.ToString()) {
                    object result;if(reply.ContainsKey("error"))return null;
                    return reply.TryGetValue("result",out result)?result as Dictionary<string,object>:null;
                }
            }return null;
        }
        public static string FindCodex() {
            string configured=Environment.GetEnvironmentVariable("SOLEVIA_CODEX_EXECUTABLE");
            if(!String.IsNullOrWhiteSpace(configured)&&File.Exists(configured))return configured;
            foreach(string directory in (Environment.GetEnvironmentVariable("PATH")??"").Split(Path.PathSeparator)) {
                try{string candidate=Path.Combine(directory.Trim('"'),"codex.exe");if(File.Exists(candidate))return candidate;}catch{}
            }
            string npm=Path.Combine(Environment.GetEnvironmentVariable("APPDATA")??Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),@"npm\node_modules\@openai\codex\node_modules");
            foreach(string arch in new[]{"x64","arm64"}) {
                string candidate=Path.Combine(npm,"@openai", "codex-win32-"+arch,"vendor",arch=="x64"?"x86_64-pc-windows-msvc":"aarch64-pc-windows-msvc","bin","codex.exe");
                if(File.Exists(candidate))return candidate;
            }
            string bundled=Path.Combine(Environment.GetEnvironmentVariable("LOCALAPPDATA")??Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),@"OpenAI\Codex\bin");
            try {if(Directory.Exists(bundled))return Directory.GetDirectories(bundled).Select(d=>Path.Combine(d,"codex.exe")).Where(File.Exists).OrderByDescending(File.GetLastWriteTimeUtc).FirstOrDefault();}catch{}
            return null;
        }
        public static string SavedMode(string root) {
            string path=Path.Combine(root,@"user-data\.env");
            if(!File.Exists(path))return null;
            Package.RejectLinks(path);
            // Inspect just the mode; never return, log, or copy any credential fields.
            string result=null;
            foreach(string line in File.ReadLines(path)) {
                var match=Regex.Match(line,@"^\s*(?:export\s+)?SOLEVIA_KERNEL\s*=\s*['"" ]*(api|codex|auto)\b",RegexOptions.IgnoreCase);
                if(match.Success)result=match.Groups[1].Value.ToLowerInvariant();
            }
            return result;
        }
        public const string RepairPrompt="请帮我检查本机 Codex 是否已安装并完成登录，确认 codex app-server 可以启动，然后在 Solevia 的模型连接中选择本机 Codex。不要读取或复制登录密钥，不要关闭沙箱，不要授予整个磁盘权限，不要修改全局配置。需要安装、登录或改动设置时先向我说明。";
    }
}
