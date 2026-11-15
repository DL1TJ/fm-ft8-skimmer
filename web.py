"""The skimmer's own web page: a tiny read-only HTTP server, standard library only.

It serves the files of web/ and the current data as /data/status.json, recent.json, stats.json and
config.json. Nothing else: every path it answers is in a table built at start, so no request can
reach the file system; only GET and HEAD are answered; nothing a visitor sends is stored or parsed
beyond the request line. It listens on 127.0.0.1 unless configured otherwise and has no login -
put a reverse proxy in front of it if it is to face the internet.
"""
import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
HEADERS = (
    ("Cache-Control", "no-cache"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
)
DATA = ("status", "recent", "stats")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Server6(_Server):
    address_family = socket.AF_INET6


class Page:
    def __init__(self, bind, port, webdir, log=print):
        self.log, self.lock = log, threading.Lock()
        self.files = {}                                   # path -> (content type, body, etag)
        for f in sorted(webdir.iterdir()):
            if f.is_file() and f.suffix in TYPES:
                self._set("/" + f.name, TYPES[f.suffix], f.read_bytes())
        if "/index.html" not in self.files:
            raise OSError(f"no index.html in {webdir}")
        self.files["/"] = self.files["/index.html"]
        for name in DATA:
            self.put(name, None)
        self._set("/data/config.json", "application/json", b'{"mqtt":null}')
        page = self

        class Handler(BaseHTTPRequestHandler):
            server_version, sys_version, protocol_version = "fm-ft8-skimmer", "", "HTTP/1.1"
            timeout = 15                                  # a visitor who sends nothing does not keep a thread

            def _answer(self, body=True):
                with page.lock:
                    hit = page.files.get(self.path.split("?", 1)[0])
                if hit is None:
                    self.send_error(404)
                    return
                ctype, data, etag = hit
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    data = b""
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("ETag", etag)
                for k, v in HEADERS:
                    self.send_header(k, v)
                self.end_headers()
                if body and data:
                    self.wfile.write(data)

            def do_GET(self):
                self._answer()

            def do_HEAD(self):
                self._answer(body=False)

            def send_error(self, code, message=None, explain=None):
                body = f"{code}\n".encode()
                self.send_response(code, message)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                self.close_connection = True

            def log_message(self, *a):                    # no access log: nothing about visitors is kept
                pass

        self.httpd = (_Server6 if ":" in bind else _Server)((bind, port), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _set(self, path, ctype, body):
        with self.lock:
            self.files[path] = (ctype, body, f'"{zlib.crc32(body):08x}"')

    def put(self, name, payload):
        """Replace /data/<name>.json (status, recent or stats)."""
        if name in DATA:
            self._set(f"/data/{name}.json", "application/json", json.dumps(payload, separators=(",", ":")).encode())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
