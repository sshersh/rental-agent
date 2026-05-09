#!/usr/bin/env python3
"""Download geodata for the Brooklyn Rent Stabilized Finder.

Sources:
  Subway stations  — OpenStreetMap Overpass API (public, no key needed)
  ZIP boundaries   — NYC Open Data Socrata JSON API (public)
"""
import json
from pathlib import Path
import requests

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)


def fetch_subway_stations() -> dict:
    """Query OSM Overpass for NYC subway stations."""
    print("Downloading NYC subway stations (OpenStreetMap)...")
    overpass_url = "https://overpass.kumi.systems/api/interpreter"
    headers = {"User-Agent": "BrooklynRSBFinder/1.0"}
    # Three overlapping boxes to cover all of Brooklyn without hitting Overpass timeouts
    boxes = [
        (40.57, -74.04, 40.65, -73.93),  # south Brooklyn
        (40.65, -73.95, 40.74, -73.83),  # northeast Brooklyn / Queens border
        (40.65, -74.04, 40.74, -73.95),  # northwest Brooklyn + downtown
    ]
    seen_ids: set[int] = set()
    elements = []
    for bb in boxes:
        query = (
            f"[out:json][timeout:30];"
            f'node["railway"="station"]["subway"="yes"]({bb[0]},{bb[1]},{bb[2]},{bb[3]});'
            f"out body;"
        )
        resp = requests.get(overpass_url, params={"data": query}, headers=headers, timeout=60)
        resp.raise_for_status()
        for el in resp.json().get("elements", []):
            if el["id"] not in seen_ids:
                seen_ids.add(el["id"])
                elements.append(el)
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
            "properties": {
                "name": el.get("tags", {}).get("name", ""),
                "lines": el.get("tags", {}).get("line", ""),
            },
        }
        for el in elements
        if "lat" in el and "lon" in el
    ]
    print(f"  ✓ {len(features)} stations")
    return {"type": "FeatureCollection", "features": features}


def fetch_brooklyn_zips() -> dict:
    """Download Brooklyn ZIP/ZCTA boundaries from NYC Open Data."""
    print("Downloading Brooklyn ZIP boundaries (NYC Open Data)...")
    # pri4-ifjk = NYC MODZCTA (modified ZIP code tabulation areas)
    url = "https://data.cityofnewyork.us/resource/pri4-ifjk.json?$limit=200"
    rows = requests.get(url, timeout=30).json()
    brooklyn_zips = {str(z) for z in range(11200, 11260)}
    features = []
    for row in rows:
        zcta = str(row.get("modzcta", "") or row.get("zcta", ""))
        geom = row.get("the_geom")
        if zcta in brooklyn_zips and geom:
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {"zip": zcta, "label": row.get("label", zcta)},
            })
    print(f"  ✓ {len(features)} ZIP areas")
    return {"type": "FeatureCollection", "features": features}


subway = fetch_subway_stations()
(DATA_DIR / "subway-stops.geojson").write_text(json.dumps(subway))

zips = fetch_brooklyn_zips()
(DATA_DIR / "brooklyn-zips.geojson").write_text(json.dumps(zips))

print("\nDone. Now run:  .venv/bin/python app.py")
