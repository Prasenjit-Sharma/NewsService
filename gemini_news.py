"""
Fetches real, recent headlines (crude oil/energy, global conflicts/
geopolitics, India economy) via RSS, then asks Gemini to rank the most
relevant subset for a polymer/petrochemical pricing desk and write a market
summary. Gemini is only trusted to (a) pick from what was actually fetched,
by index, and (b) write the summary — never to reproduce headline/url
itself, so there's no risk of it subtly mangling a link.

Source: feedparser against Google News' RSS search endpoint, once per topic.
This aggregates across many outlets instead of scraping one site's HTML.

Requires: pip install -r requirements.txt

Run hourly via .github/workflows/news-digest.yml. Currently just prints the
digest; a follow-up will have it upsert into Supabase instead.
"""
from __future__ import annotations

import os
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = "gemini-3.5-flash"

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
    "India economy & energy": "India economy OR India energy OR India trade",
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
    market_summary: str = Field(
        description=(
            "A concise synthesis (no more than 200 words) of the key market impacts and "
            "geopolitical developments across the selected headlines, focused on drivers "
            "relevant to polymer/petrochemical pricing."
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

Then write one market summary of no more than 200 words synthesizing the key market impacts
and geopolitical developments across the selected headlines, focused on implications for
polymer/petrochemical pricing.
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

        print("Market Summary")
        print(result.market_summary)
