#!/usr/bin/env python3
"""Pull listening stats from the Last.fm API.

Fetches top artists/albums/tracks (all-time and last 12 months) plus a
year-by-year scrobble breakdown stacked by artist, writing everything
to _data/lastfm_stats.yml.

Last.fm's public read methods (user.getInfo, user.getTopArtists, etc.)
only need an API key - no OAuth/login flow, since this just reads a
public profile's own stats. Register a key at
https://www.last.fm/api/account/create and set it as the
LASTFM_API_KEY environment variable / GitHub secret.

Rate limits are generous (~5 req/s) and this script only makes a
couple dozen requests total, so unlike the Strava script there's no
need for checkpointing or a daily budget.

Usage:
    python scripts/update_lastfm_stats.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "_data" / "lastfm_stats.yml"

API_BASE = "https://ws.audioscrobbler.com/2.0/"
USERNAME = "SetheryJ"
TOP_N = 5
CHART_TOP_ARTISTS = 5


def _get(params, retries=3):
    params = dict(params)
    params["api_key"] = os.environ["LASTFM_API_KEY"]
    params["format"] = "json"
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "sethjsa.github.io stats script"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"Failed to fetch {params.get('method')} after retries")


def get_user_info():
    return _get({"method": "user.getinfo", "user": USERNAME})["user"]


def get_top_artists(period, limit=TOP_N):
    data = _get({"method": "user.gettopartists", "user": USERNAME, "period": period, "limit": limit})
    artists = data["topartists"]["artist"]
    return artists if isinstance(artists, list) else [artists]


def get_top_albums(period, limit=TOP_N):
    data = _get({"method": "user.gettopalbums", "user": USERNAME, "period": period, "limit": limit})
    albums = data["topalbums"]["album"]
    return albums if isinstance(albums, list) else [albums]


def get_top_tracks(period, limit=TOP_N):
    data = _get({"method": "user.gettoptracks", "user": USERNAME, "period": period, "limit": limit})
    tracks = data["toptracks"]["track"]
    return tracks if isinstance(tracks, list) else [tracks]


def get_recent_tracks(limit=200):
    data = _get({"method": "user.getrecenttracks", "user": USERNAME, "limit": limit})
    tracks = data["recenttracks"]["track"]
    tracks = tracks if isinstance(tracks, list) else [tracks]
    # The currently-playing track (if any) has no date field - skip it, it's
    # not yet a completed scrobble.
    return [t for t in tracks if "date" in t]


def recent_albums(limit=5, scan=400):
    """Most recently played distinct albums, newest first."""
    tracks = get_recent_tracks(limit=scan)
    seen = set()
    albums = []
    for t in tracks:
        album_name = (t.get("album") or {}).get("#text")
        artist_name = (t.get("artist") or {}).get("#text")
        if not album_name:
            continue
        key = (artist_name, album_name)
        if key in seen:
            continue
        seen.add(key)
        albums.append({
            "name": album_name,
            "artist": artist_name,
            "url": t.get("url"),
            "image": best_image(t.get("image")),
        })
        if len(albums) >= limit:
            break
    return albums


def get_weekly_artist_chart(from_ts, to_ts):
    data = _get({"method": "user.getweeklyartistchart", "user": USERNAME, "from": from_ts, "to": to_ts})
    chart = data.get("weeklyartistchart", {})
    artists = chart.get("artist", [])
    if isinstance(artists, dict):
        artists = [artists]
    return artists


def best_image(image_list):
    if not image_list:
        return None
    by_size = {img.get("size"): img.get("#text") for img in image_list}
    for size in ("extralarge", "large", "medium"):
        if by_size.get(size):
            return by_size[size]
    return None


def registered_year(user_info):
    registered = user_info.get("registered")
    unixtime = registered.get("unixtime") if isinstance(registered, dict) else registered
    return datetime.fromtimestamp(int(unixtime), tz=timezone.utc).year


def epoch_range_for_year(year):
    start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()) - 1
    return start, end


def fmt_artist(a):
    return {"name": a["name"], "playcount": int(a["playcount"]), "url": a.get("url")}


def fmt_album(a):
    return {
        "name": a["name"],
        "artist": (a.get("artist") or {}).get("name"),
        "playcount": int(a["playcount"]),
        "url": a.get("url"),
        "image": best_image(a.get("image")),
    }


def fmt_track(t):
    return {
        "name": t["name"],
        "artist": (t.get("artist") or {}).get("name"),
        "playcount": int(t["playcount"]),
        "url": t.get("url"),
    }


def build_scrobbles_by_year(user_info):
    start_year = registered_year(user_info)
    current_year = datetime.now(timezone.utc).year
    now_ts = int(time.time())

    yearly_counts = {}
    total_by_artist = {}
    for year in range(start_year, current_year + 1):
        start, end = epoch_range_for_year(year)
        if start > now_ts:
            break
        chart = get_weekly_artist_chart(start, min(end, now_ts))
        counts = {}
        for a in chart:
            pc = int(a.get("playcount", 0))
            if pc <= 0:
                continue
            name = a["name"]
            counts[name] = counts.get(name, 0) + pc
            total_by_artist[name] = total_by_artist.get(name, 0) + pc
        if counts:
            yearly_counts[year] = counts

    top_chart_artists = sorted(total_by_artist, key=lambda n: -total_by_artist[n])[:CHART_TOP_ARTISTS]

    scrobbles_by_year = {}
    for year, counts in yearly_counts.items():
        bucketed = {}
        for name, pc in counts.items():
            bucket = name if name in top_chart_artists else "Other"
            bucketed[bucket] = bucketed.get(bucket, 0) + pc
        scrobbles_by_year[str(year)] = bucketed

    return scrobbles_by_year, top_chart_artists


def main():
    user_info = get_user_info()

    top_artists_overall = get_top_artists("overall")
    top_artists_12month = get_top_artists("12month")
    top_albums_overall = get_top_albums("overall")
    top_albums_12month = get_top_albums("12month")
    top_tracks_overall = get_top_tracks("overall")
    top_tracks_12month = get_top_tracks("12month")

    scrobbles_by_year, chart_artists = build_scrobbles_by_year(user_info)
    recent_albums_list = recent_albums(limit=15)

    out = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_scrobbles": int(user_info.get("playcount", 0)),
        "top_artists": {
            "overall": [fmt_artist(a) for a in top_artists_overall],
            "12month": [fmt_artist(a) for a in top_artists_12month],
        },
        "top_albums": {
            "overall": [fmt_album(a) for a in top_albums_overall],
            "12month": [fmt_album(a) for a in top_albums_12month],
        },
        "top_tracks": {
            "overall": [fmt_track(t) for t in top_tracks_overall],
            "12month": [fmt_track(t) for t in top_tracks_12month],
        },
        "scrobbles_by_year": scrobbles_by_year,
        "chart_artists": chart_artists,
        "recent_albums": recent_albums_list,
    }
    with open(DATA_FILE, "w") as f:
        f.write("# Auto-generated by scripts/update_lastfm_stats.py - do not hand-edit.\n")
        yaml.dump(out, f, sort_keys=False, allow_unicode=True)
    print(f"Wrote {DATA_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
