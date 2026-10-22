using System;
using System.Text.RegularExpressions;

namespace Solevia.Desktop {
    public static class InstallDiagnostics {
        public static string Clean(string value) {
            if(String.IsNullOrEmpty(value))return "";
            string home=Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            if(!String.IsNullOrEmpty(home))value=Regex.Replace(value,Regex.Escape(home),"%USERPROFILE%",RegexOptions.IgnoreCase);
            foreach(string name in new[]{Environment.MachineName,Environment.UserName})
                if(name.Length>=3)value=Regex.Replace(value,@"(?<![\w])"+Regex.Escape(name)+@"(?![\w])","[本机]",RegexOptions.IgnoreCase);
            value=Regex.Replace(value,@"(?i)(https?://)[^\s/@:]+:[^\s/@]+@","$1[凭据已隐藏]@");
            value=Regex.Replace(value,@"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b","[密钥已隐藏]");
            value=Regex.Replace(value,@"(?i)((?:api[_-]?key|access[_-]?token|authorization|password)\s*[:=]\s*)(?:Bearer\s+)?[^\s,;]+","$1[凭据已隐藏]");
            return value;
        }
    }
}
