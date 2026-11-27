"""The local OAuth and inference entry points, against a synthetic OpenAI service."""
import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from textfold import chatgpt, models, server, store


class ChatGPTPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.signing_key.public_key()))
        cls.jwk.update(kid="fixture", alg="RS256", use="sig")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.connection = chatgpt.Connection()
        self.key = "a" * 64
        self.calls = []
        self.subject, self.scope, self.limit = "fixture-user", chatgpt.SCOPES, False
        self.wrong_nonce = False
        for patcher in (
            patch.dict(os.environ), patch.object(chatgpt, "connection", self.connection),
            patch.object(chatgpt, "_open", self.openai), patch.object(store, "HOME", self.home),
            patch.object(models, "SETTINGS", self.home / "settings.json"), patch.object(models, "OLD_HOMES", ()),
            patch.object(models.agents, "models", return_value=[]),
            patch.object(models.providers, "env_providers", return_value=[]),
            patch.object(models.providers, "ollama_models", return_value=[]),
            patch.object(models.bridges, "bridge_provider", return_value=None),
            patch.object(server.engine, "status", return_value={}),
            patch.object(server.store, "storage_status", return_value={}),
            patch.object(server.webbrowser, "open"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("TEXTFOLD_NO_MODELS", None)
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(None))
        self.port = httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 1)
        self.addCleanup(httpd.shutdown)

    def openai(self, url, *, form=None, body=None, token=None, timeout=20):
        self.calls.append((url, form or body, token))
        if url == chatgpt.DISCOVERY:
            return io.BytesIO(json.dumps({"issuer": chatgpt.ISSUER, "id_token_signing_alg_values_supported": ["RS256"],
                                         "jwks_uri": chatgpt.ISSUER + "/keys", "revocation_endpoint": chatgpt.ISSUER + "/revoke"}).encode())
        if url == chatgpt.ISSUER + "/keys":
            return io.BytesIO(json.dumps({"keys": [self.jwk]}).encode())
        if url == chatgpt.TOKEN:
            reply = {"access_token": "access-fixture", "refresh_token": "refresh-fixture",
                     "scope": self.scope, "token_type": "Bearer", "expires_in": 3600}
            if form["grant_type"] == "refresh_token":
                reply.update(access_token="new-access-fixture", refresh_token="new-refresh-fixture")
                del reply["scope"]
            else:
                nonce = self.fields["nonce"][0] if not self.wrong_nonce else "another-nonce"
                reply["id_token"] = jwt.encode({"iss": chatgpt.ISSUER, "aud": "oaiapp_fixture",
                                                "sub": self.subject, "email": "fixture@example.test", "nonce": nonce,
                                                "iat": int(time.time()), "exp": int(time.time()) + 600},
                                               self.signing_key, algorithm="RS256", headers={"kid": "fixture"})
            return io.BytesIO(json.dumps(reply).encode())
        if url == chatgpt.RESOURCE + "/models":
            return io.BytesIO(b'{"models":[{"visibility":"list","slug":"study","display_name":"Study"},'
                              b'{"visibility":"hidden","slug":"hidden"}]}')
        if url == chatgpt.RESOURCE + "/responses":
            if self.limit:
                return io.BytesIO(b'data: {"type":"response.failed","response":{"error":'
                                  b'{"code":"subscription_sharing_usage_limit_exceeded"}}}\n\n')
            return io.BytesIO(b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",\n'
                              b'data: "delta":"{\\"ok\\":true}"}\n\ndata: {"type":"response.completed"}\n\n')
        if url == chatgpt.ISSUER + "/revoke":
            return io.BytesIO()
        self.fail(f"Unexpected service URL {url}")

    def request(self, path, body=None, key=None, origin=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            headers = {chatgpt.READER_HEADER: key} if key else {}
            if body is not None:
                headers.update(Origin=origin or self.origin, **{"Content-Type": "application/json"})
            client.request("GET" if body is None else "POST", path, None if body is None else json.dumps(body), headers)
            response = client.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else {}
        finally:
            client.close()

    def start(self, **body):
        status, data = self.request("/api/chatgpt/start", body, self.key)
        self.assertEqual(status, 200, data)
        url = urlsplit(data["url"])
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", chatgpt.AUTH)
        self.fields = parse_qs(url.query)
        self.assertEqual(self.fields["redirect_uri"], [self.origin + chatgpt.CALLBACK])
        self.assertEqual(self.fields["resource"], [chatgpt.RESOURCE])
        return self.fields

    def callback(self, **changes):
        args = {"state": self.fields["state"][0], "code": "fixture-code", "client_id": "oaiapp_fixture",
                "scope": chatgpt.SCOPES}
        return self.request(chatgpt.CALLBACK + "?" + urlencode({**args, **changes}))[0]

    def status(self):
        return self.request("/api/chatgpt/status", key=self.key)[1]

    def choose_model(self):
        data = self.request("/api/models", key=self.key)[1]
        row, = data["models"]
        self.assertEqual(row["id"], "chatgpt-plan/study")
        self.assertIsNone(data["selected"])
        saved = {"model": row["id"], "target": row["target"]}
        self.assertEqual(self.request("/api/models/select", saved, self.key)[1]["selected"], row["id"])
        return saved

    def test_identity_only_then_plan_with_explicit_model_selection(self):
        self.assertEqual(self.request("/api/chatgpt/start", {}, self.key, "https://other.example")[0], 403)
        self.assertEqual(self.request("/api/chatgpt/start", {})[0], 403)
        self.scope = "openid profile email"
        fields = self.start()
        self.assertEqual(fields["client_id"], ["dynamic_agent_client"])
        self.assertEqual(fields["agent_name_hint"], ["TextFold"])
        self.assertEqual(self.callback(state="different"), 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.callback(), 303)
        self.assertEqual(self.callback(), 400)
        self.assertTrue(self.status()["connected"])
        self.assertFalse(self.status()["sharing"])
        self.assertNotIn(chatgpt.RESOURCE + "/models", [url for url, *_ in self.calls])
        token_form = self.calls[0][1]
        verifier_digest = hashlib.sha256(token_form["code_verifier"].encode()).digest()
        self.assertEqual(fields["code_challenge"], [base64.urlsafe_b64encode(verifier_digest).decode().rstrip("=")])
        self.assertEqual(token_form["redirect_uri"], fields["redirect_uri"][0])
        self.scope = chatgpt.SCOPES
        fields = self.start(consent=True)
        self.assertEqual(fields["client_id"], ["oaiapp_fixture"])
        self.assertEqual(fields["prompt"], ["consent"])
        self.assertEqual(self.callback(), 303)
        saved = self.choose_model()
        self.assertEqual(self.request("/api/models/select", saved, "b" * 64)[0], 409)
        self.assertFalse(self.request("/api/settings/test", {}, "b" * 64)[1]["ok"])
        self.assertTrue(self.request("/api/settings/test", {}, self.key)[1]["ok"])
        url, body, token = self.calls[-1]
        self.assertEqual((url, body["model"], body["store"], body["stream"], token),
                         (chatgpt.RESOURCE + "/responses", "study", False, True, "access-fixture"))
        self.assertEqual(body["input"][0]["role"], "user")
        self.assertEqual(set(body), {"model", "instructions", "input", "store", "stream"})
        registration = json.loads((self.home / chatgpt.REGISTRATION).read_text())
        self.assertEqual(set(registration), {"host_id", "client", "subject"})
        self.assertNotIn("fixture@example.test", json.dumps(self.request("/api/models", key="b" * 64)[1]))

    def test_bad_nonce_and_returning_account_mismatch(self):
        self.wrong_nonce = True
        self.start()
        self.assertEqual(self.callback(), 303)
        self.assertFalse(self.status()["connected"])
        self.wrong_nonce = False
        self.start()
        self.callback()
        self.assertTrue(self.status()["connected"])
        self.request("/api/chatgpt/disconnect", {}, self.key)
        self.subject = "other-user"
        self.start()
        self.callback()
        self.assertFalse(self.status()["connected"])
        self.assertIn("different account", self.status()["error"])

    def test_queued_jobs_limits_refresh_rotation_and_sign_out(self):
        self.start()
        self.callback()
        with chatgpt.reader(self.key):
            before_selection = models.job_context()
        self.choose_model()
        with self.assertRaisesRegex(RuntimeError, "choose a model"):
            before_selection.run(models.resolve)
        with chatgpt.reader(self.key):
            selected = models.job_context()
        self.assertEqual(selected.run(models.resolve)["model"], "study")

        self.connection.account["credentials"]["expires"] = 0
        self.limit = True
        failed = self.request("/api/settings/test", {}, self.key)[1]
        self.assertIn("usage limit reached", failed["error"])
        self.assertTrue(self.status()["paused"])
        sent = len(self.calls)
        self.assertFalse(self.request("/api/settings/test", {}, self.key)[1]["ok"])
        self.assertEqual(len(self.calls), sent)
        self.assertEqual(self.request("/api/chatgpt/disconnect", {}, self.key)[0], 200)
        self.assertEqual(self.calls[-1][0], chatgpt.ISSUER + "/revoke")
        self.assertEqual(self.calls[-1][1]["token"], "new-refresh-fixture")
        self.assertFalse(self.status()["connected"])
        self.start()
        self.callback()
        with self.assertRaisesRegex(RuntimeError, "choose a model"):
            selected.run(models.resolve)

    def test_a_partial_response_cannot_be_used(self):
        with self.assertRaisesRegex(chatgpt.SignInError, "ended before completion"):
            chatgpt._response_text(io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'))


if __name__ == "__main__":
    unittest.main()
