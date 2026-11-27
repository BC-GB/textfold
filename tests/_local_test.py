"""Authenticated local client for tests of the normal reader routes."""
from urllib.request import Request, urlopen as _urlopen

from textfold import local_access

KEY = local_access.key()
AUTH = {"Authorization": "Bearer " + KEY}


def urlopen(value, *, timeout):
    req = Request(value) if isinstance(value, str) else value
    req.add_unredirected_header("Authorization", AUTH["Authorization"])
    return _urlopen(req, timeout=timeout)
