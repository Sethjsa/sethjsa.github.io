#!/usr/bin/env python3
"""Scrape concert stats from a public Concert Archives profile page.

Concert Archives has no public API, and the site sits behind a
Cloudflare JS challenge that a plain HTTP request can't get past, so
this uses Playwright (a real headless browser) instead of urllib like
the other scripts here. That's heavier (needs `playwright install
chromium` in CI) and more fragile (depends on the site's HTML, which
could change), so this runs weekly rather than daily, and fails soft:
if scraping breaks, it prints a warning and leaves the last known-good
_data/concert_stats.yml alone rather than crashing the whole run.

The profile overview page (concertarchives.org/<username>) already has
everything needed pre-computed server-side: favorite (ranked) concerts,
most recent concerts, top locations with counts, and grand totals -
so one page load covers it all.

Usage:
    python scripts/update_concert_stats.py
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "_data" / "concert_stats.yml"

PROFILE_URL = "https://www.concertarchives.org/setheryj"
BASE_URL = "https://www.concertarchives.org"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_html():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(PROFILE_URL, wait_until="load", timeout=30000)
        page.wait_for_timeout(4000)  # let the Cloudflare challenge resolve
        html = page.content()
        browser.close()
        return html


def section_table(soup, heading_text):
    """Find the <h2>/<caption> containing heading_text and return its table.

    <caption> lives inside its own table, so the table is an ancestor.
    <h2> section headers sit before a separate <table>, so the table is
    the next one in document order.
    """
    node = soup.find(string=re.compile(re.escape(heading_text)))
    if node is None:
        return None
    container = node.find_parent(["h2", "caption"])
    if container is None:
        return None
    if container.name == "caption":
        return container.find_parent("table")
    return container.find_next("table")


def parse_concert_rows(table, limit=None):
    if table is None:
        return []
    rows = []
    for tr in table.select("tbody tr"):
        cells = [c for c in tr.find_all("td") if c.get_text(strip=True) or c.find("a")]
        if len(cells) < 4:
            continue
        date_cell, concert_cell, venue_cell, location_cell = cells[:4]
        date_text = date_cell.get_text(strip=True)
        concert_link = concert_cell.find("a")
        venue_link = venue_cell.find("a")
        location_link = location_cell.find("a")
        if concert_link is None:
            continue
        rows.append({
            "date": date_text,
            "name": concert_link.get_text(strip=True),
            "url": BASE_URL + concert_link.get("href", ""),
            "venue": venue_link.get_text(strip=True) if venue_link else None,
            "location": location_link.get_text(strip=True) if location_link else None,
        })
        if limit and len(rows) >= limit:
            break
    return rows


def parse_count_table(table, limit=None):
    """Parse a two-column {label link, count} table like Top Locations/Bands/Venues."""
    if table is None:
        return []
    rows = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        name_cell, count_cell = cells[0], cells[1]
        link = name_cell.find("a")
        name = (link or name_cell).get_text(strip=True)
        count_text = count_cell.get_text(strip=True)
        count_match = re.search(r"[\d,]+", count_text)
        count = int(count_match.group().replace(",", "")) if count_match else None
        rows.append({"name": name, "count": count})
        if limit and len(rows) >= limit:
            break
    return rows


def parse_grand_totals(table):
    if table is None:
        return {}
    out = {}
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True)
        value_text = cells[1].get_text(strip=True)
        match = re.search(r"[\d,]+", value_text)
        out[label] = int(match.group().replace(",", "")) if match else value_text
    return out


def parse_header_summary(soup):
    header = soup.find("h1", class_="body-content")
    if header is None:
        return {}
    text = header.get_text(" ", strip=True)
    numbers = {}
    for label in ("performances", "concerts", "bands", "venues", "locations"):
        m = re.search(r"([\d,]+)\s+" + label, text)
        if m:
            numbers[label] = int(m.group(1).replace(",", ""))
    return numbers


def main():
    try:
        html = fetch_html()
    except Exception as e:
        print(f"Failed to fetch Concert Archives page ({e!r}) - leaving existing data alone", file=sys.stderr)
        return

    soup = BeautifulSoup(html, "html.parser")

    summary = parse_header_summary(soup)
    favorites = parse_concert_rows(section_table(soup, "Favorite Concerts"), limit=5)
    recent = parse_concert_rows(section_table(soup, "Most Recent Concerts"), limit=5)
    top_locations = parse_count_table(section_table(soup, "Top Locations"), limit=5)
    for loc in top_locations:
        loc["name"] = loc["name"].split(",")[0].strip()
    grand_totals = parse_grand_totals(section_table(soup, "Grand Totals"))

    if not recent and not favorites:
        print("Parsed zero concerts - site markup likely changed, leaving existing data alone", file=sys.stderr)
        return

    out = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "summary": summary,
        "grand_totals": grand_totals,
        "top_ranked": favorites,
        "most_recent": recent,
        "top_locations": top_locations,
    }
    with open(DATA_FILE, "w") as f:
        f.write("# Auto-generated by scripts/update_concert_stats.py - do not hand-edit.\n")
        yaml.dump(out, f, sort_keys=False, allow_unicode=True)
    print(f"Wrote {DATA_FILE} ({len(recent)} recent, {len(favorites)} favorites)", file=sys.stderr)


if __name__ == "__main__":
    main()
