"""Trusted fixture for exercising the real child process boundary."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from planner_api.worker_sandbox import install_parser_policy, worker_ready

folder = Path.cwd()
worker_ready(folder)
if sys.argv[1] == "tree":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (folder / "child.pid").write_text(str(child.pid))
    time.sleep(120)
elif sys.argv[1] == "memory":
    try:
        payload = bytearray(700 * 1024 * 1024)
        (folder / "memory.json").write_text(json.dumps({"limited": False}))
    except MemoryError:
        (folder / "memory.json").write_text(json.dumps({"limited": True}))
else:
    outside = sys.argv[2]
    install_parser_policy(folder)
    checks = {"secret_removed": "SANDBOX_TEST_SECRET" not in os.environ}
    operations = {
        "file_read_denied": lambda: Path(outside).read_text(),
        "file_write_denied": lambda: Path(outside).write_text("bad"),
        "network_denied": lambda: socket.create_connection(("127.0.0.1", 8000)),
        "process_denied": lambda: subprocess.Popen([sys.executable, "-c", "print(1)"]),
    }
    for name, operation in operations.items():
        try:
            operation()
            checks[name] = False
        except PermissionError:
            checks[name] = True
    (folder / "result.json").write_text(json.dumps(checks))
