"""Writes observed traffic, assets and page snapshots. Only ever called while the session is SCRAPING."""

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .config import Config
from .redaction import Redactor
from .storage import RunDir, asset_relpath, ext_for, short_hash

OVERLAY_ID = "__web_cloner_overlay"
OVERLAY_TAG_RE = re.compile(rf'<div id="{OVERLAY_ID}"[^>]*>\s*</div>')
SET_OVERLAY_DISPLAY = f"d => {{ const e = document.getElementById('{OVERLAY_ID}'); if (e) e.style.display = d; }}"

EXTRACT_JS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const all = (sel, f) => [...document.querySelectorAll(sel)].slice(0, 2000).map(f);
  const txt = e => (e.innerText || '').trim().slice(0, 200);
  return {
    title: document.title,
    url: location.href,
    viewport: {width: innerWidth, height: innerHeight, device_pixel_ratio: devicePixelRatio},
    user_agent: navigator.userAgent,
    links: all('a[href]', a => ({href: a.href, text: txt(a), visible: vis(a)})),
    forms: all('form', f => ({action: f.action, method: (f.getAttribute('method') || 'get').toLowerCase(), id: f.id || null,
      fields: [...f.elements].map(el => ({tag: el.tagName.toLowerCase(), type: el.type || null, name: el.name || null,
        id: el.id || null, required: !!el.required}))})),
    controls: all('button, [role=button], [role=tab], [role=menuitem], input[type=submit], input[type=button], select, summary',
      e => ({tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), aria_label: e.getAttribute('aria-label'),
        text: txt(e) || (['submit', 'button'].includes(e.type) ? e.value : ''), visible: vis(e)})),
    resources: performance.getEntriesByType('resource').map(r => ({url: r.name, initiator_type: r.initiatorType,
      transfer_size: r.transferSize, duration_ms: Math.round(r.duration)})),
  };
}"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Capture:
    def __init__(self, cfg: Config, run: RunDir, redactor: Redactor):
        self.cfg, self.run, self.r = cfg, run, redactor
        self.counts = dict(pages=0, requests=0, responses=0, assets=0, api_bodies=0, failures=0, errors=0, bytes_saved=0)
        self.limit_hit: str | None = None
        self.started = time.monotonic()
        self.started_at = now_iso()
        self.closed = False
        self._seq = {}  # Request -> seq; only requests issued while scraping get one
        self._t0 = {}
        self._assets = {}  # original url -> record
        self._paths = {}  # relpath -> original url
        self._nav_requested = {}  # Page -> url of the navigation's first request
        self.pages = []
        self._snap_lock = asyncio.Lock()

    def _limit(self, reason: str) -> None:
        self.limit_hit = self.limit_hit or reason

    def _budget(self, n: int) -> bool:
        if self.counts["bytes_saved"] + n > self.cfg.max_total_bytes:
            self._limit("max_total_bytes")
            return False
        self.counts["bytes_saved"] += n
        return True

    # --- network -------------------------------------------------------------
    def on_request(self, req) -> None:
        if self.closed or self.limit_hit:
            return
        if self.counts["requests"] >= self.cfg.max_requests:
            return self._limit("max_requests")
        seq = self._seq[req] = self.counts["requests"] = self.counts["requests"] + 1
        self._t0[req] = time.monotonic()
        if req.is_navigation_request() and req.redirected_from is None:
            try:
                if req.frame == req.frame.page.main_frame:
                    self._nav_requested[req.frame.page] = req.url
            except Exception:
                pass  # frame detached / service worker request
        headers = req.headers
        self.run.jsonl("network/requests.jsonl", {
            "seq": seq, "ts": now_iso(), "method": req.method, "url": self.r.url(req.url),
            "resource_type": req.resource_type, "is_navigation": req.is_navigation_request(),
            "redirected_from": self._seq.get(req.redirected_from), "headers": self.r.header_map(headers),
            "post_data": self.r.post_data(req.post_data, headers.get("content-type", "")),
        })

    def on_failed(self, req) -> None:
        seq = self._seq.get(req)
        if seq is None or self.closed:
            return
        self.counts["failures"] += 1
        self.run.jsonl("network/failures.jsonl", {
            "seq": seq, "ts": now_iso(), "method": req.method, "url": self.r.url(req.url),
            "resource_type": req.resource_type, "failure": req.failure,
        })

    async def on_response(self, resp) -> None:
        req = resp.request
        seq = self._seq.get(req)
        if seq is None or self.closed:
            return
        headers = resp.headers
        ct = headers.get("content-type", "")
        host = urlsplit(resp.url).hostname
        rec = {
            "seq": seq, "ts": now_iso(), "method": req.method, "url": self.r.url(resp.url), "status": resp.status,
            "status_text": resp.status_text, "resource_type": req.resource_type, "content_type": ct,
            "headers": self.r.header_map(headers), "host_allowed": self.cfg.host_allowed(host),
            "from_service_worker": resp.from_service_worker,
        }
        want_asset = req.resource_type in self.cfg.save_resource_types and resp.status == 200 and resp.url not in self._assets
        want_api = (req.resource_type in ("xhr", "fetch") and self.cfg.save_api_bodies
                    and any(t in ct.lower() for t in self.cfg.api_body_content_types))
        body = None
        if 300 <= resp.status < 400 or not rec["host_allowed"] or not (want_asset or want_api):
            pass  # redirects have no body; other hosts are metadata-only
        elif int(headers.get("content-length") or 0) > self.cfg.max_body_bytes:
            rec["body_skipped"] = "max_body_bytes"
        else:
            try:
                body = await resp.body()
            except Exception as e:
                rec["body_error"] = str(e)[:300]
        rec["duration_ms"] = round((time.monotonic() - self._t0.get(req, time.monotonic())) * 1000)
        if body is not None and len(body) > self.cfg.max_body_bytes:
            rec["body_skipped"], body = "max_body_bytes", None
        if body is not None and not self.closed and self._budget(len(body)):
            rec["body_size"] = len(body)
            if want_asset:
                if req.resource_type == "document":  # server-rendered HTML carries per-session tokens; static files stay byte-exact
                    body = self.r.body(body, ct or "text/html")
                rec["asset_path"] = self._save_asset(resp.url, ct, req.resource_type, resp.status, body)
            if want_api:
                rel = f"network/api_bodies/{seq:06d}{ext_for(ct)}"
                self.run.write_bytes(rel, self.r.body(body, ct))
                rec["api_body_path"] = rel
                self.counts["api_bodies"] += 1
        elif body is not None:
            rec["body_skipped"] = "max_total_bytes"
        if not self.closed:
            self.counts["responses"] += 1
            self.run.jsonl("network/responses.jsonl", rec)

    def _save_asset(self, url: str, ct: str, rtype: str, status: int, body: bytes) -> str:
        if url in self._assets:  # two in-flight fetches of the same URL
            return self._assets[url]["path"]
        rel = asset_relpath(url, ct)
        if self._paths.get(str(rel).lower(), url) != url:  # lower(): Windows/macOS filenames ignore case
            rel = f"_collisions/{short_hash(url, 16)}{ext_for(ct)}"  # two URLs sanitized to the same name
        try:
            self.run.write_bytes(f"assets/{rel}", body)
        except OSError:  # /a saved as a file then /a/b needs a dir, or the path exceeds Windows' 260-char limit
            rel = f"_collisions/{short_hash(url, 16)}{ext_for(ct)}"
            self.run.write_bytes(f"assets/{rel}", body)
        self._paths[str(rel).lower()] = url
        self._assets[url] = {
            "url": self.r.url(url), "path": f"assets/{rel}", "resource_type": rtype, "content_type": ct,
            "status": status, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        }
        self.counts["assets"] += 1
        return f"assets/{rel}"

    # --- console -------------------------------------------------------------
    def on_console(self, msg) -> None:
        if self.closed or msg.type not in ("error", "warning"):
            return
        self.counts["errors"] += msg.type == "error"
        loc = msg.location or {}
        self.run.jsonl("console.jsonl", {
            "ts": now_iso(), "type": msg.type, "text": self.r.text(msg.text)[:5000],
            "source": self.r.url(loc.get("url", "")), "line": loc.get("lineNumber"),
            "page": self.r.url(msg.page.url) if msg.page else None,
        })

    def on_page_error(self, err) -> None:
        if self.closed:
            return
        self.counts["errors"] += 1
        self.run.jsonl("console.jsonl", {
            "ts": now_iso(), "type": "pageerror", "text": self.r.text(str(err.error))[:5000],
            "page": self.r.url(err.page.url) if err.page else None,
        })

    def tool_error(self, where: str, e: Exception) -> None:
        if not self.closed:
            self.counts["errors"] += 1
            self.run.jsonl("console.jsonl", {"ts": now_iso(), "type": "tool_error", "where": where, "text": self.r.text(str(e))[:2000]})

    # --- pages ---------------------------------------------------------------
    async def snapshot(self, page, label: str = "auto") -> str | None:
        async with self._snap_lock:
            if self.closed or page.is_closed():
                return None
            if self.counts["pages"] >= self.cfg.max_pages:
                self._limit("max_pages")
                return None
            self.counts["pages"] += 1
            d = f"pages/page_{self.counts['pages']:04d}"
            try:
                await page.evaluate(SET_OVERLAY_DISPLAY, "none")
                try:
                    html = await page.content()
                    await page.screenshot(path=self.run.file(f"{d}/viewport.png"))
                    if self.cfg.full_page_screenshot:
                        await page.screenshot(path=self.run.file(f"{d}/full_page.png"), full_page=True)
                finally:
                    await page.evaluate(SET_OVERLAY_DISPLAY, "")
                info = await page.evaluate(EXTRACT_JS)
            except Exception as e:
                self.tool_error(f"snapshot {d}", e)
                return None
            self.run.write_bytes(f"{d}/dom.html", self.r.html(OVERLAY_TAG_RE.sub("", html)).encode())
            for link in info["links"]:
                link["href"] = self.r.url(link["href"])
            for form in info["forms"]:
                form["action"] = self.r.url(form["action"])
            for res in info["resources"]:
                res["url"] = self.r.url(res["url"])
            meta = {
                "label": label, "captured_at": now_iso(), "title": info.pop("title"),
                "requested_url": self.r.url(self._nav_requested.get(page, info["url"])),
                "final_url": self.r.url(info.pop("url")), **info,
                "files": {"dom": "dom.html", "viewport": "viewport.png",
                          "full_page": "full_page.png" if self.cfg.full_page_screenshot else None},
            }
            self.run.write_json(f"{d}/metadata.json", meta)
            self.pages.append({"dir": d, "label": label, "url": meta["final_url"], "title": meta["title"], "captured_at": meta["captured_at"]})
            return d

    # --- finish --------------------------------------------------------------
    def finish(self, manifest: dict) -> None:
        self.closed = True
        self.run.write_json("assets/assets.json", list(self._assets.values()))
        self.run.write_json("manifest.json", {
            **manifest, "started_at": self.started_at, "finished_at": now_iso(), "counts": self.counts,
            "limit_hit": self.limit_hit, "pages": self.pages,
            "files": {
                "assets_index": "assets/assets.json", "requests": "network/requests.jsonl",
                "responses": "network/responses.jsonl", "failures": "network/failures.jsonl",
                "api_bodies": "network/api_bodies/", "console": "console.jsonl", "readme": "README.txt",
            },
            "note": "Network data is OBSERVED traffic only: requests made during this session's visited pages and interactions.",
        })
        (self.run.path / "README.txt").write_text(README_TXT.format(**self.counts, path=self.run.path, limit=self.limit_hit or "none"), encoding="utf-8")
        self.run.close()


README_TXT = """Web Cloner capture: {path}

Contents
  manifest.json              run metadata, counts, page index, file map
  pages/page_NNNN/           dom.html (rendered DOM), viewport.png, full_page.png, metadata.json
                             (links, forms without values, controls, resource inventory)
  assets/<host>/<path>       downloaded frontend files; assets/assets.json maps URL -> local file
  network/requests.jsonl     sanitized request metadata (one JSON object per line, joined to responses by "seq")
  network/responses.jsonl    status, content type, headers, duration, saved body paths
  network/api_bodies/        size-limited, redacted XHR/fetch bodies, named <seq>.<ext>
  network/failures.jsonl     failed requests
  console.jsonl              console errors/warnings, page exceptions, tool errors

Counts: {pages} pages, {requests} requests, {responses} responses, {assets} assets, {api_bodies} API bodies,
        {failures} failures, {errors} errors, {bytes_saved} bytes saved. Limit hit: {limit}.

Limitations
  * This is OBSERVED traffic only. API calls that did not happen during this session are not here.
  * Server-side code, databases and secrets cannot be recovered from a browser; this is frontend/runtime evidence.
  * Only hosts in allowed_hosts have bodies/assets saved; other hosts are recorded as metadata.

Redaction
  Authorization/cookie/CSRF headers, secret-looking query/body keys, bearer tokens and JWTs, password and
  secret-named hidden inputs are replaced with [REDACTED]. Redaction is pattern-based; review before sharing.

Search examples
  jq -c 'select(.resource_type=="fetch" or .resource_type=="xhr") | [.seq,.method,.status,.url]' network/responses.jsonl
  jq -c 'select(.status>=400)' network/responses.jsonl
"""
