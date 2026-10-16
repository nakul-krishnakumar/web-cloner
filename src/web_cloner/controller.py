"""Browser session + the single capture state machine. Every browser event passes the gate in here."""

import asyncio
import json
import secrets
import sys
import time
from collections import deque
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

from playwright.async_api import async_playwright

from .capture import Capture
from .config import Config
from .redaction import Redactor
from .storage import RunDir

OVERLAY_JS = (Path(__file__).parent / "overlay.js").read_text(encoding="utf-8")


class State(StrEnum):
    WAITING_LOGIN = "waiting_login"
    READY = "ready"
    SCRAPING = "scraping"
    PAUSED = "paused"
    FINISHED = "finished"
    ERROR = "error"


ALLOWED = {
    State.WAITING_LOGIN: {State.READY, State.FINISHED, State.ERROR},
    State.READY: {State.SCRAPING, State.WAITING_LOGIN, State.FINISHED, State.ERROR},
    State.SCRAPING: {State.PAUSED, State.FINISHED, State.ERROR},
    State.PAUSED: {State.READY, State.FINISHED, State.ERROR},
    State.ERROR: {State.FINISHED},
    State.FINISHED: {State.ERROR},
}


class InvalidTransition(Exception):
    pass


def log(msg: str) -> None:
    print(f"[web-cloner] {msg}", file=sys.stderr, flush=True)


class Session:
    def __init__(self, cfg: Config, poll_interval: float = 1.0):
        self.cfg = cfg
        self.poll_interval = poll_interval
        self.state, self.reason, self.message = State.WAITING_LOGIN, None, ""
        self.redactor = Redactor(cfg.redact_headers, cfg.redact_keys)
        self.capture: Capture | None = None
        self.output: str | None = None
        self.token = secrets.token_urlsafe(32)
        self.closed = asyncio.Event()
        self.context = None
        self._pw = self._browser = None
        self._tasks: set[asyncio.Task] = set()
        self._confirmed = False
        self._started_once = False
        self._last_snap = {}
        self._crawler_pages = set()
        self._page_handlers = []
        self._handlers = {
            "request": self._on_request, "response": self._on_response, "requestfailed": self._on_failed,
            "console": self._on_console, "weberror": self._on_weberror, "page": self._on_page,
        }

    # --- state machine -------------------------------------------------------
    def _to(self, new: State, reason: str | None = None, message: str = "") -> None:
        if new not in ALLOWED[self.state]:
            raise InvalidTransition(f"{self.state} -> {new}")
        self.state, self.reason, self.message = new, reason, message
        if new is State.WAITING_LOGIN:
            self._confirmed = False
        log(f"{new.value}{': ' + message if message else ''}")

    def status(self, error: str | None = None) -> dict:
        c = self.capture
        return {
            "state": self.state.value, "reason": self.reason, "message": error or self.message,
            "elapsed": round(time.monotonic() - c.started) if c else 0, "counts": c.counts if c else None,
            "output": str(c.run.path) if c else None,
        }

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception():
            log(f"background task failed: {t.exception()!r}")
            if self.capture:
                self.capture.tool_error("task", t.exception())

    # --- lifecycle -----------------------------------------------------------
    async def open(self) -> None:
        cfg = self.cfg
        self._pw = await async_playwright().start()
        vp = {"width": cfg.viewport_width, "height": cfg.viewport_height}
        if cfg.profile_dir:
            prof = Path(cfg.profile_dir).expanduser().resolve()
            if prof.is_relative_to(Path(cfg.output_dir).resolve()):
                raise ValueError("profile_dir must not be inside output_dir")
            prof.mkdir(parents=True, exist_ok=True)
            prof.chmod(0o700)
            log(f"Using persistent profile {prof}: it holds session cookies. Keep it private; delete it to log out.")
            self.context = await self._pw.chromium.launch_persistent_context(prof, headless=cfg.headless, viewport=vp)
        else:
            self._browser = await self._pw.chromium.launch(headless=cfg.headless)
            self.context = await self._browser.new_context(viewport=vp)
        await self.context.expose_binding("__webClonerControl", self._binding)
        await self.context.add_init_script(OVERLAY_JS.replace("__WC_TOKEN__", json.dumps(self.token)))
        for ev, h in self._handlers.items():
            self.context.on(ev, h)
        self.context.on("close", self._on_context_close)
        for p in self.context.pages:
            self._on_page(p)
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self._poll_task = asyncio.create_task(self._poll())
        self._to_message("Sign in normally in the browser. Capture is OFF.")
        try:
            await page.goto(cfg.url)
        except Exception as e:
            log(f"initial navigation failed ({e}); navigate manually in the browser")

    def _to_message(self, msg: str) -> None:
        self.message = msg
        log(msg)

    async def finish(self, close: bool = False) -> None:
        if self.state is not State.FINISHED:
            self._to(State.FINISHED, message="Saving…")
            if self.context:
                for ev, h in self._handlers.items():
                    self.context.remove_listener(ev, h)
            for page, ev, h in self._page_handlers:
                page.remove_listener(ev, h)
            self._page_handlers.clear()
            pending = [t for t in self._tasks if t is not asyncio.current_task()]
            if pending:
                _, late = await asyncio.wait(pending, timeout=30)
                for t in late:
                    t.cancel()
            if self.capture:
                try:
                    self.capture.finish({"tool": "web-cloner", "start_url": self.redactor.url(self.cfg.url),
                                         "browser": "chromium " + (self._browser.version if self._browser else ""),
                                         "config": self.redactor.obj(self.cfg.public())})
                except Exception as e:
                    self._to(State.ERROR, message=f"Saving failed: {e}")
                    raise
                self.output = str(self.capture.run.path)
                self._to_message(f"Saved to {self.output}")
            else:
                self._to_message("Nothing was captured (Start Scraping was never clicked).")
        if close:
            await self._close()

    async def _close(self) -> None:
        for closer in (self.context and self.context.close, self._browser and self._browser.close, self._pw and self._pw.stop):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass
        self.closed.set()

    def _on_context_close(self, *_):
        self._spawn(self.finish(close=True))  # user closed the browser: save what we have

    # --- controls (overlay binding / terminal) --------------------------------
    async def _binding(self, source, token, action, arg=None):
        if not secrets.compare_digest(str(token), self.token):
            return {"error": "forbidden"}
        return await self.action(action, arg, source.get("page"))

    async def action(self, action: str, arg: str | None = None, page=None) -> dict:
        if action == "confirm":
            if self.state is State.WAITING_LOGIN or (self.state is State.PAUSED and self.reason == "auth"):
                self._confirmed = True
                self._to(State.READY, message="Login confirmed by you. Capture is OFF until you click Start Scraping.")
        elif action == "start":
            if self.state is not State.READY:
                return self.status(error=f"Start Scraping is only available when ready (now: {self.state.value}).")
            if self.capture is None:
                try:
                    run = RunDir(Path(self.cfg.output_dir), urlsplit(self.cfg.url).hostname or "site")
                    self.capture = Capture(self.cfg, run, self.redactor)
                except Exception as e:
                    self._to(State.ERROR, message=f"Cannot create output directory: {e}")
                    return self.status()
            self._to(State.SCRAPING, message="Recording observed traffic. Navigate and interact normally.")
            first, self._started_once = not self._started_once, True
            self._spawn(self._after_start(page, first))
        elif action == "checkpoint":
            if self.state is State.SCRAPING:
                target = page or self._pages()[-1]
                self._spawn(self.capture.snapshot(target, f"checkpoint:{(arg or '').strip()[:80] or 'unnamed'}"))
        elif action in ("stop", "finish"):
            self._spawn(self.finish(close=action == "finish"))
        elif action != "state":
            return self.status(error=f"unknown action {action!r}")
        return self.status()

    # --- auth detection ------------------------------------------------------
    def _url_signal(self, url: str) -> bool | None:
        p = urlsplit(url)
        if p.scheme not in ("http", "https"):
            return None
        bare = f"{p.scheme}://{p.netloc}{p.path}".lower()  # ignore ?next=/login style queries; case-insensitive
        if any(fnmatchcase(bare, pat.lower()) for pat in self.cfg.login_url_patterns):
            return False
        if any(fnmatchcase(bare, pat.lower()) for pat in self.cfg.authenticated_url_patterns):
            return True
        return None

    async def _detect(self, page) -> bool | None:
        sig = self._url_signal(page.url)
        if sig is not None:
            return sig
        for sel in self.cfg.authenticated_selectors:
            try:
                if await page.locator(sel).first.is_visible():
                    return True
            except Exception:
                pass
        return None  # inconclusive: user must confirm

    def _pages(self):
        return [p for p in self.context.pages if not p.is_closed()]

    def _pause_auth(self) -> None:
        self._to(State.PAUSED, "auth", "Signed out. Sign in again, then click Start Scraping to resume.")

    async def _poll(self) -> None:
        while self.state is not State.FINISHED:
            await asyncio.sleep(self.poll_interval)
            try:
                await self._check()
            except Exception as e:
                log(f"state check failed: {e}")

    async def _check(self) -> None:
        if self.state is State.SCRAPING:
            c = self.capture
            if time.monotonic() - c.started > self.cfg.max_duration_s:
                c._limit("max_duration_s")
            if c.limit_hit:
                self._to(State.PAUSED, "limit", f"Safety limit reached ({c.limit_hit}). Click Finish & Save.")
            elif any(self._url_signal(p.url) is False for p in self._pages()):
                self._pause_auth()
        elif self.state is State.WAITING_LOGIN or (self.state is State.PAUSED and self.reason == "auth"):
            if not self.cfg.require_manual_confirm and True in [await self._detect(p) for p in self._pages()]:
                self._to(State.READY, message="Login detected. Capture is OFF until you click Start Scraping.")
        elif self.state is State.READY and not self._confirmed:
            sigs = [await self._detect(p) for p in self._pages()]
            if True not in sigs and False in sigs:
                self._to(State.WAITING_LOGIN, message="Login no longer detected. Capture is OFF.")

    # --- gated browser events ------------------------------------------------
    def _on_request(self, req) -> None:
        if self.state is not State.SCRAPING:
            return
        if req.is_navigation_request() and self._url_signal(req.url) is False:
            return self._pause_auth()  # session expired: don't record the login flow
        self.capture.on_request(req)

    def _on_response(self, resp) -> None:
        if self.state is State.SCRAPING:
            self._spawn(self.capture.on_response(resp))

    def _on_failed(self, req) -> None:
        if self.state is State.SCRAPING:
            self.capture.on_failed(req)

    def _on_console(self, msg) -> None:
        if self.state is State.SCRAPING:
            self.capture.on_console(msg)

    def _on_weberror(self, err) -> None:
        if self.state is State.SCRAPING:
            self.capture.on_page_error(err)

    def _on_page(self, page) -> None:
        def on_nav(frame):
            if frame is page.main_frame and self.state is State.SCRAPING and page not in self._crawler_pages:
                self._spawn(self._auto_snapshot(page))

        def on_close(_):
            if not self._pages() and self.state is not State.FINISHED:
                self._spawn(self.finish(close=True))  # last tab closed

        for ev, h in (("framenavigated", on_nav), ("close", on_close)):
            page.on(ev, h)
            self._page_handlers.append((page, ev, h))
        if self.state is State.SCRAPING:  # popups/new tabs often navigate before this event fires
            self._spawn(self._auto_snapshot(page))

    async def _auto_snapshot(self, page) -> None:
        try:
            await page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(0.5)  # shortcut: fixed settle delay for client-side rendering; use Checkpoint for late UI
        url = page.url
        if (self.state is not State.SCRAPING or page.is_closed() or page in self._crawler_pages
                or not url.startswith("http") or self._url_signal(url) is False):
            return
        last = self._last_snap.get(page)
        if last and last[0] == url and time.monotonic() - last[1] < 2:
            return
        self._last_snap[page] = (url, time.monotonic())
        await self.capture.snapshot(page)

    async def _after_start(self, page, first: bool) -> None:
        pages = [p for p in self._pages() if p.url != "about:blank"]
        for p in pages:
            self._last_snap[p] = (p.url, time.monotonic())
            await self.capture.snapshot(p, "start")
        target = page if page and not page.is_closed() else (pages[0] if pages else None)
        if not (first and target):
            return
        if self.cfg.reload_on_start and self.state is State.SCRAPING:
            await target.reload(wait_until="load")  # listeners are live now: records the authenticated page load
        if self.cfg.crawl_enabled and self.state is State.SCRAPING:
            await self._crawl(target.url)

    async def _crawl(self, start_url: str) -> None:
        """Optional BFS over GET links within allowed_hosts. Never clicks or submits anything."""
        page = await self.context.new_page()
        self._crawler_pages.add(page)
        seen, queue, visited = {urldefrag(start_url)[0]}, deque([(start_url, 0)]), 0
        try:
            while queue and self.state is State.SCRAPING and visited < self.cfg.crawl_max_pages:
                url, depth = queue.popleft()
                try:
                    await page.goto(url, wait_until="load", timeout=30000)
                except Exception as e:
                    self.capture.tool_error(f"crawl {self.redactor.url(url)}", e)
                    continue
                if self.state is not State.SCRAPING:
                    break
                if depth:
                    visited += 1
                    await self.capture.snapshot(page, f"crawl:depth{depth}")
                if depth >= self.cfg.crawl_max_depth:
                    continue
                for link in await page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)"):
                    link = urldefrag(link)[0]
                    p = urlsplit(link)
                    if (link not in seen and p.scheme in ("http", "https") and self.cfg.host_allowed(p.hostname)
                            and self._url_signal(link) is not False
                            and not any(x in link.lower() for x in self.cfg.crawl_exclude_patterns)):
                        seen.add(link)
                        queue.append((link, depth + 1))
        finally:
            if not page.is_closed():
                await page.close()
