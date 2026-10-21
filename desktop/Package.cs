using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;

namespace Solevia.Desktop {
    public static class Package {
        public static bool Embedded {get {return Assembly.GetExecutingAssembly().GetManifestResourceInfo("solevia.payload")!=null;}}
        public static string Hash(byte[] bytes) {
            using(var hash=SHA256.Create())return BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();
        }
        public static byte[] Resource(string name) {
            using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream(name)) {
                if(stream==null)throw new InvalidDataException("安装包资源缺失："+name);
                using(var memory=new MemoryStream()){stream.CopyTo(memory);return memory.ToArray();}
            }
        }
        public static string SafePath(string root,string relative) {
            string[] parts=relative.Split('/');
            string[] forbidden={".git",".runtime","user-data","data","uploads","node_modules",".venv",".env","models","checkpoints"};
            string[] types={".exe",".dll",".db",".sqlite",".key",".pem",".safetensors",".pt",".zip"};
            if(String.IsNullOrWhiteSpace(relative)||relative.Contains("\\")||relative.Contains(":")||relative.StartsWith("/")||
                parts.Any(p=>p==".."||p=="."||p.Length==0||p.EndsWith(".")||p.EndsWith(" ")||p.Any(c=>c<32)||forbidden.Contains(p.ToLowerInvariant()))||
                types.Contains(Path.GetExtension(relative).ToLowerInvariant()))throw new InvalidDataException("无效安装路径");
            foreach(string part in parts) {
                string name=part.Split('.')[0].ToUpperInvariant();
                if(new[]{"CON","PRN","AUX","NUL"}.Contains(name)||System.Text.RegularExpressions.Regex.IsMatch(name,@"^(COM|LPT)[1-9]$"))throw new InvalidDataException("保留文件名");
            }
            string full=Path.GetFullPath(Path.Combine(root,relative.Replace('/',Path.DirectorySeparatorChar)));
            if(!full.StartsWith(Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("安装路径超出目标目录");
            RejectLinks(full);return full;
        }
        public static void RejectLinks(string full) {
            for(string path=full;!String.IsNullOrEmpty(path);path=Path.GetDirectoryName(path)) {
                if((File.Exists(path)||Directory.Exists(path))&&(File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0)
                    throw new InvalidDataException("请选择普通文件夹，安装位置不能包含目录链接。");
            }
        }
        public static Dictionary<string,byte[]> Verify(byte[] bytes) {
            var files=new Dictionary<string,byte[]>(StringComparer.OrdinalIgnoreCase);
            using(var memory=new MemoryStream(bytes))using(var zip=new ZipArchive(memory,ZipArchiveMode.Read)) {
                if(zip.Entries.Count==0||zip.Entries.Count>5000||zip.Entries.Sum(e=>e.Length)>200L*1024*1024)throw new InvalidDataException("安装包大小异常");
                string prefix=zip.Entries[0].FullName.Split('/')[0]+"/";
                foreach(var entry in zip.Entries) {
                    if(entry.FullName.EndsWith("/"))continue;
                    if(!entry.FullName.StartsWith(prefix,StringComparison.Ordinal)||entry.Length>12*1024*1024)throw new InvalidDataException("安装包结构无效");
                    string name=entry.FullName.Substring(prefix.Length);SafePath(Path.GetTempPath(),name);
                    if(files.ContainsKey(name))throw new InvalidDataException("安装包含重复路径");
                    using(var source=entry.Open())using(var output=new MemoryStream()){source.CopyTo(output);files.Add(name,output.ToArray());}
                }
            }
            var manifest=Product.Json.Deserialize<Dictionary<string,object>>(Encoding.UTF8.GetString(files["MANIFEST.json"]));
            var expected=new HashSet<string>(StringComparer.OrdinalIgnoreCase){"MANIFEST.json"};
            foreach(Dictionary<string,object> row in (ArrayList)manifest["files"]) {
                string name=Convert.ToString(row["path"]);
                if(!expected.Add(name)||!files.ContainsKey(name)||files[name].LongLength!=Convert.ToInt64(row["bytes"])||Hash(files[name])!=Convert.ToString(row["sha256"]))
                    throw new InvalidDataException("安装文件校验失败");
            }
            if(!expected.SetEquals(files.Keys))throw new InvalidDataException("安装清单与文件不一致");
            var config=Product.Json.Deserialize<Dictionary<string,object>>(Encoding.UTF8.GetString(files["release.json"]));
            if(Convert.ToString(config["product"])!="Solevia"||Convert.ToString(config["repository"])!=Product.Repository||Convert.ToString(config["platform"])!="windows-x64")throw new InvalidDataException("产品标识不匹配");
            foreach(string required in new[]{"scripts/install.ps1","scripts/install-options.ps1","scripts/build-desktop.ps1","desktop/Program.cs","desktop/Host.cs"})
                if(!files.ContainsKey(required))throw new InvalidDataException("安装包缺少必要文件");
            return files;
        }
        public static void Extract(string root,Action<string> report) {
            byte[] bytes=Resource("solevia.payload");
            if(Hash(bytes)!=Encoding.UTF8.GetString(Resource("solevia.sha256")).Trim())throw new InvalidDataException("安装包完整性校验失败，请重新下载。");
            var files=Verify(bytes);
            RejectLinks(Path.GetFullPath(root));
            if(Directory.Exists(root)&&Directory.EnumerateFileSystemEntries(root).Any()) {
                if(!File.Exists(Path.Combine(root,"release.json")))throw new IOException("此文件夹不是 Solevia 安装目录。请选择空文件夹。");
                var current=Product.Read(Path.Combine(root,"release.json"));
                var incoming=Product.Json.Deserialize<Dictionary<string,object>>(Encoding.UTF8.GetString(files["release.json"]));
                if(Convert.ToString(current["repository"])!=Product.Repository||Convert.ToString(current["version"])!=Convert.ToString(incoming["version"]))
                    throw new IOException("该目录已有其他版本。请从 Solevia 管理窗口更新，或选择新的空文件夹安装。");
                if(File.Exists(Path.Combine(root,@".runtime\services.json")))throw new IOException("请先退出此目录运行中的 Solevia，再进行修复安装。");
            }
            // Validate every destination before the first write; never traverse a user-created link.
            foreach(string name in files.Keys)SafePath(root,name);
            Directory.CreateDirectory(root);
            foreach(var pair in files) {
                string path=SafePath(root,pair.Key);Directory.CreateDirectory(Path.GetDirectoryName(path));File.WriteAllBytes(path,pair.Value);
            }
            report("产品文件已校验并解压；个人资料目录保持独立。");
        }
    }
}
