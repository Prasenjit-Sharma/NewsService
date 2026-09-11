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
# Optional here (unlike gemini_news.py, which requires it) — extraction just
# falls back to storing items unstructured, text-only, if this is unset.
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


# ---- structured extraction + cross-company consolidation ------------------
#
# Plastemart publishes the SAME price move once per company that announced
# it (Reliance raises PP, then IOC raises PP — same amount, same date, two
# separate captions). Left as raw text, those show up as near-duplicate
# rows. This step asks Gemini to pull structured fields out of each caption,
# then groups them in plain Python (deterministic, not left to the LLM) by
# (product, direction, amount, unit, effective_date) so every company that
# announced the same move collapses into one row with one crisp, no-company
# sentence — e.g. "PP increased by Rs. 2,000/MT, effective Sep 7, 2026."
#
# Extraction is best-effort: if GEMINI_API_KEY isn't set, or the call fails,
# or a given caption isn't a clean quantified price move, that item just
# falls back to being stored as unstructured text (product = null, no card)
# exactly as before — nothing is ever silently dropped.


class ExtractedPriceEvent(BaseModel):
    raw_index: int = Field(description="0-based index into the numbered caption list this row was extracted from.")
    product: str = Field(description="The polymer/grade named, as printed, e.g. 'PP', 'HD-Raffia', 'LL 2MFI'.")
    direction: Literal["up", "down"]
    delta_amount: float = Field(description="The price-change magnitude, always positive — direction carries the sign.")
    currency: str = Field(description="Currency symbol/abbreviation as printed, e.g. 'Rs.', 'INR', '$'.")
    per_unit: str = Field(description="The unit the price is quoted per, as printed, e.g. 'MT', 'kg'.")
    effective_date: date = Field(
        description="The date the price change takes/took effect, if stated; otherwise the caption's own date."
    )
    company: str = Field(description="The company/producer named, e.g. 'Reliance Industries Limited'.")


class ExtractionResult(BaseModel):
    events: list[ExtractedPriceEvent] = Field(
        description=(
            "One row per caption that announces a specific, quantified price change. Skip captions that "
            "aren't a quantified price-change announcement (general articles, RFQs, capacity news with no "
            "price figure, etc.) — do not force a row for those."
        )
    )


def _extract_price_events(items: list[ScrapedPriceNews]) -> dict[int, ExtractedPriceEvent]:
    if not GEMINI_API_KEY or not items:
        return {}

    listing = "\n".join(f"{i}. [{it.news_date.isoformat()}] {it.details}" for i, it in enumerate(items))
    prompt = f"""
You are extracting structured price-change data from Indian polymer trade
news captions, one row per caption below that announces a specific,
quantified price move (an amount and a direction). Return raw_index
matching the caption's number, the product/grade, direction (up/down), the
numeric delta_amount (always positive — direction carries the sign),
currency and per_unit as printed (e.g. "Rs." and "MT"), the effective_date,
and the company/producer named. Skip any caption that is not a quantified
price-change announcement.

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
                "schema": ExtractionResult.model_json_schema(),
            },
        )
        result = ExtractionResult.model_validate_json(interaction.output_text)
    except Exception as exc:
        print(f"Price-event extraction failed ({exc.__class__.__name__}: {exc}); storing items unstructured.")
        return {}

    return {e.raw_index: e for e in result.events if 0 <= e.raw_index < len(items)}


def _fmt_amount(n: float) -> str:
    return f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"


def _event_key(e: ExtractedPriceEvent) -> str:
    basis = (
        f"{e.product.strip().lower()}|{e.direction}|{e.delta_amount:g}|"
        f"{e.currency.strip().lower()}|{e.per_unit.strip().lower()}|{e.effective_date.isoformat()}"
    )
    return hashlib.sha256(basis.encode()).hexdigest()


def _crisp_text(e: ExtractedPriceEvent) -> str:
    verb = "increased" if e.direction == "up" else "decreased"
    when = f"{e.effective_date.strftime('%b')} {e.effective_date.day}, {e.effective_date.year}"
    return f"{e.product} {verb} by {e.currency} {_fmt_amount(e.delta_amount)}/{e.per_unit}, effective {when}."


def _consolidate(items: list[ScrapedPriceNews], extracted: dict[int, ExtractedPriceEvent]) -> list[dict[str, Any]]:
    groups: dict[str, list[ExtractedPriceEvent]] = {}
    rows: list[dict[str, Any]] = []

    for i, item in enumerate(items):
        event = extracted.get(i)
        if event is None:
            rows.append(
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
                    "companies": None,
                    "event_key": None,
                }
            )
            continue
        groups.setdefault(_event_key(event), []).append(event)

    for key, events in groups.items():
        first = events[0]
        companies = sorted({e.company for e in events})
        rows.append(
            {
                "headline": _crisp_text(first),
                "details": _crisp_text(first),
                "category": CATEGORY,
                "url": None,
                "published_at": datetime.combine(first.effective_date, time.min, tzinfo=timezone.utc).isoformat(),
                # Reusing the event key as the dedup fingerprint means the
                # existing unique constraint on `fingerprint` does the
                # cross-company merge for free — no second unique key needed.
                "fingerprint": key,
                "product": first.product,
                "delta_amount": first.delta_amount,
                "delta_unit": f"{first.currency}/{first.per_unit}",
                "direction": first.direction,
                "effective_date": first.effective_date.isoformat(),
                "companies": companies,
                "event_key": key,
            }
        )

    return rows


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

        extracted = _extract_price_events(news)
        rows = _consolidate(news, extracted)
        merged_away = len(news) - len(rows)
        print(f"\nConsolidated into {len(rows)} row(s)" + (f" ({merged_away} duplicate(s) merged away)" if merged_away else ""))

        store_price_news(rows)
