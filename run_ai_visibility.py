#!/usr/bin/env python3
"""
B&S AI Visibility runner
Queries ChatGPT, Claude, Gemini with every prompt in prompts.yaml,
uses Claude to score each response, writes both raw + analysed rows to BigQuery.

Runs weekly via GitHub Actions. Local runs also work — see README.
"""
import os
import sys
import json
import time
import yaml
import traceback
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI
from anthropic import Anthropic
from google import genai
from google.cloud import bigquery

# ─── CONFIG ────────────────────────────────────────────────────────────
PROJECT   = "commanding-air-450109-p0"
DATASET   = "ai_visibility"
LOCATION  = "europe-west2"
PROMPTS_FILE = Path(__file__).parent / "prompts.yaml"

# Model versions (bump when new models ship — history preserved in run rows)
OPENAI_MODEL   = "gpt-4o"                # switch to gpt-5 once stable
CLAUDE_MODEL   = "claude-opus-4-7"
GEMINI_MODEL   = "gemini-2.5-pro"
SCORER_MODEL   = "claude-opus-4-7"       # Claude scores all responses

BRAND_NAMES   = ["barker and stonehouse", "barker & stonehouse", "barker&stonehouse", "b&s"]
BRAND_DOMAIN  = "barkerandstonehouse.co.uk"

# Common furniture competitors — surface in the competitors array even if AI wraps them oddly
KNOWN_COMPETITORS = [
    "dfs", "john lewis", "sofology", "sofa workshop", "heals", "heal's",
    "furniture village", "loaf", "made.com", "oak furnitureland",
    "the cotswold company", "cotswold company", "habitat", "west elm",
    "next home", "marks and spencer", "m&s", "ikea", "west & willow",
    "restoration hardware", "swoon"
]

# ─── PROMPT / TIMING ───────────────────────────────────────────────────
SYSTEM_HINT = (
    "You are a helpful assistant answering questions from UK customers. "
    "Be specific, mention actual retailers and brand names where relevant, "
    "and cite sources if you can. Do not hedge unnecessarily."
)

MAX_WORKERS_PER_MODEL = 4   # parallelism per model (rate-limit friendly)
RETRY_ATTEMPTS = 2
TIMEOUT_SECONDS = 60

# ─── CLIENTS ───────────────────────────────────────────────────────────
def _env(key):
    v = os.environ.get(key)
    if not v:
        print(f"[FATAL] Missing env var {key}", file=sys.stderr)
        sys.exit(2)
    return v

openai_client   = OpenAI(api_key=_env("OPENAI_API_KEY"))
anthropic_client = Anthropic(api_key=_env("ANTHROPIC_API_KEY"))
gemini_client   = genai.Client(api_key=_env("GEMINI_API_KEY"))
bq              = bigquery.Client(project=PROJECT, location=LOCATION)

# ─── QUERY FUNCTIONS ───────────────────────────────────────────────────
def query_openai(prompt):
    t0 = time.time()
    r = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role":"system","content":SYSTEM_HINT},{"role":"user","content":prompt}],
        timeout=TIMEOUT_SECONDS,
    )
    ms = int((time.time()-t0)*1000)
    return {
        "text": r.choices[0].message.content or "",
        "tokens": r.usage.total_tokens if r.usage else None,
        "latency_ms": ms,
        "model_version": OPENAI_MODEL,
        # rough cost: gpt-4o is ~$2.50/M input, $10/M output
        "cost_usd": None,
    }

def query_claude(prompt):
    t0 = time.time()
    r = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=SYSTEM_HINT,
        messages=[{"role":"user","content":prompt}],
        timeout=TIMEOUT_SECONDS,
    )
    ms = int((time.time()-t0)*1000)
    text = "".join(b.text for b in r.content if hasattr(b, "text"))
    return {
        "text": text,
        "tokens": (r.usage.input_tokens + r.usage.output_tokens) if r.usage else None,
        "latency_ms": ms,
        "model_version": CLAUDE_MODEL,
        "cost_usd": None,
    }

def query_gemini(prompt):
    t0 = time.time()
    r = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=f"{SYSTEM_HINT}\n\n{prompt}",
    )
    ms = int((time.time()-t0)*1000)
    return {
        "text": r.text or "",
        "tokens": r.usage_metadata.total_token_count if r.usage_metadata else None,
        "latency_ms": ms,
        "model_version": GEMINI_MODEL,
        "cost_usd": None,
    }

MODEL_RUNNERS = {
    "chatgpt": query_openai,
    "claude":  query_claude,
    "gemini":  query_gemini,
}

# ─── SCORING ───────────────────────────────────────────────────────────
SCORING_SYSTEM = """You are analysing how AI assistants respond to UK customer questions about furniture retailers.

Given a prompt and the AI's response, extract structured data about whether "Barker and Stonehouse" (also written as B&S, Barker & Stonehouse) is mentioned, and if so how prominently and favourably.

Return ONLY valid JSON, no prose, no code fences. Schema:
{
  "bs_mentioned": boolean,
  "bs_rank": integer or null,        // if the response contains a ranked list of retailers, B&S's position (1 = first). null if not in a ranked list or not mentioned.
  "bs_sentiment": "positive" | "neutral" | "negative" | "mixed" | "n/a",
  "bs_sentiment_reason": "one short sentence explaining the sentiment score, or empty if n/a",
  "bs_citations": ["barkerandstonehouse.co.uk URLs cited by the AI, or empty array"],
  "all_citations": ["every URL cited by the AI, or empty array"],
  "competitors": [{"name": "competitor brand", "rank": integer or null}],
  "key_claims": ["notable factual claims the AI made about B&S (delivery, quality, price, provenance, etc.) — one string per claim"],
  "claim_flags": ["any claim from key_claims that looks factually wrong or suspect — copy the claim text verbatim"]
}

Rules:
- "mentioned" means named directly. A generic reference to "premium UK furniture retailers" without naming B&S is not a mention.
- If B&S appears in a bullet list but not ranked (e.g. an unordered list), set bs_rank to null and record the position in bs_sentiment_reason if useful.
- For competitors, extract every named retailer/brand that competes with B&S (DFS, John Lewis, Heal's, Sofology, Furniture Village, Loaf, Made.com, Oak Furnitureland, etc.). Include them whether or not B&S was mentioned.
- Sentiment applies only to how the AI portrayed B&S. If not mentioned, use "n/a".
- Be strict on claim_flags — only flag things clearly wrong (e.g. "delivery only in Yorkshire" would be wrong; "premium prices" would not be).
"""

def score_response(prompt, response_text):
    if not response_text:
        return None, "empty response"
    try:
        r = anthropic_client.messages.create(
            model=SCORER_MODEL,
            max_tokens=1200,
            system=SCORING_SYSTEM,
            messages=[{"role":"user","content":f"PROMPT: {prompt}\n\nRESPONSE:\n{response_text}"}],
            timeout=TIMEOUT_SECONDS,
        )
        raw = "".join(b.text for b in r.content if hasattr(b, "text")).strip()
        # Strip markdown fences if the model added them despite instructions
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"): raw = raw[4:].strip()
        data = json.loads(raw)
        return data, None
    except json.JSONDecodeError as e:
        return None, f"scoring returned non-JSON: {e}"
    except Exception as e:
        return None, f"scoring error: {e}"

# ─── ORCHESTRATION ─────────────────────────────────────────────────────
def run_prompt_on_model(prompt_row, model_name):
    """Query one model with one prompt. Retries on transient errors."""
    runner = MODEL_RUNNERS[model_name]
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return runner(prompt_row["text"]), None
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    return None, last_err

def process_prompt(prompt_row, run_ts, run_date):
    """Fan out one prompt across all 3 models, score each response, return rows for both tables."""
    runs_rows, analysis_rows = [], []
    for model_name in MODEL_RUNNERS:
        result, err = run_prompt_on_model(prompt_row, model_name)
        row = {
            "run_date": run_date.isoformat(),
            "run_timestamp": run_ts.isoformat(),
            "model": model_name,
            "model_version": result["model_version"] if result else None,
            "prompt_id": prompt_row["id"],
            "prompt_text": prompt_row["text"],
            "category": prompt_row["category"],
            "response_text": result["text"] if result else None,
            "response_tokens": result["tokens"] if result else None,
            "latency_ms": result["latency_ms"] if result else None,
            "cost_usd": None,
            "error": err,
        }
        runs_rows.append(row)

        # Score if we got a response
        if result and result["text"]:
            scored, score_err = score_response(prompt_row["text"], result["text"])
            if scored:
                analysis_rows.append({
                    "run_date": run_date.isoformat(),
                    "run_timestamp": run_ts.isoformat(),
                    "model": model_name,
                    "prompt_id": prompt_row["id"],
                    "category": prompt_row["category"],
                    "bs_mentioned": bool(scored.get("bs_mentioned")),
                    "bs_rank": scored.get("bs_rank"),
                    "bs_sentiment": scored.get("bs_sentiment") or "n/a",
                    "bs_sentiment_reason": scored.get("bs_sentiment_reason") or "",
                    "bs_citations": scored.get("bs_citations") or [],
                    "all_citations": scored.get("all_citations") or [],
                    "competitors": [
                        {"name": c.get("name",""), "rank": c.get("rank")}
                        for c in (scored.get("competitors") or [])
                        if c.get("name")
                    ],
                    "key_claims": scored.get("key_claims") or [],
                    "claim_flags": scored.get("claim_flags") or [],
                    "scoring_error": None,
                })
            else:
                analysis_rows.append({
                    "run_date": run_date.isoformat(),
                    "run_timestamp": run_ts.isoformat(),
                    "model": model_name,
                    "prompt_id": prompt_row["id"],
                    "category": prompt_row["category"],
                    "scoring_error": score_err,
                })
        print(f"  [{model_name}] {prompt_row['id']} {'OK' if result else 'FAIL: '+str(err)}")
    return runs_rows, analysis_rows

def write_to_bq(table, rows):
    if not rows: return
    full = f"{PROJECT}.{DATASET}.{table}"
    errors = bq.insert_rows_json(full, rows)
    if errors:
        print(f"[WARN] {len(errors)} insert errors on {table}:", errors[:3], file=sys.stderr)
    else:
        print(f"[OK] Inserted {len(rows)} rows into {table}")

# ─── ENTRY POINT ───────────────────────────────────────────────────────
def main():
    print(f"[START] {datetime.now(timezone.utc).isoformat()}")
    prompts = yaml.safe_load(PROMPTS_FILE.read_text())
    print(f"Loaded {len(prompts)} prompts")

    run_ts = datetime.now(timezone.utc)
    run_date = run_ts.date()

    all_runs, all_analysis = [], []

    # Process prompts in parallel across the outer level — each prompt already fans out to 3 models sequentially
    with ThreadPoolExecutor(max_workers=MAX_WORKERS_PER_MODEL) as pool:
        futures = {pool.submit(process_prompt, p, run_ts, run_date): p for p in prompts}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                runs, analysis = fut.result()
                all_runs.extend(runs)
                all_analysis.extend(analysis)
            except Exception as e:
                print(f"[ERROR] {p['id']}: {e}\n{traceback.format_exc()}", file=sys.stderr)

    # Batch insert — BigQuery is happier with fewer, larger inserts
    write_to_bq("runs", all_runs)
    write_to_bq("analysis", all_analysis)

    mentioned = sum(1 for a in all_analysis if a.get("bs_mentioned"))
    total_scored = sum(1 for a in all_analysis if "scoring_error" not in a or a.get("scoring_error") is None)
    print(f"[DONE] {len(all_runs)} runs, {len(all_analysis)} analyses, B&S mentioned in {mentioned}/{total_scored} scored responses")

if __name__ == "__main__":
    main()
