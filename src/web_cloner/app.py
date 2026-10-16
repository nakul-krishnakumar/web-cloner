"""CLI entry point: `uv run web-cloner URL [-c config.toml]`."""

import argparse
import asyncio
import sys
import threading
from urllib.parse import urlsplit

from .config import load
from .controller import Session, log

HELP = "Terminal controls (fallback for the in-browser panel): confirm | start | checkpoint <name> | stop | finish | status"


def _stdin_commands(loop: asyncio.AbstractEventLoop, session: Session) -> None:
    for line in sys.stdin:
        cmd, _, arg = line.strip().partition(" ")
        if not cmd:
            continue
        fut = asyncio.run_coroutine_threadsafe(session.action("state" if cmd == "status" else cmd, arg), loop)
        try:
            s = fut.result(timeout=10)
            log(f"{s['state']}: {s['message']}" + (f" | {s['counts']}" if s["counts"] else ""))
        except Exception as e:
            log(f"command failed: {e}")


async def run(session: Session) -> None:
    try:
        await session.open()
        log(HELP)
        threading.Thread(target=_stdin_commands, args=(asyncio.get_running_loop(), session), daemon=True).start()
        await session.closed.wait()
    finally:
        await session.finish(close=True)  # also runs on Ctrl-C so captured data is flushed


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["replay"]:
        from .replay import main as replay_main
        return replay_main(argv[1:])
    ap = argparse.ArgumentParser(prog="web-cloner", description="Capture a site's frontend and observed traffic after you log in manually.")
    ap.add_argument("url", nargs="?", help="starting URL (or set url in the config file)")
    ap.add_argument("-c", "--config", help="TOML config file (see config.example.toml)")
    ap.add_argument("-o", "--output-dir", help="where capture directories are created (default: captures)")
    ap.add_argument("--profile-dir", help="opt-in: keep the login between runs in this browser profile dir (holds session cookies)")
    ap.add_argument("--crawl", action="store_true", default=None, help="enable the same-origin link crawler")
    ap.add_argument("--no-reload", dest="reload_on_start", action="store_false", default=None,
                    help="don't reload the current page after Start Scraping")
    args = ap.parse_args(argv)
    try:
        cfg = load(args.config, url=args.url, output_dir=args.output_dir, profile_dir=args.profile_dir,
                   crawl_enabled=args.crawl, reload_on_start=args.reload_on_start)
    except (OSError, ValueError) as e:
        ap.error(str(e))
    if urlsplit(cfg.url).scheme not in ("http", "https"):
        ap.error("a http(s) url is required")
    session = Session(cfg)
    try:
        asyncio.run(run(session))
    except KeyboardInterrupt:
        pass
    print(session.output or "No capture written.")
