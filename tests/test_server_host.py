"""DNS rebinding: a web page that points its own domain at 127.0.0.1 is, to the browser, the same origin as its requests
to TextFold's server, and could read the library (its documents, their PDFs, the notes). Only the name the requests
are addressed to tells them apart. The server accepts loopback hosts and a single explicitly configured Tailscale
Serve hostname. On a server of its own (its own HOME and data folder, no model).

Run from the repo root:
    python3 -m unittest discover tests
"""
from _local_test import AUTH, KEY, urlopen
import base64
import http.client
import json
import os
import site
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from textfold import bridges, local_access, server

ROOT = Path(__file__).resolve().parent.parent
TAILSCALE_HOST = "textfold.example.ts.net"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Names(unittest.TestCase):
    def test_this_computer_under_any_port(self):
        for host in ("localhost:31917", "127.0.0.1:3017", "[::1]:3017", "LOCALHOST:31917", "localhost", "127.0.0.1"):
            self.assertTrue(server.addressed_here(host), host)

    def test_any_other_name(self):
        for host in ("attacker.example:31917", "attacker.example", "127.0.0.1.attacker.example:3017", "localhost.attacker.example",
                     "localhost:31917@attacker.example", "[::1].attacker.example", "127.0.0.2:3017", "0.0.0.0:3017",
                     "localhost:abc", "", None):
            self.assertFalse(server.addressed_here(host), host)

    def test_configured_tailscale_host_must_be_an_exact_hostname(self):
        for host in (f"https://{TAILSCALE_HOST}", "*.example.ts.net", f"{TAILSCALE_HOST}:443", "example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                server.make_handler(None, KEY, tailscale_host=host)


class ProviderSettings(unittest.TestCase):
    def test_reuse_key_only_for_its_provider_origin(self):
        previous = {"base": "https://models.example/v1", "api_key": "saved-test-value"}
        self.assertEqual(server.provider_secret({}, "https://models.example:443/v2", previous), previous["api_key"])
        for changed in ("http://models.example/v1", "https://models.example:8443", "https://other.example"):
            self.assertEqual(server.provider_secret({}, changed, previous), "")
            self.assertEqual(server.provider_secret({"api_key": "replacement"}, changed, previous), "replacement")

    def test_openrouter_key_requires_original_provider_and_current_authorization(self):
        with patch.object(bridges, "_OR", {"minted": "test-value", "at": time.monotonic()}):
            with self.assertRaises(ValueError):
                server.provider_secret({"minted": True}, "https://other.example", {})
            self.assertEqual(server.provider_secret({"minted": True}, "https://openrouter.ai/api/v1", {}), "test-value")
            bridges.clear_minted("outdated")
            self.assertEqual(bridges.minted(), "test-value")
            bridges.clear_minted("test-value")
            with self.assertRaises(ValueError):
                server.provider_secret({"minted": True}, "https://openrouter.ai/api/v1", {})

    def test_pending_authorization_expires(self):
        with patch.object(bridges, "_OR", {"minted": "unused", "at": time.monotonic() - 3600}):
            self.assertIsNone(bridges.minted())

    def test_oauth_can_return_to_a_reader_path(self):
        self.assertEqual(server.oauth_return_path("/p/example?lang=en"), "/p/example?lang=en")
        self.assertEqual(server.oauth_return_path("https://other.example/"), "/")
        self.assertEqual(server.oauth_return_path("//other.example/"), "/")


class Rebinding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        home = Path(cls.tmp.name) / "home"
        home.mkdir()
        cls.port = free_port()
        env = {**os.environ, "TEXTFOLD_LOCAL_ACCESS": KEY, "HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(home / "AppData" / "Local"),
               "TEXTFOLD_DATA": str(Path(cls.tmp.name) / "data"), "TEXTFOLD_NO_MODELS": "1", "PYTHONUSERBASE": site.getuserbase(),
               "TEXTFOLD_TAILSCALE_HOST": TAILSCALE_HOST}
        cls.proc = subprocess.Popen([sys.executable, "-m", "textfold", "serve", "--port", str(cls.port)], cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urlopen(f"http://127.0.0.1:{cls.port}/api/papers", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}/api/import", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"name": "notes.md", "text": "# A private draft\n\nWhat only I should read.\n"}).encode())
        cls.pid = json.loads(urlopen(req, timeout=30).read())["paper"]

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.tmp.cleanup()

    def ask(self, method, path, host, body=None, origin=None, *, headers=None, authorization=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.putrequest(method, path, skip_host=True)
        if authorization is None:
            authorization = AUTH["Authorization"]
        if authorization:
            c.putheader("Authorization", authorization)
        if host is not None:
            c.putheader("Host", host)
        if origin:
            c.putheader("Origin", origin)
        for name, value in (headers or {}).items():
            c.putheader(name, value)
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    def test_a_page_under_another_name_reads_nothing(self):
        bad = f"attacker.example:{self.port}"
        for path in ("/", "/api/papers", f"/p/{self.pid}", f"/api/notes/{self.pid}", f"/api/pdf/{self.pid}", "/api/folders",
                     "/api/models", "/api/jobs", "/api/zotero", "/static/app.js", "/oauth/openrouter?code=x"):
            status, data = self.ask("GET", path, bad)
            self.assertEqual(status, 421, path)
            self.assertNotIn(b"private draft", data, path)

    def test_nor_writes(self):
        status, _ = self.ask("POST", "/api/folders", f"attacker.example:{self.port}", {"op": "new", "name": "x"},
                             origin=f"http://attacker.example:{self.port}")
        self.assertEqual(status, 421)
        status, _ = self.ask("POST", "/api/openrouter/start", f"attacker.example:{self.port}", {"back": "/"})
        self.assertEqual(status, 421)   # (its callback would carry the other name)
        self.assertEqual(self.ask("GET", "/api/folders", f"127.0.0.1:{self.port}")[1], b'{"folders": [], "of": {}}')

    def test_browser_posts_require_exact_reader_origin(self):
        host = f"localhost:{self.port}"
        path = "/api/unknown"
        different_port = 32000 if self.port != 32000 else 32001
        self.assertEqual(self.ask("POST", path, host, {}, f"http://localhost:{different_port}")[0], 403)
        self.assertEqual(self.ask("POST", path, host, {}, f"https://{host}")[0], 403)
        for trusted in (None, f"http://{host}"):
            self.assertEqual(self.ask("POST", path, host, {}, trusted)[0], 404)

    def test_get_rejects_browser_requests_from_another_origin(self):
        host = f"localhost:{self.port}"
        url = "/api/papers"
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                        {"Referer": "https://external.example/"}, {"Origin": "null"}):
            self.assertEqual(self.ask("GET", url, host, headers=headers)[0], 403)
        for headers in ({"Sec-Fetch-Site": "none"}, {"Sec-Fetch-Site": "same-origin", "Referer": f"http://{host}/"}):
            self.assertEqual(self.ask("GET", url, host, headers=headers)[0], 200)

    def test_this_computer_reads_as_before(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"):
            status, data = self.ask("GET", f"/p/{self.pid}", host)
            self.assertEqual(status, 200, host)
            self.assertIn("private draft".encode(), data)
        self.assertEqual(self.ask("GET", "/api/papers", None)[0], 421)   # (no name at all: not a browser's)

    def test_private_https_browser_origin_uses_the_session_password(self):
        password = local_access.browser_password(KEY)
        browser = "Basic " + base64.b64encode(f"textfold:{password}".encode()).decode()
        here = f"https://{TAILSCALE_HOST}"
        for credential in ("", AUTH["Authorization"]):
            self.assertEqual(self.ask("GET", "/api/papers", TAILSCALE_HOST, authorization=credential)[0], 401)
        for host in (TAILSCALE_HOST.upper(), f"{TAILSCALE_HOST}:443"):
            self.assertEqual(self.ask("GET", "/api/papers", host, authorization=browser)[0], 200)
        for host in ("other.example.ts.net", f"{TAILSCALE_HOST}:3017", f"{TAILSCALE_HOST}.other.example"):
            self.assertEqual(self.ask("GET", "/api/papers", host, authorization=browser,
                                      headers={"X-Forwarded-Host": TAILSCALE_HOST, "X-Forwarded-Proto": "https"})[0], 421)

        doc = {"name": "private-host.md", "text": "# Tailnet reader\n\nA synthetic sample."}
        for origin in (None, f"http://{TAILSCALE_HOST}", "https://other.example.ts.net"):
            self.assertEqual(self.ask("POST", "/api/import", TAILSCALE_HOST, doc, origin, authorization=browser)[0], 403)
        status, data = self.ask("POST", "/api/import", TAILSCALE_HOST, doc, here, authorization=browser)
        self.assertEqual(status, 200)
        path = "/p/" + json.loads(data)["paper"]
        self.assertIn(b"Tailnet reader", self.ask("GET", path, TAILSCALE_HOST, authorization=browser)[1])

    def test_private_host_does_not_use_local_oauth_exceptions(self):
        self.assertEqual(self.ask("GET", "/oauth/chatgpt/done", TAILSCALE_HOST, authorization="")[0], 401)
        self.assertEqual(self.ask("GET", "/oauth/openrouter/" + KEY, TAILSCALE_HOST, authorization="")[0], 401)

    def test_electron_can_open_a_top_level_reader_from_its_splash(self):
        host = f"127.0.0.1:{self.port}"
        headers = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        for path in ("/", f"/p/{self.pid}"):
            self.assertEqual(self.ask("GET", path, host, headers=headers)[0], 200)
        self.assertEqual(self.ask("GET", "/api/papers", host, headers=headers)[0], 403)
        frame = {**headers, "Sec-Fetch-Dest": "iframe"}
        self.assertEqual(self.ask("GET", "/", host, headers=frame)[0], 403)
        password = local_access.browser_password(KEY)
        basic = "Basic " + base64.b64encode(f"textfold:{password}".encode()).decode()
        self.assertEqual(self.ask("GET", "/", host, headers=headers, authorization=basic)[0], 403)


if __name__ == "__main__":
    unittest.main()
