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
     block per tile) - a literal "squares" visualisation - and again
     overlaid on real OpenStreetMap raster tiles for geographic context.

These numbers won't be bit-identical to StatsHunters' own UI (its
exact "cluster"/"touching tiles" definitions aren't public), but the
core metrics were verified against the real dashboard numbers during
development and matched closely.

Usage:
    python scripts/update_squadrats_stats.py
"""
import io
import json
import math
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from PIL import Image, ImageDraw
from shapely.geometry import Point, shape
from shapely.ops import transform
from shapely.prepared import prep

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "_data" / "squadrats_stats.yml"
HEATMAP_FILE = REPO_ROOT / "images" / "squadrats_tiles.svg"
MAP_FILE = REPO_ROOT / "images" / "squadrats_map.png"

SHARE_HASH = "4a769afeb9f9"
API_BASE = f"https://www.statshunters.com/share/{SHARE_HASH}/api"
ZOOM = 14
COUNTRIES_URL = "https://raw.githubusercontent.com/datasets/geo-countries/main/data/countries.geojson"
USER_AGENT = "sethjsa.github.io stats script (+https://sethjsa.github.io)"

# The largest connected cluster's *bounding box* can be far bigger than its
# tile count if it's a sprawling/winding shape (e.g. a route network) rather
# than a compact blob, so fetching one real map tile per zoom-ZOOM cell in
# that bbox can mean thousands of tile requests for a mostly-empty area.
# Background map imagery is fetched at a coarser zoom instead (few tiles,
# one HTTP request each) and the visited squares are projected onto it -
# web-mercator tile coordinates scale by an exact power of two between zoom
# levels, so this needs no lat/lng round-trip, just integer division.
OSM_TILE_PX = 256
BG_MIN_ZOOM = 6
BG_TARGET_TILES = 5
MAP_OUTPUT_MAX_DIM = 480


def fetch_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
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


def latlng_to_tile_float(lat, lng, zoom=ZOOM):
    """Continuous (non-floored) version of latlng_to_tile, for projecting
    arbitrary coordinates (e.g. a country border) into the same
    tile-space grid the visited tiles use."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = (lng + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def tile_center_latlng(x, y, zoom=ZOOM):
    n = 2 ** zoom
    lng = (x + 0.5) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n)))
    return math.degrees(lat_rad), lng


# Indoor/simulated activity types with no real-world location. VirtualRide
# in particular (e.g. MyWhoosh) ships GPS-shaped coordinates for a
# *simulated* course, which decode to real but entirely fictional
# locations (verified: one such activity's "route" sits in Saudi Arabia,
# another mid-Amazon) - excluded so they don't inject phantom countries.
NON_OUTDOOR_TYPES = {"VirtualRide", "Workout", "Yoga", "WeightTraining", "StairStepper"}


def tiles_on_segment(x0, y0, x1, y1):
    """Grid traversal (supercover line) between two points in continuous
    tile-space coordinates: every integer tile the straight segment passes
    through, not just the tiles its two endpoints happen to fall in.

    This matters because the polyline points fed in are GPS samples, not
    every point along the route - on a long or fast-moving stretch (e.g. a
    highway ride) consecutive samples can be several tiles apart, and even
    adjacent samples that step diagonally (x and y tile index both change)
    can skip the "elbow" tile a continuous path would actually cross.
    Verified against real ride data: ~0.1% of consecutive-point pairs are a
    diagonal or multi-tile jump, and those gaps lined up with exactly the
    missing loop segments reported against the StatsHunters reference map.
    """
    tx0, ty0 = int(math.floor(x0)), int(math.floor(y0))
    tx1, ty1 = int(math.floor(x1)), int(math.floor(y1))
    tiles = {(tx0, ty0)}
    if tx0 == tx1 and ty0 == ty1:
        return tiles

    dx, dy = x1 - x0, y1 - y0
    stepx = 1 if dx > 0 else -1
    stepy = 1 if dy > 0 else -1

    t_max_x = ((tx0 + (1 if dx > 0 else 0)) - x0) / dx if dx != 0 else float("inf")
    t_max_y = ((ty0 + (1 if dy > 0 else 0)) - y0) / dy if dy != 0 else float("inf")
    t_delta_x = abs(1.0 / dx) if dx != 0 else float("inf")
    t_delta_y = abs(1.0 / dy) if dy != 0 else float("inf")

    tx, ty = tx0, ty0
    while (tx, ty) != (tx1, ty1):
        if t_max_x < t_max_y:
            tx += stepx
            t_max_x += t_delta_x
        else:
            ty += stepy
            t_max_y += t_delta_y
        tiles.add((tx, ty))
    return tiles


def collect_visited_tiles(activities, lines):
    id_to_line = {a["id"]: a["data"] for a in lines if a.get("data")}
    tile_counts = defaultdict(int)
    for act in activities:
        if act.get("type") in NON_OUTDOOR_TYPES:
            continue
        encoded = id_to_line.get(act["id"])
        if not encoded:
            continue
        points = decode_polyline(encoded)
        tiles = set()
        prev = None
        for lat, lng in points:
            x, y = latlng_to_tile_float(lat, lng)
            if prev is not None:
                tiles |= tiles_on_segment(prev[0], prev[1], x, y)
            else:
                tiles.add((int(math.floor(x)), int(math.floor(y))))
            prev = (x, y)
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


def _lnglat_to_tile_coords(lng, lat):
    return latlng_to_tile_float(lat, lng)


def count_tiles_in_geom(geom):
    """Estimate how many zoom-ZOOM tiles a country's geometry covers.

    A brute-force "test every candidate tile's center" scan is what the
    numerator (visited tiles) uses, but running it for the denominator too
    is infeasible for a whole country: a fragmented/archipelago country
    (e.g. Finland) has a huge bounding box relative to its land area, and a
    physically huge country (e.g. the US) has tens of millions of
    candidate tiles. Both previously either got skipped outright or had
    their bounding box silently truncated by a size guard, which produced
    wildly undercounted totals.

    Since the web-mercator tile grid is uniform in projected space - each
    zoom-ZOOM tile is exactly a 1x1 square in tile-coordinate space - the
    country polygon's area *in tile coordinates* is a very close estimate
    of the number of tiles it covers, computed in O(vertices) instead of
    O(candidate tiles), regardless of how large or fragmented the country
    is.
    """
    projected = transform(_lnglat_to_tile_coords, geom)
    return projected.area


def compute_country_completion(visited, countries):
    if not visited:
        return [], {}

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
        total = round(count_tiles_in_geom(country["geom"]))
        if total == 0:
            continue
        results.append({
            "name": name,
            "visited": len(tiles),
            "total": total,
            "pct": round(len(tiles) / total * 100, 1),
        })

    results.sort(key=lambda r: r["pct"], reverse=True)
    return results, visited_by_country


def render_tile_grid(tiles, scale=8, pad=1):
    """Render the tile grid as SVG rather than raster - it's just solid
    blocks on a grid, so a vector square per tile stays crisp at any zoom
    instead of blurring like a small raster does when scaled up."""
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    w = (max_x - min_x + 1 + pad * 2) * scale
    h = (max_y - min_y + 1 + pad * 2) * scale

    color = "#fc4c02"  # Strava orange, matches the rest of the outdoors section
    rects = []
    for (x, y) in sorted(tiles):
        px0 = (x - min_x + pad) * scale
        py0 = (y - min_y + pad) * scale
        rects.append(f'<rect x="{px0}" y="{py0}" width="{scale - 1}" height="{scale - 1}" fill="{color}"/>')

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}">\n' + "\n".join(rects) + "\n</svg>\n"
    )

    HEATMAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEATMAP_FILE.write_text(svg)


def fetch_osm_tile(z, x, y, size):
    """Fetch a single OpenStreetMap raster tile and downscale it to
    `size`x`size`. Falls back to a plain grey square on any fetch error so
    one missing tile doesn't fail the whole render - the public tile
    server is unauthenticated and best-effort."""
    url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            tile = Image.open(io.BytesIO(resp.read())).convert("RGBA")
        return tile.resize((size, size), Image.LANCZOS)
    except Exception as e:
        print(f"  osm tile fetch failed for {z}/{x}/{y}: {e!r}", file=sys.stderr)
        return Image.new("RGBA", (size, size), (230, 230, 230, 255))


def choose_background_zoom(grid_w, grid_h):
    """Pick a zoom level for the background map tiles that covers the
    tile-grid's bounding box in roughly BG_TARGET_TILES tiles along its
    longer side, without zooming out past BG_MIN_ZOOM."""
    span = max(grid_w, grid_h)
    if span <= BG_TARGET_TILES:
        return ZOOM
    zoom_out = max(0, round(math.log2(span / BG_TARGET_TILES)))
    return max(BG_MIN_ZOOM, ZOOM - zoom_out)


def render_map_overlay(tiles, pad=1):
    """Same tiles as render_tile_grid, overlaid on real OpenStreetMap raster
    tiles for geographic context. Background imagery is fetched at a zoom
    coarser than the zoom-ZOOM visited-tile grid (see choose_background_zoom)
    and each visited tile is projected onto it as a small rectangle."""
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    grid_w = max_x - min_x + 1 + pad * 2
    grid_h = max_y - min_y + 1 + pad * 2

    bg_zoom = choose_background_zoom(grid_w, grid_h)
    factor = 2 ** (ZOOM - bg_zoom)  # zoom-ZOOM tile units per background tile

    bg_min_x = math.floor((min_x - pad) / factor)
    bg_max_x = math.floor((max_x + pad) / factor)
    bg_min_y = math.floor((min_y - pad) / factor)
    bg_max_y = math.floor((max_y + pad) / factor)
    bg_grid_w = bg_max_x - bg_min_x + 1
    bg_grid_h = bg_max_y - bg_min_y + 1

    base = Image.new("RGBA", (bg_grid_w * OSM_TILE_PX, bg_grid_h * OSM_TILE_PX), (255, 255, 255, 255))
    for gx in range(bg_grid_w):
        for gy in range(bg_grid_h):
            tile_img = fetch_osm_tile(bg_zoom, bg_min_x + gx, bg_min_y + gy, OSM_TILE_PX)
            base.paste(tile_img, (gx * OSM_TILE_PX, gy * OSM_TILE_PX))

    # Draw the visited squares on a separate transparent layer and alpha-
    # composite it over the fetched map tiles, rather than drawing directly
    # onto `base` - a direct draw would overwrite pixels outright (including
    # alpha), losing the map detail a translucent overlay is meant to show
    # through.
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    tile_color = (252, 76, 2, 140)
    square_px = OSM_TILE_PX / factor
    for (x, y) in tiles:
        px0 = (x / factor - bg_min_x) * OSM_TILE_PX
        py0 = (y / factor - bg_min_y) * OSM_TILE_PX
        draw.rectangle([px0, py0, px0 + square_px, py0 + square_px], fill=tile_color)

    img = Image.alpha_composite(base, overlay)

    if max(img.size) > MAP_OUTPUT_MAX_DIM:
        ratio = MAP_OUTPUT_MAX_DIM / max(img.size)
        new_size = (max(1, round(img.width * ratio)), max(1, round(img.height * ratio)))
        img = img.resize(new_size, Image.LANCZOS)

    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    img.save(MAP_FILE)


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
    countries = fetch_country_polygons()
    country_completion, visited_by_country = compute_country_completion(visited, countries)

    # Render just the largest connected cluster - rendering the whole home
    # country made the box far too big (Den Helder to Rotterdam). This is
    # meant to be a small square showing the biggest connected grid.
    render_tiles = components[0]
    render_tile_grid(render_tiles)
    render_map_overlay(render_tiles)

    out = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "zoom": ZOOM,
        **stats,
        "country_completion": country_completion,
    }
    with open(DATA_FILE, "w") as f:
        f.write("# Auto-generated by scripts/update_squadrats_stats.py - do not hand-edit.\n")
        yaml.dump(out, f, sort_keys=False, allow_unicode=True)
    print(f"Wrote {DATA_FILE}, {HEATMAP_FILE} and {MAP_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
