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
MAX_SUMMARY_POINTS = 10

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


class MarketBullet(BaseModel):
    text: str = Field(
        description=(
            "One sentence, no leading dash or bullet character — the UI adds that. Stay concise, no "
            "more than ~25 words. Cover only ONE headline's development, never merge two distinct "
            "developments into one bullet even if they happened the same day."
        )
    )
    source_ref: str = Field(
        pattern=r"^H\d+$",
        description="The single H# label from the list above that this bullet is primarily based on, e.g. 'H5'.",
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
            f"At most {MAX_SUMMARY_POINTS} bullet points synthesizing the key market impacts and "
            "geopolitical developments from the H-list, focused on drivers relevant to polymer/"
            "petrochemical pricing (crude, energy, conflict, India economy). Most significant point "
            "first. Specific polymer/grade price moves are NOT this list's job — those are surfaced "
            "elsewhere as structured price-event cards — so don't invent or restate one here even if "
            "a headline happens to mention a price."
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

Then write at most {MAX_SUMMARY_POINTS} bullet points synthesizing the key market impacts, most
significant first, tagging each with the single H# label it's primarily based on.
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


def _resolve_source_timestamp(source_ref: str, headlines: list[ScrapedHeadline]) -> Optional[datetime]:
    ref = source_ref.strip().upper()
    try:
        idx = int(ref[1:])
    except (ValueError, IndexError):
        return None
    if ref.startswith("H") and 0 <= idx < len(headlines):
        return headlines[idx].published_at
    return None


def store_digest(picked: list[ScrapedHeadline], bullets: list[dict]) -> None:
    """Upserts into Supabase: news_items deduped on fingerprint (the RSS
    article URL — polymer price rows from plastemart_news.py live in the
    same table but are written and deduped separately, on their own
    event_key-based fingerprint), market_summary_daily keyed by today's UTC
    date rather than the old news_summary singleton, so PolyInsights can
    keep a day-by-day history and a superadmin can publish/hide/edit any
    given day from Content Control. No-ops with a note if Supabase env vars
    aren't set yet.

    Only {summary_date, auto_bullets, auto_summary, generated_at} are sent
    on the upsert — Supabase's upsert only touches the columns given, so an
    admin's mode/override_bullets edit for today is never clobbered by this
    nightly write landing on the same row.

    auto_bullets is a jsonb array of {text, published_at} so each point
    carries its own real timestamp; auto_summary stays a plain
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

    today = datetime.now(timezone.utc).date().isoformat()
    client.table("market_summary_daily").upsert(
        {
            "summary_date": today,
            "auto_summary": "\n".join(b["text"] for b in bullets),
            "auto_bullets": [{"text": b["text"], "published_at": b["published_at"].isoformat()} for b in bullets],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="summary_date",
    ).execute()

    print(f"Stored {len(picked)} headline(s) + {len(bullets)} summary point(s) in Supabase (date={today}).")


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

        # Belt-and-braces cap, same reasoning as the indices above. Falls
        # back to "now" for any bullet whose source_ref didn't resolve.
        now = datetime.now(timezone.utc)
        capped_bullets = result.market_bullets[:MAX_SUMMARY_POINTS]
        bullets = [
            {
                "text": b.text.strip(),
                "published_at": _resolve_source_timestamp(b.source_ref, fresh_headlines) or now,
            }
            for b in capped_bullets
        ]

        print("Market Commentary")
        for b in bullets:
            print(f"- [{b['published_at'].isoformat()}] {b['text']}")

        store_digest(picked, bullets)
