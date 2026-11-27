"""Offline web-import policy checks. No name resolution or network connection is needed."""
import contextlib
import io
import unittest
from unittest.mock import patch

import urllib3

from textfold import web_requests, webpage


class Network:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def urlopen(self, method, url, **kw):
        self.calls.append((url, kw))
        status, headers, body = next(self.replies)
        return urllib3.response.HTTPResponse(body=io.BytesIO(body), headers=headers, status=status, preload_content=False)

    def clear(self):
        pass


class WebRequests(unittest.TestCase):
    def test_article_and_related_resources_keep_the_address_and_http_identity(self):
        pages = web_requests.PageRequests("https://notes.example:8443/article")
        network = Network([(302, {"Location": "/paper"}, b""), (200, {"Content-Type": "text/html"}, b"<p>article</p>")])
        with patch.object(web_requests, "_addresses", return_value=("10.1.2.3",)) as lookup, \
                patch.object(web_requests, "_manager", return_value=network) as manager:
            body, final, _ = webpage._get("https://notes.example:8443/article", pages)
            self.assertEqual((body, final), (b"<p>article</p>", "https://notes.example:8443/paper"))
            self.assertEqual(pages.addresses(pages.origin), ("10.1.2.3",))
        lookup.assert_called_once_with("notes.example", 8443)
        manager.assert_called_with("https", "notes.example", 8443)
        self.assertEqual([url for url, _ in network.calls], ["https://10.1.2.3:8443/article", "https://10.1.2.3:8443/paper"])
        for _, kw in network.calls:
            self.assertEqual(kw["headers"]["Host"], "notes.example:8443")
            self.assertFalse(kw["redirect"])

    def test_pdf_handoff_keeps_the_original_origin_grant(self):
        pages = web_requests.PageRequests("https://notes.example/doc")
        network = Network([(200, {"Content-Type": "application/pdf"}, b"")])
        with patch.object(web_requests, "_addresses", return_value=("10.1.2.3",)), \
                patch.object(web_requests, "_manager", return_value=network):
            with self.assertRaises(webpage.WebPDF) as result:
                webpage._get("https://notes.example/doc", pages)
        self.assertIs(result.exception.requests, pages)
        self.assertEqual(result.exception.url, "https://notes.example/doc")

    def test_different_origins_must_resolve_entirely_to_public_addresses(self):
        page = web_requests.PageRequests("https://notes.example/article")
        for origin in (("https", "assets.example", 443), ("http", "notes.example", 80), ("https", "notes.example", 444)):
            with self.subTest(origin=origin), patch.object(web_requests, "_addresses", return_value=("10.1.2.3",)):
                with self.assertRaises(ValueError):
                    page.addresses(origin)
        for origin in (page.origin, ("https", "images.example", 443)):
            with self.subTest(origin=origin), patch.object(web_requests, "_addresses", return_value=("8.8.8.8", "192.168.1.10")):
                with self.assertRaises(ValueError):
                    page.addresses(origin)

    def test_redirects_check_the_new_destination_before_creating_a_connection(self):
        page = web_requests.PageRequests("https://article.example/start")
        addresses = {"article.example": ("8.8.8.8",), "service.example": ("10.1.2.3",)}
        network = Network([(302, {"Location": "https://service.example/page"}, b"")])
        with patch.object(web_requests, "_addresses", side_effect=lambda host, port: addresses[host]), \
                patch.object(web_requests, "_manager", return_value=network):
            with self.assertRaises(ValueError):
                webpage._get("https://article.example/start", page)
        self.assertEqual(len(network.calls), 1)

    def test_https_proxy_tunnel_uses_ip_and_origin_tls_identity(self):
        with patch("urllib.request.getproxies", return_value={"https": "http://proxy.example:8080"}), \
                patch("urllib.request.proxy_bypass", return_value=False):
            manager = web_requests._manager("https", "article.example", 443)
        with contextlib.closing(manager.connection_from_url("https://[2001:4860:4860::8888]/")) as pool:
            conn = pool._new_conn()
            with patch.object(conn, "connect"):
                pool._prepare_proxy(conn)
            self.assertEqual(conn._tunnel_host.strip("[]"), "2001:4860:4860::8888")
            self.assertEqual(conn.server_hostname, "article.example")
            self.assertEqual(conn.assert_hostname, "article.example")
        manager.clear()

    def test_image_prefetch_and_fallback_share_the_article_policy(self):
        policy = web_requests.PageRequests("https://notes.example/article")
        with patch("textfold.local.prefetch", return_value={}) as prefetch, patch("textfold.local.Images") as images:
            resolver = webpage.image_resolver('<img src="https://assets.example/figure">', policy)
            resolver("image-folder")
            self.assertIs(prefetch.call_args.kwargs["open_url"].__self__, policy)
            self.assertIs(images.call_args.kwargs["open_url"].__self__, policy)

    def test_public_routing_classification(self):
        for ip in ("::1", "100.64.0.1", "224.0.0.5", "::ffff:192.168.1.5", "64:ff9b::1", "fec0::1"):
            with self.subTest(ip=ip):
                self.assertFalse(web_requests._public(ip))
        self.assertTrue(web_requests._public("8.8.8.8"))
        self.assertTrue(web_requests._public("2001:4860:4860::8888"))
