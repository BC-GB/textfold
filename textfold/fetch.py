"""Download a paper's arXiv HTML (LaTeXML) and its metadata, once; and every download of TextFold's (urlread): a
network that drops a connection now and then is asked again, rather than ending the import."""
import email.utils
import http.client
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html import unescape
from pathlib import Path

from .web_requests import PUBLIC, DestinationDenied
from . import __version__

UA = {"User-Agent": f"TextFold/{__version__} (research prototype; github.com/chenxiachan/textfold)"}


def paper_id(ref):
    """'https://arxiv.org/abs/2512.23916v2' -> '2512.23916' (the reader always follows the latest version)."""
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", ref)
    if not m:
        raise ValueError(f"not an arXiv id: {ref}")
    return m.group(1)


AGAIN = (1, 2, 5, 10)              # seconds before each new try: five in all, 18 s of waiting at most
BUSY = {429, 500, 502, 503, 504}   # a server busy or briefly down: a later try is answered
MAX_SOURCE = 50 * 1024 * 1024
MAX_API = 5 * 1024 * 1024
MAX_IMAGE = 15_000_000
MAX_IMAGE_JOB = 250 * 1024 * 1024
READ_CHUNK = 64 * 1024


class ResponseTooLarge(ValueError):
    def __init__(self):
        super().__init__("response-too-large")


class DownloadBudget:
    """One byte allowance for the images fetched concurrently for a document."""
    def __init__(self, limit=MAX_IMAGE_JOB):
        self.remaining = limit
        self.lock = threading.Lock()

    def take(self, size):
        with self.lock:
            if size > self.remaining:
                raise ResponseTooLarge()
            self.remaining -= size


class Unreachable(urllib.error.URLError):
    """Every try failed on the way. It reads as the code the app has a sentence for, with the host: net-down:arxiv.org
    (a URLError still, for those who try another address on one)."""
    def __init__(self, host, last):
        super().__init__(last)
        self.host = host

    def __str__(self):
        return f"net-down:{self.host}"


def _later(e, wait):
    """The wait before the next try: what a busy server asks (Retry-After, in seconds or as a date; a minute at most),
    else `wait`."""
    v = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
    if v.isdigit():
        return min(int(v), 60)
    try:
        return min(max(0.0, email.utils.parsedate_to_datetime(v).timestamp() - time.time()), 60)
    except (TypeError, ValueError, IndexError):
        return wait


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("download deadline exceeded")
    return remaining


def _read_body(response, limit, deadline, truncate, budget):
    try:
        length = int(response.headers.get("Content-Length") or -1)
    except (TypeError, ValueError):
        length = -1
    if length > limit and not truncate:
        raise ResponseTooLarge()
    read = getattr(response, "read1", response.read)
    body = bytearray()
    while True:
        remaining = _remaining(deadline)
        stream = getattr(response, "fp", response)
        sock = getattr(getattr(stream, "connection", None), "sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        room = limit - len(body)
        chunk = read(min(READ_CHUNK, room + (not truncate)))
        _remaining(deadline)
        if not chunk:
            return bytes(body)
        if budget is not None:
            budget.take(len(chunk))
        if len(chunk) > room:
            raise ResponseTooLarge()
        body.extend(chunk)
        if truncate and len(body) == limit:
            return bytes(body)


def urlread(req, timeout=60, limit=MAX_SOURCE, check=None, busy=BUSY, again=AGAIN, open_url=None, *, truncate=False, budget=None):
    """A request's body, its address after redirects, and its headers. A failure on the way (a connection dropped or
    reset, a TLS stream cut short, a timeout, no network) is tried again after `again`'s waits, and so is a busy
    server's answer (`busy`). An answer that will not change (404, 403, a certificate refused) is raised at once, as is
    what `check` raises on the response, before its body is read. Reads have a byte limit and share a deadline across
    retries. Callers that explicitly allow truncation receive a prefix. Every try failed on the way: Unreachable."""
    if limit < 1:
        raise ValueError("invalid response limit")
    if isinstance(req, str):
        req = urllib.request.Request(req, headers=UA)
    deadline = time.monotonic() + timeout
    for i in range(len(again) + 1):
        try:
            with (open_url or urllib.request.urlopen)(req, timeout=_remaining(deadline)) as r:
                if check:
                    check(r)
                return _read_body(r, limit, deadline, truncate, budget), r.geturl(), r.headers
        except urllib.error.HTTPError as e:
            if e.code not in busy or i == len(again) or time.monotonic() >= deadline:
                raise
            wait = _later(e, again[i])
            e.close()
        except (OSError, http.client.HTTPException) as e:   # (URLError is an OSError)
            if isinstance(e, ssl.SSLCertVerificationError) or isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                raise
            if i == len(again) or time.monotonic() >= deadline:
                raise Unreachable(urllib.parse.urlsplit(req.full_url).hostname or "", e) from e
            wait = again[i]
        time.sleep(wait)
        deadline += wait


def get(url, limit=MAX_SOURCE, budget=None):
    data, final, _ = urlread(url, limit=limit, open_url=PUBLIC.open, budget=budget)
    return data, final


def fetch(pid, root: Path, force=False):
    d = root / "papers" / pid
    d.mkdir(parents=True, exist_ok=True)
    src, meta_f = d / "source.html", d / "meta.json"
    if src.exists() and meta_f.exists() and not force:
        return d
    if src.exists() and not force:   # its HTML came, then its metadata did not: only that is asked for again
        html, final = src.read_bytes(), ""
    else:
        html, final = get(f"https://arxiv.org/html/{pid}")
        tmp = src.with_name(f"source.html.{uuid.uuid4().hex[:8]}.part")   # (never half a page, if the disk fails)
        tmp.write_bytes(html)
        tmp.replace(src)
    abs_html = get(f"https://arxiv.org/abs/{pid}", limit=MAX_API)[0].decode("utf-8", "replace")
    metas = re.findall(r'<meta name="citation_(\w+)" content="([^"]*)"', abs_html)
    lic = re.search(r'href="(https?://(?:arxiv\.org/licenses|creativecommons\.org)[^"]+)"', abs_html)
    version = re.search(r"/html/\d{4}\.\d{4,5}(v\d+)", final) or re.search(r'src="\d{4}\.\d{4,5}(v\d+)/', html.decode("utf-8", "replace"))
    meta = {
        "id": pid,
        "version": version.group(1) if version else "",
        "title": unescape(next((v for k, v in metas if k == "title"), pid)),
        "authors": [unescape(v) for k, v in metas if k == "author"],
        "date": next((v for k, v in metas if k == "date"), ""),
        "abs_url": f"https://arxiv.org/abs/{pid}",
        "html_url": final or f"https://arxiv.org/html/{pid}{version.group(1) if version else ''}",
        "license": lic.group(1) if lic else "",
    }
    meta_f.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return d


def image_name(src):
    """The name a page's figure is kept under beside it (fetch_images)."""
    return re.sub(r"[^\w.\-]", "_", src.split("/")[-1])


def fetch_images(srcs, base_url, out_dir: Path):
    """Download figure images next to the built page, four at a time, each kept smaller (picture.py; one kept before
    that, as it came, stays as it is); returns {original src: local path}."""
    from concurrent.futures import ThreadPoolExecutor
    budget = DownloadBudget()

    from . import picture

    out_dir.mkdir(parents=True, exist_ok=True)

    def one(s):
        name = image_name(s)
        if not name or name in (".", ".."):
            return s, None
        f = next((g for g in (out_dir / name, out_dir / (name + ".webp")) if not g.is_symlink() and g.is_file()), None)
        if f is None:
            url = urllib.parse.urljoin(base_url, s)
            target = urllib.parse.urlsplit(url)
            if target.scheme not in ("http", "https") or not target.hostname:
                return s, None
            try:
                data, kept = picture.compact(get(url, limit=MAX_IMAGE, budget=budget)[0], name)
            except DestinationDenied:
                return s, None
            f = out_dir / kept
            tmp = f.with_name(f"{f.name}.{uuid.uuid4().hex[:8]}.part")   # (a page built meanwhile never finds half a picture)
            tmp.write_bytes(data)
            tmp.replace(f)
        return s, f"{out_dir.name}/{f.name}"

    with ThreadPoolExecutor(4) as ex:
        return {src: path for src, path in ex.map(one, srcs) if path}

