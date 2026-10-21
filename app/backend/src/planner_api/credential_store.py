"""Windows current-user DPAPI storage. No encryption keys are shipped with Solevia."""
import ctypes
import os
from pathlib import Path
import tempfile

from dotenv import set_key


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt=False) -> bytes:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), buffer)
    target = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    function.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN; omit LOCAL_MACHINE to bind to the current user.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("无法使用当前 Windows 用户读取或保存模型密钥。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def key_path(env_path: Path) -> Path:
    return env_path.with_name(env_path.name + ".model-key.dpapi")


def write_key(env_path: Path, value: str) -> None:
    if os.name != "nt":
        set_key(str(env_path), "LLM_API_KEY", value, quote_mode="auto")
        env_path.chmod(0o600)
        return
    destination = key_path(env_path)
    if value:
        encrypted = _crypt(value.encode("utf8"))
        fd, temporary = tempfile.mkstemp(prefix=".model-key-", dir=env_path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    else:
        destination.unlink(missing_ok=True)
    set_key(str(env_path), "LLM_API_KEY", "", quote_mode="auto")


def read_key(env_path: Path, legacy: str = "", *, migrate=False) -> str:
    if os.name != "nt":
        return legacy
    if legacy:
        if migrate:
            write_key(env_path, legacy)
        return legacy
    destination = key_path(env_path)
    return _crypt(destination.read_bytes(), decrypt=True).decode("utf8") if destination.exists() else ""
