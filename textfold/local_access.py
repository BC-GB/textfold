"""Credentials for the local reader and its same-user integrations."""
import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat

from ._storage_layout import DirectoryFiles

ANNOUNCEMENT = "server.json"
_KEY = re.compile(r"[a-f0-9]{64}")
_URL = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})")


def key():
    return secrets.token_hex(32)


def valid(value):
    return isinstance(value, str) and _KEY.fullmatch(value) is not None


def origin_port(url):
    match = _URL.fullmatch(url) if isinstance(url, str) else None
    port = int(match[1]) if match else 0
    return port if 1 <= port <= 65535 else None


def proof(secret, url, pid):
    return hmac.new(secret.encode("ascii"), f"{pid}:{url}".encode("ascii"), hashlib.sha256).hexdigest()


def credentials(header, secret):
    """Basic auth is scoped to this browser origin. Native clients send a bearer token."""
    scheme, _, value = (header or "").partition(" ")
    scheme = scheme.lower()
    if scheme == "basic":
        try:
            user, colon, value = base64.b64decode(value, validate=True).decode("ascii").partition(":")
        except (ValueError, binascii.Error):
            return None
        if user != "textfold" or not colon:
            return None
    elif scheme != "bearer":
        return None
    return scheme if valid(value) and hmac.compare_digest(value, secret) else None


def _dpapi(data, decrypt=False):
    """Use the Windows user's DPAPI key even when the library lives in a shared folder."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.c_void_p)]

    raw = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.addressof(raw))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.length)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(output.data)


def publish(data, record, secret):
    private = json.dumps({"url": record["url"], "pid": record["pid"], "key": secret}).encode("ascii")
    if os.name == "nt":
        private = _dpapi(private)
    value = {**record, "access": base64.b64encode(private).decode("ascii"),
             "proof": proof(secret, record["url"], record["pid"])}
    files = DirectoryFiles(data)
    files.write(ANNOUNCEMENT, json.dumps(value, indent=2).encode("utf-8"))
    return Path(data) / ANNOUNCEMENT


def _read_private(path):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name == "posix":
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16_384:
            raise ValueError("Invalid local record")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("The local record must be private to its owner")
        return stream.read(16_385)


def discover(data):
    """Return (origin, key) for the active process belonging to this OS user."""
    path = Path(data) / ANNOUNCEMENT
    try:
        record = json.loads(_read_private(path))
        private = base64.b64decode(record["access"], validate=True)
        if os.name == "nt":
            private = _dpapi(private, decrypt=True)
        bound = json.loads(private)
        url, pid, secret = record["url"], record["pid"], bound["key"]
        if not origin_port(url) or type(pid) is not int or pid <= 0 or not valid(secret):
            return None
        if (url, pid) != (bound["url"], bound["pid"]) or not hmac.compare_digest(record["proof"], proof(secret, url, pid)):
            return None
        from .storage_settings import _process_alive
        return (url, secret) if _process_alive(pid) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _agent_record(command):
    if not isinstance(command, str) or not 0 < len(command) <= 2_048 or any(not ch.isprintable() for ch in command):
        raise ValueError("Enter the command on a single line")
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
    return "agent-approvals/" + digest + ".approval"


def approve_agent(settings_dir, command):
    """Called only from the desktop confirmation or the user's terminal."""
    record = _agent_record(command)
    data = command.encode("utf-8")
    DirectoryFiles(settings_dir).write(record, _dpapi(data) if os.name == "nt" else data)


def approved_agent(settings_dir, command):
    """Return the approved command from the local configuration for execution."""
    try:
        data = _read_private(DirectoryFiles(settings_dir).path(_agent_record(command)))
        if os.name == "nt":
            data = _dpapi(data, decrypt=True)
        approved = data.decode("utf-8")
        return approved if approved == command else None
    except (OSError, ValueError):
        return None
