"""Translate the landlord-lookup agent's JSON output into the flat dict
shape that agent_cache.store_result expects.

Agent emits (per the skill's frontmatter):

    {
      "address": ...,
      "wow_url": ...,
      "building_stats": { units, year_built, rent_stabilized_change,
                          open_violations, total_violations,
                          eviction_filings, portfolio_size },
      "owners":    [ { name, contactout_profile_count,
                       location_filter_used, company_filter_used,
                       emails: [...] } ],
      "llcs":      [...],
      "portfolio": [ { address, borough, zip, bbl, landlord } ]
    }

Storage layer wants the legacy flat shape (`num_units`, `landlords[]`, etc.).
"""
from __future__ import annotations

import re
from typing import Any


def _format_bbl(raw_bbl: str | None) -> str | None:
    """Convert a 10-digit BBL string (e.g. "1013880117") into the dashed
    form used in this project ("1-01388-0117"). Returns None if it doesn't
    look like a BBL."""
    if not raw_bbl or not isinstance(raw_bbl, str):
        return None
    digits = re.sub(r"\D", "", raw_bbl)
    if len(digits) != 10:
        return None
    return f"{digits[0]}-{digits[1:6]}-{digits[6:]}"


def _int_or_none(v: Any) -> int | None:
    if v in (None, "", "N/A"):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        m = re.match(r"-?\d+", s)
        if m:
            try:
                return int(m.group(0))
            except ValueError:
                pass
    return None


def _rs_units_2024(rent_stab_change: str | None) -> int | None:
    """Parse the 'after' number from a 'N/A → 4' / '4 → 4' string."""
    if not isinstance(rent_stab_change, str):
        return None
    parts = re.split(r"→|->|-+>", rent_stab_change)
    if len(parts) >= 2:
        return _int_or_none(parts[-1].strip())
    return None


def _translate_landlord(owner: dict) -> dict:
    emails = [e for e in (owner.get("emails") or []) if isinstance(e, str)]
    out: dict = {"name": owner.get("name") or "(unnamed)"}
    if emails:
        out["email"] = emails[0]
        if len(emails) > 1:
            out["all_emails"] = emails
    if owner.get("contactout_profile_count"):
        out["contactout_profile_count"] = owner["contactout_profile_count"]
    if owner.get("location_filter_used"):
        out["location_filter_used"] = owner["location_filter_used"]
    if owner.get("company_filter_used"):
        out["company_filter_used"] = owner["company_filter_used"]
    matched = owner.get("matched_profile")
    if isinstance(matched, dict):
        snippet = matched.get("snippet")
        if isinstance(snippet, str) and snippet.strip():
            out["matched_profile_snippet"] = snippet.strip()
        reason = matched.get("reason")
        if isinstance(reason, str) and reason.strip():
            out["matched_profile_reason"] = reason.strip()
    occupation = owner.get("occupation")
    if isinstance(occupation, str) and occupation.strip():
        # Skill contract: single lowercase word. Normalize defensively in case
        # the model emits stray casing/whitespace.
        out["occupation"] = occupation.strip().lower()
    return out


def _translate_portfolio_entry(entry: dict) -> dict:
    """Agent's portfolio entry → storage's portfolio entry. BBLs are
    reformatted to the dashed form so portfolio fanout can match against
    the rent-stab CSV. We lose the rich per-building stats (rs_units_2024,
    hpd_complaints_*, top_complaint) the legacy agent provided."""
    out: dict = {}
    addr = entry.get("address")
    if addr:
        out["address"] = addr
    bbl = _format_bbl(entry.get("bbl"))
    if bbl:
        out["bbl"] = bbl
    if entry.get("borough"):
        out["borough"] = entry["borough"]
    if entry.get("zip"):
        out["zip"] = entry["zip"]
    if entry.get("landlord"):
        out["landlord"] = entry["landlord"]
    return out


def translate(agent_json: dict, props: dict) -> dict:
    """Translate one agent response + the originating building's props
    (bbl/address/block/lot/zip) into a flat dict for agent_cache.store_result.
    """
    flat: dict[str, Any] = {}

    boro = str(props.get("boro") or "").strip()
    bbl = props.get("bbl")
    if not bbl and boro and props.get("block") is not None and props.get("lot") is not None:
        bbl = f"{boro}-{int(props['block']):05d}-{int(props['lot']):04d}"
    if bbl:
        flat["bbl"] = bbl
    if props.get("address"):
        flat["address"] = props["address"]
        flat["search_address"] = props["address"]
    if props.get("block") is not None:
        flat["block"] = str(props["block"])
    if props.get("lot") is not None:
        flat["lot"] = str(props["lot"])
    if props.get("zip"):
        flat["zip"] = str(props["zip"])

    flat["matched_building"] = agent_json.get("address") or props.get("address")
    if agent_json.get("wow_url"):
        flat["wow_url"] = agent_json["wow_url"]

    stats = agent_json.get("building_stats") or {}
    if (v := _int_or_none(stats.get("units"))) is not None:
        flat["num_units"] = v
    if (v := _int_or_none(stats.get("year_built"))) is not None:
        flat["year_built"] = v
    if (v := _int_or_none(stats.get("portfolio_size"))) is not None:
        flat["num_buildings_in_portfolio"] = v
    if (v := _int_or_none(stats.get("open_violations"))) is not None:
        flat["open_hpd_violations"] = v
    if (v := _int_or_none(stats.get("total_violations"))) is not None:
        flat["total_hpd_violations"] = v
    if (v := _int_or_none(stats.get("eviction_filings"))) is not None:
        flat["eviction_filings_since_2017"] = v
    if stats.get("rent_stabilized_change"):
        flat["rent_stab_note"] = stats["rent_stabilized_change"]
        rs = _rs_units_2024(stats["rent_stabilized_change"])
        if rs is not None:
            flat["rs_units_2024"] = rs

    owners = agent_json.get("owners") or []
    flat["landlords"] = [
        _translate_landlord(o) for o in owners if isinstance(o, dict)
    ]

    portfolio = agent_json.get("portfolio") or []
    flat["portfolio"] = [
        _translate_portfolio_entry(p) for p in portfolio if isinstance(p, dict)
    ]

    # `flags` not produced by the local agent; keep the field present and empty
    # so the UI section is preserved in shape.
    flat["flags"] = []

    flat["source"] = "direct"
    return flat
