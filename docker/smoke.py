"""Exercise the Compose launch, browser login and persisted library on Linux."""

import base64
import errno
import http.client
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import signal
import socket
import subprocess
import time
from contextlib import suppress

ROOT = Path(__file__).resolve().parents[1]
TAILSCALE_HOST = "textfold.example.ts.net"


def terminal_output(command, environment):
    # Exercise the same interactive command the reader uses. Keep its output
    # in the test process so the temporary password never appears in CI logs.
    child, terminal = pty.fork()
    if child == 0:
        os.execvpe(command[0], command, environment)
    captured = bytearray()
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if not select.select([terminal], [], [], 0.2)[0]:
                continue
            try:
                chunk = os.read(terminal, 4096)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            captured.extend(chunk)
        else:
            raise TimeoutError("The interactive local-access command timed out.")
    except BaseException:
        with suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
        raise
    finally:
        os.close(terminal)
        _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0, "The interactive local-access command failed."
    return captured


def request(port, path, *, password=None, data=None, origin=None, host=None, reader=None):
    headers = {"Host": host} if host else {}
    if password is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(b"textfold:" + password).decode("ascii")
    if reader is not None:
        headers["X-TextFold-ChatGPT"] = reader
    if data is not None:
        headers["Content-Type"] = "application/json"
        headers["Origin"] = origin or f"http://127.0.0.1:{port}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET" if data is None else "POST", path, data, headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def main():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    project = "textfold-smoke-" + secrets.token_hex(6)
    environment = {**os.environ, "TEXTFOLD_PORT": str(port), "TEXTFOLD_TAILSCALE_HOST": TAILSCALE_HOST}
    command = ["docker", "compose", "-p", project, "-f", "compose.yml"]

    def compose(*args, profile=None, check=True):
        prefix = command + (["-f", f"compose.{profile}.yml"] if profile else [])
        return subprocess.run(prefix + list(args), cwd=ROOT, env=environment, timeout=360, check=check)

    def browser_password():
        console = terminal_output(command + ["exec", "textfold", "python", "-m", "textfold", "local-access"], environment)
        candidates = re.findall(rb"(?m)^[a-f0-9]{64}\r?$", console)
        assert len(candidates) == 1, "The terminal did not show a browser password."
        return candidates[0].strip()

    os.chdir(ROOT)
    profile = None
    try:
        compose("up", "--build", "-d", "--wait", "--wait-timeout", "120")
        compose(
            "exec", "-T", "textfold", "python", "-c",
            "import os, socket; from pathlib import Path; "
            "assert os.getuid() == 10001; "
            "assert [name for _, name in socket.if_nameindex()] == ['lo']; "
            "assert os.statvfs('/opt/textfold').f_flag & os.ST_RDONLY; "
            "assert not Path('/var/run/docker.sock').exists()",
        )
        compose(
            "exec", "-T", "browser", "python", "-c",
            "import os; from pathlib import Path; "
            "assert os.getuid() == 10001; "
            "assert os.statvfs('/run/textfold-relay').f_flag & os.ST_RDONLY; "
            "assert not Path('/data').exists()",
        )
        assert request(port, "/api/papers")[0] == 401
        password = browser_password()
        sample = json.dumps({"name": "trial.md", "text": "# Docker smoke test\n\nA sample document."}).encode()
        assert request(port, "/api/import", password=password, data=sample, origin="https://example.test")[0] == 403
        status, body = request(port, "/api/import", password=password, data=sample)
        assert status == 200, status
        path = "/p/" + json.loads(body)["paper"]
        assert b"Docker smoke test" in request(port, path, password=password)[1]
        assert request(port, path, host=TAILSCALE_HOST)[0] == 401
        assert b"Docker smoke test" in request(port, path, password=password, host=TAILSCALE_HOST)[1]

        compose("down")
        compose("up", "-d", "--wait", "--wait-timeout", "120")
        updated = browser_password()
        assert updated != password, "The browser password did not rotate on restart."
        assert request(port, path, password=password)[0] == 401
        assert b"Docker smoke test" in request(port, path, password=updated)[1]
        print("Offline browser login, import, isolation and library persistence passed.")

        profile = "online"
        compose("up", "-d", "--wait", "--wait-timeout", "120", profile=profile)
        compose(
            "exec", "-T", "textfold", "python", "-c",
            "import os, socket; assert not os.getenv('TEXTFOLD_NO_MODELS'); "
            "assert any(name != 'lo' for _, name in socket.if_nameindex())",
            profile=profile,
        )
        assert b"Docker smoke test" in request(port, path, password=browser_password())[1]
        print("The online override started with the same library and local browser access.")

        profile = "chatgpt"
        compose("up", "--build", "-d", "--wait", "--wait-timeout", "120", profile=profile)
        compose("exec", "-T", "textfold", "python", "-c", """
import os
import socket
from urllib.error import HTTPError
from urllib.request import Request
from textfold import openai_transport

assert [name for _, name in socket.if_nameindex()] == ["lo"]
assert os.statvfs("/run/textfold-openai").f_flag & os.ST_RDONLY
try:
    openai_transport.open_request(os.environ[openai_transport.SOCKET_ENV], Request("https://example.invalid"), 2)
except HTTPError as error:
    assert error.code == 403, error.code
else:
    raise AssertionError("The OpenAI gateway accepted an unrelated destination")
""", profile=profile)
        compose(
            "exec", "-T", "openai", "python", "-c",
            "import os; from pathlib import Path; "
            "assert os.getuid() == 10001; "
            "assert os.statvfs('/opt/textfold-docker').f_flag & os.ST_RDONLY; "
            "assert not Path('/data').exists(); "
            "assert not Path('/var/run/docker.sock').exists()",
            profile=profile,
        )
        password = browser_password()
        start = "/api/chatgpt/start"
        reader = secrets.token_hex(32)
        status, body = request(port, start, password=password, data=b"{}", reader=reader)
        assert status == 200 and json.loads(body)["url"].startswith("https://auth.openai.com/"), status
        assert request(port, start, password=password, data=b"{}", reader=reader, host=TAILSCALE_HOST,
                       origin=f"https://{TAILSCALE_HOST}")[0] == 403
        assert b"Docker smoke test" in request(port, path, password=password, host=TAILSCALE_HOST)[1]
        print("The ChatGPT override retained the library and denied unrelated egress and remote OAuth starts.")
    except BaseException:
        compose("logs", "--tail", "25", profile=profile, check=False)
        raise
    finally:
        compose("down", "--volumes", "--remove-orphans", profile="chatgpt", check=False)


if __name__ == "__main__":
    main()
