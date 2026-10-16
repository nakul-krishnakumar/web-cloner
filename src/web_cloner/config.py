"""Settings. A TOML file may group keys under any [section]; sections are flattened."""

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit


def _l(*v):
    return field(default_factory=lambda: list(v))


@dataclass
class Config:
    url: str = ""
    headless: bool = False
    output_dir: str = "captures"
    profile_dir: str = ""  # opt-in persistent login profile; empty = fresh profile each run
    viewport_width: int = 1366
    viewport_height: int = 900
    full_page_screenshot: bool = True
    # auth
    login_url_patterns: list = _l("*/login*", "*/signin*", "*/sign-in*", "*/sign_in*", "*/oauth/*", "*/sso/*")
    authenticated_url_patterns: list = _l()
    authenticated_selectors: list = _l()
    require_manual_confirm: bool = False
    reload_on_start: bool = True
    # scope
    allowed_hosts: list = _l()  # empty = host of url (and its subdomains)
    save_resource_types: list = _l("document", "stylesheet", "script", "image", "font", "media", "manifest")
    save_api_bodies: bool = True
    api_body_content_types: list = _l("json", "text/", "xml", "graphql", "javascript")
    # limits
    max_body_bytes: int = 5_000_000
    max_total_bytes: int = 500_000_000
    max_requests: int = 20_000
    max_pages: int = 200
    max_duration_s: int = 3600
    # crawler (off by default)
    crawl_enabled: bool = False
    crawl_max_depth: int = 1
    crawl_max_pages: int = 20
    crawl_exclude_patterns: list = _l("logout", "log-out", "signout", "sign-out", "delete", "remove", "destroy", "unsubscribe")
    # redaction
    redact_headers: list = _l()
    redact_keys: list = _l()

    def __post_init__(self):
        if self.url and not self.allowed_hosts:
            self.allowed_hosts = [urlsplit(self.url).hostname or ""]

    def host_allowed(self, host: str | None) -> bool:
        return bool(host) and any(host == h or host.endswith("." + h) for h in self.allowed_hosts)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("profile_dir")
        return d


def load(path: str | None = None, **overrides) -> Config:
    flat = {}
    if path:
        for k, v in tomllib.loads(Path(path).read_text(encoding="utf-8")).items():
            flat.update(v) if isinstance(v, dict) else flat.__setitem__(k, v)
    flat.update({k: v for k, v in overrides.items() if v is not None})
    known = {f.name: f for f in fields(Config)}
    defaults = Config()
    for k, v in flat.items():
        if k not in known:
            raise ValueError(f"unknown config key: {k}")
        if type(v) is not type(getattr(defaults, k)):
            raise ValueError(f"config key {k} must be {type(getattr(defaults, k)).__name__}")
    return Config(**flat)
