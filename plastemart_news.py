from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Literal, Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field
from supabase import Client, create_client

# Loads .env for local runs; a no-op in GitHub Actions, where these are
# injected directly as environment variables (see the workflow file).
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
# Optional here (unlike gemini_news.py, which requires it) — consolidation
# just falls back to storing items unstructured, text-only, if this is unset.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

# Stored in the same `news_items` table gemini_news.py writes RSS headlines
# into (category = "Polymer News"), rather than a separate table — it's
# read from there too, as the highest-priority Market Commentary input.
CATEGORY = "Polymer News"

URL = "https://www.plastemart.com/whats-new-plastics-industry"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Plastemart renders dates like "13-Aug-26" with a 2-digit year.
DATE_FORMAT = "%d-%b-%y"


@dataclass
class ScrapedPriceNews:
    news_date: date
    title: str
    details: str
    fingerprint: str


def _parse_news_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), DATE_FORMAT).date()
    except ValueError:
        return None


def get_price_news() -> list[ScrapedPriceNews]:
    """Scrapes Plastemart's "what's new" price-news list. Ported from
    PolyInsights' backend/app/services/news_service.py, which used to do
    this live on every home-page request — now scraped here on a schedule
    and persisted instead."""
    try:
        response = requests.get(URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    product_container = soup.find("div", id="products")
    if not product_container:
        return []

    seen_fingerprints: set[str] = set()
    items: list[ScrapedPriceNews] = []

    for item in product_container.find_all("div", class_="item"):
        caption = item.find("div", class_="caption")
        if not caption:
            continue

        date_div = caption.find("div", class_="news-date")
        date_val = date_div.get_text(strip=True) if date_div else None
        if date_div:
            date_div.extract()

        news_date = _parse_news_date(date_val) if date_val else None
        if news_date is None:
            continue

        details = caption.get_text(separator=" ", strip=True)
        if not details:
            continue
        title = details.split(".")[0] if "." in details else details[:60] + "..."

        fingerprint = hashlib.sha256(f"{news_date.isoformat()}|{details}".encode()).hexdigest()
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)

        items.append(
            ScrapedPriceNews(news_date=news_date, title=title, details=details, fingerprint=fingerprint)
        )

    return items


# ---- Gemini assessment: one call, whole batch, grade-level output ---------
#
# Plastemart publishes the SAME price move once per company that announced
# it (Reliance raises PP, then IOC raises PP — same amount, same date, two
# separate captions). Rather than extract structured fields per caption and
# group them in Python by an exact-match tuple (brittle — "PP Raffia" vs
# "PP-Raffia" wouldn't merge), this hands Gemini the WHOLE batch of scraped
# captions in one call and asks it to assess and return the final,
# deduplicated list of distinct price moves per grade, irrespective of which
# company announced it — company names are read but never part of the
# output; only product/grade, direction, amount, unit and effective date
# come back.


class ConsolidatedPriceEvent(BaseModel):
    product: str = Field(description="The polymer/grade this move applies to, normalized, e.g. 'PP', 'HD-Raffia'.")
    direction: Literal["up", "down"]
    delta_amount: float = Field(description="The price-change magnitude, always positive — direction carries the sign.")
    currency: str = Field(description="Currency symbol/abbreviation as printed, e.g. 'Rs.', 'INR', '$'.")
    per_unit: str = Field(description="The unit the price is quoted per, as printed, e.g. 'MT', 'kg'.")
    effective_date: date = Field(
        description="The date the price change takes/took effect, if stated; otherwise the caption's own date."
    )


class ConsolidationResult(BaseModel):
    events: list[ConsolidatedPriceEvent] = Field(
        description=(
            "The final, deduplicated list of distinct price-change events found across ALL captions "
            "below — one row per (product/grade, direction, amount, unit, effective date) combination. "
            "Several companies often announce the exact same move separately; merge every company's "
            "announcement of the same move into a single row here, never emit two rows for the same "
            "move. Normalize product/grade naming across captions that clearly refer to the same grade "
            "(minor spelling/spacing differences). Skip captions that are not a quantified price-change "
            "announcement (general articles, RFQs, capacity news with no price figure, etc.)."
        )
    )


def _consolidate_price_events(items: list[ScrapedPriceNews]) -> list[ConsolidatedPriceEvent]:
    if not GEMINI_API_KEY or not items:
        return []

    listing = "\n".join(f"{i}. [{it.news_date.isoformat()}] {it.details}" for i, it in enumerate(items))
    prompt = f"""
You are assessing Indian polymer trade price-change captions, scraped just now from a trade news
site. Several different companies often announce the SAME price move independently — same product/
grade, same direction, same amount, same effective date, just a different company name. Read ALL
{len(items)} captions below and return the final, deduplicated list of DISTINCT price-change events:
one row per (product/grade, direction, amount, unit, effective date) combination, merging every
company's separate announcement of the same move into a single row. Only emit separate rows for
genuinely different moves (different product/grade, different amount, different direction, or a
different date). Company names are irrelevant to your output — do not include them.

{listing}
""".strip()

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        interaction = client.interactions.create(
            model=GEMINI_MODEL,
            input=prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": ConsolidationResult.model_json_schema(),
            },
        )
        return ConsolidationResult.model_validate_json(interaction.output_text).events
    except Exception as exc:
        print(f"Price-event consolidation failed ({exc.__class__.__name__}: {exc}); storing items unstructured.")
        return []


def _fmt_amount(n: float) -> str:
    return f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"


def _event_key(e: ConsolidatedPriceEvent) -> str:
    basis = (
        f"{e.product.strip().lower()}|{e.direction}|{e.delta_amount:g}|"
        f"{e.currency.strip().lower()}|{e.per_unit.strip().lower()}|{e.effective_date.isoformat()}"
    )
    return hashlib.sha256(basis.encode()).hexdigest()


def _crisp_text(e: ConsolidatedPriceEvent) -> str:
    verb = "increased" if e.direction == "up" else "decreased"
    when = f"{e.effective_date.strftime('%b')} {e.effective_date.day}, {e.effective_date.year}"
    return f"{e.product} {verb} by {e.currency} {_fmt_amount(e.delta_amount)}/{e.per_unit}, effective {when}."


def _build_rows(items: list[ScrapedPriceNews], events: list[ConsolidatedPriceEvent]) -> list[dict[str, Any]]:
    if events:
        rows = []
        for e in events:
            key = _event_key(e)
            rows.append(
                {
                    "headline": _crisp_text(e),
                    "details": _crisp_text(e),
                    "category": CATEGORY,
                    "url": None,
                    "published_at": datetime.combine(e.effective_date, time.min, tzinfo=timezone.utc).isoformat(),
                    # Reusing the event key as the dedup fingerprint means the
                    # existing unique constraint on `fingerprint` does the
                    # cross-company merge for free — no second unique key needed.
                    "fingerprint": key,
                    "product": e.product,
                    "delta_amount": e.delta_amount,
                    "delta_unit": f"{e.currency}/{e.per_unit}",
                    "direction": e.direction,
                    "effective_date": e.effective_date.isoformat(),
                    "event_key": key,
                }
            )
        return rows

    # Gemini unavailable or the call failed for this whole run — fall back
    # to storing every scraped caption unstructured (no card, no dedup
    # beyond the raw content hash) rather than lose the run entirely.
    return [
        {
            "headline": item.title,
            "details": item.details,
            "category": CATEGORY,
            "url": None,
            "published_at": datetime.combine(item.news_date, time.min, tzinfo=timezone.utc).isoformat(),
            "fingerprint": item.fingerprint,
            "product": None,
            "delta_amount": None,
            "delta_unit": None,
            "direction": None,
            "effective_date": None,
            "event_key": None,
        }
        for item in items
    ]


def store_price_news(rows: list[dict[str, Any]]) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("(SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping persistence)")
        return
    if not rows:
        return

    client: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    client.table("news_items").upsert(rows, on_conflict="fingerprint", ignore_duplicates=True).execute()
    print(f"Stored {len(rows)} polymer price-news item(s) in Supabase.")


if __name__ == "__main__":
    news = get_price_news()

    if not news:
        print("No polymer price-news items found.")
    else:
        print(f"Found {len(news)} scraped caption(s)\n")
        for n in news:
            print(f"[{n.news_date.isoformat()}] {n.title}")

        events = _consolidate_price_events(news)
        rows = _build_rows(news, events)
        print(f"\nGemini assessed {len(events)} distinct price event(s)" if events else "\nStoring unstructured (no assessment available)")

        store_price_news(rows)
