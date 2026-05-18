#!/usr/bin/env python3
"""Phase 3.5e/3.5f — Per-patient Pinecone ingestion for Helix CRM.

Reads the original clinician paste-text from `summaries.raw_output['question']`
(the field Helix Extractor preserves alongside structured extraction), chunks
it to match the `Helix Document Summarization` Flowise chatflow's settings,
embeds via OpenAI `text-embedding-3-large` at the Matryoshka-reduced dimension
1024, and upserts to the Pinecone `helix-docs` index with `patient_id`
metadata so the `Helix Pattern Recognition` retriever can find it.

This closes the architectural gap where `submit_new_patient` writes structured
fields to Supabase but never ingests the narrative into Pinecone.

Two entry points share the core logic in this module:

  - CLI:  `python3 scripts/ingest_patient_pinecone.py SYNTH-013`
          (run manually for backfills or emergency re-ingest)

  - HTTP: `scripts/serve_ingest.py` imports `ingest_patient()` and exposes
          it as POST /ingest, called from n8n's Helix CRM Sync workflow as
          part of the Submit cascade.

Public API:
    ingest_patient(patient_id, *, dry_run=False, keep_existing=False) -> dict

Client-side errors (no summary row, malformed JSON, missing question field)
raise `ValueError`. Upstream errors (network, OpenAI, Pinecone) propagate as
their native exception types so callers can distinguish 4xx vs 5xx.

Default behavior is delete-then-upsert by `patient_id` metadata filter, so
re-running for the same patient cleanly replaces their vectors.

Logs to /tmp/helix_ingest.log alongside stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import OpenAI
from pinecone import Pinecone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
PINECONE_API_KEY = os.environ["PINECONE_API_KEY"]
PINECONE_INDEX = os.environ.get("PINECONE_INDEX", "helix-docs")

# Match Helix Document Summarization chunking exactly.
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

# Matryoshka-reduced dim — must match helix-docs index creation setting.
EMBED_MODEL = "text-embedding-3-large"
EMBED_DIM = 1024

LOG_PATH = Path("/tmp/helix_ingest.log")


def log(msg: str) -> None:
    """Append a timestamped line to stdout AND LOG_PATH.

    Append-only so the log persists across multiple ingest_patient() calls
    when invoked from a long-running server. CLI main() truncates LOG_PATH
    at the start of each invocation to keep per-run logs readable.
    """
    line = "[{}] {}".format(time.strftime("%H:%M:%S", time.localtime()), msg)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def fetch_note_text(patient_id: str) -> str:
    """Pull the original clinician paste-text from summaries.raw_output.

    Helix Extractor preserves the input under the `question` JSON field. We
    use that as the ingestion source rather than reconstructing from structured
    columns — keeps retrieval semantically close to the SYNTH-001..011 chunks
    that were ingested from the original PDFs.

    Raises:
        ValueError: no summary row, empty raw_output, malformed JSON, or
            missing/non-string `question` field. (Client-side errors — 400-ish.)
        requests.RequestException: network or Supabase failure. (5xx upstream.)
    """
    url = "{}/rest/v1/summaries".format(SUPABASE_URL)
    params = {
        "patient_id": "eq.{}".format(patient_id),
        "select": "raw_output,created_at",
        "order": "created_at.desc",
        "limit": "1",
    }
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer {}".format(SUPABASE_KEY),
    }
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError("No summaries row for {}".format(patient_id))
    raw = rows[0].get("raw_output")
    if not raw:
        raise ValueError(
            "summaries.raw_output is empty/null for {}".format(patient_id)
        )
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "summaries.raw_output is not valid JSON for {}: {}".format(
                patient_id, exc
            )
        )
    question = obj.get("question")
    if not question or not isinstance(question, str):
        raise ValueError(
            "summaries.raw_output JSON has no 'question' string field for {}. "
            "Top-level keys present: {}".format(patient_id, list(obj.keys()))
        )
    return question


def chunk_text(text: str) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    return splitter.split_text(text)


def embed_chunks(client: OpenAI, chunks: list[str]) -> list[list[float]]:
    """One batched embedding call for all chunks."""
    resp = client.embeddings.create(
        model=EMBED_MODEL,
        input=chunks,
        dimensions=EMBED_DIM,
    )
    return [d.embedding for d in resp.data]


def _upsert_to_pinecone(
    index,
    patient_id: str,
    chunks: list[str],
    embeddings: list[list[float]],
    *,
    keep_existing: bool = False,
) -> list[str]:
    """Delete existing vectors for the patient (unless keep_existing), then
    upsert fresh chunks. Returns the list of upserted vector IDs.

    Vector ID convention: `{patient_id}-chunk-{i}` (deterministic so reruns
    overwrite cleanly even if delete-by-filter is rejected by Pinecone tier).
    """
    if not keep_existing:
        log("Deleting existing vectors where patient_id={!r}...".format(
            patient_id
        ))
        try:
            index.delete(filter={"patient_id": patient_id})
            log("  delete OK")
        except Exception as exc:
            # On serverless, delete-by-filter may not be supported in all
            # tiers. Deterministic upsert IDs below will overwrite same-index
            # chunks regardless; only orphans from longer prior notes linger.
            log("  delete warning: {}: {} — continuing with upsert".format(
                type(exc).__name__, exc
            ))

    vectors = []
    vector_ids = []
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        vid = "{}-chunk-{}".format(patient_id, i)
        vector_ids.append(vid)
        vectors.append({
            "id": vid,
            "values": emb,
            "metadata": {
                "patient_id": patient_id,
                "text": chunk,
                "chunk_index": i,
                "source": "ingest_patient_pinecone.py",
            },
        })

    log("Upserting {} vectors to {}...".format(len(vectors), PINECONE_INDEX))
    index.upsert(vectors=vectors)
    log("Upsert OK")
    return vector_ids


def ingest_patient(
    patient_id: str,
    *,
    dry_run: bool = False,
    keep_existing: bool = False,
) -> dict:
    """Run the full ingest pipeline for one patient and return a result dict.

    Public API — called by both `main()` (CLI) and `serve_ingest.py` (HTTP).

    Returns:
        {
          "patient_id": str,
          "chunks": int,
          "vector_ids": list[str] | None,  # None on dry-run
          "elapsed_ms": int,
          "note_chars": int,
          "skipped_upsert": bool,           # True on dry-run
        }

    Raises:
        ValueError: client-side data problem (no summary, malformed JSON).
        Exception: upstream service failure (network, OpenAI, Pinecone).
    """
    started = time.monotonic()

    log("Ingest starting for {}".format(patient_id))
    log("Pinecone index: {} (dim {})".format(PINECONE_INDEX, EMBED_DIM))
    log("Embedding model: {}".format(EMBED_MODEL))

    text = fetch_note_text(patient_id)
    log("Fetched clinician note ({} chars) from summaries.raw_output.question"
        .format(len(text)))

    chunks = chunk_text(text)
    log("Chunked into {} pieces (size={}, overlap={})".format(
        len(chunks), CHUNK_SIZE, CHUNK_OVERLAP
    ))
    for i, c in enumerate(chunks):
        preview = c[:70].replace("\n", " ")
        log("  chunk {}: {} chars — {!r}".format(i, len(c), preview))

    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    log("Embedding...")
    embeddings = embed_chunks(openai_client, chunks)
    log("Embedded {} chunks, first vector length = {}".format(
        len(embeddings), len(embeddings[0]) if embeddings else 0
    ))

    if dry_run:
        log("Dry run — skipping Pinecone delete + upsert.")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log("--- Dry run complete for {} in {}ms ---".format(
            patient_id, elapsed_ms
        ))
        return {
            "patient_id": patient_id,
            "chunks": len(chunks),
            "vector_ids": None,
            "elapsed_ms": elapsed_ms,
            "note_chars": len(text),
            "skipped_upsert": True,
        }

    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index(PINECONE_INDEX)
    vector_ids = _upsert_to_pinecone(
        index, patient_id, chunks, embeddings, keep_existing=keep_existing
    )

    try:
        stats = index.describe_index_stats()
        total = stats.get("total_vector_count") if isinstance(stats, dict) \
            else getattr(stats, "total_vector_count", "?")
        log("Index now reports {} total vectors".format(total))
    except Exception as exc:
        log("describe_index_stats failed (non-fatal): {}".format(exc))

    elapsed_ms = int((time.monotonic() - started) * 1000)
    log("--- Ingest complete for {} in {}ms ---".format(patient_id, elapsed_ms))

    return {
        "patient_id": patient_id,
        "chunks": len(chunks),
        "vector_ids": vector_ids,
        "elapsed_ms": elapsed_ms,
        "note_chars": len(text),
        "skipped_upsert": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "patient_id",
        help="Canonical patient ID, e.g. SYNTH-012",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Skip deleting existing Pinecone vectors for this patient_id "
             "before upsert. Default is delete-then-upsert.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + chunk + embed, but do not upsert to Pinecone.",
    )
    args = parser.parse_args()

    LOG_PATH.write_text("")  # truncate per-run log for CLI usage

    try:
        ingest_patient(
            args.patient_id,
            dry_run=args.dry_run,
            keep_existing=args.keep_existing,
        )
    except ValueError as exc:
        log("ERROR (client-side): {}".format(exc))
        return 2
    except Exception as exc:
        log("ERROR (upstream): {}: {}".format(type(exc).__name__, exc))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
