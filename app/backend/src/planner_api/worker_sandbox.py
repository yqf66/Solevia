"""Resource and capability boundaries for trusted workers handling untrusted data.

Job Objects/rlimits are OS resource limits. The Python audit policy is defense in
depth for our parsers, not a VM for running attacker-supplied Python/native code.
"""
from __future__ import annotations

import ctypes
from contextvars import ContextVar
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def worker_environment(folder: Path | None = None) -> dict[str, str]:
    # Allowlist, never copy application/provider/database credentials or PYTHONPATH.
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "SYSTEMDRIVE",
               "PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA", "LANG", "LC_ALL",
               "TEMP", "TMP", "TMPDIR"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHON_DOTENV_DISABLED="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if folder is not None:
        env.update({key: str(folder) for key in ("TEMP", "TMP", "TMPDIR")})
    return env


class WorkerJob:
    """Close kills the entire Windows job, including venv launcher descendants."""
    def __init__(self, memory_mb: int = 4096, max_processes: int = 8):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.api.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "Cannot create worker job")
        info = Extended()
        # KILL_ON_JOB_CLOSE | JOB_MEMORY | ACTIVE_PROCESS; no breakaway permission.
        info.basic.flags = 0x2000 | 0x200 | 0x8
        info.basic.active = max_processes
        info.job_memory = memory_mb * 1024 * 1024
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "Cannot set worker job limits")

    def attach(self, pid: int):
        if self.handle is None:
            return
        handle = self.api.OpenProcess(0x100 | 0x1, False, pid)
        try:
            if not handle or not self.api.AssignProcessToJobObject(self.handle, handle):
                raise OSError(ctypes.get_last_error(), "Cannot contain worker process")
        finally:
            if handle:
                self.api.CloseHandle(handle)

    def close(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)
            # Termination is asynchronous. Wait for descendants to release
            # staged documents/logs before TemporaryDirectory removes them.
            accounting = (ctypes.c_byte * 48)()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not self.api.QueryInformationJobObject(self.handle, 1,
                        accounting, ctypes.sizeof(accounting), None):
                    break
                if ctypes.c_uint32.from_buffer(accounting, 40).value == 0:
                    break
                time.sleep(0.02)
            self.api.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def limit_current_process(memory_mb: int):
    """On Windows join a self-owned job before any parser/browser starts."""
    if os.name == "nt":
        job = WorkerJob(memory_mb, 64)
        job.attach(os.getpid())
        # Keep the handle until process termination (also cleans orphan browsers).
        _jobs.append(job)
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024**2,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


_jobs: list[WorkerJob] = []
_worker_command = ContextVar("contained_worker_command", default=None)


def worker_ready(folder: Path, memory_mb: int = 4096):
    limit_current_process(memory_mb)
    (folder / "ready.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    deadline = time.monotonic() + 15
    while not (folder / "start").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Worker containment handshake timed out")
        time.sleep(0.02)


def run_contained(command, folder: Path, *, env: dict[str, str], timeout_seconds: float,
                  memory_mb: int = 4096, check_cancelled=None):
    """Do not release untrusted work until the actual interpreter joins our job."""
    with WorkerJob(memory_mb, 64) as job, (folder / "process.log").open("wb") as log:
        token = _worker_command.set(command)
        try:
            process = subprocess.Popen(command, cwd=folder, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
        finally:
            _worker_command.reset(token)
        released = False
        started = time.monotonic()
        try:
            job.attach(process.pid)
            while process.poll() is None:
                if check_cancelled:
                    check_cancelled()
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError("Worker time budget exceeded")
                if not released and (folder / "ready.json").exists():
                    try:
                        ready = json.loads((folder / "ready.json").read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        continue
                    job.attach(int(ready["pid"]))
                    (folder / "start").touch()
                    released = True
                if (folder / "process.log").stat().st_size > 1_000_000:
                    raise RuntimeError("Worker diagnostic output budget exceeded")
                time.sleep(0.05)
            if check_cancelled:
                check_cancelled()
            if not released or process.returncode:
                raise RuntimeError("Contained worker failed")
        finally:
            # Close the job before waiting: the Windows venv launcher waits on
            # the real interpreter, which is a different PID.
            job.close()
            if os.name != "nt":
                import signal
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait()


def install_parser_policy(folder: Path, *, model_home: str = "", mineru_python: str = ""):
    """Deny parser network, arbitrary subprocesses and out-of-scope Python file IO."""
    folder = folder.resolve()
    roots = [Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(),
             Path(__file__).resolve().parent, folder]
    if model_home:
        roots.append(Path(model_home).resolve())
    if os.name == "nt":
        roots.append((Path(os.environ["SYSTEMROOT"]) / "Fonts").resolve())
    runner = str((Path(__file__).parent / "materials" / "mineru_runner.py").resolve())
    executable = str(Path(mineru_python or sys.executable).resolve())

    def inside(path, allowed):
        resolved = Path(os.fsdecode(path)).resolve()
        return any(resolved.is_relative_to(root) for root in allowed)

    def audit(event, args):
        if event == "open" and not isinstance(args[0], int):
            if os.fsdecode(args[0]).casefold() == os.devnull.casefold():
                return
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if not inside(args[0], [folder] if writing else roots):
                raise PermissionError("PARSER_FILE_SCOPE_DENIED")
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chdir", "os.chmod", "os.utime"}:
            if args[0] is not None and not inside(args[0], [folder]):
                raise PermissionError("PARSER_WRITE_SCOPE_DENIED")
        elif event in {"os.rename", "os.link", "os.symlink"}:
            if not all(inside(p, [folder]) for p in args[:2]):
                raise PermissionError("PARSER_WRITE_SCOPE_DENIED")
        elif event.startswith("socket.") and event not in {"socket.__new__"}:
            raise PermissionError("PARSER_NETWORK_DENIED")
        elif event == "subprocess.Popen":
            argv = _worker_command.get()
            if not (isinstance(argv, (list, tuple)) and len(argv) == 7 and argv[1] == "-I"
                    and str(Path(argv[0]).resolve()) == executable
                    and str(Path(argv[2]).resolve()) == runner
                    and args[1] == (subprocess.list2cmdline(argv) if os.name == "nt" else argv)
                    and all(inside(p, [folder]) for p in argv[3:5])):
                raise PermissionError("PARSER_PROCESS_DENIED")
        elif event in {"os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty"}:
            raise PermissionError("PARSER_PROCESS_DENIED")
    sys.addaudithook(audit)
