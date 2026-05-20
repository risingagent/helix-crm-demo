#!/usr/bin/env python3
"""Purge Pinecone vectors for one or more patient_ids.

Companion to the DB-side `helix_purge_test_debris(p_cutoff)` function: once the
SQL purge removes patients (with CASCADE-removing summaries/patterns/etc.),
this script removes the matching per-patient vectors from the `helix-docs`
Pinecone index.

Two-pass deletion for robustness on Pinecone serverless tiers:
  1. Metadata filter delete (`filter={"patient_id": pid}`) — preferred.
  2. Deterministic ID delete for `{pid}-chunk-0` through `{pid}-chunk-19` —
     idempotent backstop in case the tier rejects filter deletes (the cumulative
     ingest pattern produces vector IDs of the form `{patient_id}-chunk-{i}`,
     bounded by chunk count; 20 is well above the worst-case observed).

Usage:
    python3 scripts/purge_pinecone_by_ids.py SYNTH-014 SYNTH-015 [...]
    python3 scripts/purge_pinecone_by_ids.py --stdin   # read one ID per line
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pinecone import Pinecone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

PINECONE_API_KEY = os.environ["PINECONE_API_KEY"]
PINECONE_INDEX = os.environ.get("PINECONE_INDEX", "helix-docs")
MAX_CHUNKS_PER_PATIENT = 20  # backstop range for deterministic ID delete


def purge(patient_ids: list[str]) -> int:
    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index(PINECONE_INDEX)
    started = time.monotonic()
    errors = 0

    for pid in patient_ids:
        # Pass 1: metadata filter (preferred — covers any chunk count)
        try:
            index.delete(filter={"patient_id": pid})
            print(f"  filter-delete OK  patient_id={pid!r}")
        except Exception as exc:
            print(
                f"  filter-delete WARN {pid}: {type(exc).__name__}: {exc} "
                "(falling back to deterministic ID delete)",
                file=sys.stderr,
            )

        # Pass 2: deterministic ID backstop (idempotent — no-op on missing)
        ids = [f"{pid}-chunk-{i}" for i in range(MAX_CHUNKS_PER_PATIENT)]
        try:
            index.delete(ids=ids)
            print(f"  id-delete OK      patient_id={pid!r} (range 0..{MAX_CHUNKS_PER_PATIENT - 1})")
        except Exception as exc:
            errors += 1
            print(f"  id-delete ERR {pid}: {type(exc).__name__}: {exc}", file=sys.stderr)

    try:
        stats = index.describe_index_stats()
        total = (
            stats.get("total_vector_count")
            if isinstance(stats, dict)
            else getattr(stats, "total_vector_count", "?")
        )
        print(f"Index now reports {total} total vectors")
    except Exception as exc:
        print(f"describe_index_stats failed (non-fatal): {exc}", file=sys.stderr)

    elapsed = int((time.monotonic() - started) * 1000)
    print(f"Done in {elapsed}ms — {len(patient_ids)} ids processed, {errors} errors")
    return errors


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "patient_ids",
        nargs="*",
        help="patient_id strings (canonical, e.g. SYNTH-014)",
    )
    p.add_argument(
        "--stdin",
        action="store_true",
        help="Read patient_ids from stdin (one per line) in addition to args",
    )
    args = p.parse_args()

    ids = list(args.patient_ids)
    if args.stdin:
        ids += [ln.strip() for ln in sys.stdin if ln.strip()]
    if not ids:
        p.error("provide at least one patient_id (positional or via --stdin)")
    return 1 if purge(ids) else 0


if __name__ == "__main__":
    sys.exit(main())
