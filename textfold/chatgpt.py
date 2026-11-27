"""Direct Sign in with ChatGPT for the local TextFold app."""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
from http.client import HTTPException
import importlib.util
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import store
from ._storage_layout import DirectoryFiles

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTH = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
CALLBACK = "/oauth/chatgpt"
PLAN = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN
MODEL_PREFIX = "chatgpt-plan/"
READER_HEADER = "X-TextFold-ChatGPT"
REGISTRATION = "chatgpt-registration.json"
PENDING_TTL = 600
_READER = ContextVar("textfold-chatgpt-reader", default=None)
job_choice = ContextVar("textfold-chatgpt-job-choice", default=None)
_INVALID_REFRESH = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
}
_LIMIT = "subscription_sharing_usage_limit_exceeded"
_ID_KEY_TYPES = {"RS256": "RSA", "PS256": "RSA", "ES256": "EC", "EdDSA": "OKP"}
_MESSAGES = {
    _LIMIT: "ChatGPT plan usage limit reached. Review your plan or app limit in ChatGPT settings.",
    "subscription_sharing_usage_unavailable": "ChatGPT plan usage is temporarily unavailable. Try again later.",
    "subscription_sharing_user_not_eligible": "This account or workspace cannot use its ChatGPT plan here.",
    "subscription_sharing_invalid_user": "ChatGPT did not accept this connection. Sign in again.",
    "chatpass_v2_scope_not_authorized": "This ChatGPT connection does not have permission for plan usage.",
    "chatpass_v2_invalid_authorization_context": "This ChatGPT connection does not have permission for plan usage.",
    "subscription_sharing_unsupported_capability": "This model does not support the request through ChatGPT plan usage.",
}


class SignInError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def available():
    return not os.environ.get("TEXTFOLD_NO_MODELS") and all(
        importlib.util.find_spec(module) is not None for module in ("jwt", "cryptography")
    )


def reader_key():
    return _READER.get()


@contextmanager
def reader(key):
    value = key if isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key) else None
    entered = _READER.set(value)
    try:
        yield
    finally:
        _READER.reset(entered)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _auth_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "auth.openai.com" or parts.fragment or parts.query:
        raise SignInError("OpenAI's sign-in metadata could not be verified.")
    return url


def _failure(body, status, request_id=None):
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else error
    if not isinstance(code, str):
        code = None
    message = _MESSAGES.get(code)
    if not message:
        if status == 401:
            message = "ChatGPT did not accept this connection. Sign in again."
        elif status == 403:
            message = "ChatGPT blocked this request for the selected account, workspace, or policy."
        else:
            message = f"ChatGPT request failed (HTTP {status})."
    if request_id and re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", request_id):
        message += f" Request ID {request_id}."
    return SignInError(message, code)


def _open(url, *, form=None, body=None, token=None, timeout=20):
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    if body is not None:
        headers.update({"Content-Type": "application/json", "Accept": "text/event-stream"})
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        with error:
            try:
                detail = json.loads(error.read(32_768))
            except (ValueError, OSError, HTTPException):
                detail = {}
        raise _failure(detail, error.code, error.headers.get("X-Request-ID") if error.headers else None) from None
    except (OSError, ValueError, HTTPException):
        raise SignInError("Could not reach OpenAI. Check your connection and try again.") from None


def _json_request(url, **kwargs):
    with _open(url, **kwargs) as response:
        try:
            value = json.loads(response.read(2_000_000))
        except (ValueError, OSError, HTTPException):
            raise SignInError("OpenAI returned an unreadable response.") from None
    if not isinstance(value, dict):
        raise SignInError("OpenAI returned an unexpected response.")
    return value


def _identity(token, client, nonce):
    import jwt

    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        key_type = _ID_KEY_TYPES.get(algorithm)
        if key_type is None or not isinstance(header.get("kid"), str):
            raise ValueError
        op = _json_request(DISCOVERY)
        if op.get("issuer") != ISSUER or algorithm not in op["id_token_signing_alg_values_supported"]:
            raise ValueError
        obj = _json_request(_auth_url(op["jwks_uri"]))
        key, = [k for k in obj["keys"] if k.get("kid") == header["kid"] and k.get("kty") == key_type
                and k.get("use", "sig") == "sig" and k.get("alg", algorithm) == algorithm]
        claims = jwt.decode(token, jwt.PyJWK.from_dict(key, algorithm=algorithm).key, algorithms=[algorithm],
                            issuer=ISSUER, audience=client, options={"require": ["exp", "iat", "sub", "nonce"]})
        if not claims["sub"] or claims.get("azp", client) != client or not isinstance(claims["nonce"], str):
            raise ValueError
        if not hmac.compare_digest(claims["nonce"].encode(), nonce.encode()):
            raise ValueError
    except (jwt.PyJWTError, ValueError, KeyError, TypeError, AttributeError):
        raise SignInError("OpenAI's sign-in response could not be verified. Try signing in again.") from None
    return claims


def _secret(data, field):
    value = data.get(field)
    if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,16384}", value):
        raise SignInError("OpenAI did not return valid sign-in credentials.")
    return value


def _credentials(reply, old=None):
    # On refresh an omitted scope leaves the existing authorization unchanged.
    scope = reply.get("scope", old["scope"] if old else "")
    lifetime = reply.get("expires_in")
    if not isinstance(scope, str) or not isinstance(lifetime, int) or not 0 < lifetime <= 86_400:
        raise SignInError("OpenAI returned an invalid token grant.")
    if str(reply.get("token_type", "")).lower() != "bearer":
        raise SignInError("OpenAI returned an unsupported token type.")
    renewable = "offline_access" in scope.split() and (PLAN in scope.split() or "refresh_token" in reply)
    return {"access": _secret(reply, "access_token"), "refresh": _secret(reply, "refresh_token") if renewable else None,
            "scope": scope, "expires": time.monotonic() + lifetime}


def _sharing(account):
    return account and {PLAN, "resource.invoke"} <= set(account["credentials"]["scope"].split())


class Connection:
    """One local reader session. Bearer tokens are held in memory for the life of the app."""

    def __init__(self):
        self.lock = threading.RLock()
        self.owner = None
        self.pending = None
        self.account = None
        self.error = ""
        self.paused = False

    def _owned(self):
        key = reader_key()
        return bool(key and self.owner and hmac.compare_digest(key, self.owner))

    def _registrations(self):
        files = DirectoryFiles(store.HOME)
        if files.exists(REGISTRATION):
            try:
                data = json.loads(files.read(REGISTRATION))
                host = data["host_id"]
                if not re.fullmatch(r"urn:uuid:[a-f0-9-]{36}", host):
                    raise ValueError
                return data
            except (ValueError, KeyError, TypeError):
                raise SignInError("TextFold could not read its local ChatGPT registration.") from None
        data = {"host_id": f"urn:uuid:{uuid.uuid4()}"}
        self._save_registration(data)
        return data

    def _save_registration(self, data):
        DirectoryFiles(store.HOME).write(REGISTRATION, json.dumps(data).encode())

    def _expire_pending(self):
        if self.pending and self.pending["until"] < time.monotonic():
            self.pending = None
            self.error = "The sign-in expired. Choose Continue with ChatGPT to try again."

    def start(self, port, *, consent=False, new_account=False):
        if not available() or not reader_key():
            raise SignInError("ChatGPT sign-in is unavailable in this browser session.")
        with self.lock:
            self._expire_pending()
            if (self.account or self.pending) and not self._owned():
                raise SignInError("Manage this ChatGPT connection from the original TextFold tab.")
            saved = self._registrations()
            if new_account and self.account:
                raise SignInError("Sign out of this connection before choosing another account.")
            client = None if new_account else saved.get("client")
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            redirect = f"http://127.0.0.1:{port}{CALLBACK}"
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            fields = {
                "client_id": client or "dynamic_agent_client", "response_type": "code", "redirect_uri": redirect,
                "ext_agent_host_id": saved["host_id"], "scope": SCOPES, "resource": RESOURCE, "state": state,
                "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
            }
            if client is None:
                fields["agent_name_hint"] = "TextFold"
            elif consent:
                fields["prompt"] = "consent"
            self.owner = reader_key()
            self.pending = {"state": state, "nonce": nonce, "verifier": verifier, "redirect": redirect,
                            "client": client, "subject": saved.get("subject") if client else None,
                            "new_account": new_account,
                            "host_id": saved["host_id"], "until": time.monotonic() + PENDING_TTL}
            self.error = ""
            return f"{AUTH}?{urllib.parse.urlencode(fields)}"

    def finish(self, params, port):
        with self.lock:
            self._expire_pending()
            pending = self.pending
            state = params.get("state", "")
            if (not pending or not hmac.compare_digest(state.encode(), pending["state"].encode())
                    or pending["redirect"] != f"http://127.0.0.1:{port}{CALLBACK}"):
                return False
            self.pending = None
            if params.get("error") or not params.get("code"):
                self.error = "ChatGPT sign-in was cancelled or declined."
                return True
            client = pending["client"] or params.get("client_id")
            if (not client or client == "dynamic_agent_client" or not re.fullmatch(r"[a-zA-Z0-9_.-]{6,256}", client)
                    or params.get("client_id", client) != client):
                self.error = "OpenAI did not return the expected ChatGPT registration."
                return True
            try:
                if pending["client"] is None and not pending["new_account"]:
                    self._save_registration({"host_id": pending["host_id"], "client": client})
                reply = _json_request(TOKEN, form={
                    "grant_type": "authorization_code", "client_id": client, "code": params["code"],
                    "code_verifier": pending["verifier"], "redirect_uri": pending["redirect"], "resource": RESOURCE,
                })
                claims = _identity(_secret(reply, "id_token"), client, pending["nonce"])
                if pending["subject"] is not None and pending["subject"] != claims["sub"]:
                    raise SignInError("OpenAI returned a different account. Choose another account explicitly to switch.")
                fresh = _credentials(reply)
                account = {"id": uuid.uuid4().hex, "client": client, "subject": claims["sub"], "credentials": fresh,
                           "label": str(claims.get("email") or claims.get("name") or "ChatGPT")[:160], "models": []}
                self._save_registration({"host_id": pending["host_id"], "client": client, "subject": claims["sub"]})
                self.account = account
                self.error = ""
                self.paused = False
                if _sharing(account):
                    try:
                        self._load_models(account)
                    except SignInError as error:
                        self._note_error(error)
            except SignInError as error:
                self.error = str(error)
            except OSError:
                self.error = "TextFold could not save its local ChatGPT registration."
            return True

    def status(self):
        with self.lock:
            self._expire_pending()
            enabled = available()
            try:
                registered = enabled and DirectoryFiles(store.HOME).exists(REGISTRATION)
            except (OSError, ValueError):
                registered = False
            basic = {"available": enabled, "registered": registered, "connected": False, "pending": False, "sharing": False}
            if not self._owned():
                return basic
            account = self.account
            registration = hashlib.sha256(f"{account['client']}\n{account['subject']}".encode()).hexdigest() if account else ""
            return {**basic, "connected": bool(account), "pending": bool(self.pending), "registration": registration,
                    "sharing": bool(_sharing(account)), "paused": self.paused, "label": account["label"] if account else "",
                    "error": self.error, "models": len(account["models"]) if account else 0}

    def clear(self):
        with self.lock:
            if not self._owned():
                raise SignInError("Manage this ChatGPT connection from the original TextFold tab.")
            account, self.account = self.account, None
            self.pending = None
            self.paused = False
            self.error = ""
            if not account or not account["credentials"]["refresh"]:
                return
            form = {"token": account["credentials"]["refresh"], "token_type_hint": "refresh_token", "client_id": account["client"]}
            try:
                doc = _json_request(DISCOVERY)
                if doc.get("issuer") != ISSUER:
                    raise SignInError("OpenAI's sign-in metadata could not be verified.")
                url = _auth_url(doc["revocation_endpoint"])
                for attempt in range(2):
                    try:
                        with _open(url, form=form):
                            return
                    except SignInError:
                        if attempt:
                            raise
                        time.sleep(0.5)
            except (KeyError, TypeError, ValueError, SignInError):
                self.error = "Signed out locally. Check your ChatGPT settings to disconnect the app."

    def provider(self):
        with self.lock:
            account = self.account
            if not self._owned() or not _sharing(account) or self.paused:
                return None
            choice = job_choice.get()
            listed = account["models"]
            if choice is not None:
                if not choice or choice[0] != account["id"]:
                    return None
                listed = [m for m in listed if m["id"] == choice[1]]
            return {"key": "chatgpt-plan", "source": "chatgpt-plan", "protocol": "chatgpt-plan", "name": "ChatGPT plan",
                    "base": RESOURCE, "api_key": "", "connection": account["id"],
                    "identity": (account["client"], account["subject"]), "models": listed}

    def _access(self, account):
        if self.paused:
            raise SignInError(_MESSAGES[_LIMIT], _LIMIT)
        if not _sharing(account):
            raise SignInError("Enable ChatGPT plan usage before choosing a model.")
        credentials = account["credentials"]
        if credentials["expires"] > time.monotonic() + 30:
            return credentials["access"]
        if not credentials["refresh"]:
            raise SignInError("The ChatGPT connection expired. Sign in again.")
        try:
            fresh = _json_request(TOKEN, form={"grant_type": "refresh_token", "client_id": account["client"],
                                                "refresh_token": credentials["refresh"], "resource": RESOURCE})
            account["credentials"] = _credentials(fresh, credentials)
        except SignInError as error:
            if error.code in _INVALID_REFRESH:
                self.account = None
                self.error = "The ChatGPT connection expired. Sign in again."
                raise SignInError(self.error) from None
            raise
        if not _sharing(account):
            raise SignInError("This ChatGPT connection no longer grants plan usage.")
        return account["credentials"]["access"]

    def _load_models(self, account):
        body = _json_request(RESOURCE + "/models", token=self._access(account))
        rows = body.get("models")
        if not isinstance(rows, list):
            raise SignInError("OpenAI did not return a model list for this ChatGPT connection.")
        account["models"] = [{"id": MODEL_PREFIX + m["slug"], "name": str(m.get("display_name") or m["slug"])[:160],
                              "slug": m["slug"]} for m in rows if isinstance(m, dict) and m.get("visibility") == "list"
                             and isinstance(m.get("slug"), str) and re.fullmatch(r"[\w.-]{1,128}", m["slug"])][:100]
        if not account["models"]:
            raise SignInError("OpenAI did not list any models for this ChatGPT connection.")

    def _note_error(self, error):
        self.error = str(error)
        if error.code == _LIMIT:
            self.paused = True

    def retry(self):
        with self.lock:
            if not self._owned() or not _sharing(self.account):
                raise SignInError("Connect ChatGPT and approve plan usage first.")
            self.paused = False
            self.error = ""
            try:
                self._load_models(self.account)
            except SignInError as error:
                self._note_error(error)
                raise

    def responses(self, connection, model, prompt, instructions, schema, timeout):
        with self.lock:
            account = self.account
            if not account or account["id"] != connection or not any(m["slug"] == model for m in account["models"]):
                raise SignInError("The selected ChatGPT connection is unavailable. Sign in and choose a model again.")
            token = self._access(account)
        request = {"model": model, "instructions": instructions + "\nReturn a JSON object matching: " + json.dumps(schema),
                   "input": [{"role": "user", "content": prompt}], "store": False, "stream": True}
        try:
            with _open(RESOURCE + "/responses", body=request, token=token, timeout=timeout) as response:
                return _response_text(response)
        except SignInError as error:
            with self.lock:
                if self.account is account:
                    self._note_error(error)
            raise


def _response_events(stream):
    lines, size = [], 0
    while raw := stream.readline(1_000_001):
        if len(raw) > 1_000_000:
            raise SignInError("The ChatGPT reply contained an oversized event.")
        line = raw.rstrip(b"\r\n")
        if not line and lines:
            yield json.loads(b"\n".join(lines))
            lines, size = [], 0
        elif line.startswith(b"data:"):
            size += len(line)
            if size > 1_000_000:
                raise SignInError("The ChatGPT reply contained an oversized event.")
            lines.append(line[5:].removeprefix(b" "))


def _response_text(stream):
    chunks = []
    size = 0
    try:
        for event in _response_events(stream):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                delta = event.get("delta", "")
                size += len(delta)
                if size > 2_000_000:
                    raise SignInError("The ChatGPT reply was too large.")
                chunks.append(delta)
            elif kind == "response.completed":
                return "".join(chunks)
            elif kind in ("response.failed", "error"):
                problem = event.get("response", {}).get("error") if kind == "response.failed" else event
                raise _failure({"error": problem}, 429 if isinstance(problem, dict) and problem.get("code") == _LIMIT else 502)
            elif kind == "response.incomplete":
                raise SignInError("ChatGPT did not complete the reply.")
    except (ValueError, TypeError, AttributeError, OSError, HTTPException):
        raise SignInError("The ChatGPT reply stream could not be read.") from None
    raise SignInError("The ChatGPT reply ended before completion.")


connection = Connection()
