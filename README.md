# web-cloner

This tool captures a website's rendered frontend and the browser traffic it observes, so you can rebuild the frontend. You log in yourself in a visible browser, and nothing is recorded until you click **Start Scraping**.

> Use it only on sites you own or are authorized to analyze. It does not bypass logins, CAPTCHA or MFA, and it does not replay or probe APIs.

## Setup and run (nothing preinstalled)

Unzip the folder and run the launcher. It needs no Python, uv or Playwright.

**Windows:** double-click `run.cmd` and paste the URL when it asks. Or, from a terminal in this folder:

```bat
run.cmd "https://app.example.com/dashboard"     :: keep the quotes: URLs contain &
run.cmd replay captures\<host>_<timestamp>       :: replay offline
run.cmd -c config.toml                          :: copy config.example.toml first
```

If Windows shows "Windows protected your PC" for a downloaded zip, right-click the zip, choose **Properties**, tick **Unblock**, and then extract it.

**Linux / macOS:** `./run.sh "<url>"`, `./run.sh replay captures/<dir>`.

The first run needs internet and takes about a minute. It downloads uv, Python 3.13, the dependencies and Chromium (about 700 MB in total) into `.uv/` and `.browsers/` *inside this folder*. Nothing is installed system-wide. To uninstall, delete the folder. Later runs start immediately. Captures go to `captures/` in this folder.

On a minimal Linux server image, Chromium may fail to start because system libraries are missing. Fix it once with `sudo .uv/uv run playwright install-deps chromium`.

To share the tool, zip it without the downloaded parts:
`zip -r web-cloner.zip . -x '.venv/*' '.uv/*' '.browsers/*' 'captures/*' 'profiles/*' '*__pycache__*' '.pytest_cache/*' '.git/*'`

**Developers** with uv already installed can use it directly: `uv sync && uv run playwright install chromium && uv run web-cloner URL`.

1. A Chromium window opens on the URL. A **Web Cloner** panel sits in the bottom-right corner and reads *Waiting for login*. Capture is off.
2. Log in normally. The panel switches to *Ready to scrape* when it detects the login:
   - the URL is not a login URL, and
   - an `authenticated_url_patterns` pattern or an `authenticated_selectors` element matches.
   If you configured neither, it can't decide, so click **I'm signed in**.
3. Click **Start Scraping**. The output directory is created now; nothing is written before this.
   - If `reload_on_start` is on, the current page reloads so its load-time API calls are recorded.
   - If `--crawl` is on, the crawler starts.
4. Browse and interact as you normally would. Each page navigation in any tab or popup is snapshotted. For in-page states that a navigation won't catch (an open menu, a modal, a tab), type a name and click **Checkpoint**.
5. Click **Stop Scraping** to save and keep the browser open, or **Finish & Save** to save and close. Closing the browser or pressing Ctrl-C also saves. The output path is printed.

If the panel can't show (a hostile CSP, for example), type commands in the terminal instead: `confirm`, `start`, `checkpoint <name>`, `stop`, `finish`, `status`.

**Session expiry:** if a navigation goes to a login URL during capture, capture pauses (*Paused*). Sign in again, then click **Start Scraping** to resume into the same output directory.

**Safety limits:** `max_requests`, `max_total_bytes` and `max_duration_s` pause capture when they are reached. `max_pages` stops new snapshots. When a limit pauses capture, click **Finish & Save**.

## Output

```
captures/<host>_<YYYYMMDD_HHMMSS>/
├── manifest.json            # run metadata, counts, page index, file map, sanitized config
├── README.txt               # summary, limitations, redaction note, jq examples
├── pages/page_NNNN/         # dom.html (rendered DOM), viewport.png, full_page.png, metadata.json
├── assets/<host>/<path>     # css/js/images/fonts/html; query strings become __<hash> suffixes
├── assets/assets.json       # original URL (redacted) -> local path, type, size, sha256
├── network/requests.jsonl   # one sanitized request per line, keyed by "seq"
├── network/responses.jsonl  # status, content-type, headers, duration, saved body paths
├── network/api_bodies/      # XHR/fetch bodies, <seq>.<ext>, redacted and size-limited
├── network/failures.jsonl
└── console.jsonl            # console errors/warnings, page exceptions, tool errors
```

`metadata.json` holds the links, forms (field names and types, never values), visible controls and the browser's resource inventory.

To search API traffic:

```bash
jq -c 'select(.resource_type=="fetch" or .resource_type=="xhr") | [.method,.status,.content_type,.url]' network/responses.jsonl
```

## Replay a capture offline

```bash
uv run web-cloner replay captures/<host>_<timestamp>            # opens the first captured page
uv run web-cloner replay captures/<host>_<timestamp> --url https://site/table
```

Replay opens a browser in which every request (page, asset or API call) is answered from the capture, so nothing reaches the network. Buttons, pagination and links therefore work for anything you clicked during capture. Anything you didn't click shows "not captured", and the terminal lists it.

**Capturing a paginated table with detail pages:** after **Start Scraping**, click **Next** a few times and open a few row links (new tabs are captured automatically). You need a representative sample of each page *type*, not every row. Each new tab and each URL change is snapshotted after a short settle delay. Use **Checkpoint** for states that don't change the URL.

## Configuration

Every option is documented in [config.example.toml](config.example.toml). A misspelled or wrongly typed key is an error.

## What gets redacted

The following are replaced with `[REDACTED]` before anything is written:

- these headers: `Authorization`, `Cookie`, `Set-Cookie`, CSRF/XSRF headers, API-key headers, and any header whose name looks secret;
- query, fragment, JSON and form keys that match password, token, secret, session, csrf, auth, signature, api key, `code`, `state`, and so on;
- bearer/basic tokens and JWTs in free text;
- `value`/`content` on password inputs and on inputs or meta tags with secret-looking names, both in `dom.html` and in saved HTML documents.

Static JS, CSS and image assets are saved byte-for-byte. Redaction matches patterns; it is not a guarantee, so review a capture before you share it. Browser profiles, storage state and HAR files are never written to the capture directory.

## Limitations

- **Observed traffic only.** The capture contains only the requests made during your session. An API call that a page or interaction you never visited would make is not in it.
- No backend code, database, server secrets or server-side logic can be recovered from a browser. This is frontend and runtime evidence.
- Bodies and assets are saved only for `allowed_hosts`. Other hosts (CDNs, analytics) are recorded as metadata; add them to `allowed_hosts` to keep their files.
- A client-side (SPA) route change is snapshotted after a fixed 0.5 s settle delay. Use **Checkpoint** for UI that renders late.
- Auth expiry is detected by navigation to a login URL. A site that only returns 401s from its API, without redirecting, will not pause capture.

## Development

```bash
uv run pytest        # unit tests + end-to-end tests against a local mock site (headless)
```
