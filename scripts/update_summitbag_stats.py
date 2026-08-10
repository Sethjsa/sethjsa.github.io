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
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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


def _get(url, access_token, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    _throttle()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
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


def get_access_token():
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


def list_activity_summaries(access_token, after_epoch=None):
    """Return activity summaries (id, sport_type, start_date), oldest first."""
    page = 1
    out = []
    while True:
        params = {"per_page": 100, "page": page}
        if after_epoch is not None:
            params["after"] = after_epoch
        batch = _get(f"{API_BASE}/athlete/activities", access_token, params)
        if not batch:
            break
        out.extend(batch)
        page += 1
    out.sort(key=lambda a: a["start_date"])
    return out


def get_activity_detail(access_token, activity_id):
    return _get(f"{API_BASE}/activities/{activity_id}", access_token)


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
    top_peaks = [dict(p) for p in sorted(peaks_sorted, key=lambda p: p["height_m"], reverse=True)[:5]]
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
    "comment_count", "photo_count", "athlete_count", "description",
)


def process_activity(access_token, state, summary):
    detail = get_activity_detail(access_token, summary["id"])
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
    state["activities"][detail["id"]] = activity_record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="backfill entire activity history (resumable)")
    args = parser.parse_args()

    access_token = get_access_token()
    state = load_state()
    exhausted = False
    unexpected_error = None

    try:
        if args.full and not state["full_backfill_complete"]:
            summaries = list_activity_summaries(access_token, None)
            todo = [s for s in summaries if epoch_of(s) > state["backfill_cursor_epoch"]]
            print(f"Full backfill: {len(todo)} activities remaining (of {len(summaries)} total)", file=sys.stderr)
            for summary in todo:
                process_activity(access_token, state, summary)
                epoch = epoch_of(summary)
                state["backfill_cursor_epoch"] = epoch
                state["last_processed_epoch"] = max(state["last_processed_epoch"], epoch)
            state["full_backfill_complete"] = True
            print("Full backfill complete", file=sys.stderr)
        else:
            summaries = list_activity_summaries(access_token, state["last_processed_epoch"])
            print(f"Found {len(summaries)} new activities to check", file=sys.stderr)
            for summary in summaries:
                process_activity(access_token, state, summary)
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
