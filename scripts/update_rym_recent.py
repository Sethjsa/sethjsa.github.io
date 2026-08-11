#!/usr/bin/env python3
"""Pull recently-rated albums from RateYourMusic's private activity RSS feed.

RateYourMusic sits behind Cloudflare bot protection that blocks plain
requests and headless browsers alike (verified: both a direct request
and a Playwright render with a long wait got stuck on the challenge
page). The feed does work with a real logged-in browser's cookies,
though - so this script sends the full cookie string from a real
browser session (env var RYM_COOKIE) instead of trying to solve the
challenge itself.

That cookie includes Cloudflare's cf_clearance token, which is
short-lived and tied to the browser/IP that solved the challenge - it
WILL eventually stop working when it expires and needs replacing:
  1. Open https://rateyourmusic.com/~<username>/data/rss in a browser
     while logged in.
  2. Open devtools -> Network, find the request, copy the Cookie header.
  3. Update the RYM_COOKIE GitHub Actions secret with the new value.
This fails soft (leaves existing data alone) if the cookie has expired,
rather than crashing the workflow.

The feed's own "by Multiple Artists"/"by Various Artists" text is
unreliable (RYM's RSS generator doesn't resolve real artist names for
this feed), so this uses the release URL slug as a rough artist guess,
then corrects it (and fetches cover art) via iTunes's public search API.

Usage:
    RYM_COOKIE='...' python scripts/update_rym_recent.py
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "_data" / "rym_recent.yml"

RSS_URL = "https://rateyourmusic.com/~SetheryJ/data/rss"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
KEEP = 15

TITLE_RE = re.compile(r"^Rated (.+) by .+?\s+([\d.]+) stars$")
LINK_RE = re.compile(r"/release/album/([^/]+)/")


def fetch_rss():
    # cf_clearance is fingerprint-bound to the User-Agent that solved the
    # Cloudflare challenge - this MUST stay in sync with whatever browser
    # RYM_COOKIE was captured from, or Cloudflare rejects the request even
    # with an otherwise-valid cookie.
    cookie = os.environ["RYM_COOKIE"]
    user_agent = os.environ.get(
        "RYM_USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) Gecko/20100101 Firefox/152.0",
    )
    req = urllib.request.Request(
        RSS_URL,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cookie": cookie,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    if b"<rss" not in body[:200]:
        raise RuntimeError("response doesn't look like RSS - cookie likely expired or challenge page returned")
    return body


def slug_to_name(slug):
    return " ".join(w.capitalize() for w in slug.split("-"))


def parse_items(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pubdate_el = item.find("pubDate")
        if title_el is None or link_el is None:
            continue
        m = TITLE_RE.match(html.unescape(title_el.text or ""))
        if not m:
            continue
        name, rating = m.group(1), float(m.group(2))
        link = link_el.text
        slug_m = LINK_RE.search(link or "")
        artist_guess = slug_to_name(slug_m.group(1)) if slug_m else None
        items.append({
            "name": name,
            "artist_guess": artist_guess,
            "rating": rating,
            "url": link,
            "pub_date": pubdate_el.text if pubdate_el is not None else None,
        })
    return items


def dedupe_keep_first(items):
    """The feed lists newest-first; keep only the first (most recent)
    rating per album if it was rated more than once."""
    seen = set()
    out = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


def lookup_artwork(artist_guess, title):
    params = {"term": f"{artist_guess or ''} {title}".strip(), "media": "music", "entity": "album", "limit": 1}
    url = f"{ITUNES_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "sethjsa.github.io stats script"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"  iTunes lookup failed for {artist_guess} - {title}: {e!r}", file=sys.stderr)
        return None, None
    results = data.get("results") or []
    if not results:
        return None, None
    r = results[0]
    artwork = (r.get("artworkUrl100") or "").replace("100x100", "600x600") or None
    return artwork, r.get("collectionArtistName") or r.get("artistName")


def parse_pubdate(s):
    if not s:
        return None
    dt = datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
    return dt.strftime("%Y-%m-%d")


def main():
    try:
        xml_bytes = fetch_rss()
    except Exception as e:
        print(f"Failed to fetch RYM RSS ({e!r}) - leaving existing data alone. "
              f"The RYM_COOKIE secret has likely expired and needs refreshing.", file=sys.stderr)
        return

    items = dedupe_keep_first(parse_items(xml_bytes))[:KEEP]

    for it in items:
        artwork, itunes_artist = lookup_artwork(it["artist_guess"], it["name"])
        it["image"] = artwork
        it["artist"] = itunes_artist or it["artist_guess"]
        it["date"] = parse_pubdate(it.pop("pub_date"))
        it.pop("artist_guess", None)
        time.sleep(0.5)

    out = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "recent": items,
    }
    with open(DATA_FILE, "w") as f:
        f.write("# Auto-generated by scripts/update_rym_recent.py - do not hand-edit.\n")
        yaml.dump(out, f, sort_keys=False, allow_unicode=True)
    print(f"Wrote {DATA_FILE} ({len(items)} recent ratings)", file=sys.stderr)


if __name__ == "__main__":
    main()
