"""A one-origin test server for a JupyterLite build: static files + /api proxy.

Mirrors the routes of ``config/Caddyfile`` that JupyterLite depends on, so
the in-browser kernel can call the API same-origin as it does on a real
site:

* ``/jupyter``   -> 301 to ``/jupyter/``
* ``/jupyter/*`` -> files of the build, with the COOP/COEP headers the
  Pyodide kernel needs (SharedArrayBuffer)
* ``/api/*``     -> the ant-serve upstream with the ``/api`` prefix stripped
  (Caddy's ``handle_path``), adding ``X-Forwarded-For`` as Caddy does, so
  ant-serve treats the caller as a remote, anonymous client and not as a
  loopback tool

Standard library only. Used by ``tests/test_jupyterlite_e2e.py``; can also be
run by hand::

    python3 tests/jupyterlite_proxy.py --root webapp --port 18701 \\
        --api http://localhost:8080
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import threading
import urllib.parse
from functools import partial
from pathlib import Path
from typing import Optional, Tuple

ISOLATION_HEADERS = (
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Embedder-Policy", "require-corp"),
)
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-length"}
CLIENT_IDENTITY_HEADERS = {"remote-user", "remote-groups", "remote-name", "remote-email",
                           "x-anthive-internal"}


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Static files under ``root`` plus the /api reverse proxy."""

    api_upstream: Tuple[str, int, str] = ("localhost", 8080, "http")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib name
        pass

    # ── static ───────────────────────────────────────────────────────────

    def end_headers(self) -> None:
        if self.path.startswith("/jupyter/"):
            for name, value in ISOLATION_HEADERS:
                self.send_header(name, value)
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if self.path.startswith("/api/"):
            self._proxy()
            return
        if self.path == "/jupyter":
            self.send_response(301)
            self.send_header("Location", "/jupyter/")
            self.end_headers()
            return
        if not self.path.startswith("/jupyter/"):
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self._proxy()
            return
        super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self._proxy()
            return
        self.send_error(405)

    # ── proxy ────────────────────────────────────────────────────────────

    def _proxy(self) -> None:
        host, port, scheme = self.api_upstream
        upstream_path = self.path[len("/api"):]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() not in HOP_BY_HOP | CLIENT_IDENTITY_HEADERS
                   and name.lower() != "host"}
        headers["Host"] = f"{host}:{port}"
        headers["X-Forwarded-For"] = self.client_address[0]
        headers["X-Forwarded-Proto"] = "http"
        headers["X-Forwarded-Host"] = self.headers.get("Host", "")
        connection_class = (http.client.HTTPSConnection if scheme == "https"
                            else http.client.HTTPConnection)
        connection = connection_class(host, port, timeout=600)
        try:
            connection.request(self.command, upstream_path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
        except OSError as exc:
            self.send_error(502, f"upstream {host}:{port}: {exc}")
            return
        finally:
            connection.close()
        self.send_response(response.status)
        for name, value in response.getheaders():
            if name.lower() not in HOP_BY_HOP:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


class JupyterLiteTestServer:
    """Serve ``root`` (which holds ``jupyter/``) and proxy ``/api`` to ``api_url``.

    Args:
        root: directory whose ``jupyter/`` subfolder is the JupyterLite build.
        api_url: ant-serve base URL, e.g. ``http://localhost:8080``.
        port: port to listen on (127.0.0.1).
    """

    def __init__(self, root: Path, api_url: str, port: int) -> None:
        parsed = urllib.parse.urlsplit(api_url)
        handler = type("BoundHandler", (_Handler,), {
            "api_upstream": (parsed.hostname or "localhost",
                             parsed.port or (443 if parsed.scheme == "https" else 80),
                             parsed.scheme or "http")})
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", port), partial(handler, directory=str(root)))
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "JupyterLiteTestServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, required=True,
                        help="directory holding the jupyter/ build")
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--port", type=int, default=18701)
    args = parser.parse_args()
    server = JupyterLiteTestServer(args.root, args.api, args.port)
    print(f"serving {args.root}/jupyter at {server.url}/jupyter/ (api -> {args.api})")
    server.httpd.serve_forever()


if __name__ == "__main__":
    main()
