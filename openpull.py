#!/usr/bin/env python3
"""
OpenPull — download files that are openly available on the web.

Paste a link to a public file or to an item in an open archive. OpenPull finds the
real file, downloads it with resume and parallel workers, verifies it, and writes
down where it came from and under what license.

It deliberately does NOT log in, bypass paywalls, strip DRM or rip streaming
platforms. If a server asks for a login or payment, OpenPull stops.

Supported sources
  direct links        any public https URL to a file (pdf, zip, mp4, mp3, jpg, csv …)
  Internet Archive    https://archive.org/details/<id>        (every file, md5-verified)
  Wikimedia Commons   https://commons.wikimedia.org/wiki/File:<name>  (original + license)
  NASA Image Library  https://images.nasa.gov/details/<nasa_id>       (original asset)
  arXiv               https://arxiv.org/abs/<id>                       (paper PDF)
  GitHub releases     https://github.com/<owner>/<repo>                (latest release assets)

Usage
  python3 openpull.py <link> [<link> ...]
  python3 openpull.py -f links.txt --only pdf,mp4 -o downloads --workers 4
  python3 openpull.py <link> --dry-run          # list what would be downloaded

Only the Python 3.9+ standard library is needed.
"""
from __future__ import annotations

import argparse, concurrent.futures as cf, hashlib, html, json, os, re, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

VERSION = "1.0"
UA = f"OpenPull/{VERSION} (+open-access downloader; respects logins and paywalls)"
CHUNK = 1 << 16
BLOCKED_STATUS = {401: "needs a login", 402: "needs payment", 403: "access is forbidden", 407: "needs proxy auth", 451: "unavailable for legal reasons"}
STREAM_MANIFESTS = (".m3u8", ".mpd", ".ism", ".f4m")


class Refused(Exception):
    """The source is not openly available, so OpenPull will not fetch it."""


@dataclass
class Item:
    url: str                      # the real file URL
    name: str                     # file name to save as
    source: str                   # page or link the user pasted
    kind: str = "direct"          # adapter that resolved it
    size: int | None = None
    md5: str | None = None
    sha1: str | None = None
    sha256: str | None = None
    license: str | None = None
    author: str | None = None
    title: str | None = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- http helpers
def request(url: str, method: str = "GET", headers: dict | None = None, timeout: int = 30):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, method=method, headers=h)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            raise IOError(f"{url}: rate limited by the server, try again later") from None
        if e.code in BLOCKED_STATUS:
            raise Refused(f"{url} {BLOCKED_STATUS[e.code]} (HTTP {e.code}); OpenPull only downloads open files") from None
        raise


def get_json(url: str) -> dict:
    with request(url, headers={"Accept": "application/json"}) as r:
        return json.loads(r.read().decode("utf-8"))


def safe_name(name: str) -> str:
    name = urllib.parse.unquote(name).strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    return name[:180] or "download"


def strip_tags(s: str | None) -> str | None:
    if not s:
        return s
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip() or None


# ---------------------------------------------------------------- adapters
def resolve_internet_archive(url: str) -> list[Item]:
    m = re.search(r"archive\.org/(?:details|download)/([^/?#]+)", url)
    ident = m.group(1)
    meta = get_json(f"https://archive.org/metadata/{ident}")
    if not meta or "files" not in meta:
        raise Refused(f"archive.org item '{ident}' was not found or is not public")
    md = meta.get("metadata", {})
    lic = md.get("licenseurl") or md.get("rights")
    if md.get("access-restricted-item") in ("true", True):
        raise Refused(f"archive.org item '{ident}' is access-restricted (lending library); not open")
    items = []
    for f in meta["files"]:
        if f.get("source") == "metadata" or f.get("private") in ("true", True):
            continue
        name = f["name"]
        items.append(Item(url=f"https://archive.org/download/{ident}/{urllib.parse.quote(name)}", name=safe_name(name),
                          source=url, kind="internet-archive", size=int(f["size"]) if f.get("size") else None,
                          md5=f.get("md5"), sha1=f.get("sha1"), license=lic, author=md.get("creator") if isinstance(md.get("creator"), str) else None,
                          title=md.get("title")))
    return items


def resolve_wikimedia(url: str) -> list[Item]:
    m = re.search(r"/wiki/(File:[^?#]+)", url)
    title = urllib.parse.unquote(m.group(1))
    q = urllib.parse.urlencode({"action": "query", "titles": title, "prop": "imageinfo",
                                "iiprop": "url|sha1|size|extmetadata", "format": "json"})
    data = get_json(f"https://commons.wikimedia.org/w/api.php?{q}")
    page = next(iter(data["query"]["pages"].values()))
    if "imageinfo" not in page:
        raise Refused(f"Commons file '{title}' was not found")
    ii = page["imageinfo"][0]; em = ii.get("extmetadata", {})
    val = lambda k: strip_tags(em.get(k, {}).get("value"))
    return [Item(url=ii["url"], name=safe_name(ii["url"]), source=url, kind="wikimedia-commons", size=ii.get("size"),
                 sha1=ii.get("sha1"), license=val("LicenseShortName"), author=val("Artist"), title=val("ObjectName") or title,
                 extra={"license_url": val("LicenseUrl"), "attribution_required": val("AttributionRequired")})]


def resolve_nasa(url: str) -> list[Item]:
    m = re.search(r"images\.nasa\.gov/details[-/]([^/?#]+)", url)
    nasa_id = urllib.parse.unquote(m.group(1))
    assets = get_json(f"https://images-api.nasa.gov/asset/{urllib.parse.quote(nasa_id)}")["collection"]["items"]
    hrefs = [a["href"] for a in assets]
    orig = [h for h in hrefs if "~orig" in h] or [h for h in hrefs if "~large" in h] or hrefs[:1]
    title = None
    try:
        s = get_json(f"https://images-api.nasa.gov/search?nasa_id={urllib.parse.quote(nasa_id)}")
        title = s["collection"]["items"][0]["data"][0].get("title")
    except Exception:
        pass
    return [Item(url=h.replace("http://", "https://"), name=safe_name(h), source=url, kind="nasa-images",
                 license="NASA media guidelines (generally not copyrighted)", author="NASA", title=title) for h in orig]


def resolve_arxiv(url: str) -> list[Item]:
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?$", url.rstrip("/"))
    aid = m.group(1)
    return [Item(url=f"https://arxiv.org/pdf/{aid}", name=safe_name(aid.replace("/", "_")) + ".pdf", source=url, kind="arxiv",
                 license=f"see https://arxiv.org/abs/{aid} (license varies per paper)", title=f"arXiv:{aid}")]


def resolve_github(url: str) -> list[Item]:
    m = re.search(r"github\.com/([^/]+)/([^/?#]+)", url)
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    rel = get_json(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
    lic = None
    try:
        lic = (get_json(f"https://api.github.com/repos/{owner}/{repo}").get("license") or {}).get("spdx_id")
    except Exception:
        pass
    out = []
    for a in rel.get("assets", []):
        dg = a.get("digest") or ""
        out.append(Item(url=a["browser_download_url"], name=safe_name(a["name"]), source=url, kind="github-release",
                        size=a.get("size"), sha256=dg.split(":", 1)[1] if dg.startswith("sha256:") else None,
                        license=lic, author=owner, title=f"{owner}/{repo} {rel.get('tag_name', '')}".strip()))
    if not out:  # no binaries: fall back to the source archive of the release
        out.append(Item(url=rel["zipball_url"], name=f"{repo}-{rel.get('tag_name', 'latest')}.zip", source=url,
                        kind="github-release", license=lic, author=owner, title=f"{owner}/{repo} {rel.get('tag_name', '')}"))
    return out


def resolve_direct(url: str) -> list[Item]:
    path = urllib.parse.urlparse(url).path
    if path.lower().endswith(STREAM_MANIFESTS):
        raise Refused("streaming manifests (HLS/DASH) are not downloaded: they are often DRM-protected")
    with request(url, method="HEAD") as r:
        ctype = r.headers.get("Content-Type", "")
        size = r.headers.get("Content-Length")
        disp = r.headers.get("Content-Disposition", "")
        final = r.geturl()
    if "text/html" in ctype.lower():
        raise Refused(f"{url} is a web page, not a file. Paste the direct file link, or a link from a supported archive")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disp)
    name = safe_name(m.group(1) if m else urllib.parse.urlparse(final).path)
    return [Item(url=final, name=name, source=url, kind="direct", size=int(size) if size and size.isdigit() else None)]


ADAPTERS = [
    (r"archive\.org/(details|download)/", resolve_internet_archive),
    (r"commons\.wikimedia\.org/wiki/File:", resolve_wikimedia),
    (r"images\.nasa\.gov/details", resolve_nasa),
    (r"arxiv\.org/(abs|pdf)/", resolve_arxiv),
    (r"github\.com/[^/]+/[^/?#]+/?$", resolve_github),
]


def resolve(url: str) -> list[Item]:
    url = url.strip()
    if not url.startswith(("https://", "http://")):
        raise Refused(f"'{url}' is not a web link")
    for pat, fn in ADAPTERS:
        if re.search(pat, url):
            return fn(url)
    return resolve_direct(url)


# ---------------------------------------------------------------- download
_print_lock = threading.Lock()


def log(msg: str):
    with _print_lock:
        print(msg, flush=True)


def verify(path: Path, it: Item) -> str:
    want = [("sha256", it.sha256), ("sha1", it.sha1), ("md5", it.md5)]
    for algo, digest in want:
        if digest:
            h = hashlib.new(algo)
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(CHUNK), b""):
                    h.update(block)
            if h.hexdigest().lower() != digest.lower():
                path.unlink(missing_ok=True)
                raise ValueError(f"{it.name}: {algo} mismatch, file was corrupt and has been deleted")
            return f"{algo} ok"
    if it.size and path.stat().st_size != it.size:
        raise ValueError(f"{it.name}: size {path.stat().st_size} ≠ expected {it.size}")
    return "size ok" if it.size else "no checksum published"


def download(it: Item, outdir: Path, retries: int = 4) -> dict:
    dest = outdir / it.name
    part = dest.with_name(dest.name + ".part")
    if dest.exists() and (not it.size or dest.stat().st_size == it.size):
        return {"file": str(dest), "status": "already there", "check": verify(dest, it)}
    for attempt in range(1, retries + 1):
        try:
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            with request(it.url, headers=headers, timeout=60) as r:
                resumed = have and r.status == 206
                total = it.size or (int(r.headers.get("Content-Length", 0)) + (have if resumed else 0)) or None
                mode = "ab" if resumed else "wb"
                got = have if resumed else 0
                last = time.time()
                with open(part, mode) as f:
                    for block in iter(lambda: r.read(CHUNK), b""):
                        f.write(block); got += len(block)
                        if time.time() - last > 1.5 and total:
                            log(f"  … {it.name}: {got * 100 // total}%  ({got / 1e6:.1f} / {total / 1e6:.1f} MB)"); last = time.time()
            if total and got < total:
                raise ConnectionError(f"connection dropped at {got / 1e6:.1f} of {total / 1e6:.1f} MB")
            part.replace(dest)
            return {"file": str(dest), "status": "resumed" if resumed else "downloaded", "check": verify(dest, it)}
        except Refused:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt == retries:
                raise
            wait = 2 ** attempt
            log(f"  ! {it.name}: {e}; retry {attempt}/{retries - 1} in {wait}s, resuming from the saved part")
            time.sleep(wait)


def write_credits(outdir: Path, rows: list[dict]):
    (outdir / "credits.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Credits", "", "Files downloaded with OpenPull. Keep this file with them.", ""]
    for r in rows:
        lines.append(f"- **{r['name']}** — {r.get('title') or ''}".rstrip(" —"))
        lines.append(f"  - source: {r['source']}")
        if r.get("author"): lines.append(f"  - author: {r['author']}")
        lines.append(f"  - license: {r.get('license') or 'not stated by the source; check before reuse'}")
    (outdir / "CREDITS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(prog="openpull", description="Download files that are openly available on the web.")
    ap.add_argument("links", nargs="*", help="links to files or to items in open archives")
    ap.add_argument("-f", "--file", help="text file with one link per line")
    ap.add_argument("-o", "--out", default="openpull-downloads", help="output folder")
    ap.add_argument("--only", help="comma-separated extensions to keep, e.g. pdf,mp4")
    ap.add_argument("--workers", type=int, default=4, help="parallel downloads (default 4)")
    ap.add_argument("--dry-run", action="store_true", help="list files without downloading")
    ap.add_argument("--version", action="version", version=f"OpenPull {VERSION}")
    a = ap.parse_args(argv)

    links = list(a.links)
    if a.file:
        links += [l.strip() for l in Path(a.file).read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    if not links:
        ap.print_help(); return 2
    only = {e.strip().lower().lstrip(".") for e in a.only.split(",")} if a.only else None

    items, refused = [], []
    for link in links:
        try:
            found = resolve(link)
            if only:
                found = [i for i in found if i.name.rsplit(".", 1)[-1].lower() in only]
            log(f"✓ {link}\n  → {len(found)} file(s) via {found[0].kind if found else 'n/a'}")
            items += found
        except Refused as e:
            refused.append(link); log(f"✗ {link}\n  → skipped: {e}")
        except Exception as e:
            refused.append(link); log(f"✗ {link}\n  → error: {e}")

    total = sum(i.size or 0 for i in items)
    log(f"\n{len(items)} file(s), {total / 1e6:.1f} MB known size, {len(refused)} link(s) skipped")
    if a.dry_run:
        for i in items:
            log(f"  {i.name:60s} {((i.size or 0) / 1e6):8.1f} MB  {i.license or ''}")
        return 0
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rows, failed = [], 0
    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(download, i, out): i for i in items}
        for fu in cf.as_completed(futs):
            i = futs[fu]
            try:
                res = fu.result(); log(f"⬇ {i.name}: {res['status']} · {res['check']}")
                rows.append(asdict(i) | {"saved_as": res["file"], "check": res["check"]})
            except Exception as e:
                failed += 1; log(f"✗ {i.name}: {e}")
    if rows:
        write_credits(out, rows)
        log(f"\nSaved to {out.resolve()} · credits in CREDITS.md")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
