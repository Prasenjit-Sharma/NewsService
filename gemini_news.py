from __future__ import annotations

import os
import time
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

# One Google News RSS search per topic — "when:1d" is Google's own (loose)
# recency filter; get_fresh_headlines() re-checks precisely against each
# entry's real published timestamp below.
RSS_TOPICS: dict[str, str] = {
    "Crude oil & energy": "crude oil OR OPEC OR oil prices OR refinery",
    "Global conflicts & geopolitics": "war OR conflict OR geopolitical tensions",
    "India economy & energy": "India economy OR India energy OR India trade OR India politics or India Sensex or India Nifty or LPG diversion or polymers or petrochemicals or refinery",
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


class MarketDriver(BaseModel):
    label: str = Field(description="A short 2-4 word theme name, e.g. 'Crude Oil', 'Geopolitics', 'India Economy'.")
    text: str = Field(description="1-2 sentences, ~25-45 words, this theme's specific development and its market implication.")


class RankedSummary(BaseModel):
    selected_indices: list[int] = Field(
        description=(
            f"0-based indices into the H-list (general headlines) only — at most {TOP_N} — chosen "
            "for relevance to polymer market drivers (crude oil prices/supply, global conflicts/"
            "wars/geopolitical tensions affecting energy or trade, and India-specific economic/"
            "energy developments). Ordered most significant first."
        )
    )
    teaser: str = Field(
        description=(
            "ONE punchy sentence, 20-30 words, distilling the single most important takeaway across "
            "ALL the drivers below — for a reader who only has a few seconds, not necessarily "
            "whatever happens to be the first driver. Prose, no leading dash, no bullet."
        )
    )
    drivers: list[MarketDriver] = Field(
        description=(
            "3 to 5 distinct thematic drivers synthesizing the overall market situation across ALL "
            "the headlines below (crude oil trajectory, geopolitical developments, India-specific "
            "economic conditions, and their bearing on polymer/petrochemical pricing) — each its own "
            "short, scannable point, not a restatement of one individual headline and not a single "
            "merged paragraph. Most significant driver first."
        )
    )


def build_prompt(headlines: list[ScrapedHeadline]) -> str:
    headline_listing = "\n".join(
        f"H{i}. [{h.category}] [{h.hours_ago:.1f}h ago] {h.headline}" for i, h in enumerate(headlines)
    )
    return f"""
You are a market intelligence analyst for a polymer/petrochemical pricing desk. You are given
GENERAL MARKET HEADLINES (last {MAX_AGE_HOURS}h) covering crude oil/energy, global conflicts/
geopolitics, and India's economy — NOT polymer/grade-specific price announcements, which are
tracked and shown separately and are not your job here. Do not invent new headlines — only choose
from this list.

{headline_listing}

From this list, select at most {TOP_N} most relevant to polymer market drivers (crude oil prices/
supply, global conflicts/geopolitical tensions affecting energy or trade, and India-specific
economic/energy developments), ordered most significant first, and return their H-indices as
selected_indices.

Then, reading across ALL the headlines above (not just the selected subset), write TWO things:
1. teaser — ONE punchy sentence, 20-30 words, the single most important takeaway across everything
   below, for a reader who only has a few seconds.
2. drivers — 3 to 5 distinct thematic points (each a short 2-4 word label plus a 1-2 sentence
   takeaway) covering the overall market situation and its bearing on polymer/petrochemical
   pricing — synthesized themes, not one bullet per headline and not a single merged paragraph.
""".strip()


# How many times to try the Gemini call before letting the run fail. The
# hourly autorun's actual failures (checked against GitHub Actions run
# logs) are almost all transient: 429 rate-limit ("Please retry in ~4-50s"),
# 500 "high demand", or a reset connection — each one likely to succeed a
# few seconds later, well within one job run.
_SUMMARIZE_MAX_ATTEMPTS = 3
# Used only when the error itself doesn't say how long to wait.
_SUMMARIZE_FALLBACK_BACKOFF_SECONDS = (5, 20)


def _retry_delay_seconds(exc: Exception, fallback: float) -> float:
    """Prefers the API's own Retry-After header (present on the 429/500
    responses this project actually hits) over a fixed guess."""
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", None)
    retry_after = header.get("retry-after") if header is not None else None
    if retry_after:
        try:
            return max(float(retry_after), 1.0)
        except ValueError:
            pass
    return fallback


def summarize(headlines: list[ScrapedHeadline]) -> RankedSummary:
    # attempts=1 disables the SDK's default retry-on-429 behavior, which
    # retries near-instantly and would burn through this project's small
    # free-tier quota on a request that's already over the per-minute
    # limit — the bounded, backed-off retry loop below replaces it.
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
    )

    for attempt in range(_SUMMARIZE_MAX_ATTEMPTS):
        try:
            interaction = client.interactions.create(
                model=GEMINI_MODEL,
                input=build_prompt(headlines),
                # No tools needed — the model is summarizing/ranking
                # headlines we already fetched, not searching the web itself.
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": RankedSummary.model_json_schema(),
                },
            )
            return RankedSummary.model_validate_json(interaction.output_text)
        except Exception as exc:
            if attempt == _SUMMARIZE_MAX_ATTEMPTS - 1:
                raise
            fallback = _SUMMARIZE_FALLBACK_BACKOFF_SECONDS[
                min(attempt, len(_SUMMARIZE_FALLBACK_BACKOFF_SECONDS) - 1)
            ]
            delay = _retry_delay_seconds(exc, fallback)
            print(
                f"Gemini call failed ({exc.__class__.__name__}: {exc}); "
                f"retrying in {delay:.0f}s (attempt {attempt + 2}/{_SUMMARIZE_MAX_ATTEMPTS})..."
            )
            time.sleep(delay)

    raise AssertionError("unreachable")  # loop always returns or raises


def store_digest(picked: list[ScrapedHeadline], teaser_text: str, drivers: list[dict]) -> None:
    """Upserts into Supabase: news_items deduped on fingerprint (the RSS
    article URL — polymer price rows from plastemart_news.py live in the
    same table but are written and deduped separately, on their own
    event_key-based fingerprint), market_summary_daily keyed by today's UTC
    date rather than the old news_summary singleton, so PolyInsights can
    keep a day-by-day history and a superadmin can publish/hide/edit any
    given day from Content Control. No-ops with a note if Supabase env vars
    aren't set yet.

    Only {summary_date, auto_teaser, auto_drivers, generated_at} are sent
    on the upsert — Supabase's upsert only touches the columns given, so an
    admin's mode/override_summary edit for today is never clobbered by this
    nightly write landing on the same row."""
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

    today = datetime.now(timezone.utc).date().isoformat()
    client.table("market_summary_daily").upsert(
        {
            "summary_date": today,
            "auto_teaser": teaser_text,
            "auto_drivers": drivers,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="summary_date",
    ).execute()

    print(f"Stored {len(picked)} headline(s) + {len(drivers)} driver(s) in Supabase (date={today}).")


if __name__ == "__main__":
    fresh_headlines = get_fresh_headlines()

    if not fresh_headlines:
        print(f"No headlines found within the last {MAX_AGE_HOURS}h.")
    else:
        result = summarize(fresh_headlines)

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

        teaser_text = result.teaser.strip()
        drivers = [{"label": d.label.strip(), "text": d.text.strip()} for d in result.drivers]
        print("Market Commentary\n")
        print(f"Teaser: {teaser_text}\n")
        for d in drivers:
            print(f"- [{d['label']}] {d['text']}")

        store_digest(picked, teaser_text, drivers)
