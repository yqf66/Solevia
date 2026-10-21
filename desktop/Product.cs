using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Web.Script.Serialization;

namespace Solevia.Desktop {
    public static class Product {
        public const string Repository="yqf66/Solevia";
        public static JavaScriptSerializer Json = new JavaScriptSerializer {MaxJsonLength=16*1024*1024};
        public static Dictionary<string,object> Read(string path) {
            return Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(path,Encoding.UTF8).TrimStart('\ufeff'));
        }
        public static string Python(string root) {return Path.Combine(root,@".runtime\venv\Scripts\python.exe");}
        public static string Version(string root) { return Convert.ToString(Read(Path.Combine(root,"release.json"))["version"]); }
        public static bool Healthy(string url) {
            Uri uri;
            if(!Uri.TryCreate(url,UriKind.Absolute,out uri) || uri.Scheme!="http" || uri.Host!="127.0.0.1")return false;
            try {var request=(HttpWebRequest)WebRequest.Create(url);request.Proxy=null;request.Timeout=1200;
                using(var response=(HttpWebResponse)request.GetResponse())return response.StatusCode==HttpStatusCode.OK;
            } catch {return false;}
        }
        public static string RunningUrl(string root) {
            try {string url=Convert.ToString(Read(Path.Combine(root,@".runtime\services.json"))["url"]);
                return Healthy(url+"/_solevia/health")?url:null;
            }catch{return null;}
        }
        public static string FeatureArguments(bool[] choices) {
            return "-NonInteractive -Web "+(choices[0]?"1":"0")+" -Documents "+(choices[1]?"1":"0")+
                " -Ocr "+(choices[2]?"1":"0")+" -Semantic "+(choices[3]?"1":"0");
        }
        public static bool NeedsBrowser(string root) {
            foreach(var pair in new[]{new[]{"ProgramFiles(x86)",@"Microsoft\Edge\Application\msedge.exe"},new[]{"ProgramFiles",@"Microsoft\Edge\Application\msedge.exe"},
                new[]{"ProgramFiles",@"Google\Chrome\Application\chrome.exe"},new[]{"ProgramFiles(x86)",@"Google\Chrome\Application\chrome.exe"},new[]{"LOCALAPPDATA",@"Google\Chrome\Application\chrome.exe"}})
                if(File.Exists(Path.Combine(Environment.GetEnvironmentVariable(pair[0])??"",pair[1])))return false;
            string cache=Path.Combine(root,@".runtime\browsers");
            return !Directory.Exists(cache)||!Directory.EnumerateFiles(cache,"chrome.exe",SearchOption.AllDirectories).Any();
        }
        public static void Shortcuts(string root) {
            Type type=Type.GetTypeFromProgID("WScript.Shell");
            dynamic shell=Activator.CreateInstance(type);
            var locations=new[]{Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs),"Solevia")};
            foreach(string folder in locations) {
                Directory.CreateDirectory(folder);
                dynamic link=shell.CreateShortcut(Path.Combine(folder,"Solevia.lnk"));
                link.TargetPath=Path.Combine(root,"Solevia.exe");link.WorkingDirectory=root;
                link.Description="Solevia · 足下 — 打开工作台与管理后台服务";
                link.IconLocation=Path.Combine(root,@"desktop\solevia.ico");link.Save();
            }
        }
    }
}
