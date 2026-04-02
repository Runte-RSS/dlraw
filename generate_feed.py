#!/usr/bin/env python3
# generate_feed.py
# Simple RSS generator for Fluent Reader. Add sites to SITES list.

import re
from dateutil import parser as dateparser
import json
import xml.etree.ElementTree as ET
import os
import hashlib
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urljoin
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from xml.sax.saxutils import escape
import cloudscraper
import time
import random

RSS_FILE = "rss.xml"
SEEN_FILE = "seen.json"
MAX_ITEMS = 50
USER_AGENT = "Mozilla/5.0 (compatible; rss-bot/1.0; +https://example.com/)"

# Add sites here. Each entry needs: title, url (manga page), image (thumbnail)
SITES = [
    {
        "title": "One Piece",
        "url": "https://tcbonepiecechapters.com/mangas/5/one-piece",
        "image": "https://cdn.onepiecechapters.com/file/CDN-M-A-N/Screen-Shot-2021-04-23-at-9.31.12-PM-1024x732v3.png",
    },{
        "title": "Shut Up, Evil Dragon! I don’t want to raise a child with you anymore",
        "url": "https://mangamirror.com/manga/122851-shut-up-evil-dragon-i-dont-want-to-raise-a-child-with-you-anymore",
        "image": "https://media.manhuabuddy.com/files/images/thumbs/shut-up-evil-dragon-i-dont-want-to-raise-a-child-with-you-anymore.webp",
    },{
        "title": "Transmigrating Into The Cyber Game After Being On Top For Killing Boss",
        "url": "https://mangamirror.com/manga/121372-transmigrating-into-the-cyber-game-after-being-on-top-for-killing-boss",
        "image": "https://cdn.anime-planet.com/manga/primary/after-transmigrating-into-the-cyberpunk-game-i-defeated-the-boss-and-successfully-rose-to-the-top-1.webp?t=1759754015",
    },{
        "title": "I Played the Role of the Adopted Daughter Too Well",
        "url": "https://mangamirror.com/manga/50583-i-played-the-role-of-the-adopted-daughter-too-well",
        "image": "https://www.mangaread.org/wp-content/uploads/2023/09/28-1677642819-193x278.jpg",
    },{
        "title": "The Fox-Eyed Villain of the Demon Academy",
        "url": "https://mangamirror.com/manga/127739-the-fox-eyed-villain-of-the-demon-academy",
        "image": "https://static.asurascans.my/book/f47aadb5-8bff-4ba1-bece-66f0ec681c24/cover/b54fe4a9-a71f-400c-929a-2b1ef9b4c7dd.webp?width=400&type=webp",
    },{
        "title": "Revenge Of The Sword Clan's Hound",
        "url": "https://mangamirror.com/manga/59511-revenge-of-the-sword-clans-hound",
        "image": "https://bulbasaur.poke-black-and-white.net/covers/6851547b702284f834178357/cover_1752077533539.webp",
    },{
        "title": "The Billionaire’s Replacement Wife",
        "url": "https://mangamirror.com/manga/95801-the-billionaires-replacement-wife",
        "image": "https://us-a.tapas.io/sa/76/2546a686-8b25-4e18-bdb5-2306567d34da.jpg",
    },{
        "title": "Black Killer Whale Baby",
        "url": "https://mangamirror.com/manga/119653-black-killer-whale-baby",
        "image": "https://cdn.novelupdates.com/images/2023/06/Black-Killer-Whale-Baby.jpg",
    },{
        "title": "Nia Liston: The Merciless Maiden",
        "url": "https://mangamirror.com/manga/107322-nia-liston-the-merciless-maiden",
        "image": "https://cdn.anisearch.com/images/manga/cover/76/76700_600.webp",
    }
    # Example: add more sites below
    # {
    #   "title": "Another Manga",
    #   "url": "https://example.com/manga/slug",
    #   "image": "https://example.com/thumb.png",
    # },
]
# --- headers and session (module-level, safe) ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://google.com/",
}

session = requests.Session()
session.headers.update(HEADERS)

# configure retries (safe defaults)
retries = Retry(
    total=2,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS"]
)
session.mount("https://", HTTPAdapter(max_retries=retries))
session.mount("http://", HTTPAdapter(max_retries=retries))

MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

def mime_for_url(url):
    if not url:
        return "image/jpeg"
    u = url.lower().split("?")[0].split("#")[0]
    for ext, m in MIME_BY_EXT.items():
        if u.endswith(ext):
            return m
    return "image/jpeg"


# --- helper fetch function (use everywhere instead of session.get directly) ---
def fetch_url(url, timeout=12, allow_cloudscraper=True, debug=False):
    """Use requests session first, fallback to cloudscraper on failure."""
    time.sleep(random.uniform(0.05, 0.25))   # tiny jitter
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r
    except Exception as e:
        if debug:
            print("fetch_url: requests failed for", url, "error:", repr(e))
            resp = getattr(e, "response", None)
            if resp is not None:
                print("Response head:", resp.status_code, resp.headers)
                print(resp.text[:1000])
        if allow_cloudscraper:
            try:
                scraper = cloudscraper.create_scraper(browser={'custom': HEADERS['User-Agent']})
                r2 = scraper.get(url, timeout=timeout, allow_redirects=True)
                r2.raise_for_status()
                return r2
            except Exception as e2:
                if debug:
                    print("fetch_url: cloudscraper fallback failed for", url, "error:", repr(e2))
                raise
        raise



def parse_chap_num(text):
    """
    Return a numeric chapter value for sorting (float), or -1.0 if none found.
    Handles integers and decimals like "12" or "12.5".
    """
    m = re.search(r'(\d+(?:\.\d+)?)', text)
    return float(m.group(1)) if m else -1.0


def extract_thumbnail(page_url, debug=False):
    """Return best thumbnail URL found on page_url or None."""
    r = fetch_url(page_url, timeout=12, allow_cloudscraper=True, debug=debug)
    soup = BeautifulSoup(r.text, "html.parser")

    # 1) prefer og:image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return urljoin(page_url, og["content"].strip())

    # 2) link rel=image_src
    link_img = soup.find("link", rel="image_src")
    if link_img and link_img.get("href"):
        return urljoin(page_url, link_img["href"].strip())

    # 3) check img tags for data-src/data-original/src
    for img in soup.find_all("img"):
        for attr in ("data-src", "data-original", "src"):
            val = img.get(attr)
            if val and val.strip() and "placeholder" not in val:
                return urljoin(page_url, val.strip())

    return None
    
def now_rfc2822():
    return format_datetime(datetime.now(timezone.utc))

def make_guid(text):
    return "urn:sha1:" + hashlib.sha1(text.encode("utf-8")).hexdigest()

def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"items": []}
    return {"items": []}

def save_seen(data):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def http_get(url, timeout=20):
    r = fetch_url(url, timeout=15, allow_cloudscraper=True, debug=True)
    r.raise_for_status()
    return r.text

def normalize_url(u):
    return u.rstrip('/')

from urllib.parse import urlparse, urljoin

ID_RE = re.compile(r"/manga/(\d+)[-/]?", re.IGNORECASE)

def assemble_title(scraped_title, link, site_title=None):
    """
    Prefer a scraped title only when it looks like a real series name.
    If scraped_title is just a chapter token (e.g. "Chapter 42", "Ch. 42"),
    prefer the canonical site_title or the URL slug instead, then append chapter.
    """
    # helper: decide whether scraped_title is useful (not just "Chapter N")
    def scraped_is_useful(s):
        if not s:
            return False
        s = s.strip()
        # if it starts with "chapter", "chap", "ch" followed by digits, it's not useful
        if re.match(r'^\s*(?:chapter|chap|ch)\b', s, flags=re.I):
            return False
        # if it's very short (1-2 words) and contains only "chapter" + number, reject
        words = s.split()
        if len(words) <= 2 and re.search(r'\d', s) and any(re.match(r'^(?:chapter|chap|ch)\b', w, flags=re.I) for w in words):
            return False
        # otherwise accept
        return True

    # choose base: prefer useful scraped_title, else site_title, else slug
    if scraped_title and scraped_is_useful(scraped_title):
        base = strip_leading_id(scraped_title.strip())
    else:
        base = strip_leading_id(site_title or '') if site_title else title_from_link(link) or ''

    # append chapter suffix if not already present
    chap = extract_chapter_from_link(link)
    if chap and not re.search(r'chapter\s*\d+', base, flags=re.I):
        final = (base + chap).strip()
    else:
        final = base.strip()

    # final fallback: if still empty, use slug (without removing numbers)
    if not final:
        final = title_from_link(link) or ''
    return final


# --- Title and site helpers (insert after ID_RE) ---
def domain_of(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ''

def find_site_for_link(link, SITES):
    """Return the SITES entry that best matches link (domain first, then substring)."""
    if not link:
        return None
    d = domain_of(link)
    # domain match
    for s in SITES:
        if domain_of(s.get('url', '')) == d:
            return s
    # substring match (listing URL inside chapter URL)
    for s in SITES:
        u = s.get('url', '').rstrip('/')
        if u and u in link:
            return s
    return None

def extract_chapter_from_link(link):
    """
    Return a string like ' — Chapter 94' when a chapter number is found in the URL.
    Returns empty string if none found.
    """
    if not link:
        return ''
    m = re.search(r'chapter[-_/ ]?(\d+(?:\.\d+)?)', link, flags=re.I)
    if not m:
        m = re.search(r'/chapters/(\d+(?:\.\d+)?)', link, flags=re.I)
    if m:
        try:
            return f' — Chapter {int(float(m.group(1)))}'
        except Exception:
            return f' — Chapter {m.group(1)}'
    return ''

def strip_leading_id(title):
    """Remove a leading numeric id like '119653 ' or '119653-' but keep everything else."""
    if not title:
        return title
    t = title.strip()
    # remove leading digits + optional separator
    t = re.sub(r'^\s*\d+\s*[-_:]?\s*', '', t)
    return re.sub(r'\s+', ' ', t).strip()

def title_from_link(link):
    """Fallback: derive a readable base title from the URL slug."""
    if not link:
        return None
    parts = [p for p in urlparse(link).path.split('/') if p]
    candidate = parts[-2] if len(parts) >= 2 else parts[-1]
    candidate = re.sub(r'^\d+[-_]*', '', candidate)
    candidate = candidate.replace('-', ' ').replace('_', ' ')
    candidate = re.sub(r'\s+', ' ', candidate).strip()
    return candidate.title() if candidate else None
# --- end helpers ---


def find_latest_chapter(page_url, title=None, debug=False):
    """
    Parse the listing page and return the best candidate dict:
    { "url": "...", "text": "...", "score": float } or None.
    """
    try:
        rpage = fetch_url(page_url, timeout=12, allow_cloudscraper=True, debug=debug)
        rpage.raise_for_status()
        page_html = rpage.text
        canonical_page_url = normalize_url(rpage.url)
    except Exception:
        # fallback to a simpler GET (will raise if it fails)
        page_html = http_get(page_url)
        canonical_page_url = normalize_url(page_url)
    # If the response looks like a Cloudflare challenge page, try cloudscraper once
    # (check headers and a short snippet of the body)
    cf_challenge = False
    try:
        hdrs = getattr(rpage, "headers", {}) or {}
        body_head = (page_html or "")[:512].lower()
        if hdrs.get("Cf-Mitigated") or "just a moment" in body_head or "cf-challenge" in body_head:
            cf_challenge = True
    except Exception:
        cf_challenge = False

    if cf_challenge:
        if debug:
            print("DEBUG: Detected Cloudflare challenge; retrying with cloudscraper for", page_url)
        try:
            scraper = cloudscraper.create_scraper(browser={'custom': HEADERS['User-Agent']})
            rpage2 = scraper.get(page_url, timeout=12, allow_redirects=True)
            rpage2.raise_for_status()
            page_html = rpage2.text
            canonical_page_url = normalize_url(rpage2.url)
            if debug:
                print("DEBUG: cloudscraper succeeded, using fetched page")
        except Exception as e:
            if debug:
                print("DEBUG: cloudscraper retry failed:", repr(e))
            # fall back to whatever we already have (challenge page) — but continue

    soup = BeautifulSoup(page_html, "html.parser")
    candidates = []

    # extract numeric id from canonical page URL
    m_id = ID_RE.search(canonical_page_url)
    expected_id = m_id.group(1) if m_id else None

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(canonical_page_url, href)
        text = a.get_text(" ", strip=True) or ""

        # quick filters
        is_chapter_url = bool(re.search(r"(?:/chapter/|/read/|/c/|chapter-|chap-|\bchapter\b|\bchap\b|\bch\b|/page/)", full, re.IGNORECASE))
        is_chapter_text = bool(re.search(r"(?:chapter|chap|ch)\s*\d", text, re.IGNORECASE))
        is_listing_page = bool(re.search(r"/(?:manga|manhwa|manhua)/[^/]+(?:$|/|[-]\d{4}(?:$|/))", full, re.IGNORECASE))

        if is_listing_page and not (is_chapter_url or is_chapter_text):
            continue
        if not (is_chapter_url or is_chapter_text or re.search(r"\d", full)):
            continue

                # extract numeric tokens from the candidate URL (in order)
        url_nums = re.findall(r"(\d+(?:\.\d+)?)", full)

        # helper tests
        def is_year_token(n):
            try:
                v = int(float(n)); return 1900 <= v <= 2100
            except Exception:
                return False

        def is_timestamp_token(n):
            try:
                v = int(float(n)); return v >= 1_000_000_000
            except Exception:
                return False

        # drop year-like, epoch-like, extremely large tokens, and the manga id itself
        filtered_nums = []
        for n in url_nums:
            try:
                iv = int(float(n))
            except Exception:
                continue
            if expected_id and iv == int(expected_id):
                # skip the manga id token — not a chapter number
                continue
            if is_year_token(n):
                continue
            if is_timestamp_token(n):
                continue
            if iv >= 1_000_000:   # safety threshold for absurdly large IDs
                continue
            filtered_nums.append(n)

        # 1) Prefer explicit chapter patterns in the URL (chapter-123, /chapter/123, /c/123)
        chap_num = None
        m_after = re.search(r"(?:chapter|chap|read|page|c)[^0-9]{0,6}(\d+(?:\.\d+)?)", full, re.IGNORECASE)
        if m_after:
            try:
                cand = m_after.group(1)
                if not (expected_id and int(float(cand)) == int(expected_id)):
                    chap_num = float(cand)
            except Exception:
                chap_num = None

        # 2) Prefer numbers in the anchor text (e.g., "Chapter 43")
        if chap_num is None:
            m_text = re.search(r"(?:chapter|chap|ch)[^\d]{0,6}(\d+(?:\.\d+)?)", text, re.IGNORECASE)
            if m_text:
                try:
                    cand = m_text.group(1)
                    if not (expected_id and int(float(cand)) == int(expected_id)):
                        chap_num = float(cand)
                except Exception:
                    chap_num = None

        # 3) Fallback to last reasonable numeric token from the filtered URL tokens
        if chap_num is None and filtered_nums:
            try:
                chap_num = float(filtered_nums[-1])
            except Exception:
                chap_num = None

        # 4) Final sanity checks: reject year-like or absurdly large numbers
        if chap_num is not None:
            try:
                n = int(float(chap_num))
            except Exception:
                n = None
            if n is None or (1900 <= n <= 2100) or n >= 1000000:
                chap_num = None

        if chap_num is None:
            continue


        # Strict ID check: require the page's numeric manga id to appear in candidate URL
        if expected_id and expected_id not in full:
            title_words = [w.lower() for w in (title or "").split()[:3]]
            if not any(w for w in title_words if w and w in text.lower()):
                continue

        boost = 0.1 if title and any(w.lower() in text.lower() for w in title.split()[:3]) else 0.0

        candidates.append({
            "url": normalize_url(full),
            "text": text,
            "score": chap_num + boost
        })

    if debug:
        print("DEBUG canonical_page_url:", canonical_page_url)
        print("DEBUG expected_id:", expected_id)
        print("DEBUG candidate count:", len(candidates))
        for c in sorted(candidates, key=lambda x: x['score'], reverse=True)[:12]:
            print("DEBUG CAND:", c['score'], c['url'], repr(c['text'][:80]))

    if not candidates:
        return None

    best = max(candidates, key=lambda x: x["score"])

    # follow redirects for the chosen chapter URL and normalize
    try:
        rfinal = fetch_url(best["url"], timeout=12, allow_cloudscraper=True, debug=debug)
        final_url = normalize_url(rfinal.url)
    except Exception:
        final_url = normalize_url(best["url"])

    best["url"] = final_url
    return best

def normalize_item(it, site=None, scraped_title=None):
    now = now_rfc2822()
    title = (it.get("title") or "").strip()
    if not title:
        title = assemble_title(scraped_title, it.get("link", ""), site.get("title") if site else None)

    description = it.get("description")
    if description is None:
        description = ""

    guid = it.get("guid") or make_guid(it.get("link", ""))
    return {
        "title": title,
        "link": it.get("link", ""),
        "guid": guid,
        "pubDate": it.get("pubDate", now),
        "description": description,
        "image": it.get("image", ""),
    }





def write_rss(channel_title, channel_link, channel_desc, items, out_file):
    items = items[:MAX_ITEMS]
    header = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">\n'
        "  <channel>\n"
        f"    <title>{escape(channel_title)}</title>\n"
        f"    <link>{escape(channel_link)}</link>\n"
        f"    <description>{escape(channel_desc)}</description>\n"
        f"    <lastBuildDate>{now_rfc2822()}</lastBuildDate>\n"
    )

    # optional channel-level image (use first item's image if present)
    if items and items[0].get("image"):
        header += f'    <image><url>{escape(items[0]["image"])}</url><title>{escape(channel_title)}</title><link>{escape(channel_link)}</link></image>\n'

    items_xml = ""
    for it in items:
        title_text = it.get('title') or ''
        items_xml += "    <item>\n"
        items_xml += f"      <title>{escape(title_text)}</title>\n"
        items_xml += f"      <link>{escape(it.get('link',''))}</link>\n"
        items_xml += f"      <guid isPermaLink=\"false\">{escape(it.get('guid',''))}</guid>\n"
        items_xml += f"      <pubDate>{escape(it.get('pubDate',''))}</pubDate>\n"

        img_url = it.get("image") or ""
        if img_url:
            img_type = mime_for_url(img_url)
            items_xml += f'      <media:thumbnail url="{escape(img_url)}" />\n'
            items_xml += f'      <media:content url="{escape(img_url)}" medium="image" type="{escape(img_type)}" />\n'
            items_xml += f'      <enclosure url="{escape(img_url)}" type="{escape(img_type)}" />\n'

        items_xml += f"      <description><![CDATA[{it.get('description','')}]]></description>\n"
        items_xml += "    </item>\n"

    footer = "  </channel>\n</rss>\n"

    tmp = out_file + '.tmp'
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(header + items_xml + footer)

    # validate XML (will raise if malformed)
    ET.parse(tmp)

    # atomic replace
    os.replace(tmp, out_file)



def main():
    seen = load_seen()
    history = seen.get("items", [])
    if not isinstance(history, list):
        history = []

    for site in SITES:
        title = site.get("title")
        page = site.get("url")
        thumb = site.get("image", "")
        print(f"Checking {title} -> {page}")
        try:
            latest = find_latest_chapter(page, title, debug=True)
        except Exception as e:
            print("  Error scraping:", e)
            latest = None

        if not latest:
            print("  No chapter links found; skipping.")
            # normalize any existing entry for this title
            for i, it in enumerate(history):
                if it.get("title") == title:
                    history[i] = normalize_item(it)
            continue

        chapter_url = latest["url"]
        score = latest.get("score")
                # use stable GUID per chapter URL
        guid = make_guid(chapter_url)

               # assemble final title (prefer scraped text, else site title, else slug)
        scraped_text = latest.get("text") or None
        final_title = assemble_title(scraped_text, chapter_url, title)

        # build the HTML description we use for new items and for updates
        description_from_latest = (
            f'<a href="{escape(chapter_url)}">'
            f'<img src="{escape(thumb)}" alt="{escape(title)}" '
            f'style="max-width:200px;height:auto;display:block;margin-bottom:8px;" />'
            f'</a>'
            f'<div><a href="{escape(page)}">{escape(title)}</a><br/>{escape(latest.get("text",""))}</div>'
        )

        already = any(it.get("guid") == guid for it in history)
        if already:
            print("  No new chapter.")
            # update existing entry link/image/pubDate and restore title/description if empty
            for i, it in enumerate(history):
                if it.get("title") == title or it.get("guid") == guid:
                    it["link"] = chapter_url
                    it["image"] = thumb
                    it["pubDate"] = it.get("pubDate", now_rfc2822())
                    # restore title if missing or empty
                    if not (it.get("title") or "").strip():
                        it["title"] = final_title
                    # restore description only if missing or empty
                    if not (it.get("description") or "").strip():
                        it["description"] = description_from_latest
                    history[i] = normalize_item(it, site=site, scraped_title=scraped_text)
            continue





        # new chapter -> prepend
        pubDate = now_rfc2822()
        description = (
            f'<a href="{escape(chapter_url)}">'
            f'<img src="{escape(thumb)}" alt="{escape(title)}" style="max-width:200px;height:auto;display:block;margin-bottom:8px;" />'
            f'</a>'
            f'<div><a href="{escape(page)}">{escape(title)}</a><br/>{escape(latest.get("text",""))}</div>'
        )
        item = {
            "title": final_title,
            "link": chapter_url,
            "guid": guid,
            "pubDate": pubDate,
            "description": description,
            "image": thumb,
        }
        print("  New chapter detected:", chapter_url)
        history.insert(0, normalize_item(item, site=site, scraped_title=scraped_text))
    
    history = [normalize_item(it) for it in history][:MAX_ITEMS]

            # Backfill descriptions by canonical link (one-time migration to prefer non-empty descriptions)
    by_link = {}
    for it in history:
        link = it.get("link", "") or ""
        if not link:
            continue
        desc = (it.get("description") or "").strip()
        # prefer an item that has a non-empty description
        if link not in by_link or (desc and not (by_link[link].get("description") or "").strip()):
            by_link[link] = it

    # Apply backfill: copy the best description to other items with the same link
    for it in history:
        link = it.get("link", "") or ""
        if not link:
            continue
        best = by_link.get(link)
        if best:
            best_desc = (best.get("description") or "").strip()
            if best_desc and not (it.get("description") or "").strip():
                it["description"] = best_desc


        # Optionally re-run your dedupe/canonicalization after this
        seen["items"] = [normalize_item(it) for it in seen.get("items", [])][:MAX_ITEMS]
        save_seen(seen)


    # --- canonicalize GUIDs and dedupe history by chapter URL ---
    def is_chapter_only_title(t):
        if not t:
            return True
        t = t.strip()
        return bool(re.match(r'^\s*(?:chapter|chap|ch)[\.\s\-]*\d+', t, flags=re.I))

    seen_by_guid = {}
    # iterate in current order (newest first) and pick the best item per canonical guid
    for it in history:
        link = it.get("link", "") or ""
        if not link:
            continue
        canonical_guid = make_guid(link)
        existing = seen_by_guid.get(canonical_guid)

        if existing is None:
            copy_it = dict(it)
            copy_it["guid"] = canonical_guid
            seen_by_guid[canonical_guid] = copy_it
            continue

        # decide which item is better: prefer non-chapter-only title, then non-empty description, then longer title
        cur_title = (existing.get("title") or "").strip()
        new_title = (it.get("title") or "").strip()
        cur_bad = is_chapter_only_title(cur_title)
        new_bad = is_chapter_only_title(new_title)

        # description preference
        cur_desc = (existing.get("description") or "").strip()
        new_desc = (it.get("description") or "").strip()
        cur_bad_desc = not cur_desc
        new_bad_desc = not new_desc

        # choose replacement when new item is strictly better
        replace = False
        if cur_bad and not new_bad:
            replace = True
        elif cur_bad == new_bad:
            # prefer item with a non-empty description
            if cur_bad_desc and not new_bad_desc:
                replace = True
            elif cur_bad_desc == new_bad_desc:
                # tie-breaker: prefer longer title
                if len(new_title) > len(cur_title):
                    replace = True

        if replace:
            copy_it = dict(it)
            copy_it["guid"] = canonical_guid
            seen_by_guid[canonical_guid] = copy_it

    # rebuild history preserving original order (newest first)
    new_history = []
    for it in history:
        link = it.get("link", "") or ""
        if not link:
            continue
        canonical_guid = make_guid(link)
        item = seen_by_guid.pop(canonical_guid, None)
        if item:
            new_history.append(item)

    history = new_history[:MAX_ITEMS]
    # final normalize pass that will not clobber titles/descriptions (ensure normalize_item is conservative)
    history = [normalize_item(it) for it in history]
    seen["items"] = history
    save_seen(seen)



    print("=== DESCRIPTION PREVIEW ===")
    for i, it in enumerate(history[:20]):
        print(i, "guid=", it.get("guid"), "title=", repr(it.get("title")), "desc_len=", len((it.get("description") or "").strip()))
    print("===========================")
  

    write_rss(
        channel_title="My Manga Watchlist",
        channel_link="https://example.com/",
        channel_desc="Auto-generated manga feed for Fluent Reader",
        items=history,
        out_file=RSS_FILE,
    )
    print(f"Wrote {RSS_FILE} with {len(history)} items.")


if __name__ == "__main__":
    main()
