# NewsService

Fetches real, recent headlines (crude oil/energy, global conflicts/
geopolitics, India economy) via RSS, asks Gemini to rank the most relevant
subset for a polymer/petrochemical pricing desk, and writes a market
summary. Runs hourly via GitHub Actions.

Currently prints the digest to the workflow logs. A follow-up will have it
upsert into Supabase (dedup on article URL, single-row summary) so
PolyInsights can retrieve "latest 15 + current summary" on demand instead of
re-running Gemini per request.

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=your-key-here
python gemini_news.py
```

## GitHub Actions setup

Add a repository secret named `GEMINI_API_KEY` (Settings → Secrets and
variables → Actions → New repository secret). The `news-digest` workflow
(`.github/workflows/news-digest.yml`) runs every hour, and can also be
triggered manually from the Actions tab (`workflow_dispatch`).
