#!/usr/bin/env python3
"""
generate_feed.py

Auto-generate an RSS feed (rss.xml) from one or more dlraw-style index pages.
- Writes rss.xml and seen.json in the current directory.
- Runs a single iteration and exits (scheduling should be handled by your workflow/cron).
- Easy to add more sites: edit the SITES list below.
- Keeps only newest releases (per-site canonicalization).
- Each item includes a link back to the dlraw index page (not to mirror hosts),
  a description with an <img> thumbnail, and media/enclosure tags.

Usage:
    python generate_feed.py            # run once and exit
    python generate_feed.py --debug    # verbose logging
    python generate_feed.py --max 40   # keep up to 40 items in feed

Dependencies:
    pip install requests beautifulsoup4
"""

from __future__ import annotations
import os
import re
import json
import argparse
import logging
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from xml.sax.saxutils import escape
from xml.dom import minidom
import tempfile

# -----------------------
# Configuration: add sites here
# -----------------------
SITES = [
    {
        "title": "Young King Ours",
        "url": "https://dlraw.cc/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89/%E3%83%A4%E3%83%B3%E3%82%B0%E3%82%AD%E3%83%B3%E3%82%B0%E3%82%A2%E3%83%AF%E3%83%BC%E3%82%BA/",
        "thumb": "https://upload.wikimedia.org/wikipedia/commons/8/89/Portrait_Placeholder.png",
    },{
        "title": "Youjo Senki",
        "url": "https://dlraw.cc/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89/%E5%B9%BC%E5%A5%B3%E6%88%A6%E8%A8%98/",
        "thumb": "https://puu.sh/KKZc7.png",
    },{
        "title": "Isekai Meikyuu de Harem wo",
        "url": "https://dlraw.cc/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89/%E7%95%B0%E4%B8%96%E7%95%8C%E8%BF%B7%E5%AE%AE%E3%81%A7%E3%83%8F%E3%83%BC%E3%83%AC%E3%83%A0%E3%82%92/",
        "thumb": "https://static.zerochan.net/Isekai.Meikyuu.de.Harem.wo.1024.3730804.webp",
    },
    # Add more sites here as needed:
    # {"title": "Another Series", "url": "https://dlraw.cc/.../", "thumb": "https://..."},
]

# Feed metadata
FEED_TITLE = "DL-Raw Watchlist"
FEED_LINK = "https://example.com/"
FEED_DESC = "Auto-generated manga feed (dlraw watcher)"
RSS_FILE = "rss.xml"
SEEN_FILE = "seen.json"
MAX_ITEMS = 50  # keep this many items in the feed

# HTTP settings (place near top of file)
REQUEST_TIMEOUT = 20.0
USER_AGENT = "MangaFeedBot/1.0 (+https://example.com/)"
HEADERS = {"User-Agent": USER_AGENT}

# simple fetch helper used by gather_latest_from_site
def fetch_page(url: str) -> str | None:
    try:
        import requests
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logging.debug("fetch_page error for %s: %s", url, e)
        return None


# MIME helper
MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
}

def mime_for_url(url: str) -> str:
    if not url:
        return "image/jpeg"
    u = url.split("?", 1)[0].split("#", 1)[0].lower()
    for ext, m in MIME_BY_EXT.items():
        if u.endswith(ext):
            return m
    return "image/jpeg"
# --- simple heuristics to detect release-like text ---
RAR_RE = re.compile(r"\.rar\b", flags=re.I)

def is_rar_like(text: str) -> bool:
    """
    Return True for strings that look like release filenames or version tokens,
    e.g. 'Young_King_Ours_2026-04.rar', 'Senki_v34.rar', 'v34', '2026-04.rar'.
    """
    if not text:
        return False
    t = text.strip()
    # obvious .rar filename
    if RAR_RE.search(t):
        return True
    # date-like YYYY-MM or YYYY_MM
    if re.search(r"\b20\d{2}[-_.]?(0[1-9]|1[0-2])\b", t):
        return True
    # vNN pattern
    if re.search(r"\bv\s?0*[0-9]+\b", t, flags=re.I):
        return True
    # trailing numeric tokens like _34 or -34 or v34
    if re.search(r"[_\-\s]v?0*[0-9]{1,6}\b", t, flags=re.I):
        return True
    return False

# -----------------------
# Parsing/version heuristics
# -----------------------
DATE_RE = re.compile(r"(20\d{2})[._-]?(0[1-9]|1[0-2])")
VNUM_RE = re.compile(r"\bv\s?0*([0-9]+)\b", flags=re.I)
TRAIL_NUM_RE = re.compile(r"[_\-\s]v?0*([0-9]+)(?:\.[a-zA-Z0-9]+)?$", flags=re.I)
RAR_RE = re.compile(r"\.rar\b", flags=re.I)

# --- normalize candidate text (strip mirror noise) ---
def normalize_candidate_text(s: str) -> str:
    if not s:
        return ""
    t = s.strip()
    # remove common mirror host suffixes like "(uploaded by ...)" or "[host]" or trailing " - host"
    t = re.sub(r"\(.*?\)$", "", t)
    t = re.sub(r"\[.*?\]$", "", t)
    t = re.sub(r"-\s*uploaded.*$", "", t, flags=re.I)
    t = re.sub(r"\s+[-–—]\s+.*$", "", t)  # remove trailing " - something"
    t = t.strip(" \t\n\r\"'_-")
    return t


# --- improved version key extraction (numeric, prefer largest vN) ---
def extract_version_key(title: str):
    """
    Return a tuple for comparison. Higher tuple sorts later (newer).
    Priority:
      3 -> date-like YYYY-MM (year, month)
      2 -> vNN (use max v found)
      1 -> any numeric tokens (use max number)
      0 -> fallback (string)
    """
    t = normalize_candidate_text(title or "")
    # date-like YYYY-MM
    m = DATE_RE.search(t)
    if m:
        year = int(m.group(1)); month = int(m.group(2))
        return (3, year, month, t)

    # collect all vN occurrences and pick the largest
    # allow separators like start, space, underscore, or hyphen before 'v'
    vnums = [int(x) for x in re.findall(r"(?:^|[_\-\s])v\s?0*([0-9]+)\b", t, flags=re.I)]
    if vnums:
        return (2, max(vnums), t)

    # collect any numeric tokens and pick the largest
    # look for numbers preceded by separator or start to avoid matching years inside words
    nums = [int(x) for x in re.findall(r"(?:^|[_\-\s])0*([0-9]{1,6})\b", t)]
    if nums:
        return (1, max(nums), t)

    # fallback: use the raw normalized title (lowest priority)
    return (0, 0, t)

# --- improved dlraw index parsing: inspect anchor text and href filename ---
def parse_dlraw_index(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    # 1) anchors: inspect both anchor text and href last path segment
    for a in soup.find_all("a"):
        text = (a.get_text() or "").strip()
        href = a.get("href") or ""
        # normalize and test anchor text
        nt = normalize_candidate_text(text)
        if nt and is_rar_like(nt):
            candidates.append({"title": nt, "link": base_url, "thumb": None})
            continue
        # inspect href last segment (filename)
        if href:
            last = href.split("/")[-1].strip()
            last = normalize_candidate_text(last)
            if last and is_rar_like(last):
                candidates.append({"title": last, "link": base_url, "thumb": None})
                continue

    # 2) fallback: text nodes (if no anchor candidates found)
    if not candidates:
        for tag in soup.find_all(string=True):
            txt = tag.strip()
            if not txt:
                continue
            nt = normalize_candidate_text(txt)
            if nt and is_rar_like(nt):
                candidates.append({"title": nt, "link": base_url, "thumb": None})

    # 3) fallback: page title
    if not candidates:
        page_title = soup.title.string.strip() if soup.title and soup.title.string else None
        if page_title:
            candidates.append({"title": normalize_candidate_text(page_title), "link": base_url, "thumb": None})

    # 4) find a thumbnail (first reasonable <img>)
    thumb = None
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        thumb = urljoin(base_url, src)
        break

    if thumb:
        for c in candidates:
            c["thumb"] = thumb

    return candidates


# --- gather latest (single, robust implementation) ---
def gather_latest_from_site(site: dict) -> dict | None:
    url = site["url"]
    html = fetch_page(url)
    if not html:
        logging.debug("Failed to fetch %s", url)
        return None

    candidates = parse_dlraw_index(html, url)
    if not candidates:
        logging.debug("No candidates found on %s", url)
        return None

    # debug: show all candidates and their keys
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        logging.debug("Candidates for %s:", url)
        for c in candidates:
            logging.debug("  %r -> key=%r", c["title"], extract_version_key(c["title"]))

    # pick the newest candidate by our version key
    best = None
    best_key = None
    for c in candidates:
        key = extract_version_key(c["title"])
        if best is None or key > best_key:
            best = c
            best_key = key

    # debug: chosen best
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        logging.debug("Chosen best for %s: %r key=%r", url, best["title"] if best else None, best_key)

    if best is None:
        return None

    title = best["title"]
    guid = make_guid(url, title)
    pubDate = now_rfc2822()
    thumb = best.get("thumb") or site.get("thumb") or ""
    desc_html = (
        f'<a href="{url}">'
        f'<img src="{thumb}" alt="{title}" style="max-width:200px;height:auto;display:block;margin-bottom:8px;" />'
        f'</a>'
        f'<div><a href="{url}">{site.get("title") or url}</a><br/>{title}</div>'
    )
    return {
        "title": title,
        "link": url,
        "guid": guid,
        "pubDate": pubDate,
        "description": desc_html,
        "image": thumb,
    }




# -----------------------
# Feed and seen handling
# -----------------------
def load_seen(path: str) -> dict:
    if not os.path.exists(path):
        return {"items": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"items": []}

def save_seen(path: str, seen: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)

def make_guid(site_url: str, title: str) -> str:
    return f"{site_url}|{title}"

def now_rfc2822():
    return format_datetime(datetime.now(timezone.utc))

def write_rss(channel_title, channel_link, channel_desc, items, out_file):
    """
    Write a simple RSS 2.0 feed to out_file.
    - channel_title, channel_link, channel_desc: strings
    - items: list of dicts with keys: title, link, guid, pubDate, description, image
    """
    items = items[:MAX_ITEMS]

    # build raw XML string (use CDATA for description so HTML is preserved)
    parts = []
    parts.append('<?xml version="1.0" encoding="utf-8"?>')
    parts.append('<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">')
    parts.append('  <channel>')
    parts.append(f'    <title>{escape(channel_title)}</title>')
    parts.append(f'    <link>{escape(channel_link)}</link>')
    parts.append(f'    <description>{escape(channel_desc)}</description>')
    parts.append(f'    <lastBuildDate>{now_rfc2822()}</lastBuildDate>')

    # optional channel image from first item
    if items and items[0].get("image"):
        parts.append('    <image>')
        parts.append(f'      <url>{escape(items[0]["image"])}</url>')
        parts.append(f'      <title>{escape(channel_title)}</title>')
        parts.append(f'      <link>{escape(channel_link)}</link>')
        parts.append('    </image>')

    for it in items:
        title = escape(it.get("title", ""))
        link = escape(it.get("link", ""))
        guid = escape(it.get("guid", ""))
        pubDate = it.get("pubDate", "")
        image = it.get("image", "")
        # description may contain HTML; wrap in CDATA
        desc = it.get("description", "")
        # ensure CDATA does not contain "]]>" — if it does, fall back to escaped text
        if "]]>" in desc:
            desc_block = escape(desc)
        else:
            desc_block = f"<![CDATA[{desc}]]>"

        parts.append('    <item>')
        parts.append(f'      <title>{title}</title>')
        parts.append(f'      <link>{link}</link>')
        parts.append(f'      <guid isPermaLink="false">{guid}</guid>')
        parts.append(f'      <pubDate>{pubDate}</pubDate>')
        if image:
            parts.append(f'      <media:thumbnail url="{escape(image)}" />')
            parts.append(f'      <enclosure url="{escape(image)}" type="{escape(mime_for_url(image))}" />')
        parts.append(f'      <description>{desc_block}</description>')
        parts.append('    </item>')

    parts.append('  </channel>')
    parts.append('</rss>')

    raw = "\n".join(parts).encode("utf-8")

    # pretty-print using minidom (works reliably across Python versions)
    try:
        dom = minidom.parseString(raw)
        pretty = dom.toprettyxml(indent="  ", encoding="utf-8")
    except Exception:
        # fallback: write raw if minidom fails
        pretty = raw

    # atomic write to out_file
    dirpath = os.path.dirname(out_file) or "."
    with tempfile.NamedTemporaryFile("wb", dir=dirpath, delete=False) as tf:
        tf.write(pretty)
        tempname = tf.name
    os.replace(tempname, out_file)

def build_rss(items: list[dict], out_file: str):
    """
    Backwards-compatible wrapper so existing code that calls build_rss
    continues to work while using the new write_rss implementation.
    """
    # write_rss expects channel title/link/description first
    return write_rss(FEED_TITLE, FEED_LINK, FEED_DESC, items, out_file)



def update_feed_once(sites: list[dict], rss_file: str, seen_file: str, max_items: int = MAX_ITEMS, debug: bool = False):
    # load seen and build a set of GUIDs that were seen before this run
    seen = load_seen(seen_file)
    prev_seen_guids = set()
    for it in seen.get("items", []):
        if isinstance(it, str):
            prev_seen_guids.add(it)
        elif isinstance(it, dict):
            prev_seen_guids.add(it.get("guid"))

    new_items = []
    newly_added_guids = []

    for site in sites:
        if debug:
            logging.info("Checking site: %s", site["url"])
        latest = gather_latest_from_site(site)
        if not latest:
            if debug:
                logging.info("No latest found for %s", site["url"])
            continue

        # include latest in merged feed
        new_items.append(latest)

        # if unseen before, record it and mark as newly added
        if latest["guid"] not in prev_seen_guids:
            # record as seen with timestamp
            seen.setdefault("items", []).append({"guid": latest["guid"], "seen_at": datetime.now(timezone.utc).isoformat()})
            newly_added_guids.append(latest["guid"])

    # merged = newest-per-site only (no history)
    merged = []
    seen_merge = set()
    for it in new_items:
        if it["guid"] not in seen_merge:
            merged.append(it)
            seen_merge.add(it["guid"])
    merged = merged[:max_items]

    # write rss and seen (preserve the seen structure we built)
    build_rss(merged, rss_file)
    save_seen(seen_file, seen)

    return {"added": newly_added_guids, "total": len(merged)}


# -----------------------
# CLI
# -----------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--debug", action="store_true", help="Verbose logging")
    p.add_argument("--max", type=int, default=MAX_ITEMS, help="Maximum items to keep in rss.xml")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    try:
        res = update_feed_once(SITES, RSS_FILE, SEEN_FILE, max_items=args.max, debug=args.debug)
        logging.info("Run complete. Result: %s", res)
        return 0
    except Exception as e:
        logging.exception("Error during update: %s", e)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
