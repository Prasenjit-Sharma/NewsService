# NewsService

Fetches real, recent headlines (crude oil/energy, global conflicts/
geopolitics, India economy) via RSS, asks Gemini to rank the most relevant
subset for a polymer/petrochemical pricing desk, and writes a market
summary. Runs hourly via GitHub Actions.

Prints the digest and, if Supabase credentials are set, upserts it there too
(`news_items` deduped on article URL, `news_summary` a single overwritten
row) — same Supabase project PolyInsights is migrating its other data into
from Google Sheets. If the credentials aren't set, it just prints, so the
hourly job keeps working either way. PolyInsights can then retrieve
"latest 15 + current summary" on demand instead of re-running Gemini per
request.

### Supabase tables

Run once in the Supabase project's SQL Editor:

```sql
create table news_items (
  id bigint generated always as identity primary key,
  headline text not null,
  url text not null unique,
  category text not null,
  published_at timestamptz not null,
  fetched_at timestamptz not null default now()
);

create table news_summary (
  id smallint primary key default 1 check (id = 1),
  summary text not null,
  generated_at timestamptz not null default now()
);
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
python gemini_news.py
```

## GitHub Actions setup

- **Secrets** `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` —
  Settings → Secrets and variables → Actions → **Secrets** tab → New
  repository secret.
- **Variable** `GEMINI_MODEL` (optional) — same page, **Variables** tab →
  New repository variable. Lets you switch the Gemini model for scheduled
  runs without touching code. Leave unset to use the script's default.

The `news-digest` workflow (`.github/workflows/news-digest.yml`) runs every
hour, and can also be triggered manually from the Actions tab
(`workflow_dispatch`).
