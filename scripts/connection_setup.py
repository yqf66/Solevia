"""Persist the installer's explicit kernel choice, without touching either credential profile."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile


def select_kernel(root: Path, mode: str) -> bool:
    if mode not in {"keep", "codex", "api"}:
        raise ValueError("Unknown kernel selection")
    if mode == "keep":
        return False
    root = root.absolute()
    config = root / "user-data" / ".env"
    # Fail closed on redirected paths, including Windows junctions.
    for path in (root, *root.parents, config.parent, config):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Redirected configuration path is not supported")
    content = config.read_bytes().decode("utf-8-sig") if config.exists() else ""
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines(keepends=True)
    pattern = re.compile(r"^\s*(?:export\s+)?SOLEVIA_KERNEL\s*=")
    output = []
    found = False
    for line in lines:
        if pattern.match(line):
            if not found:
                output.append(f"SOLEVIA_KERNEL={mode}{newline}")
                found = True
        else:
            output.append(line)
    # An explicit installer decision supersedes any earlier background consent.
    output = [line for line in output if not re.match(r"^\s*(?:export\s+)?SOLEVIA_CODEX_CONSENT\s*=", line)]
    if not found:
        if output and not output[-1].endswith(("\r", "\n")):
            output[-1] += newline
        output.append(f"SOLEVIA_KERNEL={mode}{newline}")
    if output and not output[-1].endswith(("\r", "\n")):
        output[-1] += newline
    output.append(f"SOLEVIA_CODEX_CONSENT={'1' if mode == 'codex' else '0'}{newline}")
    config.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".connection-", dir=config.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write("".join(output).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--kernel", choices=("keep", "api", "codex"), default="keep")
    options = parser.parse_args()
    select_kernel(options.root, options.kernel)
    print("模型连接选择已保存。" if options.kernel != "keep" else "保留已有模型连接。")
