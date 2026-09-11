# NewsService

One hourly GitHub Actions workflow (`news-digest`) running two scripts:

- **`plastemart_news.py`** — scrapes Plastemart's "what's new" price-news
  list directly (no Gemini involved) and persists it, deduped. Gated to
  actually run only every 6th invocation (UTC hour divisible by 6) inside
  the hourly workflow, rather than a separate workflow file, so both
  scrapers live in one place and stay easy to reason about together.
- **`gemini_news.py`** — fetches real, recent headlines (crude oil/energy,
  global conflicts/geopolitics, India economy) via RSS, reads the latest
  polymer price news as its highest-priority input, and asks Gemini to
  rank the most relevant subset and write a concise market-commentary
  summary for a polymer/petrochemical pricing desk. Runs every hour.

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
  -- Structured fields, populated only for polymer price-news rows that
  -- plastemart_news.py successfully extracted with Gemini. Several
  -- companies announcing the same move (same product/direction/amount/
  -- date) collapse into ONE row here — `companies` keeps the full list for
  -- audit, but PolyInsights never displays it, only the crisp
  -- product+amount+date text (`headline`/`details`).
  product text,
  direction text,          -- up | down
  delta_amount numeric,
  delta_unit text,          -- e.g. "Rs./MT"
  effective_date date,
  companies text[],
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

create table market_summary_daily (
  summary_date date primary key,
  -- What gemini_news.py generated for this date.
  auto_bullets jsonb,       -- [{text, published_at}, ...]
  auto_summary text,        -- legacy newline-joined fallback
  -- Admin override, set from PolyInsights' Content Control page — never
  -- touched by gemini_news.py's upsert (see store_digest's docstring).
  mode text not null default 'auto',  -- auto | hidden | custom
  override_bullets jsonb,
  generated_at timestamptz,
  updated_at timestamptz,
  updated_by text
);
```

Polymer price news (`plastemart_news.py`) lives in `news_items` too,
tagged `category = 'Polymer News'`, rather than its own table — `headline`
holds the crisp, deduped price-move text, `details` mirrors it, and `url`
is null since Plastemart has no per-article link. `gemini_news.py` reads it
back from there as Market Commentary's highest-priority input, and
PolyInsights reads it into the "What Moved" metric cards on the public
Global News feed alongside RSS headlines (both respect `visible`/`pinned`,
curated from Content Control).

`news_summary` (the old singleton commentary row) is superseded by
`market_summary_daily`, kept per calendar day instead of overwritten —
`gemini_news.py` no longer writes to `news_summary`.

If `news_items`/`news_summary` already exist from before this shape, run:

```sql
alter table news_summary add column if not exists bullets jsonb;

alter table news_items add column if not exists details text;
alter table news_items add column if not exists fingerprint text;
update news_items set fingerprint = url where fingerprint is null;
alter table news_items alter column fingerprint set not null;
alter table news_items add constraint news_items_fingerprint_key unique (fingerprint);
alter table news_items alter column url drop not null;
alter table news_items drop constraint if exists news_items_url_key;

-- Structured price-event fields + curation columns + market_summary_daily —
-- see backend/supabase/news_admin_control.sql in PolyInsights for the full
-- migration (it's run there since that repo owns admin-writable schema).
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
