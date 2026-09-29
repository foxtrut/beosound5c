#!/usr/bin/env python3
"""
BeoSound 5c — DR News source (Danmarks Radio RSS feeds).

Reads DR's public section feeds (title, summary, image), fetches each
article page for its body text, groups articles by feed section and
serves them to the softarc V2 frontend for browsing — the same shape
the Guardian news source serves, so articles open as page views.
No API key required.

DR's feeds carry no body text, so each article page is fetched once and
its text read from the page's embedded Next.js data (__NEXT_DATA__).
Bodies are cached by link, so a refresh only fetches new articles.
Video-only "reels" are skipped — they have no text to show. Sport feed
items often point at one post in a live blog (?focusId=…); those show
that post, or the blog's latest posts once it has scrolled out.

Config (config.json), optional:
    "dr_news": { "feeds": ["senestenyt", "indland", "udland"] }
Defaults to DEFAULT_FEEDS; names must be keys of FEEDS.

Port: 8792
"""

import asyncio
import html
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

import aiohttp
from aiohttp import web

sys.path.insert(0, "..")
sys.path.insert(0, ".")

from lib.config import cfg
from lib.source_base import SourceBase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DR_NEWS] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

FEED_URL = "https://www.dr.dk/nyheder/service/feeds/{}"
REFRESH_INTERVAL = 15 * 60  # 15 minutes
ARTICLE_CONCURRENCY = 4     # parallel article page fetches — gentle on DR and the Pi
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
THUMB_SIZE = "(480,270)"    # feed images are 1200x675; the arc never shows them that big

# Feed slug → (section name, phosphor icon, colour)
FEEDS = {
    "senestenyt": ("Seneste nyt", "lightning", "#E74C3C"),
    "indland": ("Indland", "house-line", "#3498DB"),
    "udland": ("Udland", "globe-hemisphere-west", "#2ECC71"),
    "politik": ("Politik", "bank", "#9B59B6"),
    "penge": ("Penge", "currency-circle-dollar", "#F39C12"),
    "kultur": ("Kultur", "palette", "#8E44AD"),
    "viden": ("Viden", "flask", "#4ECDC4"),
    "sporten": ("Sport", "football", "#E67E22"),
    "senestesport": ("Seneste sport", "football", "#D35400"),
    "musik": ("Musik", "music-notes", "#FD79A8"),
    "regionale": ("Regionalt", "map-pin", "#16A085"),
    "vejret": ("Vejret", "cloud-sun", "#74B9FF"),
}
DEFAULT_FEEDS = ["senestenyt", "indland", "udland", "politik", "penge",
                 "kultur", "viden", "sporten"]

_MEDIA_NS = "{http://search.yahoo.com/mrss/}"
_DR_PREFIX = "https://www.dr.dk/"
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_IMAGE_SIZE_RE = re.compile(r"(AspectCrop|Resize)=\(\d+,\d+\)")
_EMBED_RE = re.compile(r"<ncpost-content\b.*?</ncpost-content>", re.S | re.I)
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def resolve_feeds(configured):
    """Configured feed list → known slugs, or DEFAULT_FEEDS if unset."""
    if not configured:
        return list(DEFAULT_FEEDS)
    known = [f for f in configured if f in FEEDS]
    for f in configured:
        if f not in FEEDS:
            log.warning("Unknown DR feed %r in dr_news.feeds — ignored "
                        "(known: %s)", f, ", ".join(FEEDS))
    return known


def thumbnail_url(url):
    return _IMAGE_SIZE_RE.sub(lambda m: f"{m.group(1)}={THUMB_SIZE}", url)


def parse_feed(xml_text):
    """RSS text → [{link, title, summary, image}], reels left out."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        # Only DR article pages carry the embedded data (feeds have been
        # seen with a bare "https://www.dr.dk." link).
        if not link.startswith(_DR_PREFIX) or not title or "/reels/" in link:
            continue
        media = item.find(f"{_MEDIA_NS}content")
        entries.append({
            "link": link,
            "title": title,
            "summary": (item.findtext("description") or "").strip(),
            "image": media.get("url", "") if media is not None else "",
        })
    return entries


def extract_article(page_html):
    """Article page HTML → its embedded article dict, or None if not found.

    Regular articles live under viewProps.article, short news ("Kort nyt")
    under viewProps.resource.
    """
    match = _NEXT_DATA_RE.search(page_html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        view = data["props"]["pageProps"]["viewProps"]
    except (ValueError, KeyError, TypeError):
        return None
    article = view.get("article") or view.get("resource")
    return article if isinstance(article, dict) else None


def html_to_paragraphs(fragment):
    """Third-party HTML (live blog posts) → plain escaped <p> paragraphs.

    Keeps the text only: tags are stripped and the text re-escaped, and
    embedded video players (<ncpost-content>) are dropped whole.
    """
    fragment = _EMBED_RE.sub("", fragment or "")
    parts = _PARAGRAPH_RE.findall(fragment) or [fragment]
    out = []
    for part in parts:
        text = html.unescape(_TAG_RE.sub("", part)).replace("\xa0", " ").strip()
        if text:
            out.append(f"<p>{html.escape(text)}</p>")
    return "".join(out)


def _render_inline(nodes):
    out = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        if kind == "Text":
            out.append(html.escape(node.get("text", "")))
        elif kind == "Italic":
            out.append(f"<em>{_render_inline(node.get('body'))}</em>")
        elif kind == "Bold":
            out.append(f"<strong>{_render_inline(node.get('body'))}</strong>")
        else:
            # Links included: there is nowhere to follow them on the device,
            # so they read as plain text.
            out.append(_render_inline(node.get("body")))
    return "".join(out)


def render_blocks(blocks):
    """DR body blocks → HTML. Images, video, embeds and read-more links
    are dropped — the page view is for reading."""
    out = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("ParagraphComponent", "Paragraph"):
            text = _render_inline(block.get("body"))
            if text.strip():
                out.append(f"<p>{text}</p>")
        elif kind == "HeadingComponent":
            if block.get("text"):
                out.append(f"<h3>{html.escape(block['text'])}</h3>")
        elif kind == "QuoteComponent":
            quote = block.get("body")
            if isinstance(quote, str) and quote.strip():
                citation = block.get("citation")
                cite = f"<br>— {html.escape(citation)}" if citation else ""
                out.append(f"<blockquote>{html.escape(quote)}{cite}</blockquote>")
        elif kind == "FactBoxComponent":
            box = block.get("expression") or {}
            title = f"<h3>{html.escape(box['title'])}</h3>" if box.get("title") else ""
            inner = render_blocks(box.get("body"))
            if title or inner:
                out.append(f'<div class="factbox">{title}{inner}</div>')
        elif kind == "EmphasizedListComponent":
            # "Six things to watch": numbered sections, each with its own blocks.
            for item in block.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("title"):
                    marker = f"{item['marker']}. " if item.get("marker") else ""
                    out.append(f"<h3>{html.escape(marker + item['title'])}</h3>")
                out.append(render_blocks(item.get("body")))
    return "".join(out)


def render_article(article, link):
    """Embedded article → body HTML.

    A live blog link with ?focusId= shows just that post; if the post has
    scrolled out of the page's latest posts, or there is no focus, the
    blog's intro and its latest posts are shown instead.
    """
    if not article:
        return ""
    posts = [p for p in (article.get("liveBlog") or {}).get("items") or []
             if isinstance(p, dict)]
    focus = parse_qs(urlparse(link).query).get("focusId", [None])[0]
    if focus:
        for post in posts:
            if str(post.get("id")) == focus:
                return html_to_paragraphs(post.get("content"))

    out = [render_blocks(article.get("body"))]
    for post in posts:
        if post.get("title"):
            out.append(f"<h3>{html.escape(post['title'])}</h3>")
        out.append(html_to_paragraphs(post.get("content")))
    return "".join(out)


def build_article(entry, body_html, color):
    title = entry["title"]
    art = {
        "id": entry["link"],
        "name": title if len(title) <= 40 else title[:37] + "...",
    }
    if entry["image"]:
        art["image"] = thumbnail_url(entry["image"])
    else:
        art["icon"] = "article"
        art["color"] = color

    page_body = ""
    if entry["summary"]:
        page_body += f"<p><em>{html.escape(entry['summary'])}</em></p>"
    page_body += body_html or ""
    if not page_body:
        page_body = "<p>Artiklen har ingen tekst — se den på dr.dk.</p>"
    art["page"] = {"title": title, "body": page_body}
    return art


def build_sections(feed_entries, bodies):
    """[(slug, entries)] + {link: body_html} → frontend sections.

    An article listed in several feeds only appears in the first one
    (feed order is the configured order); empty sections are dropped.
    """
    seen = set()
    sections = []
    for slug, entries in feed_entries:
        name, icon, color = FEEDS[slug]
        articles = []
        for entry in entries:
            if entry["link"] in seen:
                continue
            seen.add(entry["link"])
            articles.append(build_article(entry, bodies.get(entry["link"]), color))
        if articles:
            sections.append({
                "id": f"sec-{slug}",
                "name": name,
                "icon": icon,
                "color": color,
                "articles": articles,
            })
    return sections


class DrNewsService(SourceBase):
    id = "dr_news"
    name = "DR Nyheder"
    port = 8792
    player = "local"
    action_map = {
        "go": "select",
        "up": "up",
        "down": "down",
        "left": "back",
        "right": "select",
    }

    def __init__(self):
        super().__init__()
        self._feeds = []
        self._sections = []
        self._bodies = {}        # article link → rendered body HTML
        self._last_fetch = 0

    async def on_start(self):
        self._feeds = resolve_feeds(cfg("dr_news", "feeds", default=None))
        if not self._feeds:
            log.info("No valid feeds in dr_news.feeds — DR news source disabled")
            raise SystemExit(0)

        log.info("DR feeds: %s — starting article fetch loop", ", ".join(self._feeds))
        await self.register("available")
        self._spawn(self._refresh_loop(), name="refresh_loop")

    async def on_stop(self):
        await self.register("gone")

    async def _refresh_loop(self):
        while True:
            try:
                await self._fetch_articles()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("Fetch failed: %s", e)
            await asyncio.sleep(REFRESH_INTERVAL)

    async def _fetch_text(self, url):
        async with self._http_session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.text()

    async def _fetch_feed(self, slug):
        try:
            return parse_feed(await self._fetch_text(FEED_URL.format(slug)))
        except Exception as e:
            log.warning("Feed %s failed: %s", slug, e)
            return None

    async def _fetch_body(self, entry, sem):
        async with sem:
            try:
                page = await self._fetch_text(entry["link"])
            except Exception as e:
                # Not cached — retried on the next refresh.
                log.warning("Article %s failed: %s", entry["link"], e)
                return
        # Cached even when empty (e.g. a page without embedded data), so
        # it is not refetched every refresh.
        self._bodies[entry["link"]] = render_article(extract_article(page), entry["link"])

    async def _fetch_articles(self):
        log.info("Fetching DR feeds...")
        results = await asyncio.gather(*(self._fetch_feed(s) for s in self._feeds))
        if all(r is None for r in results):
            log.error("Every DR feed failed — keeping the previous articles")
            return
        feed_entries = [(s, r or []) for s, r in zip(self._feeds, results)]

        links = {e["link"]: e for _, entries in feed_entries for e in entries}
        missing = [e for link, e in links.items() if link not in self._bodies]
        sem = asyncio.Semaphore(ARTICLE_CONCURRENCY)
        await asyncio.gather(*(self._fetch_body(e, sem) for e in missing))
        self._bodies = {k: v for k, v in self._bodies.items() if k in links}

        self._sections = build_sections(feed_entries, self._bodies)
        self._last_fetch = time.time()
        log.info("Got %d articles in %d sections (%d article pages fetched)",
                 sum(len(s["articles"]) for s in self._sections),
                 len(self._sections), len(missing))

    def add_routes(self, app):
        app.router.add_get("/articles", self._handle_articles)

    async def _handle_articles(self, request):
        return web.json_response(self._sections, headers=self._cors_headers())

    async def handle_status(self):
        return {
            "source": self.id,
            "name": self.name,
            "feeds": self._feeds,
            "article_count": sum(len(s["articles"]) for s in self._sections),
            "section_count": len(self._sections),
            "last_fetch": self._last_fetch,
        }

    async def handle_resync(self):
        await self.register("available")
        return {"status": "ok", "resynced": True}

    async def handle_command(self, cmd, data):
        if cmd == "refresh":
            await self._fetch_articles()
            return {"refreshed": True, "section_count": len(self._sections)}
        return {}


if __name__ == "__main__":
    service = DrNewsService()
    asyncio.run(service.run())
