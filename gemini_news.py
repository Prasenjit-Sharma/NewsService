from __future__ import annotations

import os
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

# Same Supabase project PolyInsights will eventually use for everything else
# it's migrating off Google Sheets — these two tables are just its first
# occupants. Optional for now (script still just prints if unset) so the
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
    "India economy & energy": "India economy OR India energy OR India trade OR India politics or India Sensex or India Nifty",
}


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


class RankedSummary(BaseModel):
    selected_indices: list[int] = Field(
        description=(
            f"0-based indices into the provided headline list — at most {TOP_N} — chosen for "
            "relevance to polymer market drivers (crude oil prices/supply, global conflicts/wars/"
            "geopolitical tensions affecting energy or trade, and India-specific economic/energy "
            "developments). Ordered most significant first."
        )
    )
    market_bullets: list[str] = Field(
        description=(
            f"At most {MAX_SUMMARY_POINTS} concise bullet points (one sentence each, no leading "
            "dash or bullet character — the UI adds that) synthesizing the key market impacts and "
            "geopolitical developments across the selected headlines, focused on drivers relevant "
            "to polymer/petrochemical pricing. Most significant point first."
        )
    )


def build_prompt(headlines: list[ScrapedHeadline]) -> str:
    listing = "\n".join(
        f"{i}. [{h.category}] [{h.hours_ago:.1f}h ago] {h.headline}" for i, h in enumerate(headlines)
    )
    return f"""
You are a market intelligence analyst for a polymer/petrochemical pricing desk.

Below is a real, freshly aggregated list of headlines (each published within the last
{MAX_AGE_HOURS} hours) covering crude oil/energy, global conflicts/geopolitics, and India's
economy. Do not invent new headlines — only choose from this list.

{listing}

From this list, select at most {TOP_N} headlines most relevant to polymer market drivers
(crude oil prices/supply, global conflicts/geopolitical tensions affecting energy or trade,
and India-specific economic/energy developments), ordered most significant first, and return
their indices.

Then write at most {MAX_SUMMARY_POINTS} concise bullet points (one sentence each) synthesizing
the key market impacts and geopolitical developments across the selected headlines, focused on
implications for polymer/petrochemical pricing. Order the most significant point first.
""".strip()


def summarize_headlines(headlines: list[ScrapedHeadline]) -> RankedSummary:
    # attempts=1 disables the SDK's default retry-on-429 behavior, which
    # would otherwise burn through this project's small free-tier quota
    # retrying a request that's already over the per-minute limit.
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
    )

    interaction = client.interactions.create(
        model=GEMINI_MODEL,
        input=build_prompt(headlines),
        # No tools needed — the model is summarizing/ranking headlines we
        # already fetched, not searching the web itself.
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": RankedSummary.model_json_schema(),
        },
    )

    return RankedSummary.model_validate_json(interaction.output_text)


def store_digest(picked: list[ScrapedHeadline], bullets: list[str]) -> None:
    """Upserts into Supabase: news_items deduped on url, news_summary
    always overwriting the single id=1 row. No-ops with a note if Supabase
    env vars aren't set yet.

    news_summary.summary stays a plain text column (no schema migration) —
    bullets are joined with newlines on write and split back apart by
    whatever reads them (PolyInsights' news_digest_service.py)."""
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
            }
            for h in picked
        ]
        client.table("news_items").upsert(rows, on_conflict="url", ignore_duplicates=True).execute()

    client.table("news_summary").upsert(
        {
            "id": 1,
            "summary": "\n".join(bullets),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="id",
    ).execute()

    print(f"Stored {len(picked)} headline(s) + {len(bullets)} summary point(s) in Supabase.")


if __name__ == "__main__":
    fresh_headlines = get_fresh_headlines()

    if not fresh_headlines:
        print(f"No headlines found within the last {MAX_AGE_HOURS}h.")
    else:
        result = summarize_headlines(fresh_headlines)

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

        # Belt-and-braces cap, same reasoning as the indices above.
        bullets = result.market_bullets[:MAX_SUMMARY_POINTS]

        print("Market Commentary")
        for point in bullets:
            print(f"- {point}")

        store_digest(picked, bullets)
