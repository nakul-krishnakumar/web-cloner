"""Output directory: safe paths, atomic JSON, append-only JSONL."""

import hashlib
import json
import mimetypes
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def safe_name(s: str, maxlen: int = 80) -> str:
    out = "".join(c if c.isalnum() or c in "._-" else "_" for c in s)[:maxlen].strip(".") or "_"
    return "_" + out if out.split(".")[0].lower() in WINDOWS_RESERVED else out  # con.js can't exist on Windows


def short_hash(s: str, n: int = 10) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


def ext_for(content_type: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    if "json" in ct:
        return ".json"
    return mimetypes.guess_extension(ct) or (".txt" if ct.startswith("text/") else ".bin")


def asset_relpath(url: str, content_type: str) -> PurePosixPath:
    """Deterministic, traversal-proof path for a URL: <host>/<path>[__<queryhash>]<ext>."""
    p = urlsplit(url)
    segs = [safe_name(s) for s in p.path.split("/") if s not in ("", ".", "..")][:20] or ["index"]
    if p.path.endswith("/") and p.path != "/":
        segs.append("index")
    stem, ext = os.path.splitext(segs[-1])
    if not ext or len(ext) > 10:
        stem, ext = segs[-1], ext_for(content_type)
    if p.query:
        stem += "__" + short_hash(p.query)
    segs[-1] = safe_name(stem + ext, 120)
    return PurePosixPath(safe_name(p.netloc), *segs)


class RunDir:
    def __init__(self, root: Path, host: str):
        base = Path(root) / f"{safe_name(host)}_{datetime.now():%Y%m%d_%H%M%S}"
        path, n = base, 1
        root.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                path.mkdir(mode=0o700)  # atomic claim; captures can hold personal data
                break
            except FileExistsError:
                n += 1
                path = base.with_name(f"{base.name}_{n}")
        self.path = path
        self._files = {}

    def file(self, rel: str | PurePosixPath) -> Path:
        p = (self.path / rel).resolve()
        if not p.is_relative_to(self.path.resolve()):
            raise ValueError(f"path escapes run dir: {rel}")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def jsonl(self, rel: str, obj) -> None:
        f = self._files.get(rel)
        if f is None:
            f = self._files[rel] = open(self.file(rel), "a", encoding="utf-8")
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")  # one write per line: no interleaving
        f.flush()

    def write_bytes(self, rel, data: bytes) -> Path:
        p = self.file(rel)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        return p

    def write_json(self, rel, obj) -> Path:
        return self.write_bytes(rel, json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode())

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()
