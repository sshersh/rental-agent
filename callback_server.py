"""Local callback receiver + cache for the openclaw agent.

Run this alongside `app.py` (different port) and expose port 9000 via
`ngrok`. The agent PUTs enrichment results here keyed by BBL; the Dash
modal polls the same server for them. Results stay cached for 30 days
since each agent run is expensive.

  POST /agent-result   { ...agent body with "BBL": ... }   — agent writes
  GET  /result/<bbl>                                        — Dash polls

Both endpoints require `Authorization: Bearer <SHARED_SECRET>`.
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SHARED_SECRET = os.getenv("SHARED_SECRET")
PORT          = int(os.getenv("CALLBACK_PORT", "9000"))
DB_PATH       = ROOT / "agent_results.db"
TTL_SECONDS   = 30 * 24 * 60 * 60   # cache expensive agent results for 30 days

if not SHARED_SECRET:
    print("error: SHARED_SECRET must be set in .env", file=sys.stderr)
    sys.exit(2)

_db_lock = Lock()


def _db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS results "
        "(id TEXT PRIMARY KEY, data TEXT NOT NULL, ts INTEGER NOT NULL)"
    )
    return conn


def _check_auth() -> bool:
    return request.headers.get("Authorization") == f"Bearer {SHARED_SECRET}"


def _to_snake(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", key.strip()).strip("_").lower()


def _normalize(obj):
    if isinstance(obj, dict):
        return {_to_snake(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


app = Flask(__name__)


@app.post("/agent-result")
def agent_result():
    if not _check_auth():
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="invalid_body"), 400
    normalized = _normalize(body)
    bbl = normalized.get("bbl")
    if not isinstance(bbl, str) or not bbl:
        return jsonify(error="missing_bbl"), 400
    data_json = json.dumps(normalized)
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO results (id, data, ts) VALUES (?, ?, ?)",
            (bbl, data_json, int(time.time())),
        )
    return jsonify(ok=True, bbl=bbl)


@app.get("/result/<bbl>")
def get_result(bbl: str):
    if not _check_auth():
        return jsonify(error="unauthorized"), 401
    cutoff = int(time.time()) - TTL_SECONDS
    with _db_lock, _db() as conn:
        row = conn.execute(
            "SELECT data, ts FROM results WHERE id = ?",
            (bbl,),
        ).fetchone()
    if not row or row[1] < cutoff:
        return jsonify(status="pending")
    return jsonify(status="ready", data=json.loads(row[0]))


if __name__ == "__main__":
    print(f"callback server listening on 127.0.0.1:{PORT}", flush=True)
    app.run(host="127.0.0.1", port=PORT, debug=False)
