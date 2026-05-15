"""Local callback receiver + cache for the openclaw agent.

Run this alongside `app.py` (different port) and expose port 9000 via
`ngrok`. The agent POSTs enrichment results here keyed by BBL; the Dash
modal polls the same server for them. Results stay cached for 30 days
since each agent run is expensive.

  POST /agent-result   { ...agent body with "BBL": ... }   — agent writes
  GET  /result/<bbl>                                        — Dash polls

Both endpoints require `Authorization: Bearer <SHARED_SECRET>`.

Storage: agent payload is flattened (top-level + result.* merged) and the
known scalar/JSON fields are projected into individual columns for SQL
queryability. The original normalized payload is also kept in the `raw`
column so GET can return the canonical shape even if the agent adds new
fields before we extend the schema.
"""
import csv
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SHARED_SECRET = os.getenv("SHARED_SECRET")
PORT          = int(os.getenv("CALLBACK_PORT", "9000"))
DB_PATH       = ROOT / "agent_results.db"
LOG_PATH      = ROOT / "agent_responses.jsonl"
CSV_PATH      = ROOT / "bklyn_rent_stabilized_buildings.csv"
TTL_SECONDS   = 30 * 24 * 60 * 60   # cache expensive agent results for 30 days


def _load_rent_stab_bbl_set():
    """Build the set of BBLs that appear in the Brooklyn rent-stabilized CSV
    so portfolio entries can be matched against it without pulling in pandas."""
    bbls = set()
    if not CSV_PATH.exists():
        print(f"warning: {CSV_PATH.name} not found — portfolio matching disabled",
              file=sys.stderr)
        return bbls
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                block = int(float(row["BLOCK"]))
                lot   = int(float(row["LOT"]))
            except (KeyError, ValueError, TypeError):
                continue
            bbls.add(f"3-{block:05d}-{lot:04d}")
    return bbls


CSV_BBL_SET = _load_rent_stab_bbl_set()
print(f"loaded {len(CSV_BBL_SET):,} rent-stab BBLs for portfolio matching",
      file=sys.stderr)

if not SHARED_SECRET:
    print("error: SHARED_SECRET must be set in .env", file=sys.stderr)
    sys.exit(2)

_db_lock  = Lock()
_log_lock = Lock()


# ── Result schema ────────────────────────────────────────────────────────
#
# Scalar fields → typed columns. Source key (in the flattened payload) is the
# column name unless mapped in COLUMN_TO_SOURCE_KEY (SQL forbids leading
# digits in identifiers, so "311_housing_calls" → "housing_311_calls").

SCALAR_COLUMNS = [
    # (column_name, sql_type)
    ("address",                     "TEXT"),
    ("block",                       "TEXT"),
    ("lot",                         "TEXT"),
    ("zip",                         "TEXT"),
    ("correlation_id",              "TEXT"),
    ("email",                       "TEXT"),  # derived from recommended_outreach.email or first landlord
    ("year_built",                  "INTEGER"),
    ("num_units",                   "INTEGER"),
    ("building_class",              "TEXT"),
    ("num_buildings_in_portfolio",  "INTEGER"),
    ("open_hpd_violations",         "INTEGER"),
    ("total_hpd_violations",        "INTEGER"),
    ("last_hpd_registration",       "TEXT"),
    ("rent_stabilized_units",       "TEXT"),
    ("rent_stab_note",              "TEXT"),
    ("housing_311_calls",           "INTEGER"),
    ("eviction_filings_since_2017", "INTEGER"),
    ("evictions_executed",          "INTEGER"),
    ("matched_building",            "TEXT"),
    ("search_address",              "TEXT"),
    # Fields the agent now provides per portfolio entry (and also for the
    # focal building when it appears in its own portfolio list).
    ("council_district",            "INTEGER"),
    ("rs_units_2007",               "INTEGER"),
    ("rs_units_2024",               "INTEGER"),
    ("hpd_complaints_total",        "INTEGER"),
    ("hpd_complaints_last_3yr",     "INTEGER"),
    ("top_complaint",               "TEXT"),
    ("landlord_summary",            "TEXT"),   # portfolio entry's "landlord" string, e.g. "MARTIN BAUMRIND +4"
    # Provenance — distinguishes direct agent lookup from portfolio-derived.
    ("source",                      "TEXT"),
]

JSON_COLUMNS = [
    "landlords",
    "corporate_entities",
    "flags",
    "portfolio",
    "recommended_outreach",
    "useful_links",
]

# Source key (in the flattened agent payload) ≠ column name only when the
# source key starts with a digit.
COLUMN_TO_SOURCE_KEY = {
    "housing_311_calls": "311_housing_calls",
}

# Never persist these (auth echo, transient internal fields).
DROP_KEYS = {"shared_secret"}


def _create_sql() -> str:
    scalar_defs = ",\n    ".join(f"{c} {t}" for c, t in SCALAR_COLUMNS)
    json_defs   = ",\n    ".join(f"{c} TEXT" for c in JSON_COLUMNS)
    return f"""
        CREATE TABLE IF NOT EXISTS results (
            bbl TEXT PRIMARY KEY,
            ts INTEGER NOT NULL,
            raw TEXT NOT NULL,
            {scalar_defs},
            {json_defs}
        )
    """


_ALL_COLS  = (
    ["bbl", "ts", "raw"]
    + [c for c, _ in SCALAR_COLUMNS]
    + JSON_COLUMNS
)
_INSERT_SQL = (
    f"INSERT OR REPLACE INTO results ({','.join(_ALL_COLS)}) "
    f"VALUES ({','.join('?' * len(_ALL_COLS))})"
)


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _looks_like_email(s) -> bool:
    return isinstance(s, str) and _EMAIL_RE.match(s.strip()) is not None


def _derive_email(flat: dict):
    """Pick the canonical contact email for a flattened payload.
    Order: recommended_outreach.email → first landlord's email / email_inferred.
    Free-text notes (e.g. "Not publicly available — try ACRIS …") are rejected."""
    ro = flat.get("recommended_outreach")
    if isinstance(ro, dict):
        v = ro.get("email")
        if _looks_like_email(v):
            return v.strip()
    for ll in (flat.get("landlords") or []):
        if isinstance(ll, dict):
            for key in ("email", "email_inferred"):
                v = ll.get(key)
                if _looks_like_email(v):
                    return v.strip()
    return None


def _flatten_for_storage(normalized_body: dict) -> dict:
    """Merge `result.*` into the top level. Drop fields we refuse to persist.
    Always re-derive scalar(s) like `email` from nested fields — don't trust
    whatever the agent put at the top level."""
    flat = {k: v for k, v in normalized_body.items() if k != "result"}
    nested = normalized_body.get("result")
    if isinstance(nested, dict):
        flat.update(nested)
    for k in DROP_KEYS:
        flat.pop(k, None)
    derived = _derive_email(flat)
    if derived:
        flat["email"] = derived
    else:
        flat.pop("email", None)
    return flat


# ── Portfolio fan-out ────────────────────────────────────────────────────
#
# Each portfolio entry (post-normalize) is a flat dict with the keys below.
# Map them to the existing flat-row keys so a portfolio entry can populate
# any rent-stab building's row.

PORTFOLIO_FIELD_MAP = {
    "bbl":                     "bbl",
    "address":                 "address",
    "year_built":              "year_built",
    "units":                   "num_units",
    "open_violations":         "open_hpd_violations",
    "total_violations":        "total_hpd_violations",
    "evictions_filed":         "eviction_filings_since_2017",
    "council_district":        "council_district",
    "rs_units_2007":           "rs_units_2007",
    "rs_units_2024":           "rs_units_2024",
    "hpd_complaints_total":    "hpd_complaints_total",
    "hpd_complaints_last_3yr": "hpd_complaints_last_3yr",
    "top_complaint":           "top_complaint",
    "landlord":                "landlord_summary",
}

# Fields copied verbatim from the focal response onto every portfolio-derived
# row, because they describe the (shared) owner, not the building.
OWNER_FIELDS = ("landlords", "corporate_entities", "recommended_outreach", "email")


def _derive_portfolio_rows(focal_flat: dict, focal_bbl: str):
    """Walk the focal response's portfolio list. For each entry whose BBL is
    a rent-stab building (and isn't the focal building itself), yield
    (bbl, derived_flat). Returns nothing when portfolio is missing or is the
    older stats-dict shape."""
    portfolio = focal_flat.get("portfolio")
    if not isinstance(portfolio, list):
        return
    shared_owner = {k: focal_flat[k] for k in OWNER_FIELDS if focal_flat.get(k)}
    for entry in portfolio:
        if not isinstance(entry, dict):
            continue
        bbl = entry.get("bbl")
        if not isinstance(bbl, str) or not bbl:
            continue
        if bbl == focal_bbl or bbl not in CSV_BBL_SET:
            continue
        derived = {}
        for src_key, dst_key in PORTFOLIO_FIELD_MAP.items():
            if src_key in entry and entry[src_key] not in (None, ""):
                derived[dst_key] = entry[src_key]
        derived.update(shared_owner)
        derived["bbl"]    = bbl
        derived["source"] = "portfolio"
        yield bbl, derived


def _upsert_portfolio_row(conn, bbl: str, derived_flat: dict, ts: int):
    """Insert a portfolio-derived row, OR if the BBL already has a row from a
    direct agent lookup, only merge the shared owner fields onto it (preserve
    its building-specific stats)."""
    cur = conn.execute(
        "SELECT raw, source FROM results WHERE bbl = ?", (bbl,)
    ).fetchone()
    if not cur:
        conn.execute(_INSERT_SQL, _row_values(derived_flat, bbl, ts))
        return "inserted"
    existing_raw, existing_source = cur
    if existing_source != "direct":
        # Portfolio-derived row already exists — replace with the freshest data.
        conn.execute(_INSERT_SQL, _row_values(derived_flat, bbl, ts))
        return "replaced"
    # Direct lookup already wrote richer data. Only merge owner fields.
    try:
        existing = json.loads(existing_raw)
    except Exception:
        return "skipped"
    if not isinstance(existing, dict):
        return "skipped"
    for k in OWNER_FIELDS:
        if derived_flat.get(k):
            existing[k] = derived_flat[k]
    re_email = _derive_email(existing)
    if re_email:
        existing["email"] = re_email
    else:
        existing.pop("email", None)
    # Preserve source='direct'. Use current ts so the row's freshness reflects
    # the most recent touch.
    conn.execute(_INSERT_SQL, _row_values(existing, bbl, ts))
    return "merged"


def _row_values(flat: dict, bbl: str, ts: int) -> tuple:
    """Map a flattened payload + metadata to a tuple ordered by _ALL_COLS."""
    raw_json = json.dumps(flat)
    values = [bbl, ts, raw_json]
    for col, _ in SCALAR_COLUMNS:
        src_key = COLUMN_TO_SOURCE_KEY.get(col, col)
        values.append(flat.get(src_key))
    for col in JSON_COLUMNS:
        v = flat.get(col)
        values.append(json.dumps(v) if v is not None else None)
    return tuple(values)


def _init_db() -> None:
    """Create the table on first run. If an older `(id, data, ts)` schema
    exists, migrate every row into the new column-wise table. If a new column
    has been added since last run, ALTER TABLE and backfill from `raw`."""
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        cur = conn.execute("PRAGMA table_info(results)")
        cols = {row[1] for row in cur.fetchall()}
        if not cols:
            conn.execute(_create_sql())
            return
        if "raw" not in cols or "bbl" not in cols:
            # Old schema: (id, data, ts) — full migration.
            print("migrating results table to flattened schema...", file=sys.stderr)
            old_rows = conn.execute("SELECT id, data, ts FROM results").fetchall()
            conn.execute("ALTER TABLE results RENAME TO results_old")
            conn.execute(_create_sql())
            migrated, skipped = 0, 0
            for old_id, old_data, old_ts in old_rows:
                try:
                    payload = json.loads(old_data)
                except Exception:
                    skipped += 1
                    continue
                if not isinstance(payload, dict):
                    skipped += 1
                    continue
                flat = _flatten_for_storage(payload)
                bbl = flat.get("bbl") or old_id
                if not isinstance(bbl, str) or not bbl:
                    skipped += 1
                    continue
                try:
                    conn.execute(_INSERT_SQL, _row_values(flat, bbl, old_ts))
                    migrated += 1
                except Exception as e:
                    print(f"migrate {bbl}: {e}", file=sys.stderr)
                    skipped += 1
            conn.execute("DROP TABLE results_old")
            print(f"migration: {migrated} rows kept, {skipped} skipped",
                  file=sys.stderr)
            return
        # New schema in place — add any columns that were appended to
        # SCALAR_COLUMNS / JSON_COLUMNS since the last run, and backfill them
        # from `raw`.
        type_map = {c: t for c, t in SCALAR_COLUMNS}
        type_map.update({c: "TEXT" for c in JSON_COLUMNS})
        missing = [c for c in type_map if c not in cols]
        if missing:
            print(f"adding columns: {missing}", file=sys.stderr)
            for col in missing:
                conn.execute(f"ALTER TABLE results ADD COLUMN {col} {type_map[col]}")
            rows = conn.execute("SELECT bbl, raw FROM results").fetchall()
            backfilled = 0
            for bbl, raw_json in rows:
                try:
                    stored = json.loads(raw_json)
                except Exception:
                    continue
                if not isinstance(stored, dict):
                    continue
                flat = _flatten_for_storage(stored)
                # Pre-existing rows all came from direct agent lookups.
                if "source" in missing and not flat.get("source"):
                    flat["source"] = "direct"
                sets, vals = [], []
                for col in missing:
                    if col in JSON_COLUMNS:
                        jv = flat.get(col)
                        v = json.dumps(jv) if jv is not None else None
                    else:
                        src_key = COLUMN_TO_SOURCE_KEY.get(col, col)
                        v = flat.get(src_key)
                    sets.append(f"{col} = ?")
                    vals.append(v)
                # Also refresh `raw` so newly-derived fields are part of it.
                sets.append("raw = ?")
                vals.append(json.dumps(flat))
                vals.append(bbl)
                conn.execute(
                    f"UPDATE results SET {', '.join(sets)} WHERE bbl = ?",
                    vals,
                )
                backfilled += 1
            print(f"backfilled {backfilled} rows for new columns: {missing}",
                  file=sys.stderr)
        _cleanup_invalid_emails(conn)
    finally:
        conn.close()


def _cleanup_invalid_emails(conn) -> None:
    """Re-derive `email` for rows whose stored value isn't a valid address.
    Idempotent: rows already valid (or null) are skipped."""
    bad = []
    for bbl, raw_json, current in conn.execute(
        "SELECT bbl, raw, email FROM results WHERE email IS NOT NULL"
    ).fetchall():
        if _looks_like_email(current):
            continue
        bad.append((bbl, raw_json))
    if not bad:
        return
    cleaned = 0
    for bbl, raw_json in bad:
        try:
            stored = json.loads(raw_json)
        except Exception:
            continue
        if not isinstance(stored, dict):
            continue
        flat = _flatten_for_storage(stored)
        new_email = flat.get("email")
        conn.execute(
            "UPDATE results SET email = ?, raw = ? WHERE bbl = ?",
            (new_email, json.dumps(flat), bbl),
        )
        cleaned += 1
    print(f"cleaned up email for {cleaned} rows (invalid format)",
          file=sys.stderr)


def _db():
    return sqlite3.connect(DB_PATH, isolation_level=None)


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


def _log_agent_response(body, status, bbl=None, error=None):
    """Append one JSON line per agent POST to agent_responses.jsonl.
    Captures the raw body so we can replay/debug agent payload changes."""
    record = {
        "ts":     datetime.now(timezone.utc).isoformat(),
        "ip":     request.remote_addr,
        "status": status,
        "bbl":    bbl,
        "error":  error,
        "body":   body,
    }
    try:
        with _log_lock, open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"log write failed: {e}", file=sys.stderr)


_init_db()

app = Flask(__name__)


@app.post("/agent-result")
def agent_result():
    if not _check_auth():
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        _log_agent_response(body, "rejected", error="invalid_body")
        return jsonify(error="invalid_body"), 400
    normalized = _normalize(body)
    bbl = normalized.get("bbl")
    if not isinstance(bbl, str) or not bbl:
        _log_agent_response(body, "rejected", error="missing_bbl")
        return jsonify(error="missing_bbl"), 400
    flat = _flatten_for_storage(normalized)
    flat.setdefault("bbl", bbl)
    flat["source"] = "direct"
    ts = int(time.time())
    fanout = 0
    with _db_lock, _db() as conn:
        conn.execute(_INSERT_SQL, _row_values(flat, bbl, ts))
        for p_bbl, p_flat in _derive_portfolio_rows(flat, bbl):
            _upsert_portfolio_row(conn, p_bbl, p_flat, ts)
            fanout += 1
    _log_agent_response(body, "accepted", bbl=bbl)
    return jsonify(ok=True, bbl=bbl, portfolio_fanout=fanout)


@app.get("/result/<bbl>")
def get_result(bbl: str):
    if not _check_auth():
        return jsonify(error="unauthorized"), 401
    cutoff = int(time.time()) - TTL_SECONDS
    with _db_lock, _db() as conn:
        row = conn.execute(
            "SELECT raw, ts FROM results WHERE bbl = ?",
            (bbl,),
        ).fetchone()
    if not row or row[1] < cutoff:
        return jsonify(status="pending")
    return jsonify(status="ready", data=json.loads(row[0]))


if __name__ == "__main__":
    print(f"callback server listening on 127.0.0.1:{PORT}", flush=True)
    app.run(host="127.0.0.1", port=PORT, debug=False)
