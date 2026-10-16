import json

import pytest

from web_cloner.config import Config, load
from web_cloner.controller import ALLOWED, State
from web_cloner.redaction import REDACTED, Redactor
from web_cloner.storage import RunDir, asset_relpath

r = Redactor(extra_keys=["my_secret_param"])


def test_defaults_are_headed_and_crawl_off():
    c = Config(url="https://app.example.com/x")
    assert c.headless is False and c.crawl_enabled is False
    assert c.allowed_hosts == ["app.example.com"]
    assert c.host_allowed("cdn.app.example.com") and not c.host_allowed("evilapp.example.com.attacker.net")


def test_load_rejects_unknown_and_mistyped_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('url = "https://a.b"\n[limits]\nmax_pages = 3\n')
    assert load(str(p)).max_pages == 3
    p.write_text("[limits]\nmax_pagez = 3\n")
    with pytest.raises(ValueError):
        load(str(p))
    p.write_text('[limits]\nmax_pages = "3"\n')
    with pytest.raises(ValueError):
        load(str(p))


def test_redact_headers():
    h = r.header_map({"Authorization": "Bearer x", "Cookie": "sid=1", "Set-Cookie": "a=b", "X-CSRF-Token": "t",
                      "X-Session-Id": "s", "Content-Type": "text/html", "Accept": "*/*"})
    assert h["Content-Type"] == "text/html" and h["Accept"] == "*/*"
    assert all(h[k] == REDACTED for k in ("Authorization", "Cookie", "Set-Cookie", "X-CSRF-Token", "X-Session-Id"))


def test_redact_url():
    u = r.url("https://user:pw@h.com/cb?code=abc&page=2&access_token=t&my_secret_param=z#id_token=j&x=1")
    assert "abc" not in u and "pw" not in u and "=t" not in u and "=z" not in u and "=j" not in u
    assert "page=2" in u and "x=1" in u and u.startswith("https://h.com/cb?")


def test_redact_bodies_and_text():
    body = json.loads(r.body(b'{"password":"p","nested":{"csrfToken":"c","ok":1},"list":[{"apiKey":"k"}],"n":"x"}', "application/json"))
    assert body == {"password": REDACTED, "nested": {"csrfToken": REDACTED, "ok": 1}, "list": [{"apiKey": REDACTED}], "n": "x"}
    assert b"password=p" not in r.body(b"user=a&password=p", "application/x-www-form-urlencoded")
    png = bytes(range(256))
    assert r.body(png, "image/png") == png  # binary untouched
    t = r.text("auth Bearer abc.def and eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig and ?token=zz")
    assert "abc.def" not in t and "eyJhbGci" not in t and "zz" not in t


def test_redact_html_inputs():
    h = r.html('<input type="hidden" name="authenticity_token" value="S1"><input type=password value=S2>'
               '<meta name="csrf-token" content="S3"><input name="q" value="keep">')
    assert "S1" not in h and "S2" not in h and "S3" not in h and 'value="keep"' in h


def test_state_machine_only_ready_can_start():
    assert {s for s, nxt in ALLOWED.items() if State.SCRAPING in nxt} == {State.READY}
    assert State.SCRAPING not in ALLOWED[State.WAITING_LOGIN] | ALLOWED[State.PAUSED]


def test_asset_paths_are_safe_and_deterministic(tmp_path):
    p = asset_relpath("https://h.com/../../etc/passwd?x=1", "text/plain")
    assert ".." not in p.parts and p.parts[0] == "h.com"
    assert asset_relpath("https://h.com/a.js?v=1", "") != asset_relpath("https://h.com/a.js?v=2", "")
    assert asset_relpath("https://h.com/", "text/html").name == "index.html"
    run = RunDir(tmp_path, "h.com")
    with pytest.raises(ValueError):
        run.file("../outside")
    assert RunDir(tmp_path, "h.com").path != run.path  # same second: no clobbering


def test_windows_safe_names():
    assert asset_relpath("https://h.com/con.js", "").name == "_con.js"
    assert asset_relpath("https://h.com/x/NUL", "text/css").name == "_NUL.css"
    assert asset_relpath("https://h.com:8080/a:b*c?.js", "").parts[0] == "h.com_8080"
    assert all(c not in str(asset_relpath("https://h.com/a<b>|c\"d.js", "")) for c in '<>|"*?:')


def test_login_patterns_match_case_insensitively(tmp_path):
    from web_cloner.controller import Session
    s = Session(Config(url="https://h.com/", output_dir=str(tmp_path)))
    assert s._url_signal("https://h.com/Account/Login?next=/") is False
    assert s._url_signal("https://h.com/table?next=/login") is None  # query ignored
