from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import Client, create_client

# Loads .env for local runs; a no-op in GitHub Actions, where these are
# injected directly as environment variables (see the workflow file).
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

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


def store_price_news(items: list[ScrapedPriceNews]) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("(SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping persistence)")
        return
    if not items:
        return

    client: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    rows = [
        {
            "headline": i.title,
            "details": i.details,
            "category": CATEGORY,
            "url": None,
            # Plastemart only gives us a date, not a time — midnight UTC on
            # that date is the best available published_at.
            "published_at": datetime.combine(i.news_date, time.min, tzinfo=timezone.utc).isoformat(),
            "fingerprint": i.fingerprint,
        }
        for i in items
    ]
    client.table("news_items").upsert(rows, on_conflict="fingerprint", ignore_duplicates=True).execute()
    print(f"Stored {len(rows)} polymer price-news item(s) in Supabase.")


if __name__ == "__main__":
    news = get_price_news()

    if not news:
        print("No polymer price-news items found.")
    else:
        print(f"Found {len(news)} polymer price-news item(s)\n")
        for n in news:
            print(f"[{n.news_date.isoformat()}] {n.title}")

        store_price_news(news)
