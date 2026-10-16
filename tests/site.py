"""Tiny local site with a cookie login, used instead of a real third-party site."""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PAGE = """<!doctype html><html><head><title>{title}</title>
<meta name="csrf-token" content="CSRFSECRET"><link rel="stylesheet" href="/static/app.css?v=1"></head>
<body><div id="avatar">Signed in</div><h1>{title}</h1>
<form action="/app/save" method="post"><input type="hidden" name="csrf_token" value="CSRFSECRET">
<input name="title"><button type="submit">Save</button></form>
<a href="/app/page2">Page 2</a> <a href="/logout">Log out</a> <a href="https://example.invalid/x">Off-site</a>
<script src="/static/app.js"></script></body></html>"""

LOGIN = """<!doctype html><html><head><title>Login</title><link rel="stylesheet" href="/static/login.css"></head>
<body><form method="post" action="/login"><input name="user"><input name="password" type="password">
<button type="submit">Sign in</button></form></body></html>"""

APP_JS = """const csrf = document.querySelector('meta[name=csrf-token]').content, s = 'SECRET';
fetch('/api/data?token=QUERY' + s + '&page=1', {headers: {Authorization: 'Bearer BEARER' + s, 'X-CSRF-Token': csrf}})
  .then(r => r.json()).then(d => { document.querySelector('h1').dataset.items = d.items.length; });
console.error('boom with Bearer BEARER' + s);"""


TABLE = """<!doctype html><html><head><title>Table</title></head><body><h1>Rows</h1>
<table><tbody id="rows"></tbody></table><button id="next" type="button">Next</button> <span id="pg"></span>
<script>
let page = +(new URLSearchParams(location.search).get('page') || 1);
async function load() {
  const d = await (await fetch('/api/rows?page=' + page + '&size=3')).json();
  document.getElementById('pg').textContent = 'page ' + d.page;
  document.getElementById('rows').innerHTML = d.rows.map(r =>
    `<tr><td>${r.name}</td><td><a target="_blank" href="/app/item/${r.id}">open</a></td></tr>`).join('');
}
document.getElementById('next').onclick = () => { page++; history.pushState(null, '', '?page=' + page); load(); };
load();
</script></body></html>"""


class Site:
    def __init__(self):
        self.sessions: set[str] = set()
        self.hits: list[str] = []
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="text/html", headers=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _authed(self):
                c = self.headers.get("Cookie", "")
                return any(part.strip().removeprefix("sid=") in site.sessions for part in c.split(";") if "sid=" in part)

            def do_GET(self):
                site.hits.append(self.path)
                path = self.path.split("?")[0]
                if path == "/login":
                    return self._send(200, LOGIN.encode())
                if path == "/static/login.css":
                    return self._send(200, b"form{margin:2em}", "text/css")
                if path == "/static/app.css":
                    return self._send(200, b"h1{color:rebeccapurple}", "text/css")
                if path == "/static/app.js":
                    return self._send(200, APP_JS.encode(), "application/javascript")
                if path == "/public":
                    return self._send(200, b"<!doctype html><title>Public</title><p>hello</p>")
                if path == "/logout":
                    site.sessions.clear()
                    return self._send(302, headers=[("Location", "/login")])
                if not self._authed():
                    if path.startswith("/api/"):
                        return self._send(401, b'{"error":"unauthorized"}', "application/json")
                    return self._send(302, headers=[("Location", "/login")])
                if path == "/api/data":
                    body = {"items": [1, 2, 3], "access_token": "BODYSECRET", "name": "ok"}
                    return self._send(200, json.dumps(body).encode(), "application/json")
                if path == "/api/rows":
                    q = parse_qs(self.path.partition("?")[2])
                    pg, size = int(q["page"][0]), int(q["size"][0])
                    rows = [{"id": i, "name": f"row {i}"} for i in range((pg - 1) * size + 1, pg * size + 1)]  # 100k rows, generated
                    return self._send(200, json.dumps({"page": pg, "total": 100000, "rows": rows}).encode(), "application/json")
                if path == "/app/table":
                    return self._send(200, TABLE.encode())
                if path.startswith("/app/item/"):
                    return self._send(200, f"<!doctype html><title>Item</title><h1>Item {path.rsplit('/', 1)[1]}</h1>".encode())
                if path in ("/app", "/app/page2"):
                    return self._send(200, PAGE.format(title=path).encode())
                self._send(404, b"not found")

            def do_POST(self):
                site.hits.append("POST " + self.path)
                n = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(n).decode())
                if self.path == "/login" and form.get("password") == ["hunter2"]:
                    sid = "SIDSECRET" + secrets.token_hex(4)
                    site.sessions.add(sid)
                    return self._send(302, headers=[("Location", "/app"), ("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")])
                self._send(403, b"bad login")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
