"""End-to-end against the local mock site (headless for CI; the CLI default is headed)."""

import asyncio
import json
import urllib.request

import pytest

from web_cloner.config import Config
from web_cloner.controller import Session, State

from .site import Site

SECRETS = [b"QUERYSECRET", b"BEARERSECRET", b"CSRFSECRET", b"BODYSECRET", b"SIDSECRET", b"hunter2"]


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


def make(site, tmp_path, **kw) -> Session:
    cfg = Config(url=site.url + "/app", headless=True, output_dir=str(tmp_path / "captures"),
                 authenticated_selectors=["#avatar"], **kw)
    return Session(cfg, poll_interval=0.2)


async def until(cond, timeout=15):
    for _ in range(int(timeout / 0.1)):
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met in time")


async def login(page):
    await page.fill("input[name=user]", "me")
    await page.fill("input[name=password]", "hunter2")
    await page.click("button[type=submit]")


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_files(run):
    return [p for p in run.rglob("*") if p.is_file()]


def test_full_flow(site, tmp_path):
    async def main():
        s = make(site, tmp_path, crawl_enabled=True, crawl_max_depth=1)
        await s.open()
        page = s.context.pages[0]
        out = tmp_path / "captures"
        try:
            await page.wait_for_url("**/login")
            await asyncio.sleep(0.6)
            assert s.state is State.WAITING_LOGIN
            assert (await s.action("start"))["state"] == "waiting_login"  # rejected before auth
            start_btn = page.locator("[data-wc-action=start]")
            assert await start_btn.is_disabled()
            await login(page)
            await until(lambda: s.state is State.READY)
            assert not out.exists() or not any(out.iterdir())  # ready, but nothing persisted
            await until(lambda: page.url.endswith("/app"))
            await page.wait_for_function("document.getElementById('__web_cloner_overlay')")
            await page.wait_for_selector("[data-wc-action=start]:not([disabled])")
            await start_btn.click()  # real click on the in-page overlay
            await asyncio.gather(*(s.action("start") for _ in range(5)))  # repeats are rejected
            assert s.state is State.SCRAPING
            assert len(list(out.iterdir())) == 1
            await until(lambda: "/app/page2" in site.hits and s.capture.counts["pages"] >= 2)
            await until(lambda: s.capture.counts["api_bodies"] >= 1)
            await page.fill("input[aria-label='Checkpoint name']", "menu open")
            await page.click("[data-wc-action=checkpoint]")
            await until(lambda: any(p["label"] == "checkpoint:menu open" for p in s.capture.pages))
            await page.click("[data-wc-action=stop]")
            await until(lambda: s.output is not None)
            assert s.state is State.FINISHED
            assert all(not s.context._impl_obj.listeners(ev) for ev in s._handlers)  # handlers detached
        finally:
            await s.finish(close=True)
        return s

    s = asyncio.run(main())
    run = next((tmp_path / "captures").iterdir())
    reqs = jsonl(run / "network/requests.jsonl")
    resps = jsonl(run / "network/responses.jsonl")
    urls = [r["url"] for r in reqs]
    assert not any("/login" in u for u in urls), "login flow must not be recorded"
    app_loads = [r for r in resps if r["url"].endswith("/app") and r["resource_type"] == "document"]
    assert app_loads and app_loads[0]["status"] == 200  # post-click reload, still authenticated
    assert any("/api/data" in u for u in urls)
    assert "/logout" not in site.hits and not any("example.invalid" in u for u in urls)  # crawler exclusions
    assets = json.loads((run / "assets/assets.json").read_text())
    assert {"stylesheet", "script", "document"} <= {a["resource_type"] for a in assets}
    for a in assets:
        assert (run / a["path"]).stat().st_size == a["size"]
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["counts"]["pages"] >= 3 and "OBSERVED" in manifest["note"]
    page1 = run / manifest["pages"][0]["dir"]
    assert (page1 / "viewport.png").exists() and (page1 / "full_page.png").exists()
    dom = (page1 / "dom.html").read_text()
    assert "__web_cloner_overlay" not in dom and "<h1 data-items=\"3\">" in dom  # rendered DOM, not server HTML
    assert any(c["type"] == "error" for c in jsonl(run / "console.jsonl"))
    for f in run_files(run):
        data = f.read_bytes()
        for secret in SECRETS:
            assert secret not in data, f"{secret} leaked into {f}"


def test_auth_expiry_pauses_and_resumes(site, tmp_path):
    async def main():
        s = make(site, tmp_path, reload_on_start=False)
        await s.open()
        page = s.context.pages[0]
        try:
            await page.wait_for_url("**/login")
            await login(page)
            await until(lambda: s.state is State.READY)
            await s.action("start", page=page)
            assert s.state is State.SCRAPING
            urllib.request.urlopen(site.url + "/logout").read()  # server-side expiry
            await page.goto(site.url + "/app")
            await until(lambda: s.state is State.PAUSED)
            assert s.reason == "auth"
            assert (await s.action("start"))["state"] == "paused"
            await login(page)
            await until(lambda: s.state is State.READY)
            await s.action("start", page=page)
            assert s.state is State.SCRAPING
        finally:
            await s.finish(close=True)
        return s

    s = asyncio.run(main())
    reqs = jsonl(s.capture.run.path / "network/requests.jsonl")
    assert not any("/login" in r["url"] for r in reqs)


def test_limits_and_allowed_hosts(site, tmp_path):
    async def main():
        s = make(site, tmp_path, allowed_hosts=["example.invalid"], max_requests=3, max_duration_s=60)
        await s.open()
        page = s.context.pages[0]
        try:
            await page.wait_for_url("**/login")
            await login(page)
            await until(lambda: s.state is State.READY)
            await s.action("start", page=page)
            await until(lambda: s.state is State.PAUSED)
            assert s.reason == "limit" and s.capture.limit_hit == "max_requests"
        finally:
            await s.finish(close=True)
        return s

    s = asyncio.run(main())
    run = s.capture.run.path
    assert len(jsonl(run / "network/requests.jsonl")) == 3
    assert json.loads((run / "assets/assets.json").read_text()) == []  # host not allowed: metadata only


def test_duration_and_body_size_limits(site, tmp_path):
    async def main():
        s = make(site, tmp_path, max_body_bytes=10, max_duration_s=2)
        await s.open()
        page = s.context.pages[0]
        try:
            await page.wait_for_url("**/login")
            await login(page)
            await until(lambda: s.state is State.READY)
            await s.action("start", page=page)
            await until(lambda: s.state is State.PAUSED, timeout=10)
            assert s.capture.limit_hit == "max_duration_s"
        finally:
            await s.finish(close=True)
        return s

    s = asyncio.run(main())
    resps = jsonl(s.capture.run.path / "network/responses.jsonl")
    assert resps and all("body_size" not in r for r in resps)
    assert any(r.get("body_skipped") == "max_body_bytes" for r in resps)


def test_manual_confirm_when_detection_inconclusive(site, tmp_path):
    async def main():
        s = Session(Config(url=site.url + "/public", headless=True, output_dir=str(tmp_path / "c")), poll_interval=0.2)
        await s.open()
        try:
            await asyncio.sleep(1)
            assert s.state is State.WAITING_LOGIN  # no signals configured: never guesses
            page = s.context.pages[0]
            await page.click("[data-wc-action=confirm]")
            await until(lambda: s.state is State.READY)
            # synthetic clicks from page scripts are ignored, and a forged token is refused
            await page.evaluate("document.getElementById('__web_cloner_overlay').shadowRoot.querySelector('[data-wc-action=start]').click()")
            assert await page.evaluate("window.__webClonerControl('guess', 'start')") == {"error": "forbidden"}
            await asyncio.sleep(0.5)
            assert s.state is State.READY
        finally:
            await s.finish(close=True)
        assert s.output is None and not (tmp_path / "c").exists()

    asyncio.run(main())


def test_table_pagination_popups_and_offline_replay(site, tmp_path):
    from web_cloner.replay import Replay

    async def capture():
        s = make(site, tmp_path, reload_on_start=False)
        await s.open()
        page = s.context.pages[0]
        try:
            await page.wait_for_url("**/login")
            await login(page)
            await until(lambda: s.state is State.READY)
            await s.action("start", page=page)
            await page.goto(site.url + "/app/table")
            await page.wait_for_selector("text=row 1")
            for n in (2, 3):  # user clicks Next twice (JS button + pushState), at human speed
                await asyncio.sleep(1)
                await page.click("#next")
                await page.wait_for_selector(f"text=page {n}")
            async with page.expect_popup() as pop:  # row link opens a new tab
                await page.click("a[href='/app/item/7']")
            await (await pop.value).wait_for_load_state()
            await until(lambda: any(p["url"].endswith("/app/item/7") for p in s.capture.pages))
            await until(lambda: sum("/app/table" in p["url"] for p in s.capture.pages) >= 3)  # SPA route changes snapshotted
        finally:
            await s.finish(close=True)
        return s.capture.run.path

    run = asyncio.run(capture())
    site.close()  # replay must not need the live site

    async def replay():
        from playwright.async_api import async_playwright
        rp = Replay(run)
        async with async_playwright() as pw:
            b = await pw.chromium.launch()
            ctx = await b.new_context(service_workers="block")
            await rp.attach(ctx)
            page = await ctx.new_page()
            await page.goto(site.url + "/app/table")
            await page.wait_for_selector("text=row 1")
            for n in (2, 3):
                await page.click("#next")
                await page.wait_for_selector(f"text=row {(n - 1) * 3 + 1}")
            async with page.expect_popup() as pop:
                await page.click("a[href='/app/item/7']")
            item = await pop.value
            await item.wait_for_selector("text=Item 7")
            await page.click("#next")  # page 4 was never visited during capture
            await until(lambda: any("page=4" in m for m in rp.misses))
            await b.close()

    asyncio.run(replay())
