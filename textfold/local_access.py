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
import time

from ._storage_layout import DirectoryFiles

ANNOUNCEMENT = "server.json"
_KEY = re.compile(r"[a-f0-9]{64}")
_URL = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})")
FOLDER_APPROVALS = {name: ".folder-approvals/" + name for name in ("library-copy", "library-existing", "engine-install")}


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


def browser_password(secret):
    """Derive a browser-only credential from this launch's automation key."""
    return hmac.new(secret.encode("ascii"), b"TextFold browser password", hashlib.sha256).hexdigest()


def show_browser_password(secret):
    """Deliver the requested credential to the operating system console."""
    console = os.open("CONOUT$" if os.name == "nt" else "/dev/tty", os.O_WRONLY)
    try:
        if not os.isatty(console):
            raise OSError("An interactive console is required")
        os.write(console, (browser_password(secret) + "\n").encode("ascii"))
    finally:
        os.close(console)


def credentials(header, secret):
    """Check each client against its own credential."""
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
    if not valid(value):
        return None
    expected = browser_password(secret) if scheme == "basic" else secret
    return scheme if hmac.compare_digest(value, expected) else None


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


def _read_private(path, limit=16_384):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name == "posix":
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid local record")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("The local record must be private to its owner")
        return stream.read(limit + 1)


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


def _folder_proof(secret, action, value):
    signed = b"TextFold folder approval\0" + action.encode("ascii") + b"\0" + value
    return hmac.new(secret.encode("ascii"), signed, hashlib.sha256).hexdigest().encode("ascii")


def approve_folder(runtime, action, requested, secret):
    """Record the canonical path approved from a native prompt or the user's terminal."""
    from .storage_settings import selected_folder

    if not 0 < len(requested) <= 32_768 or not requested.isprintable():
        raise ValueError("Enter a folder path on one line.")
    chosen = selected_folder(requested)
    value = json.dumps([requested.strip(), str(chosen), int(time.time()) + 300]).encode("ascii")
    seal = _folder_proof(secret, action, value) + b"\n" + value
    DirectoryFiles(runtime).write(FOLDER_APPROVALS[action], _dpapi(seal) if os.name == "nt" else seal)


def approved_folder(runtime, action, requested, secret):
    """Read a recent approval for this operation and server launch."""
    from .storage_settings import Refused

    if not isinstance(requested, str) or not requested.strip():
        raise Refused("path", "Enter an absolute folder path")
    try:
        data = _read_private(DirectoryFiles(runtime).path(FOLDER_APPROVALS[action]), limit=1_000_000)
        if os.name == "nt":
            data = _dpapi(data, decrypt=True)
        signature, _, value = data.partition(b"\n")
        if hmac.compare_digest(signature, _folder_proof(secret, action, value)):
            original, chosen, expires = json.loads(value)
            if original == requested.strip() and expires > time.time():
                return Path(chosen)
    except (OSError, ValueError, TypeError):
        pass
    raise Refused("folder_approval",
                  f"Approve this folder in the desktop app or run 'python -m textfold approve-local-folder {action}' "
                  "in a local terminal, enter the same path, and submit it again.")
