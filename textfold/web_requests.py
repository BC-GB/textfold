"""GET requests for an imported web page and the resources it selects.

The URL the reader chose grants access to that origin's initial addresses for this
import. Other origins must resolve entirely to public addresses. Connect to a
vetted numeric address, retaining the origin's HTTP Host and HTTPS identity.
"""
import contextlib
import ipaddress
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from urllib.response import addinfourl

import urllib3


class DestinationDenied(ValueError):
    """A document selected a destination outside its allowed network scope."""


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
        raise DestinationDenied("Invalid web resource address")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if any(char in host for char in "%/\\?#@") or any(ord(char) <= 32 for char in host):
        raise DestinationDenied("Invalid web resource host")
    port = parts.port if parts.port is not None else 443 if parts.scheme == "https" else 80
    if not port:
        raise DestinationDenied("Invalid web resource port")
    return parts.scheme, host, port


def _addresses(host, port):
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return tuple(dict.fromkeys(row[4][0] for row in rows))


def _public(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour or ip.teredo or ip.is_site_local or ip in _TRANSLATED or ip in _LOCAL_TRANSLATED:
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


_TRANSLATED = ipaddress.ip_network("64:ff9b::/96")
_LOCAL_TRANSLATED = ipaddress.ip_network("64:ff9b:1::/48")


def _authority(host, port, scheme):
    name = f"[{host}]" if ":" in host else host
    return name if port == (443 if scheme == "https" else 80) else f"{name}:{port}"


def _manager(scheme, host, port):
    options = {"server_hostname": host, "assert_hostname": host} if scheme == "https" else {}
    proxy = urllib.request.getproxies().get(scheme)
    if not proxy or urllib.request.proxy_bypass(_authority(host, port, scheme)):
        return urllib3.PoolManager(**options)
    parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Unsupported web import proxy")
    headers = {}
    if parts.username is not None:
        user = urllib.parse.unquote(parts.username)
        password = urllib.parse.unquote(parts.password or "")
        headers = urllib3.make_headers(proxy_basic_auth=f"{user}:{password}")
    return urllib3.ProxyManager(parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]).geturl(),
                                proxy_headers=headers, **options)


class PageRequests:
    def __init__(self, selected_url=None):
        self.origin = _origin(selected_url) if selected_url else None
        self.initial_addresses = None
        self.lock = threading.Lock()

    def addresses(self, origin):
        _, host, port = origin
        if origin == self.origin:
            with self.lock:
                if self.initial_addresses is None:
                    addresses = _addresses(host, port)
                    if len({_public(address) for address in addresses}) > 1:
                        raise DestinationDenied("Web origin has both public and private addresses")
                    self.initial_addresses = addresses
                return self.initial_addresses
        addresses = _addresses(host, port)
        if not all(_public(address) for address in addresses):
            raise DestinationDenied("Page resource points outside the public network")
        return addresses

    @contextlib.contextmanager
    def open(self, request, timeout):
        url = request.full_url
        for hop in range(11):
            scheme, host, port = origin = _origin(url)
            addresses = self.addresses(origin)
            if not addresses:
                raise urllib.error.URLError("No addresses for web resource")
            parts = urllib.parse.urlsplit(url)
            headers = {key: value for key, value in request.header_items() if key.lower() in ("accept", "user-agent", "accept-language")}
            headers["Host"] = _authority(host, port, scheme)
            manager = _manager(*origin)
            response = None
            try:
                for i, address in enumerate(addresses):
                    target = parts._replace(netloc=_authority(address, port, scheme), fragment="").geturl()
                    try:
                        response = manager.urlopen("GET", target, headers=headers, timeout=timeout, retries=False,
                                                   redirect=False, preload_content=False, decode_content=False)
                        break
                    except urllib3.exceptions.ConnectTimeoutError:
                        if i == len(addresses) - 1:
                            raise
                location = response.get_redirect_location()
                if location:
                    if hop == 10:
                        raise ValueError("Too many redirects for web resource")
                    url = urllib.parse.urljoin(url, location)
                    continue
                info = Message()
                for key, value in response.headers.items():
                    info[key] = value
                if not 200 <= response.status < 300:
                    raise urllib.error.HTTPError(url, response.status, str(response.reason), info, None)
                yield addinfourl(response, info, url, response.status)
                return
            except urllib3.exceptions.SSLError as e:
                # Keep certificate failures terminal and temporary TLS failures retryable.
                reason = e.args[0] if e.args else e
                if isinstance(reason, ssl.CertificateError):
                    raise reason from e
                raise urllib.error.URLError(reason) from e
            except urllib3.exceptions.HTTPError as e:
                raise urllib.error.URLError(str(e)) from e
            finally:
                if response is not None:
                    response.close()
                manager.clear()


# Public document adapters (arXiv, GitHub, etc.) have no private origin grant.
PUBLIC = PageRequests()
