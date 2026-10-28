"""Tests for the external-mode ingress reverse proxy."""
from __future__ import annotations

import http.client
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from seestar_bridge.ingress_proxy import (
    Upstream,
    ingress_prefix,
    rewrite_body,
    rewrite_url,
    serve,
)

PREFIX = "/api/hassio_ingress/abc123"
WEB = Upstream("http://192.168.1.50:5432")
IMG = Upstream("http://192.168.1.50:7556")


# --------------------------------------------------------------------- rewriting
def test_rewrites_root_relative_attributes():
    page = ('<link href="/public/bootstrap.min.css"><script src="/public/htmx.min.js"></script>'
            '<form action="/1/schedule/x"><button hx-post="/1/live/goto"></button>'
            '<a href="//cdn.example/x">cdn</a><a href="https://example.com/">ext</a>')
    out = rewrite_body(page, PREFIX, WEB, IMG)
    assert f'href="{PREFIX}/public/bootstrap.min.css"' in out
    assert f'src="{PREFIX}/public/htmx.min.js"' in out
    assert f'action="{PREFIX}/1/schedule/x"' in out
    assert f'hx-post="{PREFIX}/1/live/goto"' in out
    assert 'href="//cdn.example/x"' in out             # protocol-relative untouched
    assert 'href="https://example.com/"' in out        # external untouched


def test_rewrites_imaging_origin_to_mount():
    page = '<img src="http://192.168.1.50:7556/1/vid"><div sse-connect="http://192.168.1.50:7556/1/live/status">'
    out = rewrite_body(page, PREFIX, WEB, IMG)
    assert f'src="{PREFIX}/_imaging/1/vid"' in out
    assert f'sse-connect="{PREFIX}/_imaging/1/live/status"' in out


def test_rewrites_js_origin_builder_and_fetch():
    js = ("const baseURL = `${window.location.protocol}//${window.location.hostname}"
          "${window.location.port ? ':' + window.location.port : ''}`;\n"
          'const b2 = `${window.location.protocol}//${window.location.hostname}'
          '${window.location.port ? ":" + window.location.port : ""}`;\n'
          'fetch("/toggleplanningcard");')
    out = rewrite_body(js, PREFIX, WEB, IMG)
    assert out.count("${window.location.origin}" + PREFIX) == 2
    assert f'fetch("{PREFIX}/toggleplanningcard")' in out


def test_rewriting_is_idempotent():
    once = rewrite_body('<a href="/1/live">', PREFIX, WEB, IMG)
    assert rewrite_body(once, PREFIX, WEB, IMG) == once


def test_without_prefix_only_origins_change():
    page = '<a href="/x"></a><img src="http://192.168.1.50:7556/1/vid">'
    assert rewrite_body(page, "", WEB, IMG) == '<a href="/x"></a><img src="/_imaging/1/vid">'


def test_rewrite_url_headers():
    assert rewrite_url("/1/live", PREFIX, WEB, IMG) == f"{PREFIX}/1/live"
    assert rewrite_url("http://192.168.1.50:5432/1/live", PREFIX, WEB, IMG) == f"{PREFIX}/1/live"
    assert rewrite_url("http://192.168.1.50:7556/1/vid", PREFIX, WEB, IMG) == f"{PREFIX}/_imaging/1/vid"
    assert rewrite_url(f"{PREFIX}/x", PREFIX, WEB, IMG) == f"{PREFIX}/x"
    assert rewrite_url("https://example.com/", PREFIX, WEB, IMG) == "https://example.com/"


@pytest.mark.parametrize("raw,expected", [
    (None, ""), ("", ""), ("/api/hassio_ingress/t/", "/api/hassio_ingress/t"),
    ("/api/hassio_ingress/t", "/api/hassio_ingress/t"), ('/x"><script>', ""), ("relative", ""),
])
def test_ingress_prefix(raw, expected):
    assert ingress_prefix(raw) == expected


# ------------------------------------------------------------------ end to end
class _FakeWeb(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/1/live")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        host = self.headers["Host"].split(":")[0]
        body = (f'<script src="/public/main.js"></script>'
                f'<img src="http://{host}:{self.server.img_port}/1/vid">host={self.headers["Host"]}'
                f' enc={self.headers.get("Accept-Encoding")}').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers["Content-Length"])
        data = self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class _FakeImaging(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(3):
            self.wfile.write(f"data: {i}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.05)

    def log_message(self, *a):
        pass


def _start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture()
def proxy():
    img = _start(ThreadingHTTPServer(("127.0.0.1", 0), _FakeImaging))
    web = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWeb)
    web.img_port = img.server_address[1]
    _start(web)
    px = _start(serve(f"http://127.0.0.1:{web.server_address[1]}",
                      f"http://127.0.0.1:{img.server_address[1]}", bind="127.0.0.1", port=0))
    yield px.server_address[1], web.server_address[1]
    for s in (px, web, img):
        s.shutdown()


def _get(port, path, method="GET", body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers={"X-Ingress-Path": PREFIX + "/", **(headers or {})})
    r = c.getresponse()
    return r, r.read()


def test_end_to_end_html_is_rewritten(proxy):
    port, web_port = proxy
    r, body = _get(port, "/1/")
    text = body.decode()
    assert r.status == 200
    assert f'src="{PREFIX}/public/main.js"' in text
    assert f'src="{PREFIX}/_imaging/1/vid"' in text
    assert f"host=127.0.0.1:{web_port}" in text          # upstream sees its own Host
    assert "enc=identity" in text
    assert int(r.headers["Content-Length"]) == len(body)


def test_end_to_end_redirect_and_post(proxy):
    port, _ = proxy
    r, _ = _get(port, "/redirect")
    assert r.status == 302 and r.headers["Location"] == f"{PREFIX}/1/live"
    r, body = _get(port, "/api", method="POST", body=b'{"a":1}', headers={"Content-Type": "application/json"})
    assert r.status == 200 and body == b'{"a":1}'


def test_end_to_end_imaging_stream(proxy):
    port, _ = proxy
    r, body = _get(port, "/_imaging/1/live/status")
    assert r.headers["Content-Type"] == "text/event-stream"
    assert body == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"


def test_unreachable_upstream_is_502():
    px = _start(serve("http://127.0.0.1:9", None, bind="127.0.0.1", port=0))
    try:
        r, body = _get(px.server_address[1], "/")
        assert r.status == 502 and b"not reachable" in body
    finally:
        px.shutdown()
