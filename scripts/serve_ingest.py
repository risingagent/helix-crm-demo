#!/usr/bin/env python3
"""Phase 3.5f — Flask wrapper exposing per-patient Pinecone ingestion over HTTP.

Wraps `ingest_patient_pinecone.ingest_patient()` so the Submit cascade can
trigger ingestion automatically. Sits on Mac:8888 and is reached from the
dockerized n8n container via `host.docker.internal:8888`.

Endpoints:

  GET  /healthz   No auth. Returns {"ok": true} for liveness checks
                  (preview_start / n8n / monitoring).

  POST /ingest    Auth required via `X-Helix-Token` header.
                  Body: {"patient_id": "SYNTH-013",
                         "dry_run": false,    # optional, default false
                         "keep_existing": false}  # optional, default false
                  Success 200: full result dict from ingest_patient().
                  Client error 400: bad payload OR upstream summary missing /
                                    malformed JSON / no `question` field.
                  Auth error 401:  bad/missing X-Helix-Token.
                  Server error 500: network / OpenAI / Pinecone failure.

Run directly:
    python3 scripts/serve_ingest.py

Or via the preview launcher (.claude/launch.json includes the `serve_ingest`
configuration). For demo, recommended to keep it always-on alongside n8n.

Environment variables (loaded from `.env` at the project root):
    HELIX_INGEST_TOKEN  required — shared secret for X-Helix-Token check
    HELIX_INGEST_PORT   optional — default 8888
    (plus everything ingest_patient_pinecone.py requires: SUPABASE_*, OPENAI_*, PINECONE_*)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

# Make the sibling CLI script importable as a module.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Load .env BEFORE importing ingest_patient_pinecone, since that module reads
# env vars at import time.
PROJECT_ROOT = SCRIPT_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")

from ingest_patient_pinecone import ingest_patient  # noqa: E402

PORT = int(os.environ.get("HELIX_INGEST_PORT", "8888"))
TOKEN = os.environ.get("HELIX_INGEST_TOKEN")

if not TOKEN:
    sys.stderr.write(
        "FATAL: HELIX_INGEST_TOKEN not set in .env — refusing to start.\n"
        "Generate with: python3 -c "
        "\"import secrets; print(secrets.token_urlsafe(32))\"\n"
    )
    sys.exit(1)

app = Flask(__name__)


def _log_request(status: int, patient_id: str | None, note: str = "") -> None:
    """One line per request to stdout. preview_start captures stdout."""
    line = "[{}] {} patient_id={} HTTP {}{}".format(
        time.strftime("%H:%M:%S", time.localtime()),
        request.method + " " + request.path,
        patient_id or "-",
        status,
        " — " + note if note else "",
    )
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True, "service": "helix-ingest", "port": PORT}), 200


@app.route("/ingest", methods=["POST"])
def ingest():
    # Auth — constant-time compare would be nice for production, but for a
    # demo shared-secret on a LAN this is fine.
    if request.headers.get("X-Helix-Token") != TOKEN:
        _log_request(401, None, "bad token")
        return jsonify({"error": "missing or invalid X-Helix-Token"}), 401

    body = request.get_json(silent=True) or {}
    patient_id = body.get("patient_id")
    if not patient_id or not isinstance(patient_id, str):
        _log_request(400, None, "missing patient_id")
        return jsonify({
            "error": "missing or non-string patient_id in body",
        }), 400

    dry_run = bool(body.get("dry_run", False))
    keep_existing = bool(body.get("keep_existing", False))

    try:
        result = ingest_patient(
            patient_id,
            dry_run=dry_run,
            keep_existing=keep_existing,
        )
    except ValueError as exc:
        # Client-side data problem — no summary row, malformed JSON, etc.
        _log_request(400, patient_id, "ValueError: " + str(exc))
        return jsonify({
            "error": str(exc),
            "type": "ValueError",
            "patient_id": patient_id,
        }), 400
    except Exception as exc:
        # Upstream service failure — network, OpenAI, Pinecone, etc.
        _log_request(500, patient_id, type(exc).__name__ + ": " + str(exc))
        return jsonify({
            "error": str(exc),
            "type": type(exc).__name__,
            "patient_id": patient_id,
        }), 500

    _log_request(
        200, patient_id,
        "chunks={} elapsed_ms={}".format(
            result["chunks"], result["elapsed_ms"]
        ),
    )
    return jsonify(result), 200


if __name__ == "__main__":
    sys.stdout.write(
        "[serve_ingest] Listening on 0.0.0.0:{} "
        "(reached from Docker via host.docker.internal:{})\n".format(
            PORT, PORT
        )
    )
    sys.stdout.flush()
    # 0.0.0.0 so the dockerized n8n container can reach this via
    # host.docker.internal:8888.
    app.run(host="0.0.0.0", port=PORT, debug=False)
