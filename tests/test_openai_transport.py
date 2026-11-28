"""Exercise the gateway over a real Unix socket with synthetic upstream replies."""

import io
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request

from textfold import openai_transport as transport


class PublicAddresses(unittest.TestCase):
    def test_nonpublic_answers_stop_before_connecting(self):
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "100.64.0.1", "224.0.0.1",
                        "::1", "fc00::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::"):
            with self.subTest(address=address):
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                local = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
                records = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443)),
                           (family, socket.SOCK_STREAM, 0, "", local)]
                with patch.object(socket, "getaddrinfo", return_value=records), patch.object(socket, "socket") as create:
                    with self.assertRaises(OSError):
                        transport._PublicHTTPSConnection("api.openai.com", timeout=5).connect()
                    create.assert_not_called()

    def test_connect_uses_the_checked_address_and_openai_tls_identity(self):
        address = ("8.8.8.8", 443)
        records = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", address)]
        context = Mock()
        with patch.object(socket, "getaddrinfo", return_value=records) as resolve, patch.object(socket, "socket") as create:
            transport._PublicHTTPSConnection("api.openai.com", timeout=5, context=context).connect()
        resolve.assert_called_once_with("api.openai.com", 443, type=socket.SOCK_STREAM)
        create.return_value.connect.assert_called_once_with(address)
        context.wrap_socket.assert_called_once_with(create.return_value, server_hostname="api.openai.com")


class Reply(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}

    def getheader(self, key):
        return self.headers.get(key)


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires Unix sockets")
class SocketGateway(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "gateway.sock"
        self.upstream = Mock()
        self.wire = self.upstream.return_value
        self.gateway = transport._Gateway(self.path, upstream=self.upstream)
        thread = threading.Thread(target=self.gateway.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.gateway.shutdown)

    def request(self, url, *, data=None, headers=None, method=None):
        return transport.open_request(self.path, Request(url, data, headers or {}, method=method), 3)

    def rejected(self, status, url, **kwargs):
        with self.assertRaises(HTTPError) as raised:
            self.request(url, **kwargs)
        with raised.exception as response:
            self.assertEqual(response.code, status)
            self.assertIsNone(response.headers.get("Location"))
            return response.read()

    def test_health_and_out_of_scope_operations_use_no_upstream_connection(self):
        with self.request(transport.HEALTH) as response:
            self.assertEqual(response.status, 204)
        for url in ("https://example.invalid/report", "https://auth.openai.com:443/.well-known/openid-configuration",
                    "https://auth.openai.com/keys", "https://auth.openai.com/revoke", "https://api.openai.com/v1/files",
                    transport.RESOURCE + "/models?extra=1", transport.RESOURCE + "/responses"):
            with self.subTest(url=url):
                self.rejected(403, url)
        self.rejected(501, transport.RESOURCE + "/models", method="CONNECT")
        self.rejected(400, transport.RESOURCE + "/models")
        self.upstream.assert_not_called()

    def test_signed_in_app_requests_match_the_approved_operations(self):
        key = transport.ISSUER + "/keys"
        revoke = transport.ISSUER + "/revoke"
        document = json.dumps({"issuer": transport.ISSUER, "jwks_uri": key, "revocation_endpoint": revoke}).encode()
        self.wire.getresponse.return_value = Reply(document)
        with self.request(transport.DISCOVERY) as response:
            self.assertEqual(response.read(), document)
        self.upstream.assert_called_with("auth.openai.com", timeout=20)
        self.wire.request.assert_called_with("GET", "/.well-known/openid-configuration", None, {"Accept": transport.JSON})
        self.wire.getresponse.return_value = Reply(b'{"keys":[]}')
        with self.request(key) as response:
            self.assertEqual(response.read(), b'{"keys":[]}')
        self.wire.request.assert_called_with("GET", "/keys", None, {"Accept": transport.JSON})

        sign_in = {"grant_type": "authorization_code", "client_id": "fixture", "code": "test-code",
                   "code_verifier": "test-only", "redirect_uri": "http://127.0.0.1:3017/oauth/chatgpt",
                   "resource": transport.RESOURCE}
        refresh = {"grant_type": "refresh_token", "client_id": "fixture", "refresh_token": "test-refresh",
                   "resource": transport.RESOURCE}
        disconnect = {"token": "test-refresh", "token_type_hint": "refresh_token", "client_id": "fixture"}
        for url, expected_path, fields in ((transport.TOKEN, "/api/accounts/oauth/token", sign_in),
                                           (transport.TOKEN, "/api/accounts/oauth/token", refresh),
                                           (revoke, "/revoke", disconnect)):
            with self.subTest(fields=fields):
                self.wire.getresponse.return_value = Reply(b"{}")
                body = urlencode(fields).encode()
                with self.request(url, data=body, headers={"Content-Type": transport.FORM}) as response:
                    self.assertEqual(response.read(), b"{}")
                self.wire.request.assert_called_with("POST", expected_path, body,
                                                     {"Accept": transport.JSON, "Content-Type": transport.FORM})

        headers = {"Authorization": "Bearer test-access", "Host": "example.invalid", "X-Forwarded-For": "example.invalid"}
        self.wire.getresponse.return_value = Reply(b'{"models":[]}')
        with self.request(transport.RESOURCE + "/models", headers=headers) as response:
            self.assertEqual(response.read(), b'{"models":[]}')
        self.upstream.assert_called_with("api.openai.com", timeout=20)
        self.wire.request.assert_called_with("GET", "/v1/models", None,
                                             {"Authorization": "Bearer test-access", "Accept": transport.JSON})

        prompt = {"model": "study", "instructions": "Reply using sample data",
                  "input": [{"role": "user", "content": "Sample café text"}], "store": False, "stream": True}
        events = b'data: {"type":"response.completed"}\n\n'
        self.wire.getresponse.return_value = Reply(events, headers={"Content-Type": transport.EVENTS, "Set-Cookie": "test=1"})
        headers["Content-Type"] = transport.JSON
        body = json.dumps(prompt).encode()
        with self.request(transport.RESOURCE + "/responses", data=body, headers=headers) as response:
            self.assertEqual(response.headers["Content-Type"], transport.EVENTS)
            self.assertIsNone(response.headers.get("Set-Cookie"))
            self.assertEqual(response.readline(), events[:-1])
        self.wire.request.assert_called_with("POST", "/v1/responses", body,
                                             {"Authorization": "Bearer test-access", "Accept": transport.EVENTS,
                                              "Content-Type": transport.JSON})
        self.upstream.reset_mock()
        self.rejected(400, transport.RESOURCE + "/responses",
                      data=json.dumps({**prompt, "tools": []}).encode(), headers=headers)
        self.rejected(400, transport.TOKEN, data=urlencode({**sign_in, "resource": "https://example.invalid"}).encode(),
                      headers={"Content-Type": transport.FORM})
        self.upstream.assert_not_called()

    def test_discovery_cannot_expand_the_destination_or_follow_redirects(self):
        for key in ("https://example.invalid/keys", transport.ISSUER + "/a/../keys"):
            with self.subTest(key=key):
                metadata = json.dumps({"issuer": transport.ISSUER, "jwks_uri": key}).encode()
                self.wire.getresponse.return_value = Reply(metadata)
                self.rejected(502, transport.DISCOVERY)
                self.rejected(403, key)
        self.upstream.reset_mock()
        self.wire.getresponse.return_value = Reply(b"", 307, {"Location": "https://example.invalid"})
        self.rejected(502, transport.DISCOVERY)
        self.upstream.assert_called_once_with("auth.openai.com", timeout=20)

        self.wire.getresponse.return_value = Reply(b'{"error":"test-limit"}', 429, {"X-Request-ID": "fixture"})
        with self.assertRaises(HTTPError) as raised:
            self.request(transport.RESOURCE + "/models", headers={"Authorization": "Bearer test-access"})
        with raised.exception as error:
            self.assertEqual(error.code, 429)
            self.assertEqual(error.headers["X-Request-ID"], "fixture")
            self.assertEqual(error.read(), b'{"error":"test-limit"}')
