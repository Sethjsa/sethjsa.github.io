#!/usr/bin/env python3
"""Compute tile-hunting ("squadrats"/explorer-tile) stats from a public
StatsHunters share link.

StatsHunters has no documented public API, but its public share pages
(https://www.statshunters.com/share/<hash>) call a JSON API scoped to
that share hash with no login required:
  /share/<hash>/api/activities?page=N        - activity metadata, paginated
  /share/<hash>/api/activities/lines?page=N  - encoded polyline per activity

The site itself computes "explorer tile" stats (max square, max
cluster, etc. - the VeloViewer-style tile-hunting metrics) client-side
in the browser from this same raw data, rather than exposing them via
an endpoint, so this script reimplements that from scratch:
  1. Decode every activity's polyline and bucket each point into a
     zoom-14 web-mercator tile (the standard "explorer tile" zoom).
  2. From the resulting visited-tile set, compute: total tiles, the
     largest fully-visited square, the largest connected cluster, the
     longest connected row/column, and the single most-visited tile.
  3. Cross-reference visited tiles against a public country-boundary
     dataset to compute per-country tile completion percentages.
  4. Render the largest cluster as a small pixel-grid PNG (one pixel
     block per tile) - a literal "squares" visualisation.

These numbers won't be bit-identical to StatsHunters' own UI (its
exact "cluster"/"touching tiles" definitions aren't public), but the
core metrics were verified against the real dashboard numbers during
development and matched closely.

Usage:
    python scripts/update_squadrats_stats.py
"""
import json
import math
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from PIL import Image
from shapely.geometry import Point, shape
from shapely.prepared import prep

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "_data" / "squadrats_stats.yml"
HEATMAP_FILE = REPO_ROOT / "images" / "squadrats_tiles.png"

SHARE_HASH = "4a769afeb9f9"
API_BASE = f"https://www.statshunters.com/share/{SHARE_HASH}/api"
ZOOM = 14
COUNTRIES_URL = "https://raw.githubusercontent.com/datasets/geo-countries/main/data/countries.geojson"
MAX_COUNTRY_TILES = 300_000  # skip completion % for absurdly large countries (compute safety)
TOP_COUNTRIES = 10


def fetch_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sethjsa.github.io stats script"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_pages(endpoint):
    page = 1
    items = []
    while True:
        data = fetch_json(f"{API_BASE}/{endpoint}?page={page}")
        batch = data.get("activities", [])
        items.extend(batch)
        if len(batch) < 500:
            break
        page += 1
    return items


def decode_polyline(encoded):
    """Standard Google encoded-polyline decoder -> list of (lat, lng)."""
    points = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)
    while index < length:
        result = 1
        shift = 0
        while True:
            b = ord(encoded[index]) - 63 - 1
            index += 1
            result += b << shift
            shift += 5
            if b < 0x1f:
                break
        lat += ~(result >> 1) if (result & 1) else (result >> 1)

        result = 1
        shift = 0
        while True:
            b = ord(encoded[index]) - 63 - 1
            index += 1
            result += b << shift
            shift += 5
            if b < 0x1f:
                break
        lng += ~(result >> 1) if (result & 1) else (result >> 1)

        points.append((lat * 1e-5, lng * 1e-5))
    return points


def latlng_to_tile(lat, lng, zoom=ZOOM):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lng + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile_center_latlng(x, y, zoom=ZOOM):
    n = 2 ** zoom
    lng = (x + 0.5) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n)))
    return math.degrees(lat_rad), lng


def collect_visited_tiles(activities, lines):
    id_to_line = {a["id"]: a["data"] for a in lines if a.get("data")}
    tile_counts = defaultdict(int)
    for act in activities:
        encoded = id_to_line.get(act["id"])
        if not encoded:
            continue
        points = decode_polyline(encoded)
        tiles = set(latlng_to_tile(lat, lng) for lat, lng in points)
        for t in tiles:
            tile_counts[t] += 1
    return tile_counts


def connected_components(visited):
    seen = set()
    components = []
    for tile in visited:
        if tile in seen:
            continue
        stack = [tile]
        seen.add(tile)
        comp = []
        while stack:
            t = stack.pop()
            comp.append(t)
            x, y = t
            for nb in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if nb in visited and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        components.append(comp)
    components.sort(key=len, reverse=True)
    return components


def max_square(visited):
    dp = {}
    best = 0
    for tile in sorted(visited):
        x, y = tile
        d = 1 + min(dp.get((x - 1, y), 0), dp.get((x, y - 1), 0), dp.get((x - 1, y - 1), 0))
        dp[tile] = d
        best = max(best, d)
    return best


def max_run(visited, group_key, run_key):
    groups = defaultdict(list)
    for t in visited:
        groups[group_key(t)].append(run_key(t))
    best = 0
    for vals in groups.values():
        vals.sort()
        run = 1
        best = max(best, run)
        for i in range(1, len(vals)):
            run = run + 1 if vals[i] == vals[i - 1] + 1 else 1
            best = max(best, run)
    return best


def compute_tile_stats(tile_counts):
    visited = set(tile_counts.keys())
    components = connected_components(visited)
    top_tile, top_visits = max(tile_counts.items(), key=lambda kv: kv[1])
    return {
        "total_tiles": len(visited),
        "max_square": max_square(visited),
        "max_cluster_tiles": len(components[0]) if components else 0,
        "max_connected_row": max_run(visited, group_key=lambda t: t[1], run_key=lambda t: t[0]),
        "max_connected_column": max_run(visited, group_key=lambda t: t[0], run_key=lambda t: t[1]),
        "max_visited_tile_visits": top_visits,
    }, components


def fetch_country_polygons():
    data = fetch_json(COUNTRIES_URL)
    countries = []
    for feature in data["features"]:
        geom = shape(feature["geometry"])
        minx, miny, maxx, maxy = geom.bounds
        countries.append({
            "name": feature["properties"].get("name"),
            "geom": geom,
            "prepared": prep(geom),
            "bounds": (minx, miny, maxx, maxy),
        })
    return countries


def country_parts(geom):
    """Split a country's geometry into its constituent polygons so a
    far-flung overseas territory (e.g. Caribbean Netherlands) doesn't
    blow up the whole country's bounding box - each part gets its own
    bbox and size guard, and totals are summed across parts."""
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    return [geom]


def count_tiles_in_geom(geom, prepared):
    minx, miny, maxx, maxy = geom.bounds
    min_tx, max_ty = latlng_to_tile(miny, minx)
    max_tx, min_ty = latlng_to_tile(maxy, maxx)
    min_tx, max_tx = sorted((min_tx, max_tx))
    min_ty, max_ty = sorted((min_ty, max_ty))
    candidate_count = (max_tx - min_tx + 1) * (max_ty - min_ty + 1)
    if candidate_count > MAX_COUNTRY_TILES:
        return None

    total = 0
    for tx in range(min_tx, max_tx + 1):
        for ty in range(min_ty, max_ty + 1):
            lat, lng = tile_center_latlng(tx, ty)
            if prepared.contains(Point(lng, lat)):
                total += 1
    return total


def compute_country_completion(visited):
    if not visited:
        return []

    countries = fetch_country_polygons()

    # numerator: which country each visited tile falls in
    visited_by_country = defaultdict(set)
    for tile in visited:
        lat, lng = tile_center_latlng(*tile)
        pt = Point(lng, lat)
        for c in countries:
            minx, miny, maxx, maxy = c["bounds"]
            if minx <= lng <= maxx and miny <= lat <= maxy and c["prepared"].contains(pt):
                visited_by_country[c["name"]].add(tile)
                break

    results = []
    for name, tiles in visited_by_country.items():
        country = next(c for c in countries if c["name"] == name)
        total = 0
        skipped_a_part = False
        for part in country_parts(country["geom"]):
            part_total = count_tiles_in_geom(part, prep(part))
            if part_total is None:
                skipped_a_part = True
                continue
            total += part_total
        if total == 0:
            continue
        if skipped_a_part:
            print(f"Note: {name} has an oversized part excluded from its total tile count", file=sys.stderr)
        results.append({
            "name": name,
            "visited": len(tiles),
            "total": total,
            "pct": round(len(tiles) / total * 100, 1),
        })

    results.sort(key=lambda r: r["pct"], reverse=True)
    return results[:TOP_COUNTRIES]


def render_tile_grid(tiles, scale=4, pad=1):
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    w = (max_x - min_x + 1 + pad * 2) * scale
    h = (max_y - min_y + 1 + pad * 2) * scale

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pixels = img.load()
    color = (252, 76, 2, 255)  # Strava orange, matches the rest of the outdoors section
    for (x, y) in tiles:
        px0 = (x - min_x + pad) * scale
        py0 = (y - min_y + pad) * scale
        for dx in range(scale - 1):
            for dy in range(scale - 1):
                pixels[px0 + dx, py0 + dy] = color

    HEATMAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    img.save(HEATMAP_FILE)


def main():
    try:
        activities = fetch_all_pages("activities")
        lines = fetch_all_pages("activities/lines")
    except Exception as e:
        print(f"Failed to fetch StatsHunters data ({e!r}) - leaving existing data alone", file=sys.stderr)
        return

    tile_counts = collect_visited_tiles(activities, lines)
    visited = set(tile_counts.keys())

    if not visited:
        print("No visited tiles found - leaving existing data alone", file=sys.stderr)
        return

    stats, components = compute_tile_stats(tile_counts)
    country_completion = compute_country_completion(visited)
    render_tile_grid(components[0])

    out = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "zoom": ZOOM,
        **stats,
        "country_completion": country_completion,
    }
    with open(DATA_FILE, "w") as f:
        f.write("# Auto-generated by scripts/update_squadrats_stats.py - do not hand-edit.\n")
        yaml.dump(out, f, sort_keys=False, allow_unicode=True)
    print(f"Wrote {DATA_FILE} and {HEATMAP_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
