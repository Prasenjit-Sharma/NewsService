from __future__ import annotations

import os
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional
from urllib.parse import quote

import feedparser
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from supabase import Client, create_client

# Loads .env for local runs; a no-op in GitHub Actions, where these are
# injected directly as environment variables (see the workflow file).
load_dotenv()

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# Overridable without touching code: set GEMINI_MODEL in .env locally, or as
# a repo Variable (not Secret — it isn't sensitive) in GitHub Actions.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

# Same Supabase project PolyInsights uses for everything else it's migrated
# off Google Sheets. Optional (script still just prints if unset) so the
# existing hourly run keeps working right up until these are added.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

MAX_AGE_HOURS = 24
TOP_N = 15
MAX_SUMMARY_POINTS = 10

# Polymer price news is scraped separately (plastemart_news.py, every 6h)
# into news_items under this category — read here as the highest-priority
# prompt input.
POLYMER_NEWS_CATEGORY = "Polymer News"
# Plastemart's price-news list updates in sparse clusters (roughly every
# 1-2 weeks), not daily — a tight window would leave Gemini with nothing
# to prioritize most of the time. Each item still carries its own true
# date, so nothing gets misrepresented as fresher than it is.
POLYMER_NEWS_MAX_AGE_HOURS = 24 * 7
POLYMER_NEWS_LIMIT = 15
# Guaranteed in code below, not just asked for in the prompt — LLMs don't
# reliably honor a "must include" instruction on every run.
MIN_POLYMER_BULLETS = 2

# One Google News RSS search per topic — "when:1d" is Google's own (loose)
# recency filter; get_fresh_headlines() re-checks precisely against each
# entry's real published timestamp below.
RSS_TOPICS: dict[str, str] = {
    "Crude oil & energy": "crude oil OR OPEC OR oil prices OR refinery",
    "Global conflicts & geopolitics": "war OR conflict OR geopolitical tensions",
    "India economy & energy": "India economy OR India energy OR India trade OR India politics or India Sensex or India Nifty",
}


@lru_cache
def _get_client() -> Optional[Client]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _google_news_rss_url(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote(query)}+when:1d&hl=en-IN&gl=IN&ceid=IN:en"


@dataclass
class ScrapedHeadline:
    headline: str
    url: str
    category: str
    published_at: datetime
    hours_ago: float


def get_fresh_headlines() -> list[ScrapedHeadline]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    seen_urls: set[str] = set()
    fresh: list[ScrapedHeadline] = []

    for category, query in RSS_TOPICS.items():
        url = _google_news_rss_url(query)
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            response.raise_for_status()
        except requests.RequestException:
            continue

        feed = feedparser.parse(response.content)
        for entry in feed.entries:
            published_struct = getattr(entry, "published_parsed", None)
            if published_struct is None:
                continue
            published_at = datetime.fromtimestamp(timegm(published_struct), tz=timezone.utc)
            if published_at < cutoff or published_at > now:
                continue

            link = getattr(entry, "link", None)
            title = getattr(entry, "title", None)
            if not link or not title or link in seen_urls:
                continue
            seen_urls.add(link)

            fresh.append(
                ScrapedHeadline(
                    headline=title,
                    url=link,
                    category=category,
                    published_at=published_at,
                    hours_ago=(now - published_at).total_seconds() / 3600,
                )
            )

    fresh.sort(key=lambda h: h.published_at, reverse=True)
    return fresh


@dataclass
class PolymerNewsRow:
    title: str
    details: str
    published_at: datetime
    hours_ago: float


def get_recent_polymer_news() -> list[PolymerNewsRow]:
    """Reads price-news rows written by plastemart_news.py (runs every 6h,
    separate schedule) into the same news_items table as RSS headlines,
    tagged category="Polymer News" — treated as the highest-priority
    prompt input since it's a direct, real price-change announcement
    rather than a headline Gemini has to infer market relevance from."""
    client = _get_client()
    if client is None:
        return []

    # Filtered/sorted by published_at (the real price-change date, derived
    # from Plastemart's own date on each item) — NOT fetched_at (when our
    # scraper first saw the row). fetched_at reflects scrape time, which on
    # a first-ever/backfill run is "just now" for every row regardless of
    # how old the underlying announcement actually is — using it here would
    # make week-old rate revisions look brand new to Gemini.
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=POLYMER_NEWS_MAX_AGE_HOURS)
    try:
        response = (
            client.table("news_items")
            .select("headline,details,published_at")
            .eq("category", POLYMER_NEWS_CATEGORY)
            .gte("published_at", cutoff.isoformat())
            .order("published_at", desc=True)
            .limit(POLYMER_NEWS_LIMIT)
            .execute()
        )
    except Exception:
        return []

    rows: list[PolymerNewsRow] = []
    for r in response.data:
        published_at = datetime.fromisoformat(r["published_at"])
        rows.append(
            PolymerNewsRow(
                title=r["headline"],
                details=r["details"],
                published_at=published_at,
                hours_ago=(now - published_at).total_seconds() / 3600,
            )
        )
    return rows


class MarketBullet(BaseModel):
    text: str = Field(
        description=(
            "One sentence, no leading dash or bullet character — the UI adds that. For a P-list "
            "(polymer price news) source_ref: no strict word cap — keep the concrete specifics "
            "(company, grade(s), exact INR/MT amount, and the date) intact rather than compressing "
            "them away; cover only ONE P-list item's price move, never merge multiple companies' or "
            "grades' announcements into one bullet even if they happened the same day. For an H-list "
            "(general headline) source_ref: stay concise, no more than ~18 words."
        )
    )
    source_ref: str = Field(
        pattern=r"^[PH]\d+$",
        description=(
            "The single P# (polymer price news) or H# (general headline) label from the lists "
            "above that this bullet is primarily based on, e.g. 'P2' or 'H5'."
        ),
    )


class RankedSummary(BaseModel):
    selected_indices: list[int] = Field(
        description=(
            f"0-based indices into the H-list (general headlines) only — at most {TOP_N} — chosen "
            "for relevance to polymer market drivers (crude oil prices/supply, global conflicts/"
            "wars/geopolitical tensions affecting energy or trade, and India-specific economic/"
            "energy developments). Ordered most significant first."
        )
    )
    market_bullets: list[MarketBullet] = Field(
        description=(
            f"At most {MAX_SUMMARY_POINTS} bullet points synthesizing the key market impacts "
            "and geopolitical developments, focused on drivers relevant to polymer/petrochemical "
            f"pricing. If the P-list has {MIN_POLYMER_BULLETS} or more items, at least "
            f"{MIN_POLYMER_BULLETS} bullets MUST have a P# source_ref, placed first, ahead of "
            "bullets drawn only from general headlines. Most significant point first."
        )
    )


def build_prompt(polymer_news: list[PolymerNewsRow], headlines: list[ScrapedHeadline]) -> str:
    polymer_listing = (
        "\n".join(
            f"P{i}. [{p.published_at.date().isoformat()}] {p.details}" for i, p in enumerate(polymer_news)
        )
        if polymer_news
        else "(none available this run)"
    )
    headline_listing = "\n".join(
        f"H{i}. [{h.category}] [{h.hours_ago:.1f}h ago] {h.headline}" for i, h in enumerate(headlines)
    )
    return f"""
You are a market intelligence analyst for a polymer/petrochemical pricing desk.

POLYMER PRICE NEWS — HIGHEST PRIORITY. Real price-change announcements scraped directly from
the Indian polymer market, each tagged with the actual date it happened (not how long ago —
some of these may be several days old, and that's fine, but the date you state or imply must be
correct). If {MIN_POLYMER_BULLETS} or more are present, at least {MIN_POLYMER_BULLETS} of your
bullets MUST be based on them (source_ref starting with P), placed first, ahead of anything drawn
only from the general headlines below. One bullet per item — do not combine two different P-list
entries (e.g. two different companies, or a PP move and a separate PE move) into one bullet.

{polymer_listing}

GENERAL MARKET HEADLINES (last {MAX_AGE_HOURS}h) — crude oil/energy, global conflicts/
geopolitics, and India's economy. Do not invent new headlines — only choose from this list.

{headline_listing}

From the general headlines (H-list) only, select at most {TOP_N} most relevant to polymer
market drivers (crude oil prices/supply, global conflicts/geopolitical tensions affecting
energy or trade, and India-specific economic/energy developments), ordered most significant
first, and return their H-indices as selected_indices.

Then write at most {MAX_SUMMARY_POINTS} bullet points synthesizing the key market impacts across
both lists, most significant first, tagging each with the single P#/H# label it's primarily
based on.
""".strip()


def summarize(polymer_news: list[PolymerNewsRow], headlines: list[ScrapedHeadline]) -> RankedSummary:
    # attempts=1 disables the SDK's default retry-on-429 behavior, which
    # would otherwise burn through this project's small free-tier quota
    # retrying a request that's already over the per-minute limit.
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
    )

    interaction = client.interactions.create(
        model=GEMINI_MODEL,
        input=build_prompt(polymer_news, headlines),
        # No tools needed — the model is summarizing/ranking headlines we
        # already fetched, not searching the web itself.
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": RankedSummary.model_json_schema(),
        },
    )

    return RankedSummary.model_validate_json(interaction.output_text)


def _resolve_source_timestamp(
    source_ref: str, polymer_news: list[PolymerNewsRow], headlines: list[ScrapedHeadline]
) -> Optional[datetime]:
    ref = source_ref.strip().upper()
    try:
        idx = int(ref[1:])
    except (ValueError, IndexError):
        return None

    if ref.startswith("P") and 0 <= idx < len(polymer_news):
        return polymer_news[idx].published_at
    if ref.startswith("H") and 0 <= idx < len(headlines):
        return headlines[idx].published_at
    return None


def store_digest(picked: list[ScrapedHeadline], bullets: list[dict]) -> None:
    """Upserts into Supabase: news_items deduped on fingerprint (the RSS
    article URL, doubling as the dedup key so both this and
    plastemart_news.py's polymer rows — which have no URL, and fingerprint
    on a content hash instead — share one unique constraint), news_summary
    always overwriting the single id=1 row. No-ops with a note if Supabase
    env vars aren't set yet.

    news_summary.bullets is a jsonb array of {text, published_at} so each
    point carries its own real timestamp; .summary stays a plain
    newline-joined fallback for any reader that hasn't migrated yet."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("(SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping persistence)")
        return

    client: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    if picked:
        rows = [
            {
                "headline": h.headline,
                "url": h.url,
                "category": h.category,
                "published_at": h.published_at.isoformat(),
                "fingerprint": h.url,
            }
            for h in picked
        ]
        client.table("news_items").upsert(rows, on_conflict="fingerprint", ignore_duplicates=True).execute()

    client.table("news_summary").upsert(
        {
            "id": 1,
            "summary": "\n".join(b["text"] for b in bullets),
            "bullets": [{"text": b["text"], "published_at": b["published_at"].isoformat()} for b in bullets],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="id",
    ).execute()

    print(f"Stored {len(picked)} headline(s) + {len(bullets)} summary point(s) in Supabase.")


if __name__ == "__main__":
    polymer_news = get_recent_polymer_news()
    fresh_headlines = get_fresh_headlines()

    if not fresh_headlines and not polymer_news:
        print(f"No polymer price news and no headlines found within the last {MAX_AGE_HOURS}h.")
    else:
        result = summarize(polymer_news, fresh_headlines)

        # Belt-and-braces: keep only valid, in-range indices, capped at TOP_N.
        seen: set[int] = set()
        picked: list[ScrapedHeadline] = []
        for idx in result.selected_indices:
            if 0 <= idx < len(fresh_headlines) and idx not in seen:
                seen.add(idx)
                picked.append(fresh_headlines[idx])
            if len(picked) == TOP_N:
                break

        print(f"Top {len(picked)} news (last {MAX_AGE_HOURS}h)\n")
        for h in picked:
            print(f"[{h.category}] [{h.hours_ago:.1f}h ago] {h.headline}")
            print(f"    {h.url}")
            print()

        # Belt-and-braces cap, same reasoning as the indices above. Falls
        # back to "now" for any bullet whose source_ref didn't resolve.
        now = datetime.now(timezone.utc)
        capped_bullets = result.market_bullets[:MAX_SUMMARY_POINTS]
        bullets = [
            {
                "text": b.text.strip(),
                "published_at": _resolve_source_timestamp(b.source_ref, polymer_news, fresh_headlines) or now,
            }
            for b in capped_bullets
        ]

        # Guarantee, rather than just ask: if enough polymer price news
        # exists, make sure it's actually represented — Gemini doesn't
        # always follow the priority instruction. Promote unused P-items
        # to the front, ahead of whatever Gemini wrote, and trim the tail
        # back down to MAX_SUMMARY_POINTS so genuinely low-priority
        # general-headline bullets are what gets dropped.
        polymer_wanted = min(MIN_POLYMER_BULLETS, len(polymer_news))
        used_polymer_indices = {
            int(b.source_ref[1:]) for b in capped_bullets if b.source_ref.startswith("P")
        }
        polymer_bullet_count = len(used_polymer_indices)
        for i, p in enumerate(polymer_news):
            if polymer_bullet_count >= polymer_wanted:
                break
            if i in used_polymer_indices:
                continue
            bullets.insert(polymer_bullet_count, {"text": p.details.strip(), "published_at": p.published_at})
            polymer_bullet_count += 1
        bullets = bullets[:MAX_SUMMARY_POINTS]

        print("Market Commentary")
        for b in bullets:
            print(f"- [{b['published_at'].isoformat()}] {b['text']}")

        store_digest(picked, bullets)
