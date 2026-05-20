# Helix CRM

**AI-assisted clinical decision support for primary care.** A paste-text patient note becomes a structured chart in ~90 seconds: a longitudinal summary, recurring patterns, guideline-backed lab recommendations, differential-diagnosis exclusions, and five guided questions for the next visit — all reviewed and signed off by the clinician.

→ **Live demo:** [https://risingagent.github.io/helix-crm-demo/](https://risingagent.github.io/helix-crm-demo/)

---

## What this is

Helix CRM is a small platform that bolts an LLM-powered analytical layer onto a clinician's intake flow. The clinician submits a free-text visit note (or generates a synthetic one for the demo). Within seconds the system:

1. Extracts a structured **Summary** (chief complaint, diagnoses, medications, allergies, labs)
2. Detects **Patterns** across the patient's visit history (recurring symptoms, trending labs, unresolved follow-ups, medication changes, contradictions)
3. Generates **Lab Recommendations** cross-referenced against **USPSTF + NICE** clinical guidelines
4. Produces **Diagnosis Exclusions** — differential diagnoses considered and confidently ruled out, with guideline-backed rationale
5. Surfaces five **Guided Questions** for the next encounter, prioritized by clinical risk

Every output is structured, citation-linked where possible, and gated behind a clinician sign-off button. Nothing is auto-finalized.

---

## Architecture

```
[Retool dashboard]        ← clinician UI (cover + detail views, progress stepper)
        │
   ngrok tunnel           ← stable public endpoint for the local stack
        │
[n8n workflows]           ← orchestration (CRM Sync, Pattern/Lab/Dx/Question Sync)
        │
[Flowise chatflows]       ← Anthropic Claude Sonnet 4.6, guideline-aware prompts
        │
   ┌────┴────┬──────────────┬──────────────┐
[Supabase]  [Pinecone]    [OpenAI]        [Flask /ingest]
 Postgres   vector DB    text-embedding-   per-patient
 (RLS)      (helix-docs  3-large @ 1024d   ingestion service
            + knowledge-                   (in this repo)
            base)
```

**Two retrieval architectures, by design:**
- *Pattern + Question* flows use a Pinecone retriever filtered by `patient_id` — they reason over the patient's own notes.
- *Lab Recs + Dx Exclusions* flows inject the patient's Supabase summary as text context and retrieve only the `knowledge-base` index (NICE/USPSTF PDFs) — keeping patient data out of guideline retrieval.

Per-patient Pinecone ingestion is wired into the submit cascade so new patients are immediately searchable.

---

## What's in this repo

| Path | What it is |
|---|---|
| `helix-demo.html` | Self-contained static demo of the dashboard UI (rendered live via GitHub Pages) |
| `index.html` | Redirects the Pages root to `helix-demo.html` |
| `scripts/serve_ingest.py` | Flask service — `POST /ingest` for per-patient Pinecone ingestion, called by n8n at the end of CRM Sync |
| `scripts/ingest_patient_pinecone.py` | CLI for the same ingestion logic — chunks Supabase note text, embeds via OpenAI, upserts to Pinecone with `patient_id` metadata |
| `scripts/backfill_*.py` | One-off backfill harnesses for summaries, patterns/questions, labs/dx (used during demo data seeding) |
| `scripts/test_*.py` | Test harnesses for the lab-recs and dx-exclusion chatflows |
| `scripts/*_fixtures.py` | Hand-authored fixture cases used by the test harnesses |
| `knowledge-base/*.pdf` | 16 NICE + USPSTF clinical guideline PDFs, ingested into the `knowledge-base` Pinecone index |
| `.env.example` | Template for the local `.env` (real `.env` is gitignored) |

---

## What's *not* in this repo (and where it lives)

Helix is a multi-cloud platform; most of the runtime lives outside the repo by design:

- **Retool app** — lives in Retool Cloud. The dashboard layout, queries, event handlers, and progress stepper are configured in the Retool editor.
- **n8n workflows** — self-hosted via Docker, exposed via ngrok. Workflows include CRM Sync, Pattern Sync, Lab Recs Sync, Dx Sync, Question Sync, and Sample Note Sync.
- **Flowise chatflows** — self-hosted via Docker. Includes Helix Extractor, Pattern Recognition, Lab Recs, Dx Exclusion, Question Generation, Sample Note Generator, and the ingestion-side Document Summarizer.
- **Supabase Postgres** — managed database with six tables (`patients`, `summaries`, `patterns`, `lab_recs`, `dx_exclusions`, `questions`), RLS on, service-role-only access.
- **Pinecone** — serverless, two indexes (`helix-docs` for per-patient chunks, `knowledge-base` for guideline PDFs), both 1024-dim cosine.

The Python service in this repo is the bridge that lets the n8n submit cascade trigger per-patient Pinecone ingestion without exposing OpenAI/Pinecone credentials to the n8n container.

---

## Running the Python service locally

> The scripts depend on a running Supabase project with the schema described above, plus OpenAI + Pinecone accounts. They are not standalone — they're one component of the full system.

```bash
# 1. Clone + enter
git clone https://github.com/risingagent/helix-crm-demo.git
cd helix-crm-demo

# 2. Install dependencies
pip3 install --user flask openai pinecone langchain-text-splitters python-dotenv requests

# 3. Create your .env from the template (NEVER commit the real one)
cp .env.example .env
# Then edit .env with:
#   SUPABASE_URL=https://<your-project>.supabase.co
#   SUPABASE_SERVICE_KEY=<your service role key>
#   OPENAI_API_KEY=sk-...
#   PINECONE_API_KEY=pcsk_...
#   PINECONE_INDEX=helix-docs
#   HELIX_INGEST_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# 4. Run the Flask ingest service (port 8888)
python3 scripts/serve_ingest.py

# 5. In another terminal — test ingestion for a specific patient
python3 scripts/ingest_patient_pinecone.py SYNTH-001 --dry-run
```

For persistent operation on macOS, the service runs as a launchd LaunchAgent (`com.helix.ingest`) — out of scope for this README but the pattern is standard (plist in `~/Library/LaunchAgents/`).

---

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 – 3.4 | Knowledge-base ingestion, individual chatflows (Pattern, Lab, Dx, Question), CRM Sync intake | Done |
| 3.5e | Sign-off UX, cascade race fix, panel polish, Path C Pinecone ingestion script | Done |
| 3.5f | Submit-to-detail cascade, page split, progress stepper, sample-note generator, persistent Flask service | Done — end-to-end verified |
| 3.6 (planned) | In-session patient updates: append a follow-up note → merged living summary with new content highlighted → cumulative re-cascade | Designed, not built |
| 4 (future) | HIPAA path: SSO, audit logging, BAA-covered vector store, deletion API | Not started |

---

## Security & data posture

- All synthetic data (`test-data/`) and test output (`test-results/`) is gitignored — only public clinical-guideline PDFs ship in the repo.
- `.env` is gitignored. Rotate any key that's been pasted into a chat, log, or shared channel — credentials should only ever be entered into a terminal prompt or password manager.
- Supabase row-level security is enabled on all panel tables (zero policies, service-role-only access — appropriate for the current single-tenant demo). Multi-tenant + audit is Phase 4.

---

## License

No license specified — all rights reserved by default. This is a portfolio / demo repository. Contact the author for reuse.
