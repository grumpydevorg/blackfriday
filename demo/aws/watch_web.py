"""Filterable live viewer for the watch.py JSONL log: stdlib only, loopback only.

Run: python3 aws/watch_web.py   (then open http://127.0.0.1:7682/)
Env: BF_WATCH_JSONL (default ~/bf-watch.jsonl, same as watch.py), BF_WATCH_PORT (default 7682).

GET /          the page (watch_web/index.html, viewer.js, viewer.css; filtering happens in the browser)
GET /events    Server-Sent Events: the last 2,000 lines of the JSONL, then every appended line
"""

import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = '127.0.0.1'
BACKLOG = 2000
POLL = 0.25
HEARTBEAT = 15.0
STATIC = Path(__file__).resolve().parent / 'watch_web'
FILES = {'/': ('index.html', 'text/html; charset=utf-8'),
         '/index.html': ('index.html', 'text/html; charset=utf-8'),
         '/viewer.js': ('viewer.js', 'text/javascript; charset=utf-8'),
         '/viewer.css': ('viewer.css', 'text/css; charset=utf-8')}
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def jsonl_path() -> Path:
    return Path(os.environ.get('BF_WATCH_JSONL') or Path.home() / 'bf-watch.jsonl')


def tail_lines(path: Path, count: int = BACKLOG) -> tuple[list[str], int]:
    """The last `count` complete lines and the byte offset just after the last complete line."""
    try:
        with open(path, 'rb') as handle:
            size = handle.seek(0, os.SEEK_END)
            data, position = b'', size
            while position > 0 and data.count(b'\n') <= count:
                step = min(65536, position)
                position -= step
                handle.seek(position)
                data = handle.read(step) + data
    except FileNotFoundError:
        return [], 0
    end = data.rfind(b'\n') + 1            # a half-written last line waits for its newline
    complete = data[:end].split(b'\n')[:-1]
    if position > 0 and complete:
        complete = complete[1:]            # the first line may be cut by the chunk boundary
    return [line.decode('utf-8', 'replace') for line in complete[-count:] if line.strip()], size - (len(data) - end)


def follow(path: Path, backlog: int = BACKLOG, poll: float = POLL, heartbeat: float = HEARTBEAT,
           stop=lambda: False, sleep=time.sleep, clock=time.monotonic):
    """Yield the backlog, then each appended line; None every `heartbeat` seconds of silence.
    A truncated or replaced (rotated) file is read again from its start."""
    lines, offset = tail_lines(path, backlog)
    yield from lines
    identity = None
    try:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
    except FileNotFoundError:
        offset = 0
    pending = b''
    quiet = clock()
    while not stop():
        try:
            stat = path.stat()
        except FileNotFoundError:
            identity, offset, pending = None, 0, b''
            stat = None
        if stat is not None:
            if identity != (stat.st_dev, stat.st_ino) or stat.st_size < offset:
                identity, offset, pending = (stat.st_dev, stat.st_ino), 0, b''
            if stat.st_size > offset:
                with open(path, 'rb') as handle:
                    handle.seek(offset)
                    chunk = handle.read(stat.st_size - offset)
                offset += len(chunk)
                pending += chunk
                *done, pending = pending.split(b'\n')
                for line in done:
                    if line.strip():
                        quiet = clock()
                        yield line.decode('utf-8', 'replace')
        if clock() - quiet >= heartbeat:
            quiet = clock()
            yield None
        sleep(poll)


class Handler(BaseHTTPRequestHandler):
    server_version = 'bf-watch'

    def log_message(self, *args) -> None:   # keep the terminal quiet
        pass

    def headers_ok(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')

    def reply(self, status: int, body: bytes, content_type: str = 'text/plain; charset=utf-8') -> None:
        self.headers_ok(status, content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def host_allowed(self) -> bool:
        """Only loopback names: a web page on another origin cannot read this via DNS rebinding."""
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        return host in ('127.0.0.1', 'localhost', '::1')

    def do_GET(self) -> None:   # noqa: N802
        if not self.host_allowed():
            return self.reply(403, b'forbidden')
        path = self.path.split('?', 1)[0].split('#', 1)[0]
        if path == '/events':
            return self.events()
        if path in FILES:
            name, content_type = FILES[path]
            try:
                return self.reply(200, (STATIC / name).read_bytes(), content_type)
            except FileNotFoundError:
                return self.reply(404, b'not found')
        self.reply(404, b'not found')

    def events(self) -> None:
        self.headers_ok(200, 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(b'retry: 2000\n\n')
            self.wfile.flush()
            for line in follow(jsonl_path()):
                self.wfile.write(b': heartbeat\n\n' if line is None else f'data: {line}\n\n'.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(os.environ.get('BF_WATCH_PORT') or 7682)
    server = Server((HOST, port), Handler)
    print(f'BLACK FRIDAY log viewer: http://{HOST}:{port}/  (reading {jsonl_path()})', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == '__main__':
    main()
