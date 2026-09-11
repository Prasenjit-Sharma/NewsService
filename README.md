# NewsService

One hourly GitHub Actions workflow (`news-digest`) running two scripts:

- **`plastemart_news.py`** — scrapes Plastemart's "what's new" price-news
  list and, in ONE Gemini call, hands over the WHOLE batch of scraped
  captions and asks it to assess and return the final, deduplicated list of
  distinct price moves per grade — company names go in but never come back
  out, so several companies announcing the same move (even if worded
  slightly differently) collapse into one row with crisp, company-free
  text. This is Gemini doing the consolidation judgment call directly,
  not Python grouping by an exact-match tuple afterward. Gated to actually
  run only every 6th invocation (UTC hour divisible by 6) inside the hourly
  workflow, rather than a separate workflow file, so both scrapers live in
  one place and stay easy to reason about together.
- **`gemini_news.py`** — fetches real, recent GENERAL headlines (crude
  oil/energy, global conflicts/geopolitics, India economy) via RSS and, in
  ONE Gemini call, asks it to both rank the most relevant subset (for the
  public news feed) and write ONE cohesive 200-300 word paragraph
  synthesizing the overall market situation across all of them — prose, not
  a bulleted list of per-headline one-liners. Deliberately does NOT read
  polymer price news — those are surfaced directly as structured metric
  cards (see plastemart_news.py above), not folded into this narrative.
  Runs every hour.

Both print their results and, if Supabase credentials are set, upsert into
Supabase too — same Supabase project PolyInsights is migrating its other
data into from Google Sheets. If the credentials aren't set, they just
print, so both keep working either way. PolyInsights reads everything
back on demand instead of re-scraping/re-running Gemini per request.

### Supabase tables

Run once in the Supabase project's SQL Editor:

```sql
create table news_items (
  id bigint generated always as identity primary key,
  headline text not null,
  -- RSS headlines only — polymer price news (category = "Polymer News")
  -- has no per-article URL, so this is nullable.
  url text,
  -- Full text, only populated for polymer price news; null for RSS rows.
  details text,
  category text not null,
  published_at timestamptz not null,
  fetched_at timestamptz not null default now(),
  -- The real dedup key: RSS rows use their article url; polymer rows (no
  -- url) use the event_key content hash instead — see plastemart_news.py.
  fingerprint text not null unique,
  -- Structured fields, populated only for polymer price-news rows Gemini
  -- successfully assessed (see plastemart_news.py's _consolidate_price_events
  -- — it does the cross-company merge itself, in one call over the whole
  -- scraped batch, not Python grouping afterward). Company names are read
  -- by Gemini but never returned, so there's nothing to store per row.
  product text,
  direction text,          -- up | down
  delta_amount numeric,
  delta_unit text,          -- e.g. "Rs./MT"
  effective_date date,
  companies text[],        -- unused going forward; kept for compatibility
  event_key text,
  -- Curation, set by PolyInsights' Content Control admin page — every row
  -- (scraped or hand-authored there) gets sane defaults on insert.
  visible boolean not null default true,
  pinned boolean not null default false,
  sort_order int,
  source text not null default 'scrape',  -- scrape | manual
  updated_by text,
  updated_at timestamptz
);

create table news_summary (
  summary_date date primary key,
  -- Gemini generates BOTH in one call: teaser is a single 20-30 word
  -- takeaway, drivers are 3-5 thematic points ({label, text} each) — not
  -- one long paragraph (that read as "very unreadable" in practice).
  auto_teaser text,
  auto_drivers jsonb,
  -- Admin override, set from PolyInsights' Content Control page — never
  -- touched by gemini_news.py's upsert (see store_digest's docstring).
  mode text not null default 'auto',  -- auto | hidden | custom
  override_summary text,
  generated_at timestamptz,
  updated_at timestamptz,
  updated_by text
);
```

Polymer price news (`plastemart_news.py`) lives in `news_items` too,
tagged `category = 'Polymer News'`, rather than its own table — `headline`
holds the crisp, deduped price-move text, `details` mirrors it, and `url`
is null since Plastemart has no per-article link. `gemini_news.py` does NOT
read it — polymer rows go straight to PolyInsights' "What Moved" metric
cards as structured data, never folded into the prose Market Commentary
(`news_summary`), which is built purely from general RSS headlines. Both
news_items and news_summary respect `visible`/`pinned`/`mode`, curated
from Content Control.

`news_summary` is a day-by-day history table (one row per calendar day,
kept forever), not the old singleton commentary row — that original
`news_summary` (a single id=1 row, overwritten nightly) was dropped, and
the table that replaced it — originally named `market_summary_daily` while
`news_summary` still meant the old singleton — was renamed into
`news_summary` once the old one was gone, so the name now matches what it
actually is. See PolyInsights' `news_summary_rename.sql`.

`news_items` only keeps RSS headlines (`category != 'Polymer News'`) for
3 days — `cleanup_news_items.py`, run daily by the workflow, deletes a
day's headlines only once that day already has a `news_summary` row, so
nothing is lost before ever being distilled. Polymer price rows are never
touched by this cleanup (kept forever — they're the metric-card history,
not disposable raw text). `backfill_summary.py` was a one-time script used
to backfill `news_summary` for days that predated the daily-summary
feature; it's deleted once that gap is filled (see git history if you need
it again).

If `news_items`/`news_summary` already exist from before this shape, run:

```sql
alter table news_items add column if not exists details text;
alter table news_items add column if not exists fingerprint text;
update news_items set fingerprint = url where fingerprint is null;
alter table news_items alter column fingerprint set not null;
alter table news_items add constraint news_items_fingerprint_key unique (fingerprint);
alter table news_items alter column url drop not null;
alter table news_items drop constraint if exists news_items_url_key;

-- Structured price-event fields + curation columns + the news_summary
-- rename — see backend/supabase/news_admin_control.sql and
-- news_summary_rename.sql in PolyInsights for the full migrations (run
-- there since that repo owns admin-writable schema).
```

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (already gitignored, never committed) in the repo root:

```
GEMINI_API_KEY=your-key-here
GEMINI_MODEL=gemini-3.5-flash
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key-here
```

`GEMINI_MODEL` is optional — omit it to use the script's default.
`SUPABASE_URL`/`SUPABASE_SERVICE_KEY` are optional too — omit them and the
script just prints instead of persisting. Use the **`service_role`** key
(Settings → API), not the `anon` key — this runs server-side/in CI, not in
a browser. Then:

```bash
python plastemart_news.py
python gemini_news.py
```

## GitHub Actions setup

- **Secrets** `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` —
  Settings → Secrets and variables → Actions → **Secrets** tab → New
  repository secret.
- **Variable** `GEMINI_MODEL` (optional) — same page, **Variables** tab →
  New repository variable. Lets you switch the Gemini model for scheduled
  runs without touching code. Leave unset to use the script's default.

The `news-digest` workflow (`.github/workflows/news-digest.yml`) runs
every hour — `plastemart_news.py` only actually executes on runs where the
UTC hour is divisible by 6, `gemini_news.py` runs every time. Can also be
triggered manually from the Actions tab (`workflow_dispatch`).
