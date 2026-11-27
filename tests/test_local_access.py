"""Normal local account entry points with synthetic credentials and documents."""
import base64
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import mock_open, patch

from textfold import __main__ as cli, local_access, server


class AccessRecord(unittest.TestCase):
    def test_local_access_sends_the_password_only_to_the_console(self):
        secret = local_access.key()
        terminal = mock_open()
        terminal.return_value.isatty.return_value = True
        with patch.object(cli.sys, "argv", ["textfold", "local-access"]), \
                patch.object(cli.sys, "stdout") as stdout, patch.object(cli.sys, "stderr") as stderr, \
                patch.object(local_access, "discover", return_value=("http://127.0.0.1:3017", secret)), \
                patch.object(local_access, "open", terminal, create=True):
            stdout.isatty.return_value = True
            cli.main()
        terminal.assert_called_once_with("CONOUT$" if os.name == "nt" else "/dev/tty", "w", encoding="ascii")
        terminal.return_value.write.assert_called_once_with(local_access.browser_password(secret) + "\n")
        stdout.write.assert_not_called()
        stderr.write.assert_not_called()

    def test_record_is_bound_to_this_process_and_exact_loopback_origin(self):
        secret = local_access.key()
        url = "http://127.0.0.1:3017"
        with tempfile.TemporaryDirectory() as tmp:
            path = local_access.publish(tmp, {"url": url, "pid": os.getpid()}, secret)
            self.assertEqual(local_access.discover(tmp), (url, secret))
            if os.name == "posix":
                path.chmod(0o644)
                self.assertIsNone(local_access.discover(tmp))
                path.chmod(0o600)
            else:
                record = json.loads(path.read_bytes())
                self.assertNotIn(secret.encode(), base64.b64decode(record["access"]))
            record = json.loads(path.read_bytes())
            record["url"] = "http://127.0.0.1:3018"
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertIsNone(local_access.discover(tmp))


class LocalReader(unittest.TestCase):
    def setUp(self):
        self.key = local_access.key()
        private = tempfile.TemporaryDirectory()
        self.addCleanup(private.cleanup)
        self.settings = Path(private.name)
        scoped_settings = patch.object(server.models, "SETTINGS", self.settings / "settings.json")
        scoped_settings.start()
        self.addCleanup(scoped_settings.stop)
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(object(), self.key))
        self.port = httpd.server_port
        self.addCleanup(httpd.server_close)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True).start()
        self.addCleanup(httpd.shutdown)

    def request(self, path, *, token=None, method="GET", origin=None):
        with_client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Authorization": token} if token else {}
        body = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            body = b'{"command":"fixture"}'
        if origin:
            headers["Origin"] = origin
        try:
            with_client.request(method, path, body=body, headers=headers)
            response = with_client.getresponse()
            response.read()
            return response.status, response.getheader("WWW-Authenticate")
        finally:
            with_client.close()

    def test_private_pages_and_privileged_actions_require_local_credential(self):
        for path in ("/", "/api/papers", "/api/models", "/api/storage", "/api/bridge"):
            status, challenge = self.request(path)
            self.assertEqual(status, 401)
            self.assertTrue(challenge.startswith("Basic "))
        with patch.object(server.agents, "probe_custom", return_value={"version": "fixture"}) as probe:
            path = "/api/agents/probe"
            self.assertEqual(self.request(path, method="POST")[0], 401)
            self.assertEqual(self.request(path, method="POST", token="Bearer " + local_access.key())[0], 401)
            self.assertEqual(self.request(path, method="POST", token="Bearer " + self.key)[0], 403)
            probe.assert_not_called()
            local_access.approve_agent(self.settings, "fixture")
            self.assertEqual(self.request(path, method="POST", token="Bearer " + self.key)[0], 200)
            probe.assert_called_once_with("fixture", None)

    def test_basic_auth_is_supported_and_browser_writes_need_matching_origin(self):
        password = local_access.browser_password(self.key)
        basic = "Basic " + base64.b64encode(f"textfold:{password}".encode()).decode()
        self.assertEqual(self.request("/api/langs", token=basic)[0], 200)
        path = "/api/agents/probe"
        local_access.approve_agent(self.settings, "fixture")
        with patch.object(server.agents, "probe_custom", return_value={}) as probe:
            self.assertEqual(self.request(path, method="POST", token=basic)[0], 403)
            self.assertEqual(self.request(path, method="POST", token=basic, origin="http://127.0.0.1:1")[0], 403)
            probe.assert_not_called()
            origin = f"http://127.0.0.1:{self.port}"
            self.assertEqual(self.request(path, method="POST", token=basic, origin=origin)[0], 200)

    def test_browser_and_automation_credentials_are_distinct(self):
        password = local_access.browser_password(self.key)
        self.assertIsNone(local_access.credentials("Bearer " + password, self.key))
        raw_as_basic = base64.b64encode(f"textfold:{self.key}".encode()).decode()
        self.assertIsNone(local_access.credentials("Basic " + raw_as_basic, self.key))

    def test_only_the_oauth_return_pages_allow_an_external_browser(self):
        self.assertEqual(self.request("/oauth/chatgpt/done")[0], 200)
        self.assertEqual(self.request("/oauth/openrouter/" + local_access.key())[0], 400)
        self.assertEqual(self.request("/api/openrouter/minted")[0], 401)
