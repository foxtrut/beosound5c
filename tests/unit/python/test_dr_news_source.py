"""Tests for the DR news source's feed parsing, body extraction and rendering.

The shapes mirror what dr.dk served in September 2026: RSS items with a
media:content image, and article pages whose body lives in __NEXT_DATA__
under viewProps.article (regular) or viewProps.resource ("Kort nyt").
"""
from __future__ import annotations

import json

from sources.dr_news import (
    DEFAULT_FEEDS,
    build_sections,
    extract_article,
    html_to_paragraphs,
    parse_feed,
    render_article,
    render_blocks,
    resolve_feeds,
    thumbnail_url,
)

IMAGE = ("https://asset.dr.dk/drdk/umbraco-images/frkfu3mn/x.jpg"
         "?im=AspectCrop=(1200,675),xPosition=.5,yPosition=.5;Resize=(1200,675)"
         "&amp;impolicy=medium")

FEED = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
  <title>Udland | DR</title>
  <item>
    <title>Diplomattog var kun lige kørt forbi</title>
    <link>https://www.dr.dk/nyheder/udland/diplomattog</link>
    <description>Boris Johnson var med toget.</description>
    <media:content url="{IMAGE}" medium="image"/>
  </item>
  <item>
    <title>En video</title>
    <link>https://www.dr.dk/nyheder/udland/reels/en-video</link>
  </item>
  <item>
    <title>Broken link</title>
    <link>https://www.dr.dk.</link>
  </item>
  <item>
    <title>Kort nyt uden billede</title>
    <link>https://www.dr.dk/nyheder/seneste/kort</link>
  </item>
</channel></rss>"""


def _page(view_props):
    data = {"props": {"pageProps": {"viewProps": view_props}}}
    return (f'<html><script id="__NEXT_DATA__" type="application/json">'
            f'{json.dumps(data)}</script></html>')


def _para(*nodes):
    return {"type": "ParagraphComponent", "body": list(nodes)}


def _text(t):
    return {"type": "Text", "text": t}


# ── Feeds ────────────────────────────────────────────────────────────────────

def test_parse_feed_reads_items_and_skips_reels_and_foreign_links():
    entries = parse_feed(FEED)
    assert [e["title"] for e in entries] == [
        "Diplomattog var kun lige kørt forbi", "Kort nyt uden billede"]
    first, short = entries
    assert first["summary"] == "Boris Johnson var med toget."
    assert first["image"].startswith("https://asset.dr.dk/")
    assert short["summary"] == "" and short["image"] == ""


def test_parse_feed_tolerates_garbage():
    assert parse_feed("<html>not a feed") == []


def test_thumbnail_url_shrinks_both_crop_and_resize():
    url = thumbnail_url(parse_feed(FEED)[0]["image"])
    assert "AspectCrop=(480,270)" in url and "Resize=(480,270)" in url
    assert "1200" not in url


def test_resolve_feeds_defaults_and_drops_unknown():
    assert resolve_feeds(None) == DEFAULT_FEEDS
    assert resolve_feeds([]) == DEFAULT_FEEDS
    assert resolve_feeds(["udland", "weekendavisen", "indland"]) == ["udland", "indland"]


# ── Article pages ────────────────────────────────────────────────────────────

def test_extract_regular_article():
    article = {"body": [_para(_text("Hej"))]}
    assert extract_article(_page({"article": article})) == article


def test_extract_short_news_resource():
    article = {"body": [_para(_text("Kort"))]}
    assert extract_article(_page({"resource": article})) == article


def test_extract_missing_data():
    assert extract_article("<html>no next data</html>") is None
    assert extract_article(_page({"site": {}})) is None
    assert extract_article('<script id="__NEXT_DATA__">{broken</script>') is None


BLOG = {
    "body": [_para(_text("Her i bloggen får du seneste nyt."))],
    "liveBlog": {"id": "drsport/1", "items": [
        {"id": "12439851", "title": "Keeper slap for udvisning",
         "content": "<p>Dommeren forklarer.</p><p><i>Se klippet.</i></p>"
                    '<ncpost-content data-html="&lt;iframe src=&quot;x&quot;&gt;">'
                    "</ncpost-content>"},
        {"id": "12439488", "title": "Dommer indrømmer fejl",
         "content": "<p>Det var&nbsp;<a href='https://tv2.dk'>rødt</a> &amp; klart.</p>"},
    ]},
}


def test_live_blog_focus_shows_only_that_post():
    link = "https://www.dr.dk/sporten/fodbold/blog?focusId=12439488"
    assert render_article(BLOG, link) == "<p>Det var rødt &amp; klart.</p>"


def test_live_blog_without_focus_or_stale_focus_shows_latest_posts():
    for link in ("https://www.dr.dk/sporten/fodbold/blog",
                 "https://www.dr.dk/sporten/fodbold/blog?focusId=1"):
        html = render_article(BLOG, link)
        assert html.startswith("<p>Her i bloggen får du seneste nyt.</p>")
        assert "<h3>Keeper slap for udvisning</h3><p>Dommeren forklarer.</p>" in html
        assert "<h3>Dommer indrømmer fejl</h3>" in html


def test_html_to_paragraphs_strips_tags_embeds_and_scripts():
    html = html_to_paragraphs(
        "<p>Tekst <b>fed</b></p><ncpost-content data-type='x'>&lt;iframe&gt;"
        "</ncpost-content><p><script>alert(1)</script></p>")
    assert html == "<p>Tekst fed</p><p>alert(1)</p>"
    assert html_to_paragraphs("Ingen afsnit") == "<p>Ingen afsnit</p>"
    assert html_to_paragraphs(None) == ""


# ── Rendering ────────────────────────────────────────────────────────────────

def test_render_paragraph_formatting_and_links_as_text():
    html = render_blocks([_para(
        _text("Læs "),
        {"type": "Italic", "body": [_text("bedt")]},
        {"type": "Bold", "body": [_text("fed")]},
        {"type": "Link", "url": "https://x", "body": [_text(" kilden")]},
    )])
    assert html == "<p>Læs <em>bedt</em><strong>fed</strong> kilden</p>"


def test_render_escapes_third_party_text():
    html = render_blocks([_para(_text("<script>alert(1)</script> & co"))])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "&amp; co" in html


def test_render_heading_quote_and_factbox():
    html = render_blocks([
        {"type": "HeadingComponent", "text": "Mellemrubrik"},
        {"type": "QuoteComponent", "body": "Et citat", "citation": "Nogen"},
        {"type": "FactBoxComponent", "expression": {
            "title": "Blå bog",
            "body": [{"type": "Paragraph", "body": [_text("Født 1981")]}],
        }},
    ])
    assert "<h3>Mellemrubrik</h3>" in html
    assert "<blockquote>Et citat<br>— Nogen</blockquote>" in html
    assert '<div class="factbox"><h3>Blå bog</h3><p>Født 1981</p></div>' in html


def test_render_emphasized_list():
    html = render_blocks([{"type": "EmphasizedListComponent", "items": [
        {"title": None, "marker": None, "body": [_para(_text("Intro"))]},
        {"title": "Hvem vinder?", "marker": "1", "body": [
            {"type": "ImageComponent", "image": {}}, _para(_text("Blokkene"))]},
    ]}])
    assert html == "<p>Intro</p><h3>1. Hvem vinder?</h3><p>Blokkene</p>"


def test_render_drops_media_and_empty_paragraphs():
    html = render_blocks([
        {"type": "ImageComponent", "image": {}},
        {"type": "MediaComponent", "resource": {}},
        {"type": "OEmbedComponent", "url": "https://x"},
        _para(_text("  ")),
    ])
    assert html == ""


# ── Sections ─────────────────────────────────────────────────────────────────

def test_build_sections_dedupes_across_feeds_and_drops_empty():
    shared = {"link": "https://dr.dk/a", "title": "Delt", "summary": "", "image": ""}
    only = {"link": "https://dr.dk/b", "title": "Kun udland", "summary": "", "image": ""}
    sections = build_sections(
        [("indland", [shared]), ("udland", [shared, only]), ("penge", [])],
        {"https://dr.dk/a": "<p>Tekst</p>"},
    )
    assert [s["id"] for s in sections] == ["sec-indland", "sec-udland"]
    assert [a["name"] for a in sections[1]["articles"]] == ["Kun udland"]
    assert sections[0]["articles"][0]["page"]["body"] == "<p>Tekst</p>"


def test_build_article_summary_image_and_fallback():
    long_title = "En meget lang overskrift der skal forkortes i buen"
    with_image = {"link": "l1", "title": long_title, "summary": "Resumé & mere",
                  "image": parse_feed(FEED)[0]["image"]}
    bare = {"link": "l2", "title": "Uden tekst", "summary": "", "image": ""}
    arts = build_sections([("udland", [with_image, bare])], {})[0]["articles"]

    assert arts[0]["name"].endswith("...") and len(arts[0]["name"]) == 40
    assert arts[0]["page"]["title"] == long_title
    assert "Resize=(480,270)" in arts[0]["image"]
    assert arts[0]["page"]["body"] == "<p><em>Resumé &amp; mere</em></p>"

    assert "image" not in arts[1] and arts[1]["icon"] == "article"
    assert "dr.dk" in arts[1]["page"]["body"]
