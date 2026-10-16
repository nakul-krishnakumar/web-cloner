"""Strip authentication material from anything before it is written to disk."""

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-csrf-token",
    "x-xsrf-token", "csrf-token", "x-api-key", "x-auth-token", "x-amz-security-token",
}
# Over-redaction (e.g. "author") is the safe failure mode here.
SECRET_KEY_RE = re.compile(
    r"passw|secret|token|csrf|xsrf|session|api[-_]?key|authoriz|^auth|credential|cookie"
    r"|signature|private[-_]?key|^(code|sig|sid|key|otp|pin|jwt|ticket|nonce|state)$",
    re.I,
)
JWT_RE = re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]+")
BEARER_RE = re.compile(r"\b(Bearer|Basic|Token)\s+[\w.~+/=-]+", re.I)
KV_RE = re.compile(r"([\w.-]+)=([^&\s\"'<>]+)")
TAG_RE = re.compile(r"<(?:input|meta)\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""\b(name|id|type|property)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
VALUE_ATTR_RE = re.compile(r"""\b(value|content)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)


class Redactor:
    def __init__(self, extra_headers=(), extra_keys=()):
        self.headers = SENSITIVE_HEADERS | {h.lower() for h in extra_headers}
        self.extra_keys = {k.lower() for k in extra_keys}

    def is_secret(self, key: str) -> bool:
        return bool(key) and (key.lower() in self.extra_keys or bool(SECRET_KEY_RE.search(key)))

    def header_map(self, headers: dict) -> dict:
        return {k: REDACTED if k.lower() in self.headers or self.is_secret(k) else v for k, v in headers.items()}

    def _qs(self, qs: str) -> str:
        pairs = parse_qsl(qs, keep_blank_values=True)
        return urlencode([(k, REDACTED if self.is_secret(k) else v) for k, v in pairs], safe="[]")

    def url(self, url: str) -> str:
        try:
            p = urlsplit(url)
        except ValueError:
            return self.text(url)
        netloc = p.netloc.rsplit("@", 1)[-1]  # drop user:password@
        fragment = self._qs(p.fragment) if "=" in p.fragment else p.fragment
        return urlunsplit((p.scheme, netloc, p.path, self._qs(p.query) if p.query else "", fragment))

    def text(self, s: str) -> str:
        s = JWT_RE.sub(REDACTED, s)
        s = BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
        return KV_RE.sub(lambda m: f"{m.group(1)}={REDACTED}" if self.is_secret(m.group(1)) else m.group(0), s)

    def obj(self, o):
        if isinstance(o, dict):
            return {k: REDACTED if self.is_secret(str(k)) else self.obj(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self.obj(v) for v in o]
        if isinstance(o, str):
            return self.text(o)
        return o

    def html(self, s: str) -> str:
        def fix(m):
            tag = m.group(0)
            attrs = {k.lower(): v.strip("\"'") for k, v in ATTR_RE.findall(tag)}
            if attrs.get("type", "").lower() == "password" or any(
                self.is_secret(attrs.get(a, "")) for a in ("name", "id", "property")
            ):
                return VALUE_ATTR_RE.sub(lambda v: f'{v.group(1)}="{REDACTED}"', tag)
            return tag

        return self.text(TAG_RE.sub(fix, s))

    def body(self, data: bytes, content_type: str) -> bytes:
        """Redact text-like bodies; binary bodies are returned byte-for-byte."""
        ct = content_type.lower()
        if "json" in ct:
            try:
                return json.dumps(self.obj(json.loads(data)), ensure_ascii=False, indent=2).encode()
            except ValueError:
                pass
        if not any(t in ct for t in ("json", "text/", "xml", "javascript", "x-www-form-urlencoded", "graphql")):
            return data
        try:
            s = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        if "x-www-form-urlencoded" in ct:
            return self._qs(s).encode()
        return (self.html(s) if "html" in ct else self.text(s)).encode()

    def post_data(self, data: str | None, content_type: str, limit: int = 65536) -> str | None:
        if data is None:
            return None
        return self.body(data.encode()[:limit], content_type or "text/plain").decode("utf-8", "replace")
