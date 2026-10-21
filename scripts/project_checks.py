"""Version mirrors and byte-exact public manifest checks, for source and release trees."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def paths(root):
    installed = (root / "release.json").exists()
    return (root if installed else root / "distribution"), (root / "app" if installed else root)


def versions(root, *, write=False):
    product, app = paths(root)
    version = json.loads((product / "release.json").read_text(encoding="utf8"))["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Expected stable major.minor.patch version")
    mirrors = {
        app / "backend/pyproject.toml": (r'(?m)^version = "[^"]+"', f'version = "{version}"'),
        app / "backend/src/planner_api/main.py": (r'version="[^"]+"', f'version="{version}"'),
        product / "desktop/Program.cs": (r'AssemblyVersion\("[^"]+"\)', f'AssemblyVersion("{version}.0")'),
    }
    for path, (pattern, replacement) in mirrors.items():
        old = path.read_text(encoding="utf8")
        new, count = re.subn(pattern, replacement, old)
        if count != 1:
            raise ValueError("Missing unique version mirror: " + path.name)
        if write:
            path.write_text(new, encoding="utf8")
        elif new != old:
            raise ValueError("Version mismatch: " + path.name)
    for relative in ["frontend/package.json", "services/web-research/package.json", "services/web-research/package-lock.json"]:
        path = app / relative
        data = json.loads(path.read_text(encoding="utf8"))
        mismatch = data["version"] != version
        data["version"] = version
        if "packages" in data:
            mismatch |= data["packages"][""]["version"] != version
            data["packages"][""]["version"] = version
        if write:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf8")
        elif mismatch:
            raise ValueError("Version mismatch: " + relative)
    for path in [root / "README.md", product / "START_HERE.md"]:
        old = path.read_text(encoding="utf8")
        new = re.sub(r'(Solevia-(?:Setup-)?)\d+\.\d+\.\d+(-windows-x64)', rf'\g<1>{version}\g<2>', old)
        if write:
            path.write_text(new, encoding="utf8")
        elif new != old:
            raise ValueError("Download filename version mismatch: " + path.name)
    print("Version mirrors verified: " + version)


def manifest(root, *, write=False):
    from package_integrity import safe_path, scan_text

    target = root / "MANIFEST.json"
    metadata = json.loads((root / "release.json").read_text(encoding="utf8"))
    if write:
        previous = json.loads(target.read_text(encoding="utf8"))
        rows = []
        for row in previous["files"]:
            name = row["path"]
            safe_path(name)
            data = (root / name).read_bytes()
            scan_text(name, data)
            rows.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        target.write_text(json.dumps({"product": "Solevia", "version": metadata["version"], "files": rows}, ensure_ascii=False, indent=2), encoding="utf8")
    data = json.loads(target.read_text(encoding="utf8"))
    if data["product"] != "Solevia" or data["version"] != metadata["version"]:
        raise ValueError("Manifest product/version mismatch")
    names = set()
    for row in data["files"]:
        name = row["path"]
        safe_path(name)
        if name.casefold() in names:
            raise ValueError("Duplicate manifest entry")
        names.add(name.casefold())
        contents = (root / name).read_bytes()
        scan_text(name, contents)
        if len(contents) != row["bytes"] or hashlib.sha256(contents).hexdigest() != row["sha256"]:
            raise ValueError("Manifest mismatch: " + name)
    if (root / ".git").exists():
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf8").split("\0")
        if {name.casefold() for name in tracked if name} != names | {"manifest.json"}:
            raise ValueError("Tracked file set differs from public manifest")
    print("Public file manifest verified: " + str(len(names)) + " files")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["versions", "manifest"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    {"versions": versions, "manifest": manifest}[args.mode](args.root.resolve(), write=args.write)
