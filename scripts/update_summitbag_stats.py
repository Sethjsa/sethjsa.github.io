#!/usr/bin/env python3
"""Pull Strava activities and extract Summit Bag stats from their descriptions.

Summit Bag (summitbag.com) appends a line like this to each Strava activity
description it processes:

    (peak) Kusushidake Peak (3,725 m) * (peak) Joujugatake Peak (3,734 m) * ...
    (foot) 2026 = 28,693 m | (globe) summitbag.com

This script pulls recent Strava activities via the Strava API, parses those
lines out of each activity's description, and merges the results into
_data/summitbag_stats.yml, which the site reads at build time.

Strava's default rate limit (non-upload endpoints) is 100 requests / 15 min
and 1,000 requests / day. A full-history backfill can easily need more
requests than fit in a single day, so --full is resumable: it walks the
activity history oldest-to-newest, checkpoints progress after every request,
and stops cleanly (exit 0) once it hits the daily budget, picking back up
from the checkpoint on the next run. Once a full backfill finishes, later
runs (with or without --full) just process new activities incrementally.

Usage:
    python scripts/update_summitbag_stats.py            # incremental: only activities since last run
    python scripts/update_summitbag_stats.py --full      # backfill entire history (resumable across runs)

Requires environment variables:
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    STRAVA_REFRESH_TOKEN
"""
import argparse
import collections
import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "_data" / "summitbag_stats.yml"
ACTIVITIES_FILE = REPO_ROOT / "_data" / "summitbag_activities.json"

# Country name (as Strava's geocoder returns it) -> ISO 3166-1 alpha-2, for
# building a flag emoji. Not exhaustive - extend as new countries show up.
COUNTRY_TO_ISO2 = {
    "Japan": "JP", "United Kingdom": "GB", "Isle of Man": "IM", "Ireland": "IE",
    "United States": "US", "Iceland": "IS", "Faroe Islands": "FO", "Denmark": "DK",
    "France": "FR", "Germany": "DE", "Netherlands": "NL", "Belgium": "BE",
    "Czechia": "CZ", "Czech Republic": "CZ", "Finland": "FI", "Sweden": "SE",
    "Norway": "NO", "Singapore": "SG", "Indonesia": "ID", "Malta": "MT",
    "Spain": "ES", "Italy": "IT", "Portugal": "PT", "Switzerland": "CH",
    "Austria": "AT", "Poland": "PL", "South Korea": "KR", "Korea": "KR",
    "Canada": "CA", "Mexico": "MX", "Luxembourg": "LU",
}


def flag_for_country(country_name):
    iso2 = COUNTRY_TO_ISO2.get(country_name)
    if not iso2:
        return None
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso2)


GEONAMES_CITIES_URL = "https://download.geonames.org/export/dump/cities15000.zip"


def fetch_cities_gazetteer():
    """Strava's location_city often comes back null, but start_latlng is
    still populated - so reverse-geocode ourselves against a public
    gazetteer (GeoNames, cities with population > 15,000) rather than
    depending on Strava's field."""
    req = urllib.request.Request(GEONAMES_CITIES_URL, headers={"User-Agent": "sethjsa.github.io stats script"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        zip_bytes = resp.read()
    cities = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        with zf.open("cities15000.txt") as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t")
            for row in reader:
                # GeoNames dump columns: geonameid, name, asciiname, ...,
                # latitude(4), longitude(5), ...
                name, lat, lng = row[1], float(row[4]), float(row[5])
                cities.append((name, lat, lng))
    return cities


def nearest_city(lat, lng, cities):
    if lat is None or lng is None or not cities:
        return None
    lat_scale = math.cos(math.radians(lat))
    best_name, best_dist = None, None
    for name, city_lat, city_lng in cities:
        d = (city_lat - lat) ** 2 + ((city_lng - lng) * lat_scale) ** 2
        if best_dist is None or d < best_dist:
            best_name, best_dist = name, d
    return best_name

TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"

PEAK_RE = re.compile(r"⛰️\s*([^•\n]+?)\s*\(\s*([\d,]+)\s*m\s*\)")
ELEVATION_RE = re.compile(r"⬆️\s*(\d{4})\s*=\s*([\d,]+)\s*m")

RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
RIDE_TYPES = {"Ride", "MountainBikeRide", "GravelRide", "EBikeRide", "VirtualRide", "Velomobile", "Handcycle"}
HIKE_TYPES = {"Hike", "Walk", "Snowshoe"}
SKI_TYPES = {"AlpineSki", "BackcountrySki", "NordicSki", "Snowboard"}

# Strava's actual default limits (non-upload endpoints): 100 req / 15 min,
# 1,000 req / day. Stay just under both.
MAX_REQUESTS_PER_WINDOW = 95
WINDOW_SECONDS = 900
DAILY_REQUEST_BUDGET = 950

_request_times = collections.deque()
_request_count = 0


class BudgetExceeded(Exception):
    pass


def _throttle():
    global _request_count
    if _request_count >= DAILY_REQUEST_BUDGET:
        raise BudgetExceeded()
    now = time.monotonic()
    while _request_times and now - _request_times[0] > WINDOW_SECONDS:
        _request_times.popleft()
    if len(_request_times) >= MAX_REQUESTS_PER_WINDOW:
        sleep_for = WINDOW_SECONDS - (now - _request_times[0]) + 1
        print(f"  rate limit pacing: sleeping {sleep_for:.0f}s", file=sys.stderr)
        time.sleep(max(sleep_for, 0))
    _request_times.append(time.monotonic())
    _request_count += 1


_token_cache = {"token": None, "obtained_at": 0.0}
# Refresh well before Strava's stated 6h expiry - long runs (rate-limit
# pacing can stretch this to 1-2+ hours) shouldn't get caught out by a
# stale token, whatever the exact cause of any individual expiry.
TOKEN_REFRESH_INTERVAL = 2700  # 45 minutes


def _valid_access_token(force=False):
    now = time.monotonic()
    if force or _token_cache["token"] is None or now - _token_cache["obtained_at"] > TOKEN_REFRESH_INTERVAL:
        _token_cache["token"] = _fetch_access_token()
        _token_cache["obtained_at"] = now
    return _token_cache["token"]


def _fetch_access_token():
    client_id = os.environ["STRAVA_CLIENT_ID"]
    client_secret = os.environ["STRAVA_CLIENT_SECRET"]
    refresh_token = os.environ["STRAVA_REFRESH_TOKEN"]
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["access_token"]


def _get(url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    _throttle()
    for attempt in range(3):
        token = _valid_access_token(force=(attempt > 0))
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                # Token expired/invalid earlier than expected - force a fresh
                # one and retry once before giving up.
                print("  401 unauthorized - refreshing token and retrying", file=sys.stderr)
                continue
            if e.code == 429:
                # We already pace to stay under the 15-min window limit, so a 429
                # here means we've hit Strava's daily cap. Retrying won't help for
                # hours - stop cleanly now instead of burning the job timeout.
                print("  429 rate limited despite pacing - daily quota hit, stopping run", file=sys.stderr)
                raise BudgetExceeded()
            if e.code >= 500 and attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"  {e.code} error, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Failed to fetch {url} after retries")


def list_activity_summaries(after_epoch=None):
    """Return activity summaries (id, sport_type, start_date), oldest first."""
    page = 1
    out = []
    while True:
        params = {"per_page": 100, "page": page}
        if after_epoch is not None:
            params["after"] = after_epoch
        batch = _get(f"{API_BASE}/athlete/activities", params)
        if not batch:
            break
        out.extend(batch)
        page += 1
    out.sort(key=lambda a: a["start_date"])
    return out


def get_activity_detail(activity_id):
    return _get(f"{API_BASE}/activities/{activity_id}")


def bucket_for(sport_type):
    if sport_type in RUN_TYPES:
        return "Run"
    if sport_type in RIDE_TYPES:
        return "Ride"
    if sport_type in HIKE_TYPES:
        return "Hike"
    if sport_type in SKI_TYPES:
        return "Ski"
    return "Other"


def parse_description(description):
    """Return (peaks, elevation_reading) parsed from a Summit Bag description.

    peaks: list of (name, height_m)
    elevation_reading: (year, total_m) or None
    """
    if not description:
        return [], None
    peaks = [(name.strip(), int(height.replace(",", ""))) for name, height in PEAK_RE.findall(description)]
    elev_match = ELEVATION_RE.search(description)
    elevation_reading = None
    if elev_match:
        elevation_reading = (int(elev_match.group(1)), int(elev_match.group(2).replace(",", "")))
    return peaks, elevation_reading


def peak_key(activity_id, name, date):
    # Peak NAMES are not unique ascents - the same hill gets climbed on
    # different days, and each ascent should be kept, not overwrite the
    # last one. Key by (activity, name) so repeats are distinct entries;
    # fall back to (date, name) only for legacy entries with no activity_id.
    if activity_id:
        return f"{activity_id}:{name}"
    return f"{date}:{name}"


def load_state():
    state = {
        "last_processed_epoch": 0,
        "backfill_cursor_epoch": 0,
        "full_backfill_complete": False,
        "peaks": {},
        "elevation": {},
        "activities": {},
    }
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            raw = yaml.safe_load(f) or {}
        state["last_processed_epoch"] = raw.get("last_processed_epoch", 0)
        state["backfill_cursor_epoch"] = raw.get("backfill_cursor_epoch", 0)
        state["full_backfill_complete"] = raw.get("full_backfill_complete", False)
        state["peaks"] = {
            peak_key(p.get("activity_id"), p["name"], p["date"]): p
            for p in raw.get("peaks", [])
        }
        state["elevation"] = raw.get("elevation", {})
    if ACTIVITIES_FILE.exists():
        with open(ACTIVITIES_FILE) as f:
            raw_activities = json.load(f)
        state["activities"] = {a["id"]: a for a in raw_activities}
    return state


def save_state(state):
    peaks_sorted = sorted(state["peaks"].values(), key=lambda p: p["date"])
    latest_peaks = [dict(p) for p in reversed(peaks_sorted[-5:])]
    top_peaks = []
    seen_names = set()
    for p in sorted(peaks_sorted, key=lambda p: p["height_m"], reverse=True):
        # Downhill skiing gets you to the top by lift, not by climbing it -
        # doesn't belong alongside hike/run/ride ascents in the highlight list.
        if p["type"] == "Ski":
            continue
        if p["name"] in seen_names:
            continue
        seen_names.add(p["name"])
        top_peaks.append(dict(p))
        if len(top_peaks) == 5:
            break
    public_activities = [
        # private defaults to unset on activity records captured before this
        # field existed - treat "unknown" as NOT public rather than assuming
        # it's safe to show, so a stale record is never accidentally exposed.
        a for a in state["activities"].values()
        if a.get("private") is False and a.get("start_date")
    ]
    public_activities.sort(key=lambda a: a["start_date"], reverse=True)
    recent5 = public_activities[:5]

    # Strava's own location_city often comes back null - reverse-geocode
    # from start_latlng instead, but only for the handful of activities
    # actually shown, not the whole archive.
    cities = None
    if any(a.get("location_city") is None and a.get("start_latlng") for a in recent5):
        try:
            cities = fetch_cities_gazetteer()
        except Exception as e:
            print(f"Failed to fetch city gazetteer ({e!r}) - city names will be omitted", file=sys.stderr)

    def city_for(a):
        if a.get("location_city"):
            return a["location_city"]
        latlng = a.get("start_latlng")
        if cities and latlng and len(latlng) == 2:
            return nearest_city(latlng[0], latlng[1], cities)
        return None

    recent_public_activities = [
        {
            "id": a["id"],
            "name": a["name"],
            "sport_type": a.get("sport_type") or a.get("type"),
            "bucket": bucket_for(a.get("sport_type") or a.get("type")),
            "start_date_local": a.get("start_date_local"),
            "distance": a.get("distance"),
            "total_elevation_gain": a.get("total_elevation_gain"),
            "moving_time": a.get("moving_time"),
            "location_city": city_for(a),
            "map_summary_polyline": a.get("map_summary_polyline"),
        }
        for a in recent5
    ]

    total_elevation_m = sum(
        m for by_year in state["elevation"].values() for m in by_year.values()
    )

    out = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "last_processed_epoch": state["last_processed_epoch"],
        "backfill_cursor_epoch": state["backfill_cursor_epoch"],
        "full_backfill_complete": state["full_backfill_complete"],
        "peak_count": len(peaks_sorted),
        "latest_peaks": latest_peaks,
        "top_peaks": top_peaks,
        "peaks": peaks_sorted,
        "elevation": state["elevation"],
        "total_elevation_m": round(total_elevation_m),
        "recent_public_activities": recent_public_activities,
    }
    with open(DATA_FILE, "w") as f:
        f.write("# Auto-generated by scripts/update_summitbag_stats.py - do not hand-edit.\n")
        yaml.dump(out, f, sort_keys=False, allow_unicode=True)

    activities_sorted = sorted(state["activities"].values(), key=lambda a: a["start_date"])
    with open(ACTIVITIES_FILE, "w") as f:
        json.dump(activities_sorted, f, indent=0, ensure_ascii=False)


def epoch_of(summary):
    return int(datetime.fromisoformat(summary["start_date"].replace("Z", "+00:00")).timestamp())


ACTIVITY_FIELDS = (
    "id", "name", "sport_type", "type", "start_date", "start_date_local", "timezone",
    "distance", "moving_time", "elapsed_time", "total_elevation_gain", "elev_high",
    "elev_low", "location_city", "location_state", "location_country", "start_latlng",
    "end_latlng", "average_speed", "max_speed", "kudos_count", "achievement_count",
    "comment_count", "photo_count", "athlete_count", "description", "private",
)


def process_activity(state, summary):
    detail = get_activity_detail(summary["id"])
    peaks, elevation_reading = parse_description(detail.get("description"))
    sport_type = summary.get("sport_type") or summary.get("type", "Other")
    bucket = bucket_for(sport_type)
    date = summary["start_date"][:10]
    country = detail.get("location_country")
    city = detail.get("location_city")
    flag = flag_for_country(country) if country else None

    for name, height_m in peaks:
        # Keyed per-ascent (activity + name), not just name - the same peak
        # gets climbed on different days and each ascent must be kept.
        # Overwrite (not insert-if-missing) so re-running over an
        # already-seen activity - e.g. after adding a new field - fills in
        # the gap instead of skipping it.
        key = peak_key(detail.get("id"), name, date)
        # If this peak was previously recorded before activity_id capture
        # existed (or before this activity had its detail fetched), it'll be
        # sitting under the legacy date:name key. Now that we have the real
        # activity, drop the legacy record so it doesn't linger as a duplicate.
        if detail.get("id"):
            legacy_key = peak_key(None, name, date)
            if legacy_key != key:
                state["peaks"].pop(legacy_key, None)
        state["peaks"][key] = {
            "name": name,
            "height_m": height_m,
            "type": bucket,
            "date": date,
            "location_city": city,
            "location_country": country,
            "flag": flag,
            "activity_id": detail.get("id"),
            "activity_name": detail.get("name"),
        }

    if elevation_reading:
        year, total_m = elevation_reading
        state["elevation"].setdefault(bucket, {})
        state["elevation"][bucket][year] = total_m

    activity_record = {field: detail.get(field) for field in ACTIVITY_FIELDS}
    activity_record["peak_names"] = [name for name, _ in peaks]
    # Only the summary polyline (a coarse, already-simplified route) - the
    # full-resolution polyline is a much larger blob and unnecessary for a
    # small thumbnail map.
    activity_record["map_summary_polyline"] = (detail.get("map") or {}).get("summary_polyline")
    state["activities"][detail["id"]] = activity_record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="backfill entire activity history (resumable)")
    parser.add_argument(
        "--refresh-recent-days", type=int, default=None,
        help="reprocess activities from the last N days even if already seen "
             "(cheap way to backfill newly-added fields onto recent activities "
             "without a full history re-scan)",
    )
    args = parser.parse_args()

    state = load_state()
    exhausted = False
    unexpected_error = None

    try:
        if args.refresh_recent_days is not None:
            cutoff = int(time.time()) - args.refresh_recent_days * 86400
            summaries = list_activity_summaries(cutoff)
            print(f"Refreshing {len(summaries)} activities from the last {args.refresh_recent_days} days", file=sys.stderr)
            for summary in summaries:
                process_activity(state, summary)
        elif args.full and not state["full_backfill_complete"]:
            summaries = list_activity_summaries(None)
            todo = [s for s in summaries if epoch_of(s) > state["backfill_cursor_epoch"]]
            print(f"Full backfill: {len(todo)} activities remaining (of {len(summaries)} total)", file=sys.stderr)
            for summary in todo:
                process_activity(state, summary)
                epoch = epoch_of(summary)
                state["backfill_cursor_epoch"] = epoch
                state["last_processed_epoch"] = max(state["last_processed_epoch"], epoch)
            state["full_backfill_complete"] = True
            print("Full backfill complete", file=sys.stderr)
        else:
            summaries = list_activity_summaries(state["last_processed_epoch"])
            print(f"Found {len(summaries)} new activities to check", file=sys.stderr)
            for summary in summaries:
                process_activity(state, summary)
                state["last_processed_epoch"] = max(state["last_processed_epoch"], epoch_of(summary))
    except BudgetExceeded:
        exhausted = True
        print(f"Hit daily request budget ({DAILY_REQUEST_BUDGET}) - saving progress, will resume next run", file=sys.stderr)
    except Exception as e:
        # Whatever went wrong, don't throw away the progress made so far -
        # save it, then still surface the failure so CI shows it clearly.
        unexpected_error = e
        print(f"Unexpected error ({e!r}) - saving progress made so far before failing", file=sys.stderr)

    save_state(state)
    print(f"Wrote {DATA_FILE} ({len(state['peaks'])} peaks so far)", file=sys.stderr)
    if unexpected_error is not None:
        raise unexpected_error
    if exhausted:
        sys.exit(0)


if __name__ == "__main__":
    main()
