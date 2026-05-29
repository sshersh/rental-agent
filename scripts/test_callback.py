#!/usr/bin/env python3
"""Simulate the agent's callback. POSTs a fake enrichment result to the
callback server (through ngrok), then GETs the same BBL to verify round-trip.

Usage:
    .venv/bin/python scripts/test_callback.py <bbl> [--delay SECONDS]

If no <bbl> is given, picks the first row from the dataset and assembles a
Brooklyn BBL (3-BLOCK-LOT) so you can copy-paste it into the app.

The --delay flag waits N seconds before POSTing — useful for end-to-end
testing the modal: open the building's card in the app, then run this with
--delay 5 to see the spinner resolve into the agent payload.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CALLBACK_PUBLIC_URL = (os.getenv("CALLBACK_PUBLIC_URL") or "").rstrip("/")
CALLBACK_LOCAL_URL  = (os.getenv("CALLBACK_LOCAL_URL") or "http://localhost:9000").rstrip("/")
SHARED_SECRET       = os.getenv("SHARED_SECRET") or ""

if not SHARED_SECRET:
    print("error: SHARED_SECRET must be set in .env", file=sys.stderr)
    sys.exit(2)
# Default write target = public (so we exercise the full tunnel→server path).
# Falls back to local if no ngrok URL is configured yet.
WRITE_BASE = CALLBACK_PUBLIC_URL or CALLBACK_LOCAL_URL


def first_bbl() -> str:
    df = pd.read_csv(
        ROOT / "source_data" / "bklyn_rent_stabilized_buildings.csv"
    ).dropna(subset=["LATITUDE", "LONGITUDE"])
    row = df.iloc[0]
    return f'3-{int(row["BLOCK"]):05d}-{int(row["LOT"]):04d}'


def fake_payload(bbl: str) -> dict:
    """Mirror the real agent's schema (mixed-case keys); server normalizes."""
    return {
        "Building Address": "282 Skillman Street, Brooklyn, NY 11205",
        "BBL": bbl,
        "Year built": 2007,
        "Num Units": 5,
        "Open HPD Violations": 6,
        "311 Housing Calls": 1,
        "Building Class": None,
        "Num Buildings in Portfolio": 1,
        "Landlords": [
            {
                "Name": "ZALMEN BERKOVITS",
                "Role": "Individual",
                "Company": "Zalmen Management",
                "Phone": "(718) 972-5132",
                "Address": "3810 14th Ave, Brooklyn, NY 11218",
                "Source": "ZalmenManagement.com, BBB, NYS DOS",
            },
        ],
    }


def post_result(bbl: str) -> None:
    url = f"{WRITE_BASE}/agent-result"
    headers = {
        "Authorization": f"Bearer {SHARED_SECRET}",
        "Content-Type":  "application/json",
    }
    body = fake_payload(bbl)
    print(f"POST {url}")
    print(f"  body: {json.dumps(body)}")
    r = requests.post(url, json=body, headers=headers, timeout=10)
    print(f"  -> {r.status_code} {r.text}")
    r.raise_for_status()


def get_result(bbl: str) -> None:
    url = f"{CALLBACK_LOCAL_URL}/result/{bbl}"
    headers = {"Authorization": f"Bearer {SHARED_SECRET}"}
    print(f"GET {url}")
    r = requests.get(url, headers=headers, timeout=10)
    print(f"  -> {r.status_code} {r.text}")
    r.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bbl",
        nargs="?",
        help="BBL (BORO-BLOCK-LOT). Default: assembled from first row of the CSV.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0,
        help="seconds to sleep before POSTing (open the modal first to watch the spinner resolve).",
    )
    args = parser.parse_args()

    bbl = args.bbl or first_bbl()
    print(f"bbl = {bbl}")

    if args.delay > 0:
        print(f"sleeping {args.delay}s — open this building's card in the app now...")
        time.sleep(args.delay)

    post_result(bbl)
    print()
    get_result(bbl)


if __name__ == "__main__":
    main()
