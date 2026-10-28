"""Ingress reverse proxy for an external seestar_alp web UI.

In external mode (``alpaca_host`` set) the add-on does not run its own
seestar_alp, so nothing would serve the ingress port. This module serves it by
proxying the operator's own seestar_alp instead, so its web UI opens in the
Home Assistant sidebar and in the companion app - including remotely, without
exposing the seestar_alp host.

Two upstreams are proxied:

* the SSC web UI (``SEESTAR_EXTERNAL_WEBUI_URL``, e.g. ``http://host:5432``) at
  the ingress root, and
* the imaging server (``SEESTAR_EXTERNAL_IMAGING_URL``, e.g. ``http://host:7556``)
  under ``/_imaging/`` - the live ``/vid`` MJPEG stream and the ``/live/status``
  server-sent events. seestar_alp hands the browser an absolute ``http://host:7556``
  URL for these, which would be mixed content behind HTTPS and unreachable from
  outside the LAN.

HA's ingress serves the add-on below ``/api/hassio_ingress/<token>/`` and passes
that prefix in the ``X-Ingress-Path`` header. seestar_alp's pages use
root-relative URLs (``/public/...``, ``/1/live/...``) and build some URLs from
``window.location``, so HTML, JavaScript and CSS responses are rewritten to
carry the prefix. Everything else (images, the MJPEG stream, SSE) is streamed
through unchanged and unbuffered.

Standard library only; one thread per connection.
"""
from __future__ import annotations

import html
import http.client
import os
import re
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMAGING_MOUNT = "/_imaging"

#: Content types whose bodies are rewritten (read fully, then re-sent).
REWRITE_TYPES = ("text/html", "text/javascript", "application/javascript", "text/css")

#: Responses that never end on their own: stream with blocking reads, no timeout.
STREAM_TYPES = ("text/event-stream", "multipart/x-mixed-replace")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}

#: Response headers carrying a URL the browser follows.
URL_HEADERS = ("location", "content-location", "hx-redirect", "hx-location", "hx-push-url")

_ATTR_RE = re.compile(
    r"""(\b(?:href|src|action|poster|formaction|hx-get|hx-post|hx-put|hx-patch|hx-delete|"""
    r"""sse-connect|data-src|data-url)\s*=\s*["'])/(?!/)""",
    re.IGNORECASE,
)
_JS_CALL_RE = re.compile(r"""(\b(?:fetch|EventSource)\(\s*["'`])/(?!/)""")
_CSS_URL_RE = re.compile(r"""(url\(\s*["']?)/(?!/)""")
# `${window.location.protocol}//${window.location.hostname}${window.location.port ? ':' + window.location.port : ''}`
_JS_ORIGIN_RE = re.compile(
    r"""\$\{window\.location\.protocol\}//\$\{window\.location\.hostname\}"""
    r"""\$\{window\.location\.port\s*\?\s*(["']):\1\s*\+\s*window\.location\.port\s*:\s*\1\1\s*\}"""
)


class Upstream:
    """One proxied origin (scheme://host:port)."""

    def __init__(self, url: str):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"not an http(s) URL: {url!r}")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.netloc = f"{self.host}:{self.port}"
        self.origin = f"{self.scheme}://{self.netloc}"

    def connect(self, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=timeout)


def _no_double(prefix: str) -> str:
    """Lookahead that skips paths already carrying the prefix (keeps rewriting idempotent)."""
    return f"(?!{re.escape(prefix.lstrip('/'))}/)" if prefix else ""


def rewrite_body(text: str, prefix: str, web: Upstream, imaging: Upstream | None) -> str:
    """Point seestar_alp's URLs at the ingress prefix.

    ``prefix`` is the ingress path without a trailing slash ("" = served at /).
    """
    if imaging is not None:
        # seestar_alp builds the imaging root from the request Host's hostname plus
        # its imaging port; the proxy sends the upstream host, so this is exact.
        for origin in {imaging.origin, f"http://{web.host}:{imaging.port}"}:
            text = text.replace(origin, prefix + IMAGING_MOUNT)
    text = text.replace(web.origin + "/", prefix + "/").replace(web.origin, prefix or "/")
    if not prefix:
        return text
    skip = _no_double(prefix)
    text = re.sub(_ATTR_RE.pattern + skip, lambda m: m.group(1) + prefix + "/", text, flags=re.IGNORECASE)
    text = re.sub(_JS_CALL_RE.pattern + skip, lambda m: m.group(1) + prefix + "/", text)
    text = re.sub(_CSS_URL_RE.pattern + skip, lambda m: m.group(1) + prefix + "/", text)
    text = _JS_ORIGIN_RE.sub(lambda m: "${window.location.origin}" + prefix, text)
    return text


def rewrite_url(value: str, prefix: str, web: Upstream, imaging: Upstream | None) -> str:
    """Rewrite a redirect-style header value."""
    if imaging is not None:
        for origin in (imaging.origin, f"http://{web.host}:{imaging.port}"):
            if value.startswith(origin):
                return prefix + IMAGING_MOUNT + value[len(origin):]
    if value.startswith(web.origin):
        value = value[len(web.origin):] or "/"
    if prefix and value.startswith("/") and not value.startswith("//") and not value.startswith(prefix + "/"):
        return prefix + value
    return value


def ingress_prefix(raw: str | None) -> str:
    """Normalise X-Ingress-Path: leading slash, no trailing slash, "" when absent."""
    if not raw:
        return ""
    raw = raw.strip()
    if not raw.startswith("/") or any(c in raw for c in "\r\n\"'<> "):
        return ""
    return raw.rstrip("/")


def _content_type(headers) -> str:
    return (headers.get("Content-Type") or "").split(";")[0].strip().lower()


def make_handler(web: Upstream, imaging: Upstream | None, *, timeout: float = 15.0):
    class ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "seestar-ingress-proxy"

        # --- request plumbing --------------------------------------------
        def _route(self) -> tuple[Upstream, str]:
            path = self.path if self.path.startswith("/") else "/"
            if imaging is not None and (path == IMAGING_MOUNT or path.startswith(IMAGING_MOUNT + "/")):
                return imaging, path[len(IMAGING_MOUNT):] or "/"
            return web, path

        def _proxy(self) -> None:
            prefix = ingress_prefix(self.headers.get("X-Ingress-Path"))
            upstream, path = self._route()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else None

            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP_BY_HOP and k.lower() not in ("host", "accept-encoding", "content-length")}
            headers["Host"] = upstream.netloc
            headers["Accept-Encoding"] = "identity"     # bodies must be plain text to rewrite
            if body is not None:
                headers["Content-Length"] = str(len(body))

            try:
                conn = upstream.connect(timeout)
                conn.request(self.command, path, body=body, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                self._bad_gateway(upstream, exc)
                return

            try:
                self._relay(resp, conn, prefix)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass                                    # browser went away (e.g. closed live view)
            finally:
                conn.close()

        def _relay(self, resp: http.client.HTTPResponse, conn, prefix: str) -> None:
            ctype = _content_type(resp.headers)
            rewrite = ctype in REWRITE_TYPES and self.command != "HEAD"
            stream = ctype in STREAM_TYPES

            self.send_response(resp.status, resp.reason)
            for k, v in resp.headers.items():
                lk = k.lower()
                if lk in HOP_BY_HOP or lk == "content-length":
                    continue
                if lk in URL_HEADERS:
                    v = rewrite_url(v, prefix, web, imaging)
                self.send_header(k, v)

            if rewrite:
                charset = resp.headers.get_content_charset() or "utf-8"
                data = resp.read().decode(charset, errors="replace")
                out = rewrite_body(data, prefix, web, imaging).encode(charset, errors="replace")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

            n = resp.headers.get("Content-Length")
            if n is not None and not stream:
                self.send_header("Content-Length", n)
            else:
                self.send_header("Connection", "close")  # unknown length: end of body = end of connection
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            if stream and conn.sock is not None:
                conn.sock.settimeout(None)              # MJPEG/SSE run until the viewer leaves
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()

        def _bad_gateway(self, upstream: Upstream, exc: Exception) -> None:
            page = (
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<title>seestar_alp unreachable</title>"
                "<style>body{font-family:system-ui,sans-serif;padding:2rem;color:#e1e1e1;background:#1c1c1c}"
                "code{background:#2b2b2b;padding:.1rem .35rem;border-radius:.25rem}"
                "@media(prefers-color-scheme:light){body{color:#212121;background:#fafafa}code{background:#eee}}"
                "</style></head><body><h1>seestar_alp is not reachable</h1>"
                f"<p>The add-on could not reach <code>{html.escape(upstream.origin)}</code>: "
                f"<code>{html.escape(str(exc) or type(exc).__name__)}</code></p>"
                "<p>Check that your seestar_alp is running and that <code>alpaca_host</code>, "
                "<code>alpaca_webui_port</code> and <code>imaging_port</code> point at it.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(page)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _proxy

        def log_message(self, fmt: str, *args) -> None:
            if os.environ.get("INGRESS_PROXY_DEBUG"):
                sys.stderr.write("[ingress-proxy] " + (fmt % args) + "\n")

    return ProxyHandler


def serve(web_url: str, imaging_url: str | None, *, bind: str = "0.0.0.0", port: int = 5432) -> ThreadingHTTPServer:
    web = Upstream(web_url)
    imaging = Upstream(imaging_url) if imaging_url else None
    server = ThreadingHTTPServer((bind, port), make_handler(web, imaging))
    server.daemon_threads = True
    return server


def main() -> None:
    web_url = os.environ.get("SEESTAR_EXTERNAL_WEBUI_URL", "").strip()
    imaging_url = os.environ.get("SEESTAR_EXTERNAL_IMAGING_URL", "").strip() or None
    port = int(os.environ.get("INGRESS_PORT", "5432"))
    if not web_url:
        sys.exit("[ingress-proxy] SEESTAR_EXTERNAL_WEBUI_URL is not set")
    server = serve(web_url, imaging_url, port=port)
    print(f"[ingress-proxy] ingress :{port} -> {web_url}"
          f"{f' (imaging {imaging_url} at {IMAGING_MOUNT}/)' if imaging_url else ''}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
