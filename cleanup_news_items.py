from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
from supabase import Client, create_client

# Loads .env for local runs; a no-op in GitHub Actions, where these are
# injected directly as environment variables (see the workflow file).
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# Same category tag plastemart_news.py writes — deliberately excluded from
# this cleanup. Plain RSS headlines are disposable once summarized, but
# Polymer News rows are the structured price-event history behind the
# "What Moved" metric cards, which PolyInsights' Knowledge page lets a
# buyer browse by date — those are kept indefinitely, not just for a
# retention window.
POLYMER_NEWS_CATEGORY = "Polymer News"
RETENTION_DAYS = 3


@lru_cache
def _get_client() -> Optional[Client]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def cleanup_old_headlines(days: int = RETENTION_DAYS) -> int:
    """Deletes RSS headline rows (category != Polymer News) older than
    `days` — but ONLY for a calendar day that already has a news_summary
    row. A day's raw headlines are the source material a summary was (or
    could still be) distilled from; deleting them before that's happened
    would mean that day's news is gone for good with nothing to show for
    it. Also skips anything an admin has pinned from Content Control — a
    pin is a deliberate override, and deleting the row out from under it
    would silently break that choice."""
    client = _get_client()
    if client is None:
        print("(SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping cleanup)")
        return 0

    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    summarized = (
        client.table("news_summary")
        .select("summary_date")
        .lt("summary_date", cutoff_date.isoformat())
        .execute()
        .data
        or []
    )
    summarized_dates = sorted({date.fromisoformat(r["summary_date"]) for r in summarized})

    total_deleted = 0
    for d in summarized_dates:
        start = datetime.combine(d, time.min, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        response = (
            client.table("news_items")
            .delete()
            .neq("category", POLYMER_NEWS_CATEGORY)
            .eq("pinned", False)
            .gte("published_at", start.isoformat())
            .lt("published_at", end.isoformat())
            .execute()
        )
        deleted = len(response.data or [])
        total_deleted += deleted
        if deleted:
            print(f"  {d}: deleted {deleted} row(s) (already summarized).")

    print(
        f"Deleted {total_deleted} news_items row(s) total, across {len(summarized_dates)} "
        f"summarized day(s) older than {days} day(s)."
    )
    return total_deleted


if __name__ == "__main__":
    cleanup_old_headlines()
