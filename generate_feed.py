"""
generate_feed.py

Watch one or more web pages and generate an RSS feed when they change.

Design goals:
- Detect actual page/article changes rather than guessing from arbitrary links.
- Prefer the site's modified date when available.
- Fall back to a content hash when no reliable modified date exists.
- Ignore old dates embedded in download filenames/categories.
- Keep a history of detected updates.
- Generate valid RSS 2.0.
- Atomic writes for rss.xml and state.json.
- Works well from GitHub Actions / cron.

Dependencies:
    pip install requests beautifulsoup4

Usage:
    python generate_feed.py
    python generate_feed.py --debug
    python generate_feed.py --max 50
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from xml.sax.saxutils import escape


# ============================================================
# Configuration
# ============================================================

SITES = [
   {
        "title": "Youjo Senki",
        "url": "https://dl-raw.si/%e5%b9%bc%e5%a5%b3%e6%88%a6%e8%a8%98-raw/",
        "thumb": "https://puu.sh/KR4oa.png",
    },{
        "title": "Isekai Meikyuu de Harem wo",
        "url": "https://dl-raw.si/%e7%95%b0%e4%b8%96%e7%95%8c%e8%bf%b7%e5%ae%ae%e3%81%a7%e3%83%8f%e3%83%bc%e3%83%ac%e3%83%a0%e3%82%92-raw/",
        "thumb": "https://static.zerochan.net/Isekai.Meikyuu.de.Harem.wo.1024.3730804.webp",
    },{
        "title": "Aoki Hagane no Arpeggio",
        "url": "https://dl-raw.si/%e8%92%bc%e3%81%8d%e9%8b%bc%e3%81%ae%e3%82%a2%e3%83%ab%e3%83%9a%e3%82%b8%e3%82%aa-raw/",
        "thumb": "https://puu.sh/KR4o5.png",
    },{
        "title": "Kage no Jitsuryokusha ni Naritakute",
        "url": "https://dl-raw.si/%e9%99%b0%e3%81%ae%e5%ae%9f%e5%8a%9b%e8%80%85%e3%81%ab%e3%81%aa%e3%82%8a%e3%81%9f%e3%81%8f%e3%81%a6%ef%bc%81-raw/",
        "thumb": "https://puu.sh/KKZDj.png",
    },{
        "title": "Kurotsuki no Yerknacht",
        "url": "https://dl-raw.si/%e9%bb%92%e6%9c%88%e3%81%ae%e3%82%a4%e3%82%a7%e3%83%ab%e3%82%af%e3%83%8a%e3%83%8f%e3%83%88-raw/",
        "thumb": "https://puu.sh/KKZDv.jpg",
    },{
        "title": "Tsuki Michibiku Isekai Douchuu",
        "url": "https://dl-raw.si/%e6%9c%88%e3%81%8c%e5%b0%8e%e3%81%8f%e7%95%b0%e4%b8%96%e7%95%8c%e9%81%93%e4%b8%ad-raw/",
        "thumb": "https://puu.sh/KKZDW.jpg",
    },{
        "title": "Kage no jitsuryokusha ni naritakute masuta obu gaden shichikage retsuden",
        "url": "https://dl-raw.si/%e9%99%b0%e3%81%ae%e5%ae%9f%e5%8a%9b%e8%80%85%e3%81%ab%e3%81%aa%e3%82%8a%e3%81%9f%e3%81%8f%e3%81%a6%ef%bc%81%e3%83%9e%e3%82%b9%e3%82%bf%e3%83%bc%e3%82%aa%e3%83%96%e3%82%ac%e3%83%bc%e3%83%87%e3%83%b3/",
        "thumb": "https://puu.sh/KR4nM.jpg",
    },{
        "title": "Tenseishitara slime datta ken",
        "url": "https://dl-raw.si/%e8%bb%a2%e7%94%9f%e3%81%97%e3%81%9f%e3%82%89%e3%82%b9%e3%83%a9%e3%82%a4%e3%83%a0%e3%81%a0%e3%81%a3%e3%81%9f%e4%bb%b6-raw/",
        "thumb": "https://puu.sh/KOdNv.png",
    }
    # Add more sites here as needed:
    # {"title": "Another Series", "url": "https://dlraw.cc/.../", "thumb": "https://..."},
]



RSS_FILE = "rss.xml"
STATE_FILE = "state.json"

FEED_TITLE = "DL-Raw Watchlist"
FEED_LINK = "https://example.com/"
FEED_DESCRIPTION = "Automatic update feed"

MAX_ITEMS = 50

REQUEST_TIMEOUT = 20

USER_AGENT = (
    "MangaFeedBot/2.0 "
    "(web page change watcher; contact: example@example.com)"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
}


# ============================================================
# HTTP
# ============================================================

def fetch_page(url: str) -> tuple[str, requests.Response] | None:
    """Fetch a page and return (html, response)."""

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        return response.text, response

    except requests.RequestException as exc:
        logging.warning("Could not fetch %s: %s", url, exc)
        return None


# ============================================================
# Utility
# ============================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_rfc2822() -> str:
    return format_datetime(now_utc())


def atomic_write_text(path: str, text: str) -> None:
    """Write a file atomically."""

    directory = os.path.dirname(path) or "."

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=directory,
        delete=False,
    ) as tmp:

        tmp.write(text)
        temp_name = tmp.name

    os.replace(temp_name, path)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ============================================================
# Date handling
# ============================================================

DATE_PATTERNS = [
    # ISO:
    re.compile(
        r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b"
    ),

    # YYYY-MM:
    re.compile(
        r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b"
    ),
]


def parse_date_string(value: str) -> datetime | None:
    """
    Try to extract a date from a string.

    This is deliberately NOT used to determine the newest release
    from arbitrary links. It is only used on page metadata.
    """

    if not value:
        return None

    value = value.strip()

    # ISO datetime
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except ValueError:
        pass

    # Date patterns
    for pattern in DATE_PATTERNS:
        match = pattern.search(value)

        if not match:
            continue

        year = int(match.group(1))
        month = int(match.group(2))

        if match.lastindex >= 3:
            day = int(match.group(3))
        else:
            day = 1

        try:
            return datetime(
                year,
                month,
                day,
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue

    return None


def extract_page_date(
    soup: BeautifulSoup,
    response: requests.Response,
) -> tuple[datetime | None, str | None]:
    """
    Find the page's publication/modification date.

    Priority:
        1. article:modified_time
        2. modified_time meta
        3. <time datetime=...>
        4. article:published_time
        5. published_time meta
        6. WordPress-style .updated
        7. WordPress-style .entry-date
        8. Last-Modified HTTP header
    """

    # --------------------------------------------------------
    # OpenGraph / WordPress metadata
    # --------------------------------------------------------

    meta_names = [
        "article:modified_time",
        "modified_time",
        "og:updated_time",
        "article:published_time",
        "published_time",
    ]

    for name in meta_names:

        tag = soup.find(
            "meta",
            attrs={
                "property": name,
            },
        )

        if not tag:
            tag = soup.find(
                "meta",
                attrs={
                    "name": name,
                },
            )

        if tag:
            value = tag.get("content", "")

            dt = parse_date_string(value)

            if dt:
                logging.debug(
                    "Found page date from meta %s: %s",
                    name,
                    dt.isoformat(),
                )

                return dt, f"meta:{name}"

    # --------------------------------------------------------
    # <time datetime="">
    # --------------------------------------------------------

    for time_tag in soup.find_all("time"):

        value = time_tag.get("datetime")

        if value:
            dt = parse_date_string(value)

            if dt:
                logging.debug(
                    "Found page date from <time>: %s",
                    dt.isoformat(),
                )

                return dt, "time:datetime"

    # --------------------------------------------------------
    # WordPress updated date
    # --------------------------------------------------------

    for selector in [
        ".updated",
        ".update",
        ".modified",
        ".post-modified",
    ]:

        tag = soup.select_one(selector)

        if tag:
            dt = parse_date_string(tag.get_text(" ", strip=True))

            if dt:
                logging.debug(
                    "Found modified date using %s: %s",
                    selector,
                    dt.isoformat(),
                )

                return dt, f"selector:{selector}"

    # --------------------------------------------------------
    # WordPress published date
    # --------------------------------------------------------

    for selector in [
        ".entry-date",
        ".published",
        ".post-date",
    ]:

        tag = soup.select_one(selector)

        if tag:
            dt = parse_date_string(tag.get_text(" ", strip=True))

            if dt:
                logging.debug(
                    "Found published date using %s: %s",
                    selector,
                    dt.isoformat(),
                )

                return dt, f"selector:{selector}"

    # --------------------------------------------------------
    # HTTP Last-Modified
    # --------------------------------------------------------

    last_modified = response.headers.get("Last-Modified")

    if last_modified:

        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(last_modified)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt.astimezone(timezone.utc), "http:last-modified"

        except (TypeError, ValueError):
            pass

    return None, None


# ============================================================
# Article extraction
# ============================================================

def find_main_content(soup: BeautifulSoup):
    """
    Try to isolate the actual article/post.

    This is important.

    We do NOT want navigation menus, related posts, footer links,
    categories, etc. influencing the fingerprint.
    """

    selectors = [
        "article",
        ".entry-content",
        ".post-content",
        ".article-content",
        ".td-post-content",
        ".post-body",
        "main",
    ]

    for selector in selectors:

        element = soup.select_one(selector)

        if element:
            logging.debug(
                "Main content selected using %s",
                selector,
            )
            return element

    logging.debug("Could not identify article; using <body>")

    return soup.body or soup


def clean_content(element) -> str:
    """
    Produce a stable textual representation of the meaningful page.

    Remove things that change every request or aren't part of the post.
    """

    element = BeautifulSoup(
        str(element),
        "html.parser",
    )

    # Remove dynamic / irrelevant elements
    for tag in element.select(
        "script, style, noscript, iframe, "
        "nav, footer, header, form"
    ):
        tag.decompose()

    text = element.get_text(" ", strip=True)

    return normalize_whitespace(text)


# ============================================================
# Release title
# ============================================================

def extract_title(
    soup: BeautifulSoup,
    site: dict,
) -> str:
    """
    Prefer the actual article heading.

    We deliberately do NOT derive the title from .rar links.
    """

    for selector in [
        "article h1",
        "main h1",
        "h1.entry-title",
        "h1.post-title",
        "h1",
    ]:

        tag = soup.select_one(selector)

        if tag:

            title = normalize_whitespace(
                tag.get_text(" ", strip=True)
            )

            if title:
                return title

    # OpenGraph fallback
    meta = soup.find(
        "meta",
        attrs={"property": "og:title"},
    )

    if meta and meta.get("content"):
        return normalize_whitespace(meta["content"])

    # HTML title fallback
    if soup.title and soup.title.string:
        return normalize_whitespace(soup.title.string)

    return site["title"]


# ============================================================
# Image
# ============================================================

def extract_thumbnail(
    content,
    base_url: str,
    site: dict,
) -> str:
    """
    Find an image belonging to the article.
    """

    for img in content.find_all("img"):

        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or ""
        )

        if not src:
            continue

        return urljoin(base_url, src)

    return site.get("thumb", "")


# ============================================================
# Fingerprint
# ============================================================

def build_fingerprint(
    title: str,
    content_text: str,
) -> str:
    """
    Hash the meaningful page state.

    This catches updates even if the site's modified date
    is missing or broken.
    """

    material = (
        title.strip()
        + "\n"
        + content_text.strip()
    )

    return sha256_text(material)


# ============================================================
# Site snapshot
# ============================================================

def inspect_site(site: dict) -> dict | None:
    """
    Fetch and inspect one site.

    Returns a snapshot describing the current page state.
    """

    url = site["url"]

    result = fetch_page(url)

    if not result:
        return None

    html, response = result

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # --------------------------------------------------------
    # Identify article
    # --------------------------------------------------------

    content = find_main_content(soup)

    # --------------------------------------------------------
    # Title
    # --------------------------------------------------------

    title = extract_title(
        soup,
        site,
    )

    # --------------------------------------------------------
    # Date
    # --------------------------------------------------------

    page_date, date_source = extract_page_date(
        soup,
        response,
    )

    # --------------------------------------------------------
    # Meaningful text
    # --------------------------------------------------------

    content_text = clean_content(content)

    # --------------------------------------------------------
    # Thumbnail
    # --------------------------------------------------------

    thumb = extract_thumbnail(
        content,
        url,
        site,
    )

    # --------------------------------------------------------
    # Fingerprint
    # --------------------------------------------------------

    fingerprint = build_fingerprint(
        title,
        content_text,
    )

    logging.debug(
        "Site: %s",
        site["title"],
    )

    logging.debug(
        "Title: %s",
        title,
    )

    logging.debug(
        "Page date: %s (%s)",
        page_date.isoformat() if page_date else "none",
        date_source or "none",
    )

    logging.debug(
        "Fingerprint: %s",
        fingerprint,
    )

    return {
        "site_url": url,
        "site_title": site["title"],
        "title": title,
        "page_date": (
            page_date.isoformat()
            if page_date
            else None
        ),
        "date_source": date_source,
        "fingerprint": fingerprint,
        "thumb": thumb,
        "content_text": content_text,
    }


# ============================================================
# Change detection
# ============================================================

def determine_change(
    previous: dict | None,
    current: dict,
) -> tuple[bool, str]:
    """
    Determine whether the page changed.

    Strategy:

    1. If fingerprint is different -> changed.
    2. If modified date became newer -> changed.
    3. Otherwise -> unchanged.

    The fingerprint is the important safety net.
    """

    if previous is None:
        return True, "first-seen"

    old_fingerprint = previous.get("fingerprint")
    new_fingerprint = current.get("fingerprint")

    if (
        old_fingerprint
        and new_fingerprint
        and old_fingerprint != new_fingerprint
    ):
        return True, "content-changed"

    old_date = parse_date_string(
        previous.get("page_date") or ""
    )

    new_date = parse_date_string(
        current.get("page_date") or ""
    )

    if old_date and new_date and new_date > old_date:
        return True, "modified-date-newer"

    return False, "unchanged"


# ============================================================
# RSS
# ============================================================

def make_guid(
    site_url: str,
    fingerprint: str,
) -> str:
    """
    Stable GUID for a particular page state.
    """

    return f"{site_url}|{fingerprint}"


def make_description(
    item: dict,
) -> str:

    title = escape(item.get("title", ""))
    link = escape(item.get("link", ""))
    site_title = escape(
        item.get("site_title", "")
    )

    thumb = item.get("thumb", "")

    image_html = ""

    if thumb:
        image_html = (
            f'<a href="{link}">'
            f'<img src="{escape(thumb)}" '
            f'alt="{title}" '
            f'style="max-width:200px;'
            f'height:auto;display:block;'
            f'margin-bottom:8px;" />'
            f'</a>'
        )

    return (
        image_html
        + f"<div>"
        f"<a href=\"{link}\">{site_title}</a>"
        f"<br>{title}"
        f"</div>"
    )


def write_rss(
    items: list[dict],
    output_file: str,
    max_items: int,
) -> None:

    items = items[:max_items]

    parts = []

    parts.append(
        '<?xml version="1.0" encoding="utf-8"?>'
    )

    parts.append(
        '<rss version="2.0" '
        'xmlns:media="http://search.yahoo.com/mrss/">'
    )

    parts.append("  <channel>")

    parts.append(
        f"    <title>{escape(FEED_TITLE)}</title>"
    )

    parts.append(
        f"    <link>{escape(FEED_LINK)}</link>"
    )

    parts.append(
        f"    <description>"
        f"{escape(FEED_DESCRIPTION)}"
        f"</description>"
    )

    parts.append(
        f"    <lastBuildDate>{now_rfc2822()}</lastBuildDate>"
    )

    for item in items:

        title = escape(
            item.get("title", "")
        )

        link = escape(
            item.get("link", "")
        )

        guid = escape(
            item.get("guid", "")
        )

        pub_date = item.get(
            "pubDate",
            now_rfc2822(),
        )

        description = make_description(
            item
        )

        parts.append("    <item>")

        parts.append(
            f"      <title>{title}</title>"
        )

        parts.append(
            f"      <link>{link}</link>"
        )

        parts.append(
            f'      <guid isPermaLink="false">'
            f"{guid}"
            f"</guid>"
        )

        parts.append(
            f"      <pubDate>{pub_date}</pubDate>"
        )

        thumb = item.get("thumb", "")

        if thumb:

            parts.append(
                f'      <media:thumbnail '
                f'url="{escape(thumb)}" />'
            )

            parts.append(
                f'      <media:content '
                f'url="{escape(thumb)}" '
                f'medium="image" />'
            )

        parts.append(
            f"      <description>"
            f"<![CDATA[{description}]]>"
            f"</description>"
        )

        parts.append("    </item>")

    parts.append("  </channel>")
    parts.append("</rss>")

    xml = "\n".join(parts)

    atomic_write_text(
        output_file,
        xml,
    )


# ============================================================
# State
# ============================================================

def load_state(path: str) -> dict:

    if not os.path.exists(path):
        return {
            "sites": {},
            "items": [],
        }

    try:

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:

            return json.load(f)

    except Exception:

        logging.warning(
            "Could not read state file; starting fresh."
        )

        return {
            "sites": {},
            "items": [],
        }


def save_state(
    path: str,
    state: dict,
) -> None:

    atomic_write_text(
        path,
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
        ),
    )


# ============================================================
# Main update logic
# ============================================================

def update_feed(
    sites: list[dict],
    rss_file: str,
    state_file: str,
    max_items: int,
) -> None:

    state = load_state(state_file)

    site_states = state.setdefault(
        "sites",
        {},
    )

    feed_items = state.setdefault(
        "items",
        [],
    )

    for site in sites:

        logging.info(
            "Checking %s",
            site["title"],
        )

        current = inspect_site(site)

        if not current:

            logging.warning(
                "Could not inspect %s",
                site["url"],
            )

            continue

        site_key = site["url"]

        previous = site_states.get(
            site_key
        )

        changed, reason = determine_change(
            previous,
            current,
        )

        logging.info(
            "Result: %s (%s)",
            "CHANGED" if changed else "unchanged",
            reason,
        )

        # ----------------------------------------------------
        # First run
        # ----------------------------------------------------

        if previous is None:

            # Save baseline but don't necessarily create
            # an RSS notification on the first run.
            #
            # Change this to True if you want the first
            # execution to create an RSS item.

            create_initial_item = False

            site_states[site_key] = current

            if not create_initial_item:
                continue

        # ----------------------------------------------------
        # Update detected
        # ----------------------------------------------------

        elif changed:

            site_states[site_key] = current

        else:

            # Keep state fresh even if nothing changed.
            site_states[site_key] = current
            continue

        # ----------------------------------------------------
        # Create RSS item
        # ----------------------------------------------------

        pub_date = now_rfc2822()

        if current.get("page_date"):

            parsed = parse_date_string(
                current["page_date"]
            )

            if parsed:
                pub_date = format_datetime(
                    parsed
                )

        fingerprint = current[
            "fingerprint"
        ]

        guid = make_guid(
            site_key,
            fingerprint,
        )

        item = {
            "title": current["title"],
            "link": site_key,
            "guid": guid,
            "pubDate": pub_date,
            "description": "",
            "thumb": current.get(
                "thumb",
                "",
            ),
            "site_title": current[
                "site_title"
            ],
        }

        item["description"] = make_description(
            item
        )

        # Don't duplicate an already-created RSS item.
        existing_guids = {
            x.get("guid")
            for x in feed_items
            if isinstance(x, dict)
        }

        if guid not in existing_guids:

            feed_items.insert(
                0,
                item,
            )

            logging.info(
                "Added RSS item: %s",
                current["title"],
            )

    # --------------------------------------------------------
    # Limit RSS history
    # --------------------------------------------------------

    feed_items = feed_items[:max_items]

    state["items"] = feed_items

    # --------------------------------------------------------
    # Write files
    # --------------------------------------------------------

    write_rss(
        feed_items,
        rss_file,
        max_items,
    )

    save_state(
        state_file,
        state,
    )


# ============================================================
# CLI
# ============================================================

def main() -> int:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose logging",
    )

    parser.add_argument(
        "--max",
        type=int,
        default=MAX_ITEMS,
        help="Maximum RSS items",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=(
            logging.DEBUG
            if args.debug
            else logging.INFO
        ),
        format=(
            "%(asctime)s "
            "%(levelname)s: "
            "%(message)s"
        ),
    )

    try:

        update_feed(
            SITES,
            RSS_FILE,
            STATE_FILE,
            args.max,
        )

        logging.info(
            "Feed generation completed."
        )

        return 0

    except Exception:

        logging.exception(
            "Feed generation failed."
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
