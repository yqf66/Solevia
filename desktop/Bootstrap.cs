using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;

namespace Solevia.Desktop {
    public static class Bootstrap {
        // Official Astral uv 0.13.3 Windows x64 archive. A digest mismatch is fatal.
        public const string Digest="bb701bd35b586c45966654ea2703c9bd116a6a4fc0a8ca755bd644c2c41c5ebe";
        public static void Prepare(string root,Action<string> report) {
            if(Assembly.GetExecutingAssembly().GetManifestResourceInfo("solevia.bootstrap")==null)return;
            byte[] bytes=Package.Resource("solevia.bootstrap");
            if(Package.Hash(bytes)!=Digest)throw new InvalidDataException("[HASH_MISMATCH] 内置安装工具校验失败，请重新下载安装器。");
            string folder=Path.Combine(root,@".runtime\downloads");
            Package.RejectLinks(folder);Package.RejectLinks(Path.Combine(folder,"uv.zip"));
            Directory.CreateDirectory(folder);File.WriteAllBytes(Path.Combine(folder,"uv.zip"),bytes);
            report("[1/6] 内置安装工具已校验并就绪，无需从 GitHub 下载安装工具。");
        }
    }
}
