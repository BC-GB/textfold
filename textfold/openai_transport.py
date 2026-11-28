"""Private Unix-socket transport for TextFold's direct ChatGPT connection."""

from contextlib import closing
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import stat
import sys
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN = ISSUER + "/api/accounts/oauth/token"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
CALLBACK = "/oauth/chatgpt"
SOCKET_ENV = "TEXTFOLD_OPENAI_SOCKET"
SOCKET_PATH = Path("/run/textfold-openai/gateway.sock")
HEALTH = "http://openai-gateway/health"

ROUTES = {
    ("GET", DISCOVERY): "discovery",
    ("POST", TOKEN): "token",
    ("GET", RESOURCE + "/models"): "models",
    ("POST", RESOURCE + "/responses"): "responses",
}
FORM = "application/x-www-form-urlencoded"
JSON = "application/json"
EVENTS = "text/event-stream"
METADATA_LIMIT = 2_000_000
MODEL_LIMIT = 16 * 1024 * 1024
FORM_LIMIT = 128 * 1024


class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("openai-gateway", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def open_request(path, request, timeout):
    """Return a readable response over the local socket, with urllib's error interface."""
    with closing(_UnixHTTPConnection(path, timeout)) as connection:
        connection.request(
            request.get_method(), request.full_url, request.data, dict(request.header_items()),
        )
        response = connection.getresponse()
    if not 200 <= response.status < 300:
        raise HTTPError(request.full_url, response.status, response.reason, response.headers, response)
    return response


def _public_address(address):
    ip = ipaddress.ip_address(address[0])
    if isinstance(ip, ipaddress.IPv6Address):
        # Current native global unicast only. Reject transition and translation addresses.
        if ip not in ipaddress.IPv6Network("2000::/3") or ip.sixtofour or ip.teredo or address[3]:
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


class _PublicHTTPSConnection(HTTPSConnection):
    def connect(self):
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not addresses or any(not _public_address(sockaddr) for _, _, _, _, sockaddr in addresses):
            raise OSError("OpenAI hostname did not resolve exclusively to public addresses")
        for family, kind, protocol, _, sockaddr in addresses:
            raw = socket.socket(family, kind, protocol)
            try:
                raw.settimeout(min(10, self.timeout))
                raw.connect(sockaddr)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                self.sock.settimeout(self.timeout)
                return
            except OSError:
                raw.close()
        raise OSError("No OpenAI address accepted a verified TLS connection")


def _metadata_routes(data):
    doc = json.loads(data)
    if not isinstance(doc, dict) or doc.get("issuer") != ISSUER:
        raise ValueError("Unexpected OpenAI discovery issuer")
    routes = {}
    for field, method, operation in (("jwks_uri", "GET", "keys"), ("revocation_endpoint", "POST", "revoke")):
        url = doc.get(field)
        if url is None and operation == "revoke":
            continue
        if not isinstance(url, str) or not url.startswith(ISSUER + "/"):
            raise ValueError("Unexpected OpenAI discovery endpoint")
        suffix = url[len(ISSUER):]
        if not re.fullmatch(r"/[a-zA-Z0-9._/-]{1,512}", suffix) or any(
            fragment in {"", ".", ".."} for fragment in suffix[1:].split("/")
        ):
            raise ValueError("Unexpected OpenAI discovery path")
        route = (method, url)
        if any(known_url == url for _, known_url in (*ROUTES, *routes)):
            raise ValueError("Repeated OpenAI discovery endpoint")
        routes[route] = operation
    return routes


def _oauth_body(data, operation):
    pairs = parse_qsl(data.decode("ascii"), strict_parsing=True, errors="strict", max_num_fields=6)
    form = dict(pairs)
    if len(form) != len(pairs) or not all(form.values()):
        raise ValueError("Invalid OAuth form")
    if operation == "revoke":
        if form.keys() != {"token", "token_type_hint", "client_id"} or form["token_type_hint"] != "refresh_token":
            raise ValueError("Invalid revocation form")
    else:
        common = {"grant_type", "client_id", "resource"}
        grant = form.get("grant_type")
        if grant == "authorization_code" and form.keys() == common | {"code", "code_verifier", "redirect_uri"}:
            if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}" + CALLBACK, form["redirect_uri"]):
                raise ValueError("Invalid OAuth callback")
        elif grant != "refresh_token" or form.keys() != common | {"refresh_token"}:
            raise ValueError("Invalid token grant")
        if form["resource"] != RESOURCE:
            raise ValueError("Invalid OAuth resource")
    return urlencode(form).encode("ascii")


def _model_body(data):
    value = json.loads(data)
    if (
        not isinstance(value, dict) or value.keys() != {"model", "instructions", "input", "store", "stream"}
        or value["store"] is not False or value["stream"] is not True
        or not isinstance(value["model"], str) or not isinstance(value["instructions"], str)
    ):
        raise ValueError("Invalid Responses request")
    messages = value["input"]
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("Expected one text input")
    message = messages[0]
    if (
        not isinstance(message, dict) or message.keys() != {"role", "content"}
        or message["role"] != "user" or not isinstance(message["content"], str)
    ):
        raise ValueError("Expected one user text input")
    return json.dumps(value).encode("ascii")


class _Handler(BaseHTTPRequestHandler):
    timeout = 20

    def log_message(self, *args):
        # Request and response data can contain credentials or private documents.
        pass

    def _body(self, content_type, limit):
        length, = self.headers.get_all("Content-Length", [])
        if (
            not re.fullmatch(r"[0-9]{1,8}", length) or not 0 < int(length) <= limit
            or self.headers.get_all("Content-Type", []) != [content_type]
        ):
            raise ValueError("Invalid request body headers")
        data = self.rfile.read(int(length))
        if len(data) != int(length):
            raise ValueError("Incomplete request body")
        return data

    def _request(self, operation):
        if "Transfer-Encoding" in self.headers:
            raise ValueError("Unexpected transfer encoding")
        headers = {"Accept": JSON}
        authorizations = self.headers.get_all("Authorization", [])
        if operation in {"models", "responses"}:
            if len(authorizations) != 1 or not re.fullmatch(r"Bearer [\x21-\x7e]{1,16384}", authorizations[0]):
                raise ValueError("Expected one bearer token")
            headers["Authorization"] = authorizations[0]
        elif authorizations:
            raise ValueError("Unexpected bearer token")
        if operation == "responses":
            headers.update({"Accept": EVENTS, "Content-Type": JSON})
            return _model_body(self._body(JSON, MODEL_LIMIT)), headers
        if self.command == "POST":
            headers["Content-Type"] = FORM
            return _oauth_body(self._body(FORM, FORM_LIMIT), operation), headers
        if self.headers.get_all("Content-Length", []) not in ([], ["0"]):
            raise ValueError("Unexpected GET body")
        return None, headers

    def _respond(self, status, upstream=None):
        self.send_response(status)
        if upstream is not None:
            for field in ("Content-Type", "X-Request-ID"):
                value = upstream.getheader(field)
                if value and re.fullmatch(r"[\x20-\x7e]{1,256}", value):
                    self.send_header(field, value)
        self.end_headers()

    def _relay(self, upstream, operation):
        if upstream.status < 200 or 300 <= upstream.status < 400:
            self._respond(HTTPStatus.BAD_GATEWAY)
            return
        data = None
        if operation == "discovery" and upstream.status == HTTPStatus.OK:
            try:
                data = upstream.read(METADATA_LIMIT + 1)
                if len(data) > METADATA_LIMIT:
                    raise ValueError("OpenAI discovery document was too large")
                self.server.discovered = _metadata_routes(data)
            except (OSError, HTTPException, ValueError, RecursionError):
                self.server.discovered = {}
                self._respond(HTTPStatus.BAD_GATEWAY)
                return
        self._respond(upstream.status, upstream)
        if data is not None:
            self.wfile.write(data)
            return
        while chunk := upstream.read1(65536):
            self.wfile.write(chunk)

    def _handle(self):
        if self.command == "GET" and self.path == HEALTH:
            self._respond(HTTPStatus.NO_CONTENT)
            return
        route = (self.command, self.path)
        operation = ROUTES.get(route) or self.server.discovered.get(route)
        if operation is None:
            self._respond(HTTPStatus.FORBIDDEN)
            return
        try:
            body, headers = self._request(operation)
        except (ValueError, OSError, RecursionError):
            self._respond(HTTPStatus.BAD_REQUEST)
            return
        target = urlsplit(self.path)
        timeout = 900 if operation == "responses" else 20
        with closing(self.server.upstream(target.netloc, timeout=timeout)) as connection:
            try:
                connection.request(self.command, target.path, body, headers)
                upstream = connection.getresponse()
            except (OSError, HTTPException):
                self._respond(HTTPStatus.BAD_GATEWAY)
                return
            with upstream:
                try:
                    self._relay(upstream, operation)
                except (OSError, HTTPException):
                    # Once streaming starts, closing the socket stops a partial response.
                    return

    do_GET = _handle
    do_POST = _handle


class _Gateway(socketserver.ThreadingMixIn, socketserver.TCPServer):
    address_family = getattr(socket, "AF_UNIX", None)
    daemon_threads = True

    def __init__(self, path, upstream=_PublicHTTPSConnection):
        self.discovered = {}
        self.upstream = upstream
        super().__init__(str(path), _Handler)

    def handle_error(self, request, client_address):
        # Do not log tracebacks from processing private requests.
        pass


def _serve(path):
    import fcntl

    os.umask(0o077)
    with os.fdopen(os.open(path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise RuntimeError("Unexpected entry at the OpenAI gateway socket path")
            path.unlink()
        with _Gateway(path) as gateway:
            gateway.serve_forever()


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        with open_request(SOCKET_PATH, Request(HEALTH), 2) as response:
            raise SystemExit(response.status != HTTPStatus.NO_CONTENT)
    else:
        _serve(SOCKET_PATH)
