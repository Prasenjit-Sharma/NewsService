"""
ONE-TIME script — not part of the regular pipeline. Regenerates a
news_summary row for every past calendar day that already has news_items
rows but no summary yet (days before the daily-summary feature existed, or
a run that failed silently). Delete this file (and this note) once it's
been run successfully and every gap is filled — see the PR/commit that
introduced it for context.

Uses the same RankedSummary shape as gemini_news.py (teaser + drivers),
just reading that day's already-stored news_items instead of live RSS,
and a prompt that frames it as a retrospective of a specific past date
rather than "the last 24h".
"""
from __future__ import annotations

import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from gemini_news import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    MarketDriver,
    _get_client,
    _retry_delay_seconds,
)

POLYMER_NEWS_CATEGORY = "Polymer News"
_MAX_ATTEMPTS = 3
_FALLBACK_BACKOFF = (5, 20)
# Gentle pacing across a whole run of many calls in a row — separate from
# the per-call retry backoff above, and worth keeping even though this
# script only runs once, given the free-tier per-minute quota gemini_news.py's
# own retry logic already works around for the regular hourly job.
_BETWEEN_DAYS_SECONDS = 3


class BackfillSummary(BaseModel):
    teaser: str = Field(
        description="ONE punchy sentence, 20-30 words, the single most important takeaway from that day's headlines below."
    )
    drivers: list[MarketDriver] = Field(
        description=(
            "3 to 5 distinct thematic points (each a short 2-4 word label plus a 1-2 sentence "
            "takeaway) covering that day's market situation and its bearing on polymer/"
            "petrochemical pricing — synthesized themes, not one bullet per headline."
        )
    )


def _missing_dates(client) -> list[date]:
    items = (
        client.table("news_items")
        .select("published_at")
        .neq("category", POLYMER_NEWS_CATEGORY)
        .order("published_at")
        .execute()
        .data
        or []
    )
    all_dates = sorted({datetime.fromisoformat(r["published_at"]).date() for r in items})

    existing = {
        date.fromisoformat(r["summary_date"])
        for r in (client.table("news_summary").select("summary_date").execute().data or [])
    }

    today = datetime.now(timezone.utc).date()
    return [d for d in all_dates if d not in existing and d < today]


def _day_headlines(client, target: date) -> list[dict]:
    start = datetime.combine(target, dtime.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    response = (
        client.table("news_items")
        .select("headline,category")
        .neq("category", POLYMER_NEWS_CATEGORY)
        .gte("published_at", start.isoformat())
        .lt("published_at", end.isoformat())
        .order("published_at", desc=True)
        .execute()
    )
    return response.data or []


def _build_prompt(target: date, headlines: list[dict]) -> str:
    listing = "\n".join(f"H{i}. [{h['category']}] {h['headline']}" for i, h in enumerate(headlines))
    return f"""
You are a market intelligence analyst for a polymer/petrochemical pricing desk, writing a
RETROSPECTIVE summary of {target.isoformat()} — a past date, not today. Below are that day's
general market headlines (crude oil/energy, global conflicts/geopolitics, India's economy).

{listing}

Reading across ALL the headlines above, write TWO things:
1. teaser — ONE punchy sentence, 20-30 words, the single most important takeaway from that day.
2. drivers — 3 to 5 distinct thematic points (each a short 2-4 word label plus a 1-2 sentence
   takeaway) covering that day's market situation and its bearing on polymer/petrochemical pricing —
   synthesized themes, not one bullet per headline and not a single merged paragraph.
""".strip()


def _summarize_day(genai_client, target: date, headlines: list[dict]) -> BackfillSummary:
    for attempt in range(_MAX_ATTEMPTS):
        try:
            interaction = genai_client.interactions.create(
                model=GEMINI_MODEL,
                input=_build_prompt(target, headlines),
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": BackfillSummary.model_json_schema(),
                },
            )
            return BackfillSummary.model_validate_json(interaction.output_text)
        except Exception as exc:
            if attempt == _MAX_ATTEMPTS - 1:
                raise
            fallback = _FALLBACK_BACKOFF[min(attempt, len(_FALLBACK_BACKOFF) - 1)]
            delay = _retry_delay_seconds(exc, fallback)
            print(f"    Gemini call failed ({exc.__class__.__name__}: {exc}); retrying in {delay:.0f}s...")
            time.sleep(delay)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    client = _get_client()
    if client is None:
        print("SUPABASE_URL/SUPABASE_SERVICE_KEY not set — nothing to do.")
        raise SystemExit(0)

    targets = _missing_dates(client)
    if not targets:
        print("No missing dates — every day with news_items already has a news_summary row.")
        raise SystemExit(0)

    print(f"Backfilling {len(targets)} missing date(s): {targets[0]} .. {targets[-1]}")

    genai_client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
    )

    for target in targets:
        headlines = _day_headlines(client, target)
        if not headlines:
            print(f"{target}: no headlines stored, skipping.")
            continue

        print(f"{target}: summarizing {len(headlines)} headline(s)...")
        result = _summarize_day(genai_client, target, headlines)
        client.table("news_summary").upsert(
            {
                "summary_date": target.isoformat(),
                "auto_teaser": result.teaser.strip(),
                "auto_drivers": [{"label": d.label.strip(), "text": d.text.strip()} for d in result.drivers],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="summary_date",
        ).execute()
        print(f"{target}: stored.")
        time.sleep(_BETWEEN_DAYS_SECONDS)

    print("Backfill complete.")
