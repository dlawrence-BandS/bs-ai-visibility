# bs-ai-visibility

Weekly tracker of how ChatGPT, Claude, and Gemini answer questions about Barker & Stonehouse.
Companion dashboard published at `https://dlawrence-bands.github.io/bs-ai-visibility/`.

## What each file does

| File | Role |
|---|---|
| `prompts.yaml` | The prompt corpus (~100 prompts, 5 categories). Edit freely — the runner reads this on every run. |
| `run_ai_visibility.py` | The weekly runner. Queries the three LLMs for every prompt, scores each response via Claude, writes to BigQuery. |
| `requirements.txt` | Python deps. |
| `schema.sql` | One-off BigQuery DDL — creates the dataset, tables, and view. |
| `ai-visibility.yml` | GitHub Actions cron. Runs the script every Monday 06:00 UTC. Place at `.github/workflows/ai-visibility.yml`. |
| `index.html` | Dashboard. Same OAuth-in-browser pattern as bs-search and bs-llm. |

## One-time setup

**1. Create BigQuery dataset**

Open BigQuery in the Google Cloud console, paste the contents of `schema.sql`, run. Location `europe-west2`. This creates the `ai_visibility` dataset with `runs`, `analysis` tables and the `v_latest` view.

**2. Create a service account for the runner**

The GitHub Action needs a BigQuery service account key (this is different from the dashboard, which uses your personal Google login).

- Cloud console → IAM & Admin → Service Accounts → Create
- Name it `ai-visibility-runner`
- Grant it two roles on the project: **BigQuery Data Editor**, **BigQuery Job User**
- Keys → Add key → JSON → download the JSON file

**3. Create the GitHub repo**

- `github.com/dlawrence-BandS/bs-ai-visibility` — public (so Pages can serve the dashboard) or private (fine if you don't need public dashboard access)
- Upload every file in this bundle. Rename `ai-visibility.yml` to `.github/workflows/ai-visibility.yml`

**4. Add the secrets**

Repo → Settings → Secrets and variables → Actions → New repository secret:

- `GCP_SA_KEY` — paste the entire contents of the service account JSON file (yes, the whole JSON blob as a single secret value)
- `OPENAI_API_KEY` — from platform.openai.com
- `ANTHROPIC_API_KEY` — from console.anthropic.com (you have this already from bs-intelligence)
- `GEMINI_API_KEY` — from aistudio.google.com/apikey

**5. Enable Pages for the dashboard**

Repo → Settings → Pages → Deploy from branch → main → root → Save. Dashboard appears at `https://dlawrence-bands.github.io/bs-ai-visibility/` after ~1 minute.

**6. Run once manually to confirm it works**

Actions tab → "AI Visibility weekly run" → Run workflow. Watch the log — should complete in 5–15 minutes depending on API latency. On success, ~300 rows land in each table (100 prompts × 3 models).

## Editing the prompt list

Prompts live in `prompts.yaml`. Add, remove, or rewrite freely — the runner picks up the current file on each run. Keep the `id` values stable so historical comparison works (if you rename `disc_005` to something else, its history won't join to future runs).

## Cost

Roughly £1–2 per weekly run once all three models are wired. Trivial. Set an API budget alert if you're worried.

## Dashboard behaviour

Same OAuth flow as bs-search/bs-llm — click Run, sign in with your work Google account, queries run in `europe-west2`. Four tabs:

- **Overview**: mention rate, avg rank, positive sentiment, prompts covered — trend chart, category breakdown, sentiment mix
- **Prompts**: full corpus with each model's response summary. Click a row → side-by-side drawer showing all three responses plus flagged claims
- **Competitors**: share-of-voice leaderboard across the corpus
- **Claims log**: every notable factual claim the AIs made about B&S, flagged claims first — this is the "what needs correcting on the website" feed

Empty tables will show until the first weekly run completes.
