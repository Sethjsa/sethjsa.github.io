#!/usr/bin/env python3
"""Pull Strava activities and extract Summit Bag stats from their descriptions.

Summit Bag (summitbag.com) appends a line like this to each Strava activity
description it processes:

    (peak) Kusushidake Peak (3,725 m) * (peak) Joujugatake Peak (3,734 m) * ...
    (foot) 2026 = 28,693 m | (globe) summitbag.com

This script pulls recent Strava activities via the Strava API, parses those
lines out of each activity's description, and merges the results into
_data/summitbag_stats.yml, which the site reads at build time.

Usage:
    python scripts/update_summitbag_stats.py            # incremental: only activities since last run
    python scripts/update_summitbag_stats.py --full      # rescan entire activity history

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

TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"

PEAK_RE = re.compile(r"⛰️\s*([^•\n]+?)\s*\(\s*([\d,]+)\s*m\s*\)")
ELEVATION_RE = re.compile(r"⬆️\s*(\d{4})\s*=\s*([\d,]+)\s*m")

RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
RIDE_TYPES = {"Ride", "MountainBikeRide", "GravelRide", "EBikeRide", "VirtualRide", "Velomobile", "Handcycle"}
HIKE_TYPES = {"Hike", "Walk", "Snowshoe"}
SKI_TYPES = {"AlpineSki", "BackcountrySki", "NordicSki", "Snowboard"}

# Strava enforces ~100 requests / 15 minutes on the default rate limit.
# Stay comfortably under that.
MAX_REQUESTS_PER_WINDOW = 90
WINDOW_SECONDS = 900

_request_times = collections.deque()


def _throttle():
    now = time.monotonic()
    while _request_times and now - _request_times[0] > WINDOW_SECONDS:
        _request_times.popleft()
    if len(_request_times) >= MAX_REQUESTS_PER_WINDOW:
        sleep_for = WINDOW_SECONDS - (now - _request_times[0]) + 1
        print(f"  rate limit pacing: sleeping {sleep_for:.0f}s", file=sys.stderr)
        time.sleep(max(sleep_for, 0))
    _request_times.append(time.monotonic())


def _get(url, access_token, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    _throttle()
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 60 * (attempt + 1)
                print(f"  429 rate limited, sleeping {wait}s", file=sys.stderr)
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
    """Yield activity summaries (id, sport_type, start_date) newest-first."""
    page = 1
    while True:
        params = {"per_page": 100, "page": page}
        if after_epoch is not None:
            params["after"] = after_epoch
        batch = _get(f"{API_BASE}/athlete/activities", access_token, params)
        if not batch:
            return
        for activity in batch:
            yield activity
        page += 1


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


def load_state():
    if not DATA_FILE.exists():
        return {"last_processed_epoch": 0, "peaks": {}, "elevation": {}}
    with open(DATA_FILE) as f:
        raw = yaml.safe_load(f) or {}
    peaks = {p["name"]: p for p in raw.get("peaks", [])}
    return {
        "last_processed_epoch": raw.get("last_processed_epoch", 0),
        "peaks": peaks,
        "elevation": raw.get("elevation", {}),
    }


def save_state(state):
    peaks_sorted = sorted(state["peaks"].values(), key=lambda p: p["date"])
    latest_peaks = [dict(p) for p in reversed(peaks_sorted[-5:])]
    top_peaks = [dict(p) for p in sorted(peaks_sorted, key=lambda p: p["height_m"], reverse=True)[:5]]
    out = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "last_processed_epoch": state["last_processed_epoch"],
        "peak_count": len(peaks_sorted),
        "latest_peaks": latest_peaks,
        "top_peaks": top_peaks,
        "peaks": peaks_sorted,
        "elevation": state["elevation"],
    }
    with open(DATA_FILE, "w") as f:
        f.write("# Auto-generated by scripts/update_summitbag_stats.py - do not hand-edit.\n")
        yaml.dump(out, f, sort_keys=False, allow_unicode=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="rescan entire activity history")
    args = parser.parse_args()

    access_token = get_access_token()
    state = load_state()

    after_epoch = None if args.full else state["last_processed_epoch"]
    summaries = list(list_activity_summaries(access_token, after_epoch))
    summaries.sort(key=lambda a: a["start_date"])  # oldest -> newest

    print(f"Found {len(summaries)} activities to check", file=sys.stderr)

    newest_epoch = state["last_processed_epoch"]
    for summary in summaries:
        detail = get_activity_detail(access_token, summary["id"])
        peaks, elevation_reading = parse_description(detail.get("description"))
        sport_type = summary.get("sport_type") or summary.get("type", "Other")
        bucket = bucket_for(sport_type)
        date = summary["start_date"][:10]

        for name, height_m in peaks:
            if name not in state["peaks"]:
                state["peaks"][name] = {
                    "name": name,
                    "height_m": height_m,
                    "type": bucket,
                    "date": date,
                }

        if elevation_reading:
            year, total_m = elevation_reading
            state["elevation"].setdefault(bucket, {})
            state["elevation"][bucket][year] = total_m

        activity_epoch = int(datetime.fromisoformat(summary["start_date"].replace("Z", "+00:00")).timestamp())
        newest_epoch = max(newest_epoch, activity_epoch)

    state["last_processed_epoch"] = newest_epoch
    save_state(state)
    print(f"Wrote {DATA_FILE} ({len(state['peaks'])} peaks)", file=sys.stderr)


if __name__ == "__main__":
    main()
