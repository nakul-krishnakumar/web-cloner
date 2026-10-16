"""Open a capture offline: every browser request is answered from the capture; nothing reaches the network."""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from .controller import log
from .redaction import REDACTED, Redactor

DROP_HEADERS = {"content-length", "content-encoding", "transfer-encoding"}  # body is stored decoded (and maybe redacted)


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class Replay:
    def __init__(self, run: Path):
        self.run = Path(run)
        self.manifest = json.loads((self.run / "manifest.json").read_text(encoding="utf-8"))
        cfg = self.manifest.get("config", {})
        self.r = Redactor(cfg.get("redact_headers", []), cfg.get("redact_keys", []))
        self.misses: list[str] = []
        posts = {q["seq"]: q.get("post_data") for q in _jsonl(self.run / "network/requests.jsonl")}
        self.index = {}
        for rec in _jsonl(self.run / "network/responses.jsonl"):
            body = rec.get("api_body_path") or rec.get("asset_path")
            location = rec["headers"].get("location") if rec["status"] in (301, 302, 303, 307, 308) else None
            if body or location:
                # Captured URLs/bodies are redacted, so live requests are redacted the same way before lookup.
                self.index.setdefault((rec["method"], rec["url"], posts.get(rec["seq"])), {**rec, "body": body, "location": location})

    def start_url(self) -> str:
        pages = self.manifest.get("pages") or []
        return pages[0]["url"] if pages else self.manifest["start_url"]

    async def attach(self, context) -> None:
        await context.route("**/*", self._handle)

    async def _handle(self, route) -> None:
        req = route.request
        post = self.r.post_data(req.post_data, req.headers.get("content-type", ""))
        hit = self.index.get((req.method, self.r.url(req.url), post))
        if hit is None:
            # shortcut: exact URL match only, so cache-busting params (?_=timestamp) miss; add normalization if a site needs it
            if req.url not in self.misses:
                self.misses.append(req.url)
                log(f"not captured: {req.method} {self.r.url(req.url)}")
            return await route.fulfill(status=404, content_type="text/plain", body=f"web-cloner replay: not captured\n{self.r.url(req.url)}\n")
        headers = {k: v for k, v in hit["headers"].items() if v != REDACTED and k.lower() not in DROP_HEADERS}
        if hit["location"]:
            return await route.fulfill(status=hit["status"], headers=headers)
        await route.fulfill(status=hit["status"], headers=headers, path=self.run / hit["body"])


async def run(run_dir: str, url: str | None, headless: bool) -> list[str]:
    rp = Replay(Path(run_dir))
    cfg = rp.manifest.get("config", {})
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        # service workers would answer requests outside our route handler
        ctx = await browser.new_context(service_workers="block",
                                        viewport={"width": cfg.get("viewport_width", 1366), "height": cfg.get("viewport_height", 900)})
        await rp.attach(ctx)
        page = await ctx.new_page()
        closed = asyncio.Event()
        browser.on("disconnected", lambda *_: closed.set())
        ctx.on("page", lambda p: p.on("close", lambda *_: ctx.pages or closed.set()))
        page.on("close", lambda *_: ctx.pages or closed.set())
        await page.goto(url or rp.start_url())
        log("Replaying offline. Close the browser to exit.")
        await closed.wait()
        await browser.close()
    return rp.misses


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="web-cloner replay", description="Browse a capture offline, exactly as recorded.")
    ap.add_argument("capture_dir")
    ap.add_argument("--url", help="URL to open (default: first captured page)")
    args = ap.parse_args(argv)
    try:
        misses = asyncio.run(run(args.capture_dir, args.url, headless=False))
    except KeyboardInterrupt:
        return
    print(f"{len(misses)} requests were not in the capture" if misses else "Every request was served from the capture.")
