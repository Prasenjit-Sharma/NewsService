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
  -- url) use a content hash instead — see plastemart_news.py.
  fingerprint text not null unique
);

create table news_summary (
  id smallint primary key default 1 check (id = 1),
  summary text not null,
  -- jsonb array of {text, published_at} — lets each bullet carry its own
  -- real timestamp instead of one shared generated_at for the whole list.
  -- `summary` (newline-joined bullet text) is kept alongside as a fallback
  -- for any reader that hasn't migrated to `bullets` yet.
  bullets jsonb,
  generated_at timestamptz not null default now()
);
```

Polymer price news (`plastemart_news.py`) lives in `news_items` too,
tagged `category = 'Polymer News'`, rather than its own table — `headline`
holds the short title, `details` the full price-change text, and `url` is
null since Plastemart has no per-article link. `gemini_news.py` reads it
back from there as Market Commentary's highest-priority input, and
PolyInsights excludes that category when reading the public Global News
feed (it has no clickable url).

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
