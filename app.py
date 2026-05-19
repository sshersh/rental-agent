import argparse
import math
import os
import json
import re
import smtplib
import threading
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import pandas as pd
import dash
from dash import dcc, html, Input, Output, State, callback, ALL, ctx, no_update
import dash_leaflet as dl
import dash_bootstrap_components as dbc
from dash_extensions.javascript import assign
from dotenv import load_dotenv

from anthropic import Anthropic

import agent_cache
import agent_runner
from agent_runner import run_landlord_lookup
from agent_to_storage import translate as translate_agent_result

load_dotenv()
agent_cache.init_db()

AVAILABLE_TEMPLATE_VARS = [
    "owner_name", "owner_first_name", "address", "street_address",
    "addresses", "property_label", "building_count", "zip", "block", "lot",
    "office", "phone", "sender_name", "sender_email",
]

_COMPANY_MARKERS = {
    "LLC", "LP", "LLP", "PC", "PLLC", "INC", "CORP", "LTD",
    "MGMT", "REALTY", "REALTORS", "PROPERTIES", "PROPERTY",
    "MANAGEMENT", "ASSOCIATES", "PARTNERS", "GROUP", "HOLDINGS",
    "ENTERPRISES", "CAPITAL", "VENTURES", "TRUST",
}

_NAME_ACRONYMS = {
    "LLC", "LP", "LLP", "PC", "PLLC", "INC", "INC.", "CORP", "CORP.",
    "LTD", "LTD.", "II", "III", "IV", "V", "NYC", "USA", "NY", "PA",
    "&", "AND", "DBA", "MGMT", "MGMT.",
}


def _format_owner_name(name: str) -> str:
    """Title-case all-caps/all-lower names while preserving acronyms.

    Names from WoW often come UPPERCASED ("ABC REALTY LLC") which makes
    drafted emails look obviously templated.
    """
    if not name:
        return "there"
    s = name.strip()
    if not s:
        return "there"
    if not (s.isupper() or s.islower()):
        return s
    out = []
    for w in s.split():
        if w.upper() in _NAME_ACRONYMS:
            out.append(w.upper())
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def _owner_first_name(name: str) -> str:
    """First name for human owners; "there" for company names.

    Heuristic: any word matching a company marker (LLC, Realty, Group, …)
    flips the whole record to company-mode so we don't greet "ABC Realty
    LLC" as "Hi ABC,". Matches the `_format_owner_name` fallback.
    """
    if not name or not name.strip():
        return "there"
    stripped_words = {w.upper().rstrip(".,") for w in name.split()}
    if stripped_words & _COMPANY_MARKERS:
        return "there"
    parts = _format_owner_name(name).split()
    return parts[0] if parts else "there"


def _street_only(addr: str) -> str:
    """'129 6th Avenue, Brooklyn' -> '129 6th Avenue'. Splits on the first
    comma — borough/city/state/zip all live to the right of it in our data."""
    if not addr:
        return ""
    return addr.split(",", 1)[0].strip()


def _property_label(buildings: list) -> str:
    """Subject-friendly label: address for single, generic for portfolio."""
    n = len(buildings or [])
    if n <= 1:
        return (buildings[0].get("address", "") if n else "") or ""
    return f"your {n} properties"


def _build_owner_ctx(owner: dict) -> dict:
    """Per-owner template ctx — shared by preview rendering and SMTP send.

    Reads SENDER_NAME and EMAIL_USER from env at call time so .env edits
    take effect without restart.
    """
    bs = owner.get("buildings", [])
    addresses_str = "; ".join(b["address"] for b in bs)
    first = bs[0] if bs else {}
    return {
        "owner_name":       _format_owner_name(owner.get("name") or ""),
        "owner_first_name": _owner_first_name(owner.get("name") or ""),
        "address":          first.get("address", ""),
        "street_address":   _street_only(first.get("address", "")),
        "addresses":      addresses_str,
        "property_label": _property_label(bs),
        "building_count": len(bs),
        "zip":            first.get("zip", ""),
        "block":          first.get("block", ""),
        "lot":            first.get("lot", ""),
        "office":         owner.get("office", "") or "",
        "phone":          owner.get("phone", "") or "",
        "sender_name":    os.getenv("SENDER_NAME", "Sam Shersher"),
        "sender_email":   os.getenv("EMAIL_USER", "(your email)"),
    }


LOOKUP_TIMEOUT_SECONDS = 240   # local agent runs (WoW + multi-owner ContactOut) take ~1–3 min
_LOOKUP_ERRORS: dict = {}      # bbl -> {"reason": str, "detail": str} or legacy reason str; written by agent worker thread on failure
AGENT_HEADED = False           # set by --headed at startup; surfaces the browser the agent drives

# ── Data loading ───────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


# ── Server-side last-template persistence ────────────────────────────────
LAST_TEMPLATE_PATH = DATA_DIR / "last_email_template.json"


def _load_last_template() -> dict:
    try:
        with open(LAST_TEMPLATE_PATH) as f:
            data = json.load(f)
        return {
            "subject": str(data.get("subject", "")),
            "body":    str(data.get("body", "")),
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"subject": "", "body": ""}


def _save_last_template(subject: str, body: str) -> None:
    try:
        LAST_TEMPLATE_PATH.write_text(
            json.dumps({"subject": subject or "", "body": body or ""})
        )
    except OSError:
        pass


_LAST_TEMPLATE = _load_last_template()

df = pd.read_csv(BASE_DIR / "bklyn_rent_stabilized_buildings.csv")
df = df.dropna(subset=["LATITUDE", "LONGITUDE", "BLOCK", "LOT"])
df["BLOCK"] = df["BLOCK"].astype(int)
df["LOT"]   = df["LOT"].astype(int)
df["id"] = df["BLOCK"].astype(str) + "-" + df["LOT"].astype(str)
df["address"] = (
    df["BUILDING_NO"].astype(str) + " " + df["STREET"].str.strip() + ", Brooklyn"
)
df["statuses"] = (
    df[["STATUS1", "STATUS2", "STATUS3"]]
    .fillna("")
    .apply(lambda r: ", ".join(v for v in r if str(v).strip()), axis=1)
)
df["zip"] = df["ZIP"].astype(str)

ALL_ZIPS = sorted(df["zip"].unique())

# Initial value for the "Max parallel landlord lookups" slider in the
# settings modal. The slider persists to localStorage, so the user's choice
# overrides this on subsequent loads — this just sets the first-load default
# and the floor when the slider's value is missing.
DEFAULT_MAX_CONCURRENT_LOOKUPS = 3

ALL_FEATURES = [
    {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [float(row["LONGITUDE"]), float(row["LATITUDE"])],
        },
        "properties": {
            "id": row["id"],
            "address": row["address"],
            "zip": row["zip"],
            "block": str(row["BLOCK"]),
            "lot": str(row["LOT"]),
            "statuses": row["statuses"],
        },
    }
    for _, row in df.iterrows()
]


def filter_geojson(selected_zips=None, bbox=None):
    features = ALL_FEATURES
    if selected_zips:
        zip_set = set(selected_zips)
        features = [f for f in features if f["properties"]["zip"] in zip_set]
    if bbox:
        features = [
            f
            for f in features
            if (
                bbox["min_lat"] <= f["geometry"]["coordinates"][1] <= bbox["max_lat"]
                and bbox["min_lng"] <= f["geometry"]["coordinates"][0] <= bbox["max_lng"]
            )
        ]
    return {"type": "FeatureCollection", "features": features}


def load_geojson(path):
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {"type": "FeatureCollection", "features": []}


SUBWAY_GEOJSON = load_geojson(DATA_DIR / "subway-stops.geojson")
ROUTES_GEOJSON = load_geojson(DATA_DIR / "subway-routes.geojson")
ZIP_GEOJSON    = load_geojson(DATA_DIR / "brooklyn-zips.geojson")
EMPTY_GEOJSON  = {"type": "FeatureCollection", "features": []}

# ── JavaScript for custom markers ─────────────────────────────────────────
building_marker = assign("""
function(feature, latlng, context) {
    if (!window._mainLeafletMap) window._mainLeafletMap = context.map;
    return L.circleMarker(latlng, {
        radius: 5,
        fillColor: '#e53935',
        color: '#ff8a80',
        weight: 0.5,
        fillOpacity: 0.85,
    });
}
""")

selected_building_marker = assign("""
function(feature, latlng, context) {
    return L.circleMarker(latlng, {
        radius: 11,
        fillColor: '#FFD700',
        color: '#fff',
        weight: 2.5,
        fillOpacity: 1,
    });
}
""")

subway_marker = assign("""
function(feature, latlng, context) {
    const colors = {
        '1':'#EE352E','2':'#EE352E','3':'#EE352E',
        '4':'#00933C','5':'#00933C','6':'#00933C',
        '7':'#B933AD',
        'A':'#0039A6','C':'#0039A6','E':'#0039A6',
        'B':'#FF6319','D':'#FF6319','F':'#FF6319','M':'#FF6319',
        'G':'#6CBE45',
        'J':'#996633','Z':'#996633',
        'L':'#A7A9AC',
        'N':'#FCCC0A','Q':'#FCCC0A','R':'#FCCC0A','W':'#FCCC0A',
        'S':'#808183','GS':'#808183','FS':'#808183',
    };
    const darkText = new Set(['N','Q','R','W']);
    const lines = feature.properties.lines || [];
    if (!lines.length) {
        return L.circleMarker(latlng, {
            radius: 10, fillColor: '#808183', color: '#fff', weight: 1.5, fillOpacity: 1
        });
    }
    const bubbles = lines.map(line => {
        const bg = colors[line] || '#808183';
        const fg = darkText.has(line) ? '#000' : '#fff';
        return '<span style="display:inline-flex;align-items:center;justify-content:center;'
             + 'width:20px;height:20px;border-radius:50%;background:' + bg + ';color:' + fg + ';'
             + 'font-size:11px;font-weight:bold;font-family:Arial,sans-serif;'
             + 'border:1.5px solid rgba(255,255,255,0.85);'
             + 'box-shadow:0 1px 4px rgba(0,0,0,0.55);flex-shrink:0;">' + line + '</span>';
    });
    const perRow = Math.min(lines.length, 4);
    const rows = Math.ceil(lines.length / perRow);
    const w = perRow * 22;
    const h = rows * 22;
    const html = '<div style="display:flex;flex-wrap:wrap;gap:2px;width:' + w + 'px;">'
               + bubbles.join('') + '</div>';
    return L.marker(latlng, {
        icon: L.divIcon({
            html: html,
            className: '',
            iconSize: [w, h],
            iconAnchor: [w / 2, h / 2],
            popupAnchor: [0, -h / 2 - 4],
        })
    });
}
""")

subway_tooltip_fn = assign("""
function(feature, layer, context) {
    const name = feature.properties.name || '';
    const lines = (feature.properties.lines || []).join(' ');
    if (name) layer.bindTooltip(name + (lines ? ' (' + lines + ')' : ''),
        {sticky: true, className: 'subway-tooltip'});
}
""")

route_style_fn = assign("""
function(feature, context) {
    const colors = {
        '1':'#EE352E','2':'#EE352E','3':'#EE352E',
        '4':'#00933C','5':'#00933C','6':'#00933C',
        '7':'#B933AD',
        'A':'#0039A6','C':'#0039A6','E':'#0039A6',
        'B':'#FF6319','D':'#FF6319','F':'#FF6319','M':'#FF6319',
        'G':'#6CBE45',
        'J':'#996633','Z':'#996633',
        'L':'#A7A9AC',
        'N':'#FCCC0A','Q':'#FCCC0A','R':'#FCCC0A','W':'#FCCC0A',
        'S':'#808183',
    };
    const line = feature.properties.primary_line || 'S';
    return {color: colors[line] || '#808183', weight: 3, opacity: 0.75, fillOpacity: 0};
}
""")

zip_style_fn = assign("""
function(feature, context) {
    if (!window._mainLeafletMap) window._mainLeafletMap = context.map;
    return {color: '#4a6fa5', weight: 1.5, fill: false, dashArray: '4'};
}
""")

zip_tooltip_fn = assign("""
function(feature, layer, context) {
    const code = feature.properties.modzcta
              || feature.properties.ZIPCODE
              || feature.properties.zipcode
              || feature.properties.ZIP || '';
    layer.bindTooltip(String(code), {sticky: true, className: 'zip-tooltip'});
}
""")

# ── Layout pieces ─────────────────────────────────────────────────────────
NAVBAR = dbc.Navbar(
    dbc.Container(
        [
            dbc.NavbarBrand("House Me Daddy", className="fw-bold me-3"),
            html.Span(id="nav-count", className="text-secondary small me-auto"),
            dbc.Button(
                "⚙",
                id="settings-btn",
                color="secondary",
                outline=True,
                size="sm",
                n_clicks=0,
                className="ms-2",
                style={"fontSize": "15px", "lineHeight": "1", "padding": "2px 10px"},
            ),
        ],
        fluid=True,
        className="d-flex align-items-center",
    ),
    dark=True,
    color="dark",
    className="border-bottom border-secondary",
    style={"height": "50px", "minHeight": "50px"},
)

SIDEBAR_SHORTLIST_VIEW = html.Div(
    [
        # ── Selected buildings ─────────────────────────────────────────
        html.Div(
            [
                html.Div(
                    [
                        html.Span("Selected", className="fw-bold small"),
                        html.Span(
                            id="selected-count",
                            className="badge bg-warning text-dark rounded-pill ms-2",
                            style={"fontSize": "10px"},
                        ),
                        html.Span(
                            id="selection-status",
                            className="text-secondary small ms-2",
                            style={"fontSize": "10px"},
                        ),
                    ],
                    className="d-flex align-items-center mb-2",
                ),
                html.Div(
                    [
                        dbc.Button(
                            "Lookup all",
                            id="bulk-lookup-btn",
                            size="sm",
                            color="primary",
                            outline=True,
                            className="flex-fill",
                            style={"fontSize": "11px"},
                        ),
                        dbc.Button(
                            "+ Shortlist",
                            id="bulk-shortlist-btn",
                            size="sm",
                            color="success",
                            outline=True,
                            className="flex-fill",
                            style={"fontSize": "11px"},
                        ),
                        dbc.Button(
                            "Clear",
                            id="clear-selection-btn",
                            size="sm",
                            color="warning",
                            outline=True,
                            className="flex-fill",
                            style={"fontSize": "11px"},
                        ),
                    ],
                    id="bulk-actions",
                    className="d-flex gap-1 mb-2",
                    style={"display": "none"},
                ),
                html.Div(
                    id="selected-list",
                    style={"maxHeight": "300px", "overflowY": "auto"},
                ),
                html.Div(
                    className="small text-secondary mt-2",
                    style={"opacity": "0.5", "fontSize": "10px"},
                    children="Drag area · Click stop+radius · Click marker to add/remove",
                ),
            ],
            className="p-3 border-bottom border-secondary",
        ),
        # ── Discovered owners (every primary landlord across all sessions) ──
        html.Div(
            [
                html.Div(
                    [
                        html.Span("Discovered owners", className="fw-bold small"),
                        html.Span(
                            id="all-owners-count",
                            className="badge bg-secondary rounded-pill ms-2",
                            style={"fontSize": "10px"},
                        ),
                        dbc.Button(
                            "Show all",
                            id="all-owners-toggle-btn",
                            size="sm",
                            color="link",
                            n_clicks=0,
                            className="ms-auto p-0",
                            style={"fontSize": "10px", "display": "none"},
                        ),
                    ],
                    className="d-flex align-items-center mb-2",
                ),
                html.Div(
                    id="all-owners-items",
                    children=html.P("No lookups yet.", className="small text-secondary"),
                ),
            ],
            className="p-3 border-bottom border-secondary",
        ),
        # ── Shortlisted owners ────────────────────────────────────────
        html.Div(
            [
                html.Div(
                    [
                        html.Span("Shortlisted owners", className="fw-bold small"),
                        html.Span(
                            id="shortlist-count",
                            className="badge bg-primary rounded-pill ms-2",
                            style={"fontSize": "10px"},
                        ),
                    ],
                    className="d-flex align-items-center mb-2",
                ),
                html.Div(
                    id="shortlist-items",
                    children=html.P("No owners yet.", className="small text-secondary"),
                ),
                dbc.Button(
                    "Draft Email →",
                    id="open-email-btn",
                    color="success",
                    size="sm",
                    outline=True,
                    className="w-100 mt-2",
                ),
            ],
            className="p-3",
        ),
    ],
    id="sidebar-shortlist-view",
    style={"height": "100%", "overflowY": "auto"},
)

SIDEBAR_DRAFT_VIEW = html.Div(
    [
        # ── Header (fixed) ────────────────────────────────────────────
        html.Div(
            [
                dbc.Button(
                    "← Back",
                    id="draft-back-btn",
                    color="link",
                    size="sm",
                    className="p-0 text-secondary",
                    style={"fontSize": "12px", "textDecoration": "none"},
                ),
                html.Span(
                    "Draft Email",
                    className="fw-bold ms-2",
                    style={"fontSize": "13px"},
                ),
            ],
            className="d-flex align-items-center p-3 border-bottom border-secondary",
            style={"flex": "0 0 auto"},
        ),
        # ── Editor + LLM controls (fixed) ─────────────────────────────
        html.Div(
            [
                html.Label("Subject", className="small fw-bold mb-1"),
                dbc.Input(
                    id="draft-subject",
                    type="text",
                    value=_LAST_TEMPLATE["subject"],
                    placeholder="Apartment inquiry — {{property_label}}",
                    size="sm",
                    className="mb-2",
                    debounce=True,
                    persistence=True,
                    persistence_type="local",
                ),
                html.Label("Body", className="small fw-bold mb-1"),
                dcc.Textarea(
                    id="draft-body",
                    value=_LAST_TEMPLATE["body"],
                    placeholder="Hi {{owner_name}},\n\nI'm looking for a rent-stabilized apartment...",
                    style={
                        "width": "100%",
                        "height": "140px",
                        "fontFamily": "monospace",
                        "fontSize": "12px",
                    },
                    className="form-control mb-2",
                    persistence=True,
                    persistence_type="local",
                ),
                html.Div(
                    [
                        html.Span("Variables: ", className="text-secondary"),
                        *[
                            html.Code(
                                "{{" + v + "}}",
                                className="me-1",
                                style={"fontSize": "10px"},
                            )
                            for v in AVAILABLE_TEMPLATE_VARS
                        ],
                    ],
                    className="small mb-2",
                ),
                html.Hr(className="my-2"),
                html.Label(
                    "Generate / refine with LLM",
                    className="small fw-bold mb-1",
                ),
                dcc.Textarea(
                    id="draft-prompt",
                    placeholder="e.g. make it warmer and more concise",
                    style={
                        "width": "100%",
                        "height": "50px",
                        "fontSize": "12px",
                    },
                    className="form-control mb-2",
                ),
                dcc.Loading(
                    id="draft-llm-loading",
                    type="circle",
                    color="#ffffff",
                    delay_show=400,
                    children=dbc.Button(
                        "Generate",
                        id="draft-llm-btn",
                        color="primary",
                        size="sm",
                        className="w-100 mb-1",
                    ),
                ),
                html.Div(
                    id="draft-llm-status",
                    className="small text-danger",
                    style={"fontSize": "11px"},
                ),
            ],
            className="px-3 pt-3 pb-2 border-bottom border-secondary",
            style={"flex": "0 0 auto"},
        ),
        # ── Previews label (fixed) ────────────────────────────────────
        html.Div(
            "Filled previews (per owner)",
            className="fw-bold small px-3 pt-2 pb-1",
            style={"flex": "0 0 auto"},
        ),
        # ── Previews scroll area (flex-grow + scroll) ─────────────────
        html.Div(
            id="draft-previews",
            children=html.P(
                "Add owners to shortlist to see previews.",
                className="small text-secondary",
            ),
            style={
                "flex": "1 1 auto",
                "overflowY": "auto",
                "minHeight": "0",
                "padding": "0 1rem",
            },
        ),
        # ── Send button footer (fixed) ────────────────────────────────
        html.Div(
            [
                html.Small(
                    id="draft-send-summary",
                    className="text-secondary d-block mb-1",
                    style={"fontSize": "11px"},
                ),
                dcc.Loading(
                    id="draft-send-loading",
                    type="circle",
                    color="#ffffff",
                    delay_show=400,
                    children=dbc.Button(
                        "Send emails",
                        id="draft-send-btn",
                        color="danger",
                        size="sm",
                        className="w-100",
                    ),
                ),
                html.Div(id="draft-send-status", className="mt-2"),
            ],
            className="p-3 border-top border-secondary",
            style={"flex": "0 0 auto"},
        ),
    ],
    id="sidebar-draft-view",
    style={"display": "none"},
)

SIDEBAR = html.Div(
    [SIDEBAR_SHORTLIST_VIEW, SIDEBAR_DRAFT_VIEW],
    style={
        "width": "400px",
        "flexShrink": "0",
        "height": "100%",
        "background": "#12122a",
        "borderLeft": "1px solid #222",
    },
)

MAP_AREA = dl.Map(
    id="main-map",
    center=[40.6501, -73.9496],
    zoom=12,
    scrollWheelZoom=False,
    style={"height": "100%", "flex": "1"},
    children=[
        dl.TileLayer(
            url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
            attribution="© OpenStreetMap contributors © CARTO",
            maxZoom=19,
        ),
        dl.GeoJSON(
            id="buildings-layer",
            data=filter_geojson(),
            cluster=True,
            zoomToBoundsOnClick=True,
            superClusterOptions={"radius": 80, "maxZoom": 16},
            pointToLayer=building_marker,
        ),
        dl.GeoJSON(
            id="selected-building-layer",
            data=EMPTY_GEOJSON,
            pointToLayer=selected_building_marker,
        ),
        dl.LayerGroup(id="selection-shapes", children=[]),
        dl.GeoJSON(
            id="subway-routes-layer",
            data=EMPTY_GEOJSON,
            style=route_style_fn,
        ),
        dl.GeoJSON(
            id="subway-layer",
            data=EMPTY_GEOJSON,
            pointToLayer=subway_marker,
            onEachFeature=subway_tooltip_fn,
        ),
        dl.GeoJSON(
            id="zip-layer",
            data=ZIP_GEOJSON,
            style=zip_style_fn,
            onEachFeature=zip_tooltip_fn,
        ),
    ],
)

BUILDING_DETAIL_MODAL = dbc.Modal(
    [
        dbc.ModalHeader(dbc.ModalTitle(id="building-detail-title")),
        dbc.ModalBody(
            [
                html.Div(id="building-detail-body"),
                html.Div(
                    [
                        dbc.Button(
                            "Lookup Owner",
                            id="modal-lookup-btn",
                            size="sm",
                            color="primary",
                            n_clicks=0,
                            style={"display": "none"},
                        ),
                        dbc.Button(
                            "+ Add to shortlist",
                            id="modal-shortlist-btn",
                            size="sm",
                            color="success",
                            outline=True,
                            n_clicks=0,
                            style={"display": "none"},
                        ),
                    ],
                    className="d-flex gap-2 mt-2",
                ),
            ]
        ),
    ],
    id="building-detail-modal",
    is_open=False,
    size="md",
)

JOB_TRACKER_WIDTH = 600         # expanded panel width
JOB_TRACKER_MIN_WIDTH = 36      # minimized strip width (chevron only)
SIDEBAR_WIDTH_PX = 280          # must match SIDEBAR's width style

# Slides in absolutely-positioned to the LEFT of the sidebar when a job is
# active. Minimizable to a thin chevron strip via `job-tracker-toggle-btn`.
JOB_TRACKER = html.Div(
    id="job-tracker",
    style={
        "position": "absolute",
        "right": f"{SIDEBAR_WIDTH_PX}px",
        "top": "0",
        "bottom": "0",
        "width": f"{JOB_TRACKER_WIDTH}px",
        "background": "rgba(18,18,42,0.97)",
        "borderLeft": "1px solid #2a2a4a",
        "borderRight": "1px solid #2a2a4a",
        "boxShadow": "-4px 0 12px rgba(0,0,0,0.4)",
        "zIndex": 500,
        "display": "none",   # render_job_tracker shows it once a job starts
        "flexDirection": "column",
    },
    children=[
        # Always-visible chevron header.
        html.Div(
            dbc.Button(
                "◀",
                id="job-tracker-toggle-btn",
                size="sm",
                color="secondary",
                outline=True,
                n_clicks=0,
                style={"fontSize": "11px", "padding": "1px 8px", "minWidth": "28px"},
                title="Minimize / expand",
            ),
            className="d-flex justify-content-end p-1",
            style={"borderBottom": "1px solid #2a2a4a", "flexShrink": "0"},
        ),
        # Expanded-only body — hidden when the panel is collapsed.
        html.Div(
            id="job-tracker-body",
            style={
                "flex": "1",
                "display": "flex",
                "flexDirection": "column",
                "minHeight": "0",
            },
            children=[
                html.Div(
                    [
                        html.Span(
                            id="job-tracker-count",
                            className="small text-secondary",
                        ),
                        dbc.Button(
                            "Cancel queued",
                            id="cancel-queue-btn",
                            size="sm",
                            color="danger",
                            outline=True,
                            n_clicks=0,
                            style={
                                "fontSize": "10px",
                                "padding": "1px 8px",
                                "display": "none",
                            },
                        ),
                    ],
                    className="d-flex justify-content-between align-items-center px-2 py-1",
                    style={"flexShrink": "0"},
                ),
                dbc.Progress(
                    id="task-progress-fill",
                    value=0,
                    striped=True,
                    animated=True,
                    style={
                        "height": "4px",
                        "borderRadius": "0",
                        "flexShrink": "0",
                    },
                ),
                html.Div(
                    id="job-tracker-items",
                    style={
                        "flex": "1",
                        "overflowY": "auto",
                        "padding": "8px",
                        "minHeight": "0",
                    },
                ),
            ],
        ),
    ],
)

LOOKUP_TOAST = dbc.Toast(
    [
        html.Div(id="lookup-toast-msg", className="small mb-2"),
        dbc.Button(
            "View details →",
            id="lookup-toast-view-btn",
            size="sm",
            color="info",
            className="w-100",
            n_clicks=0,
        ),
    ],
    id="lookup-toast",
    header="Owner details found",
    icon="success",
    duration=10000,
    is_open=False,
    dismissable=True,
    style={
        "position": "fixed",
        "bottom": 20,
        "left": 20,
        "minWidth": 320,
        "zIndex": 9999,
    },
)

SETTINGS_MODAL = dbc.Modal(
    [
        dbc.ModalHeader(dbc.ModalTitle("Settings")),
        dbc.ModalBody(
            [
                html.Div(
                    "Map filters",
                    className="text-uppercase text-secondary fw-bold small mb-2",
                    style={"letterSpacing": "0.08em"},
                ),
                dcc.Dropdown(
                    id="zip-filter",
                    options=[{"label": z, "value": z} for z in ALL_ZIPS],
                    multi=True,
                    placeholder="Filter by ZIP code…",
                    style={"fontSize": "13px"},
                    className="mb-2",
                ),
                html.Div(id="building-count", className="small text-secondary mb-3"),
                dbc.Switch(
                    id="subway-toggle",
                    label="Subway stops",
                    value=True,
                    className="mb-1 small",
                ),
                dbc.Switch(
                    id="zip-toggle",
                    label="ZIP boundaries",
                    value=False,
                    className="mb-1 small",
                ),
                html.Div(
                    id="radius-control",
                    children=[
                        html.Hr(className="my-2"),
                        html.Div(
                            id="radius-label",
                            className="small text-info mb-1",
                            children="Subway-stop radius (click a stop to use)",
                        ),
                        dcc.Slider(
                            id="radius-slider",
                            min=0.25,
                            max=2.0,
                            step=0.25,
                            value=0.5,
                            marks={0.25: "0.25", 0.5: "0.5", 1.0: "1", 2.0: "2"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Div("km radius", className="small text-secondary text-center"),
                    ],
                ),
                html.Hr(className="my-3"),
                html.Div(
                    "Lookups",
                    className="text-uppercase text-secondary fw-bold small mb-2",
                    style={"letterSpacing": "0.08em"},
                ),
                html.Div(
                    "Max parallel landlord lookups",
                    className="small text-info mb-1",
                ),
                dbc.Input(
                    id="max-parallel-input",
                    type="number",
                    min=1,
                    max=20,
                    step=1,
                    value=DEFAULT_MAX_CONCURRENT_LOOKUPS,
                    persistence=True,
                    persistence_type="local",
                    style={"maxWidth": "120px"},
                ),
                html.Div(
                    "Agents share one Chromium window and one ContactOut page; "
                    "they serialize at the page but reason in parallel.",
                    className="small text-secondary mt-1",
                ),
            ]
        ),
    ],
    id="settings-modal",
    is_open=False,
    size="md",
)

# ── App init ───────────────────────────────────────────────────────────────
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.DARKLY],
    suppress_callback_exceptions=True,
    title="Brooklyn RSB Finder",
)

app.layout = html.Div(
    style={"display": "flex", "flexDirection": "column", "height": "100vh", "overflow": "hidden"},
    children=[
        dcc.Store(id="shortlist-store", storage_type="local"),
        dcc.Store(id="sidebar-mode-store", storage_type="session", data="shortlist"),
        dcc.Store(id="llm-status-store", data=None),
        dcc.Store(id="email-sent-signal", data=0),
        dcc.Store(id="bbox-store", data=None),
        dcc.Store(id="subway-selection-store", data=None),
        dcc.Store(id="modal-building-id", data=None),
        dcc.Store(id="lookup-status", storage_type="session", data={}),
        dcc.Store(id="lookup-toast-store"),
        dcc.Store(id="task-batch", data={"started": 0, "done": 0}),
        dcc.Store(id="selected-buildings", storage_type="session", data=[]),
        dcc.Store(id="lookup-queue", data=[]),
        dcc.Store(id="consecutive-timeouts", data=0),
        dcc.Store(id="job-bbls", data=[]),
        dcc.Store(id="job-tracker-minimized", data=False),
        dcc.Interval(id="agent-log-poll", interval=1000, n_intervals=0, disabled=True),
        dcc.Store(id="all-owners-expanded", data=False),
        dcc.Store(id="coowners-expanded-keys", data=[]),
        dcc.Interval(id="sel-poll", interval=150, n_intervals=0),
        dcc.Interval(
            id="lookup-poll",
            interval=2000,
            n_intervals=0,
            disabled=True,
        ),
        dcc.Interval(
            id="task-cleanup",
            interval=2000,
            n_intervals=0,
            disabled=True,
        ),
        # Fires once ~500ms after the page loads, then never again. Prunes
        # zombie `queued`/`loading` entries that survived in session storage
        # after a hard app restart (the agent threads they referenced are
        # dead) so they don't block new lookups.
        dcc.Interval(
            id="startup-heal",
            interval=500,
            n_intervals=0,
            max_intervals=1,
        ),
        NAVBAR,
        html.Div(
            [MAP_AREA, JOB_TRACKER, SIDEBAR],
            style={
                "display": "flex", "flex": "1", "minHeight": "0",
                "overflow": "hidden", "position": "relative",
            },
        ),
        BUILDING_DETAIL_MODAL,
        SETTINGS_MODAL,
        LOOKUP_TOAST,
    ]
)

# ── Callbacks ──────────────────────────────────────────────────────────────


SELECTED_CARD_LIMIT = 200


def _bbl_for(props: dict) -> str:
    try:
        return f'3-{int(float(props.get("block", ""))):05d}-{int(float(props.get("lot", ""))):04d}'
    except (ValueError, TypeError):
        return ""


def _lookup_button(bbl: str, status: str):
    btn_style = {"fontSize": "11px", "padding": "2px 8px"}
    common = dict(
        id={"type": "lookup-btn", "id": bbl},
        size="sm",
        className="flex-shrink-0",
        style=btn_style,
    )
    if status == "queued":
        return dbc.Button(
            "Queued", color="secondary", outline=True, disabled=True, **common,
        )
    if status == "loading":
        return dbc.Button(
            [
                dbc.Spinner(
                    size="sm",
                    spinner_style={"width": "0.8rem", "height": "0.8rem"},
                ),
                " Searching",
            ],
            color="info",
            outline=True,
            disabled=True,
            **common,
        )
    if status == "done":
        return dbc.Button("✓ Found", color="success", disabled=True, **common)
    if status == "timeout":
        # Clickable so user can re-queue.
        return dbc.Button("⚠ Retry", color="warning", **common)
    return dbc.Button("Lookup", color="primary", outline=True, **common)


def _job_tracker_card(props, bbl: str, status: str):
    """Compact card for a building inside the job tracker. Mirrors the look of
    a `_selected_cards` row (dark background + border, address text, status
    button)."""
    return html.Div(
        [
            html.Div(
                props["address"],
                className="small text-truncate flex-grow-1",
                style={"minWidth": 0},
            ),
            _lookup_button(bbl, status),
        ],
        className="mb-1 p-2 rounded d-flex align-items-center gap-2",
        style={"background": "#1a1a2e", "border": "1px solid #2a2a4a"},
    )


# Matches any structured log line agent_runner emits — tool calls, model
# thinking, assistant prose, user-side text, and tool results. The first
# group is the timestamp; the second is the bracketed event tag; the third
# is the rest of the line.
_STRUCTURED_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(tool|tool_result|thinking|assistant|user|runner)\] (.*)$"
)

# Renders the model line (`[runner] model=<id>`) once at the top of the
# inline panel — the rest of the [runner] lines are runner bookkeeping
# (start/done/lock waits) that aren't useful inline.
_RUNNER_MODEL_RE = re.compile(r"^model=(\S+)$")


def _recent_agent_events(bbl: str, n: int = 5) -> list[tuple[str, str, str]]:
    """Read ``.agent_logs/<bbl>.log`` and return the most recent ``n``
    structured events as ``(time, kind_label, payload)`` tuples — the inline
    log panel renders each piece with its own color.

    ``kind_label`` is one of: ``tool``, ``→`` (tool_result), ``think``,
    ``say``, ``user``, ``model``.
    """
    log = agent_runner.read_log(bbl)
    if not log:
        return []

    events: list[tuple[str, str, str]] = []
    for line in log.splitlines():
        m = _STRUCTURED_LOG_RE.match(line)
        if not m:
            continue
        ts, kind, payload = m.groups()
        time_only = ts.split(" ", 1)[1]

        if kind == "tool":
            # [tool] name=<full> input=<json>
            tm = re.match(r"name=(\S+) input=(.*)", payload)
            if not tm:
                continue
            full_name, input_json = tm.groups()
            short = full_name.rsplit("__", 1)[-1]
            try:
                args = json.loads(input_json)
                args_text = ", ".join(f"{k}={v!r}" for k, v in args.items())
            except Exception:
                args_text = input_json
            events.append((time_only, "tool", f"{short}({args_text})"))

        elif kind == "tool_result":
            events.append((time_only, "→", payload[:200]))

        elif kind == "thinking":
            events.append((time_only, "think", payload[:200]))

        elif kind == "assistant":
            events.append((time_only, "say", payload[:200]))

        elif kind == "user":
            events.append((time_only, "user", payload[:200]))

        elif kind == "runner":
            mm = _RUNNER_MODEL_RE.match(payload)
            if mm:
                events.append((time_only, "model", mm.group(1)))
            # All other [runner] lines (start/done/lock) are skipped here —
            # they're surfaced by the main tracker UI state, not the inline log.

    return events[-n:]


def _job_tracker_section_header(label: str, count: int):
    """Section divider inside the job tracker — `Running · 3`, etc."""
    return html.Div(
        f"{label} · {count}",
        className="small fw-bold text-secondary mb-1",
        style={
            "marginTop": "8px",
            "paddingBottom": "2px",
            "borderBottom": "1px solid #2a2a4a",
            "textTransform": "uppercase",
            "letterSpacing": "0.04em",
            "fontSize": "10px",
        },
    )


_LOG_TS_COLOR = "#7aa2f7"     # blue — timestamp
_LOG_KIND_COLOR = "#e0af68"   # yellow — message-type label


def _inline_log_panel(bbl: str):
    """The small auto-scrolling log block that renders directly under the
    currently-loading card. Capped at 5 entries — `_recent_agent_events`
    already returns the trailing 5; CSS height also clamps it so the panel
    can't push other cards off-screen.

    Each line is rendered as three colored spans (timestamp blue, kind
    label yellow, payload default) so the eye can jump straight to the
    event type."""
    events = _recent_agent_events(bbl, n=5)
    if not events:
        children: list = ["(waiting for first agent event…)"]
    else:
        children = []
        for i, (ts, kind, payload) in enumerate(events):
            if i > 0:
                children.append("\n")
            children.append(html.Span(ts, style={"color": _LOG_TS_COLOR}))
            children.append("  ")
            # Pad to 6 chars so payloads align across rows regardless of which
            # kind label this is ("tool", "→", "think", "say", "user", "model").
            children.append(
                html.Span(f"{kind:<6}", style={"color": _LOG_KIND_COLOR})
            )
            children.append("  ")
            children.append(payload)
    return html.Pre(
        children,
        className="mb-2 p-2 rounded",
        style={
            "background": "#0d0d1f",
            "border": "1px solid #2a2a4a",
            "borderTop": "none",
            "borderRadius": "0 0 4px 4px",
            "color": "#b8b8d0",
            "fontFamily": "ui-monospace, SFMono-Regular, monospace",
            "fontSize": "10px",
            # Long entries wrap so they flow vertically instead of clipping
            # horizontally — the panel scrolls down for older lines.
            "whiteSpace": "pre-wrap",
            "wordBreak": "break-word",
            "margin": "-4px 0 8px 0",  # snug under the card above it
            "maxHeight": "260px",
            "overflowY": "auto",
            "overflowX": "hidden",
        },
    )


def _selected_cards(building_ids, lookup_status=None):
    if not building_ids:
        return html.P(
            "Nothing selected. Drag the map or click a building marker.",
            className="small text-secondary mb-0",
        )
    status_map = lookup_status or {}
    cards = []
    for bid in building_ids[:SELECTED_CARD_LIMIT]:
        props = FEATURES_BY_ID.get(bid)
        if not props:
            continue
        bbl = _bbl_for(props)
        st = (status_map.get(bbl) or {}).get("status", "idle")
        cards.append(
            html.Div(
                [
                    html.Div(
                        props["address"],
                        id={"type": "building-card", "id": props["id"]},
                        n_clicks=0,
                        className="small text-truncate flex-grow-1",
                        style={"cursor": "pointer", "minWidth": 0},
                    ),
                    _lookup_button(bbl, st),
                    dbc.Button(
                        "×",
                        id={"type": "deselect-btn", "id": props["id"]},
                        size="sm",
                        color="danger",
                        outline=True,
                        style={"fontSize": "10px", "padding": "1px 6px", "flexShrink": "0"},
                    ),
                ],
                className="mb-1 p-2 rounded d-flex align-items-center gap-2",
                style={
                    "background": "#1a1a2e",
                    "border": "1px solid #2a2a4a",
                },
            )
        )
    if len(building_ids) > SELECTED_CARD_LIMIT:
        cards.append(
            html.Div(
                f"Showing first {SELECTED_CARD_LIMIT:,} of {len(building_ids):,}",
                className="small text-secondary mt-2",
            )
        )
    return cards


@callback(
    Output("buildings-layer", "data"),
    Output("building-count", "children"),
    Output("nav-count", "children"),
    Input("zip-filter", "value"),
    Input("selected-buildings", "data"),
)
def update_buildings(selected_zips, selected_ids):
    # While a selection is active, hide all the unselected red markers/clusters
    # so only the highlighted ones (selected-building-layer) remain on the map.
    if selected_ids:
        n = len(selected_ids)
        label = f"{n:,} selected"
        return EMPTY_GEOJSON, label, label
    geojson = filter_geojson(selected_zips)
    n = len(geojson["features"])
    total = len(ALL_FEATURES)
    label = f"{n:,} of {total:,} buildings"
    return geojson, label, label


@callback(
    Output("selected-list", "children"),
    Output("selected-count", "children"),
    Output("bulk-actions", "style"),
    Input("selected-buildings", "data"),
    Input("lookup-status", "data"),
)
def render_selected_list(selected_ids, lookup_status):
    selected_ids = selected_ids or []
    cards = _selected_cards(selected_ids, lookup_status)
    count_label = str(len(selected_ids)) if selected_ids else ""
    actions_style = (
        {"display": "flex"} if selected_ids else {"display": "none"}
    )
    return cards, count_label, actions_style


SUBWAY_STOPS_MIN_ZOOM = 14   # hide the station markers below this zoom; lines stay


@callback(
    Output("subway-layer", "data"),
    Output("subway-routes-layer", "data"),
    Input("subway-toggle", "value"),
    Input("main-map", "zoom"),
)
def toggle_subway(show, zoom):
    if not show:
        return EMPTY_GEOJSON, EMPTY_GEOJSON
    stops = SUBWAY_GEOJSON if (zoom or 0) >= SUBWAY_STOPS_MIN_ZOOM else EMPTY_GEOJSON
    return stops, ROUTES_GEOJSON


@callback(
    Output("zip-layer", "data"),
    Input("zip-toggle", "value"),
)
def toggle_zip(show):
    return ZIP_GEOJSON if show else EMPTY_GEOJSON


# Lookup for fast card-click → building props resolution
FEATURES_BY_ID  = {f["properties"]["id"]: f["properties"] for f in ALL_FEATURES}
FEATURES_BY_BBL = {
    _bbl_for(f["properties"]): f["properties"]
    for f in ALL_FEATURES
    if _bbl_for(f["properties"])
}


def _building_ids_in_bbox(bbox):
    if not bbox:
        return []
    return [
        f["properties"]["id"]
        for f in ALL_FEATURES
        if (
            bbox["min_lat"] <= f["geometry"]["coordinates"][1] <= bbox["max_lat"]
            and bbox["min_lng"] <= f["geometry"]["coordinates"][0] <= bbox["max_lng"]
        )
    ]


def _building_ids_in_radius(lat0, lng0, radius_km):
    r_m = (radius_km or 0.5) * 1000
    m_lat = 111320.0
    m_lng = 111320.0 * math.cos(math.radians(lat0))
    out = []
    for f in ALL_FEATURES:
        lng, lat = f["geometry"]["coordinates"]
        if math.sqrt(((lat - lat0) * m_lat) ** 2 + ((lng - lng0) * m_lng) ** 2) <= r_m:
            out.append(f["properties"]["id"])
    return out


def _primary_landlord(building_data):
    """Pick the first non-empty landlord dict from an unwrapped agent payload."""
    if not isinstance(building_data, dict):
        return None
    landlords = (building_data.get("landlords") or [])
    for ll in landlords:
        if isinstance(ll, dict) and ll.get("name"):
            return ll
    # Fallback to first corporate entity if no human landlord found
    for ent in (building_data.get("corporate_entities") or []):
        if isinstance(ent, dict) and ent.get("name"):
            return ent
    return None


def _owner_key(name):
    return (name or "").strip().casefold()


def _group_owners_from_buildings(building_ids, lookup_status):
    """Walk building_ids → for each looked-up building, take the primary
    landlord → group into {owner_key: {name, phone, email, office, role,
    fetched_at, buildings: [{bbl, address}, ...]}}. Returns a list sorted by
    descending fetched_at (most-recently-fetched owner first), with name as
    a tiebreaker."""
    fetched_at_by_name = agent_cache.get_landlord_fetched_at_by_name()
    grouped = {}
    for bid in building_ids:
        props = FEATURES_BY_ID.get(bid)
        if not props:
            continue
        bbl = _bbl_for(props)
        entry = (lookup_status or {}).get(bbl) or {}
        if entry.get("status") != "done" or not entry.get("data"):
            continue
        unwrapped = _unwrap_agent_data(entry["data"])
        owner = _primary_landlord(unwrapped)
        if not owner:
            continue
        key = _owner_key(owner.get("name"))
        if key not in grouped:
            grouped[key] = {
                "owner_key":  key,
                "name":       owner.get("name"),
                "phone":      owner.get("phone"),
                "email":      owner.get("email") or owner.get("email_inferred"),
                "office":     owner.get("office") or owner.get("company"),
                "role":       owner.get("role"),
                "occupation": owner.get("occupation"),
                "fetched_at": fetched_at_by_name.get(key, 0),
                "buildings":  [],
            }
        if not any(b["bbl"] == bbl for b in grouped[key]["buildings"]):
            grouped[key]["buildings"].append({
                "bbl":     bbl,
                "address": props.get("address"),
                "zip":     props.get("zip"),
                "block":   props.get("block"),
                "lot":     props.get("lot"),
            })
    return sorted(
        grouped.values(),
        key=lambda o: (-(o.get("fetched_at") or 0), (o.get("name") or "").lower()),
    )


def _run_local_agent(bbl: str, props: dict) -> None:
    """Run the local landlord-lookup agent for `props['address']` and persist
    the translated result to the SQLite cache. Fire-and-forget; runs in a
    thread so the Dash callback returns immediately. On any exception the
    bbl is flagged in _LOOKUP_ERRORS so the polling callback surfaces a
    timeout/error toast. Per-run logs land in .agent_logs/<bbl>.log."""
    address = props.get("address")
    if not address:
        agent_runner.write_log(bbl, "[worker] abort: no address on props")
        _LOOKUP_ERRORS[bbl] = "error"
        return
    try:
        agent_json = run_landlord_lookup(address, bbl=bbl, headed=AGENT_HEADED)
        if not isinstance(agent_json, dict):
            agent_runner.write_log(
                bbl,
                f"[worker] fail: runner returned {type(agent_json).__name__} "
                f"(expected dict): {agent_json!r}",
            )
            _LOOKUP_ERRORS[bbl] = "error"
            return
        flat = translate_agent_result(agent_json, props)
        agent_cache.store_result(bbl, flat)
        agent_runner.write_log(bbl, "[worker] stored result, done")
    except agent_runner.ContactOutCloudflareError as e:
        agent_runner.write_log(
            bbl,
            f"[worker] ContactOut blocked ({e.reason}); surfacing error: {e}",
        )
        _LOOKUP_ERRORS[bbl] = {"reason": e.reason, "detail": str(e)}
    except Exception:
        import traceback
        agent_runner.write_log(
            bbl, f"[worker] exception:\n{traceback.format_exc()}"
        )
        _LOOKUP_ERRORS[bbl] = "error"


def _fetch_enrichment_result(building_id: str):
    """Returns the cached agent result dict if present, None if not yet
    written. Reads directly from the SQLite cache; no HTTP roundtrip."""
    return agent_cache.get_cached_result(building_id)


STAT_FIELDS = [
    ("Year built",               "year_built"),
    ("Units",                    "num_units"),
    ("Building class",           "building_class"),
    ("Buildings in portfolio",   "num_buildings_in_portfolio"),
    ("Open HPD violations",      "open_hpd_violations"),
    ("Total HPD violations",     "total_hpd_violations"),
    ("Last HPD registration",    "last_hpd_registration"),
    ("Rent stabilized units",    "rent_stabilized_units"),
    ("Rent stab note",           "rent_stab_note"),
    ("311 housing calls",        "311_housing_calls"),
    ("Eviction filings (2017+)", "eviction_filings_since_2017"),
    ("Evictions executed",       "evictions_executed"),
    ("Matched building",         "matched_building"),
]

# Top-level metadata keys the modal already shows in the Building section.
INTERNAL_FIELDS = {
    "address", "bbl", "block", "lot", "zip",
    "building_address", "search_address",
    "source", "wow_url",
}

# Dict-shaped fields the modal renders as bulleted name/value blocks.
# The legacy agent emitted recommended_outreach/useful_links; the local agent
# doesn't, so this list is empty for now (kept for future expansion).
NAMED_DICT_SECTIONS: list[tuple[str, str, bool]] = []


def _render_dict_rows(d, as_links=False):
    rows = []
    for k, v in d.items():
        label = k.replace("_", " ").capitalize()
        if as_links and isinstance(v, str) and v.startswith(("http://", "https://")):
            content = html.A(v, href=v, target="_blank", className="small")
        elif isinstance(v, (list, dict)):
            content = html.Pre(
                json.dumps(v, indent=2),
                className="small mb-0",
                style={"whiteSpace": "pre-wrap"},
            )
        else:
            content = html.Span("—" if v in (None, "") else str(v), className="small")
        rows.append(
            html.Div([html.Strong(f"{label}: "), content], className="small mb-1")
        )
    return html.Div(rows, className="mb-3 ms-2")


def _unwrap_agent_data(stored):
    """Agent sometimes wraps the payload under a 'result' key. Flatten it."""
    if isinstance(stored, dict) and isinstance(stored.get("result"), dict):
        merged = {k: v for k, v in stored.items() if k != "result"}
        merged.update(stored["result"])
        return merged
    return stored or {}


def _entity_cards(items):
    cards = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or "(unnamed)"
        rows = []
        for k, v in it.items():
            if k == "name" or v is None or v == "":
                continue
            label = k.replace("_", " ").capitalize()
            rows.append(
                html.Div(
                    [html.Strong(f"{label}: "), html.Span(str(v))],
                    className="small mb-1",
                )
            )
        cards.append(
            dbc.Card(
                dbc.CardBody(
                    [html.Div(name, className="fw-bold mb-2")] + rows,
                    style={"padding": "10px"},
                ),
                className="mb-2",
                color="dark",
            )
        )
    return cards


def _render_database_section(props: dict, bbl: str):
    block = str(props.get("block", "")).zfill(5)
    lot   = str(props.get("lot",   "")).zfill(4)
    acris = f"https://a836-acris.nyc.gov/DS/DocumentSearch/BBL?ms_bbl=3{block}{lot}"
    return html.Div(
        [
            html.H6(
                "Building",
                className="text-uppercase text-secondary small mb-2",
                style={"letterSpacing": "0.08em"},
            ),
            html.Div([html.Strong("BBL: "), bbl or "—"], className="mb-1"),
            html.Div([html.Strong("ZIP: "), props.get("zip", "")], className="mb-1"),
            html.Div(
                [html.Strong("Block / Lot: "),
                 f"{props.get('block')} / {props.get('lot')}"],
                className="mb-1",
            ),
            html.Div(
                [html.Strong("Statuses: "), props.get("statuses") or "—"],
                className="mb-2",
            ),
            html.A(
                "ACRIS lookup ↗",
                href=acris,
                target="_blank",
                className="btn btn-sm btn-outline-info",
            ),
        ]
    )


def _render_owner_section(stored):
    data = _unwrap_agent_data(stored)
    sections = [
        html.H6(
            "Owner / Landlords",
            className="text-uppercase text-secondary small mb-2",
            style={"letterSpacing": "0.08em"},
        ),
    ]

    stat_rows = [
        html.Div(
            [html.Strong(f"{label}: "), html.Span(str(data[key]))],
            className="mb-1",
        )
        for label, key in STAT_FIELDS
        if key in data and data[key] is not None
    ]
    if stat_rows:
        sections.append(html.Div(stat_rows, className="mb-3"))

    flags = data.get("flags")
    if isinstance(flags, list) and flags:
        sections.append(
            html.Div(
                [
                    html.Div(
                        "Flags",
                        className="small text-warning fw-bold mb-1",
                    ),
                    html.Ul(
                        [html.Li(str(f), className="small") for f in flags],
                        className="mb-3 ps-3",
                    ),
                ]
            )
        )

    landlords = data.get("landlords")
    if isinstance(landlords, list) and landlords:
        sections.append(
            html.Div("Landlords", className="small fw-bold mt-2 mb-1")
        )
        sections.extend(_entity_cards(landlords))

    for label, key, as_links in NAMED_DICT_SECTIONS:
        v = data.get(key)
        if isinstance(v, dict) and v:
            sections.append(
                html.Div(label, className="small fw-bold mt-2 mb-1")
            )
            sections.append(_render_dict_rows(v, as_links=as_links))

    handled = (
        {f[1] for f in STAT_FIELDS}
        | {"flags", "landlords", "portfolio"}
        | {k for _, k, _ in NAMED_DICT_SECTIONS}
    )
    remaining = {
        k: v for k, v in data.items()
        if k not in INTERNAL_FIELDS and k not in handled and v not in (None, "", [], {})
    }
    if remaining:
        sections.append(html.Hr(className="my-2"))
        sections.append(html.Div("Other fields", className="small fw-bold mb-1"))
        sections.append(_render_dict_rows(remaining))

    # Fallback if nothing rendered beyond the header
    if len(sections) == 1:
        sections.append(
            html.Pre(
                json.dumps(stored, indent=2),
                className="small mb-0",
                style={"whiteSpace": "pre-wrap"},
            )
        )

    return html.Div(sections)


def _render_modal_body(props: dict, bbl: str, lookup_status: dict):
    entry = (lookup_status or {}).get(bbl) or {}
    state = entry.get("status", "idle")
    if state == "done" and entry.get("data"):
        owner = _render_owner_section(entry["data"])
    elif state == "loading":
        owner = html.Div(
            [
                html.H6(
                    "Owner / Landlords",
                    className="text-uppercase text-secondary small mb-2",
                    style={"letterSpacing": "0.08em"},
                ),
                dbc.Spinner(
                    html.Div(
                        "Searching for owner…",
                        className="small text-secondary",
                    ),
                    size="sm",
                    color="info",
                ),
            ]
        )
    else:
        owner = html.Div(
            [
                html.H6(
                    "Owner / Landlords",
                    className="text-uppercase text-secondary small mb-2",
                    style={"letterSpacing": "0.08em"},
                ),
                html.P(
                    "Owner details haven't been fetched yet.",
                    className="small text-secondary mb-0",
                ),
            ]
        )
    return html.Div(
        [
            _render_database_section(props, bbl),
            html.Hr(),
            owner,
        ]
    )


@callback(
    Output("building-detail-modal", "is_open"),
    Output("modal-building-id", "data"),
    Input({"type": "building-card", "id": ALL}, "n_clicks"),
    Input("lookup-toast-view-btn", "n_clicks"),
    State("lookup-toast-store", "data"),
    prevent_initial_call=True,
)
def open_modal(card_clicks, toast_clicks, toast_data):
    trig = ctx.triggered_id
    if isinstance(trig, dict) and trig.get("type") == "building-card":
        if not any(n for n in (card_clicks or []) if n):
            return no_update, no_update
        return True, trig["id"]
    if trig == "lookup-toast-view-btn":
        if not toast_clicks:
            return no_update, no_update
        bbl = (toast_data or {}).get("bbl")
        props = FEATURES_BY_BBL.get(bbl)
        if not props:
            return no_update, no_update
        return True, props["id"]
    return no_update, no_update


@callback(
    Output("building-detail-title", "children"),
    Output("building-detail-body", "children"),
    Output("lookup-status", "data", allow_duplicate=True),
    Input("modal-building-id", "data"),
    Input("lookup-status", "data"),
    prevent_initial_call=True,
)
def render_modal(building_id, lookup_status):
    if not building_id:
        return no_update, no_update, no_update
    props = FEATURES_BY_ID.get(building_id)
    if not props:
        return "", "(building not found)", no_update
    bbl = _bbl_for(props)
    status_map = dict(lookup_status or {})
    entry = dict(status_map.get(bbl) or {})
    status_update = no_update
    # Backfill: status says done but the data wasn't persisted (older sessions).
    if entry.get("status") == "done" and not entry.get("data"):
        fetched = _fetch_enrichment_result(bbl)
        if fetched:
            entry["data"] = fetched
            status_map[bbl] = entry
            status_update = status_map
    # If we've never run lookup but the server already has a cached result
    # (e.g. someone else triggered it earlier), surface it on open.
    elif not entry:
        fetched = _fetch_enrichment_result(bbl)
        if fetched:
            entry = {"status": "done", "address": props.get("address"), "data": fetched}
            status_map[bbl] = entry
            status_update = status_map
    return (
        props.get("address", ""),
        _render_modal_body(props, bbl, status_map),
        status_update,
    )


# ── Per-card "Lookup Owner" flow ──────────────────────────────────────────


def _queue_lookup_for_bbl(bbl, status, batch, queue):
    """Common helper: enqueues a BBL for lookup. Returns
    (new_status, new_batch, new_queue) or (None, None, None) if it shouldn't
    enqueue (already queued/loading/done, or unknown BBL). The actual webhook
    fires later in process_lookup_queue when the worker is idle."""
    if (status.get(bbl) or {}).get("status") in ("queued", "loading", "done"):
        return None, None, None
    props = FEATURES_BY_BBL.get(bbl)
    if not props:
        return None, None, None
    new_status = dict(status)
    new_status[bbl] = {"status": "queued", "address": props.get("address")}
    new_batch = dict(batch or {})
    new_batch["started"] = new_batch.get("started", 0) + 1
    new_batch.setdefault("done", 0)
    new_queue = list(queue or [])
    if bbl not in new_queue:
        new_queue.append(bbl)
    return new_status, new_batch, new_queue


@callback(
    Output("lookup-status", "data"),
    Output("task-batch", "data"),
    Output("lookup-queue", "data"),
    Output("job-bbls", "data"),
    Input({"type": "lookup-btn", "id": ALL}, "n_clicks"),
    State("lookup-status", "data"),
    State("task-batch", "data"),
    State("lookup-queue", "data"),
    State("job-bbls", "data"),
    prevent_initial_call=True,
)
def start_lookup(n_clicks_list, status, batch, queue, job_bbls):
    if not any(n for n in (n_clicks_list or []) if n):
        return no_update, no_update, no_update, no_update
    trig = ctx.triggered_id
    if not trig or trig.get("type") != "lookup-btn":
        return no_update, no_update, no_update, no_update
    new_status, new_batch, new_queue = _queue_lookup_for_bbl(
        trig["id"], status or {}, batch or {}, queue or []
    )
    if new_status is None:
        return no_update, no_update, no_update, no_update
    bbls = list(job_bbls or [])
    if trig["id"] not in bbls:
        bbls.append(trig["id"])
    return new_status, new_batch, new_queue, bbls


@callback(
    Output("lookup-status", "data", allow_duplicate=True),
    Output("task-batch", "data", allow_duplicate=True),
    Output("lookup-queue", "data", allow_duplicate=True),
    Output("job-bbls", "data", allow_duplicate=True),
    Input("modal-lookup-btn", "n_clicks"),
    State("modal-building-id", "data"),
    State("lookup-status", "data"),
    State("task-batch", "data"),
    State("lookup-queue", "data"),
    State("job-bbls", "data"),
    prevent_initial_call=True,
)
def start_lookup_from_modal(n, building_id, status, batch, queue, job_bbls):
    if not n or not building_id:
        return no_update, no_update, no_update, no_update
    props = FEATURES_BY_ID.get(building_id)
    if not props:
        return no_update, no_update, no_update, no_update
    bbl = _bbl_for(props)
    if not bbl:
        return no_update, no_update, no_update, no_update
    new_status, new_batch, new_queue = _queue_lookup_for_bbl(
        bbl, status or {}, batch or {}, queue or []
    )
    if new_status is None:
        return no_update, no_update, no_update, no_update
    bbls = list(job_bbls or [])
    if bbl not in bbls:
        bbls.append(bbl)
    return new_status, new_batch, new_queue, bbls


TIMEOUT_STREAK_LIMIT = 3   # consecutive timeouts before we cancel the queue


@callback(
    Output("lookup-status", "data", allow_duplicate=True),
    Output("lookup-toast-store", "data", allow_duplicate=True),
    Output("task-batch", "data", allow_duplicate=True),
    Output("lookup-queue", "data", allow_duplicate=True),
    Output("consecutive-timeouts", "data", allow_duplicate=True),
    Input("lookup-poll", "n_intervals"),
    State("lookup-status", "data"),
    State("task-batch", "data"),
    State("lookup-queue", "data"),
    State("consecutive-timeouts", "data"),
    prevent_initial_call=True,
)
def poll_lookups(_n, status, batch, queue, timeout_streak):
    status = dict(status or {})
    loading = [
        (bbl, v) for bbl, v in status.items() if (v or {}).get("status") == "loading"
    ]
    if not loading:
        return no_update, no_update, no_update, no_update, no_update
    completed = []
    failed = []   # (bbl, address, reason) — "timeout" or "error"
    now = time.time()
    for bbl, v in loading:
        err = _LOOKUP_ERRORS.pop(bbl, None)
        if err:
            if isinstance(err, dict):
                reason = err.get("reason") or "error"
                detail = err.get("detail") or ""
            else:
                reason = err if isinstance(err, str) else "error"
                detail = ""
            status[bbl] = {"status": "timeout", "address": v.get("address")}
            failed.append((bbl, v.get("address") or bbl, reason, detail))
            continue
        data = _fetch_enrichment_result(bbl)
        if data is not None:
            status[bbl] = {"status": "done", "address": v.get("address"), "data": data}
            completed.append((bbl, v.get("address") or bbl))
            continue
        started_at = v.get("started_at") or now
        if now - started_at > LOOKUP_TIMEOUT_SECONDS:
            status[bbl] = {"status": "timeout", "address": v.get("address"), "started_at": started_at}
            failed.append((bbl, v.get("address") or bbl, "timeout", ""))
    if not completed and not failed:
        return no_update, no_update, no_update, no_update, no_update

    new_batch = dict(batch or {})
    new_batch["done"] = new_batch.get("done", 0) + len(completed) + len(failed)
    new_batch.setdefault("started", 0)

    # Streak bookkeeping. Errors (hard failures like billing-out or a
    # Cloudflare block on ContactOut) bypass the streak and cancel the queue
    # immediately — they indicate a systemic problem, not a slow agent.
    # Timeouts accumulate; any successful completion in this tick breaks the
    # streak.
    timeouts_now = sum(1 for _, _, r, _ in failed if r == "timeout")
    errors_now   = sum(1 for _, _, r, _ in failed if r != "timeout")
    streak = 0 if completed else int(timeout_streak or 0)
    streak += timeouts_now
    cancel_queue = errors_now > 0 or streak >= TIMEOUT_STREAK_LIMIT

    new_queue = no_update
    if cancel_queue:
        cancelled = 0
        for bbl in list(status.keys()):
            if (status.get(bbl) or {}).get("status") == "queued":
                del status[bbl]
                cancelled += 1
        new_batch["done"] = min(new_batch.get("started", 0), new_batch["done"] + cancelled)
        new_queue = []
        streak = 0   # fresh slate once the queue is drained

    if failed:
        last_bbl, last_addr, reason, detail = failed[-1]
        toast = {
            "type":      "error",
            "reason":    reason,
            "detail":    detail,
            "bbl":       last_bbl,
            "address":   last_addr,
            "cancelled": cancel_queue,
            "streak":    streak if not cancel_queue else 0,
            "limit":     TIMEOUT_STREAK_LIMIT,
            "tick":      _n,
        }
    elif completed:
        last_bbl, last_addr = completed[-1]
        toast = {
            "type":    "success",
            "bbl":     last_bbl,
            "address": last_addr,
            "more":    len(completed) - 1,
            "tick":    _n,
        }
    else:
        toast = no_update
    return status, toast, new_batch, new_queue, streak


@callback(
    Output("lookup-poll", "disabled"),
    Input("lookup-status", "data"),
)
def manage_lookup_poll(status):
    has_loading = any(
        (v or {}).get("status") == "loading" for v in (status or {}).values()
    )
    return not has_loading


@callback(
    Output("lookup-status", "data", allow_duplicate=True),
    Output("lookup-queue", "data", allow_duplicate=True),
    Output("task-batch", "data", allow_duplicate=True),
    Output("job-bbls", "data", allow_duplicate=True),
    Input("startup-heal", "n_intervals"),
    State("lookup-status", "data"),
    prevent_initial_call=True,
)
def heal_zombie_lookup_state(n, status):
    """Runs once on page load (the `startup-heal` Interval has max_intervals=1).
    `lookup-status` is session-stored so a hard app restart leaves orphan
    `queued` / `loading` entries — the agent threads they reference are dead.
    Drop them so new lookups aren't blocked by `_queue_lookup_for_bbl`'s
    "already in-flight" guard. `done` and `timeout` are kept so the user's
    resolved lookups stay visible on the cards."""
    if not n:
        return no_update, no_update, no_update, no_update
    status = status or {}
    zombies = {
        bbl for bbl, v in status.items()
        if (v or {}).get("status") in ("queued", "loading")
    }
    if not zombies:
        return no_update, no_update, no_update, no_update
    healed = {bbl: v for bbl, v in status.items() if bbl not in zombies}
    # The in-memory queue / batch / job-bbls were already empty (they're not
    # session-stored), but reset explicitly anyway to keep all four stores
    # internally consistent.
    return healed, [], {"started": 0, "done": 0}, []


# ── Lookup queue: parallel worker ────────────────────────────────────────


@callback(
    Output("lookup-status", "data", allow_duplicate=True),
    Output("lookup-queue", "data", allow_duplicate=True),
    Output("lookup-toast-store", "data", allow_duplicate=True),
    Output("task-batch", "data", allow_duplicate=True),
    Input("lookup-status", "data"),
    Input("lookup-queue", "data"),
    State("task-batch", "data"),
    State("max-parallel-input", "value"),
    prevent_initial_call=True,
)
def process_lookup_queue(status, queue, batch, max_parallel):
    """Parallel worker: keeps up to `max_parallel` agents in flight at once,
    where `max_parallel` comes from the slider in the settings modal (with
    DEFAULT_MAX_CONCURRENT_LOOKUPS as fallback before the slider hydrates).
    Each cycle pulls (slots - currently-loading) bbls off the queue,
    cache-hitting where possible (counts toward done immediately, no slot
    consumed) and otherwise spawning a background thread."""
    status = dict(status or {})
    queue = list(queue or [])
    loading_count = sum(
        1 for v in status.values() if (v or {}).get("status") == "loading"
    )
    cap = int(max_parallel) if max_parallel else DEFAULT_MAX_CONCURRENT_LOOKUPS
    slots = cap - loading_count
    if slots <= 0 or not queue:
        return no_update, no_update, no_update, no_update

    new_batch = dict(batch or {})
    new_batch.setdefault("started", 0)
    new_batch.setdefault("done", 0)

    cache_hits: list[tuple[str, str | None]] = []   # (bbl, address)
    spawned = 0

    while slots > 0 and queue:
        next_bbl = queue.pop(0)
        props = FEATURES_BY_BBL.get(next_bbl)
        if not props:
            # Bad bbl — drop from queue but don't consume a slot.
            continue
        cached = _fetch_enrichment_result(next_bbl)
        if cached is not None:
            address = props.get("address")
            status[next_bbl] = {"status": "done", "address": address, "data": cached}
            new_batch["done"] += 1
            cache_hits.append((next_bbl, address))
            # Cache hits are synchronous — they don't tie up an in-flight slot.
            continue
        entry = dict(status.get(next_bbl) or {})
        entry["status"]     = "loading"
        entry["address"]    = entry.get("address") or props.get("address")
        entry["started_at"] = time.time()
        status[next_bbl] = entry
        threading.Thread(
            target=_run_local_agent, args=(next_bbl, props), daemon=True,
        ).start()
        spawned += 1
        slots -= 1

    if not cache_hits and not spawned:
        return no_update, no_update, no_update, no_update

    toast = no_update
    if cache_hits:
        last_bbl, last_addr = cache_hits[-1]
        toast = {
            "type":    "success",
            "bbl":     last_bbl,
            "address": last_addr,
            "more":    len(cache_hits) - 1,
            "tick":    time.time(),
        }
    return status, queue, toast, new_batch


@callback(
    Output("lookup-queue", "data", allow_duplicate=True),
    Output("lookup-status", "data", allow_duplicate=True),
    Output("task-batch", "data", allow_duplicate=True),
    Output("job-bbls", "data", allow_duplicate=True),
    Input("cancel-queue-btn", "n_clicks"),
    State("lookup-status", "data"),
    State("task-batch", "data"),
    State("lookup-queue", "data"),
    State("job-bbls", "data"),
    prevent_initial_call=True,
)
def cancel_queue(n, status, batch, queue, job_bbls):
    if not n:
        return no_update, no_update, no_update, no_update
    status = dict(status or {})
    cancelled_bbls: set[str] = set()
    for bbl in list(status.keys()):
        if (status.get(bbl) or {}).get("status") == "queued":
            del status[bbl]
            cancelled_bbls.add(bbl)
    if not cancelled_bbls and not queue:
        return no_update, no_update, no_update, no_update
    new_batch = dict(batch or {})
    new_batch["started"] = max(0, new_batch.get("started", 0) - len(cancelled_bbls))
    new_batch.setdefault("done", 0)
    bbls = [b for b in (job_bbls or []) if b not in cancelled_bbls]
    return [], status, new_batch, bbls


@callback(
    Output("cancel-queue-btn", "style"),
    Input("lookup-queue", "data"),
)
def manage_cancel_btn(queue):
    if queue:
        return {"fontSize": "10px", "padding": "1px 8px", "display": "inline-block"}
    return {"fontSize": "10px", "padding": "1px 8px", "display": "none"}


# ── Job tracker panel ────────────────────────────────────────────────────


_JOB_TRACKER_BASE_STYLE = {
    "position": "absolute",
    "right": f"{SIDEBAR_WIDTH_PX}px",
    "top": "0",
    "bottom": "0",
    "background": "rgba(18,18,42,0.97)",
    "borderLeft": "1px solid #2a2a4a",
    "borderRight": "1px solid #2a2a4a",
    "boxShadow": "-4px 0 12px rgba(0,0,0,0.4)",
    "zIndex": 500,
    "flexDirection": "column",
}
_JOB_TRACKER_HIDDEN_STYLE = {**_JOB_TRACKER_BASE_STYLE, "display": "none"}
_JOB_TRACKER_EXPANDED_STYLE = {
    **_JOB_TRACKER_BASE_STYLE,
    "display": "flex",
    "width": f"{JOB_TRACKER_WIDTH}px",
}
_JOB_TRACKER_MINIMIZED_STYLE = {
    **_JOB_TRACKER_BASE_STYLE,
    "display": "flex",
    "width": f"{JOB_TRACKER_MIN_WIDTH}px",
}
_JOB_TRACKER_BODY_VISIBLE = {
    "flex": "1", "display": "flex", "flexDirection": "column", "minHeight": "0",
}
_JOB_TRACKER_BODY_HIDDEN = {"display": "none"}


@callback(
    Output("job-tracker", "style"),
    Output("job-tracker-body", "style"),
    Output("job-tracker-count", "children"),
    Output("job-tracker-items", "children"),
    Output("job-tracker-toggle-btn", "children"),
    Output("task-progress-fill", "value"),
    Input("task-batch", "data"),
    Input("job-bbls", "data"),
    Input("lookup-status", "data"),
    Input("job-tracker-minimized", "data"),
    Input("agent-log-poll", "n_intervals"),
)
def render_job_tracker(batch, job_bbls, lookup_status, minimized, _poll_tick):
    job_bbls = job_bbls or []
    started  = (batch or {}).get("started", 0)
    done     = (batch or {}).get("done", 0)
    pct = int(100 * done / started) if started else 0

    # No active or recently-completed job → hide panel entirely.
    if not job_bbls and started <= 0:
        return (
            _JOB_TRACKER_HIDDEN_STYLE,
            no_update, no_update, no_update, no_update, no_update,
        )

    word = "lookup" if started == 1 else "lookups"
    count_text = f"{done} / {started} {word} complete"

    if minimized:
        # Show only the chevron; body collapsed; skip card rendering.
        return (
            _JOB_TRACKER_MINIMIZED_STYLE,
            _JOB_TRACKER_BODY_HIDDEN,
            count_text,
            [],
            "▶",
            pct,
        )

    # Expanded — bucket cards into Running / Completed / Queued sections, in
    # that order, preserving the original queue order within each bucket. The
    # currently-loading cards each get an inline log panel underneath.
    status_map = lookup_status or {}
    running: list[tuple[str, dict, str]]   = []   # status == loading
    completed: list[tuple[str, dict, str]] = []   # status in (done, timeout)
    queued: list[tuple[str, dict, str]]    = []   # everything else (queued/idle)
    for bbl in job_bbls:
        props = FEATURES_BY_BBL.get(bbl)
        if not props:
            continue
        st = (status_map.get(bbl) or {}).get("status", "idle")
        if st == "loading":
            running.append((bbl, props, st))
        elif st in ("done", "timeout"):
            completed.append((bbl, props, st))
        else:
            queued.append((bbl, props, st))

    cards: list = []
    for label, items in (
        ("Running",   running),
        ("Completed", completed),
        ("Queued",    queued),
    ):
        if not items:
            continue
        cards.append(_job_tracker_section_header(label, len(items)))
        for bbl, props, st in items:
            cards.append(_job_tracker_card(props, bbl, st))
            if st == "loading":
                cards.append(_inline_log_panel(bbl))
    return (
        _JOB_TRACKER_EXPANDED_STYLE,
        _JOB_TRACKER_BODY_VISIBLE,
        count_text,
        cards,
        "◀",
        pct,
    )


@callback(
    Output("agent-log-poll", "disabled"),
    Input("lookup-status", "data"),
)
def manage_agent_log_poll(lookup_status):
    # Tail the on-disk log only while at least one bbl is loading — otherwise
    # nothing's being appended and the poll is pure waste.
    has_loading = any(
        (v or {}).get("status") == "loading"
        for v in (lookup_status or {}).values()
    )
    return not has_loading


@callback(
    Output("job-tracker-minimized", "data"),
    Input("job-tracker-toggle-btn", "n_clicks"),
    State("job-tracker-minimized", "data"),
    prevent_initial_call=True,
)
def toggle_job_tracker_minimize(n, minimized):
    if not n:
        return no_update
    return not minimized


@callback(
    Output("task-cleanup", "disabled"),
    Input("task-batch", "data"),
)
def manage_task_cleanup(batch):
    started = (batch or {}).get("started", 0)
    done    = (batch or {}).get("done", 0)
    # Enable the cleanup interval only after every started task has completed.
    if started > 0 and done >= started:
        return False
    return True


@callback(
    Output("task-batch", "data", allow_duplicate=True),
    Output("task-cleanup", "disabled", allow_duplicate=True),
    Output("job-bbls", "data", allow_duplicate=True),
    Input("task-cleanup", "n_intervals"),
    prevent_initial_call=True,
)
def reset_task_batch(_n):
    return {"started": 0, "done": 0}, True, []


# ── Modal "Lookup Owner" button visibility ───────────────────────────────


_MODAL_BTN_HIDDEN  = {"display": "none"}
_MODAL_BTN_VISIBLE = {"display": "inline-block"}


@callback(
    Output("modal-lookup-btn", "style"),
    Output("modal-shortlist-btn", "style"),
    Input("modal-building-id", "data"),
    Input("lookup-status", "data"),
)
def manage_modal_action_buttons(building_id, lookup_status):
    if not building_id:
        return _MODAL_BTN_HIDDEN, _MODAL_BTN_HIDDEN
    props = FEATURES_BY_ID.get(building_id)
    if not props:
        return _MODAL_BTN_HIDDEN, _MODAL_BTN_HIDDEN
    bbl = _bbl_for(props)
    state = ((lookup_status or {}).get(bbl) or {}).get("status", "idle")
    # Lookup btn: idle (initial) or timeout (retry). Hidden while queued/loading/done.
    lookup_style    = _MODAL_BTN_VISIBLE if state in ("idle", "timeout") else _MODAL_BTN_HIDDEN
    shortlist_style = _MODAL_BTN_VISIBLE if state == "done" else _MODAL_BTN_HIDDEN
    return lookup_style, shortlist_style


@callback(
    Output("lookup-toast", "is_open"),
    Output("lookup-toast", "header"),
    Output("lookup-toast", "icon"),
    Output("lookup-toast-msg", "children"),
    Input("lookup-toast-store", "data"),
    State("lookup-status", "data"),
    prevent_initial_call=True,
)
def show_lookup_toast(payload, lookup_status):
    if not payload or not payload.get("bbl"):
        return no_update, no_update, no_update, no_update
    addr = payload.get("address") or payload["bbl"]

    if payload.get("type") == "error":
        reason = payload.get("reason")
        # Prefer the specific error string returned by the agent_runner (e.g.
        # "ContactOut blocked: Rate limited (Too Many Requests)") when we have
        # it; fall back to a generic per-reason label otherwise.
        raw_detail = (payload.get("detail") or "").strip()
        if raw_detail:
            detail = raw_detail
        elif reason == "cloudflare":
            detail = "ContactOut blocked by Cloudflare"
        elif reason == "blocked":
            detail = "ContactOut blocked"
        elif reason == "timeout":
            detail = "Lookup timed out"
        else:
            detail = "Request failed"
        # `cancelled` defaults to True to match the old payload shape (any
        # failure → cancel) for backward compat with cached toasts. New
        # payloads from poll_lookups set it explicitly.
        cancelled = payload.get("cancelled", True)
        if cancelled:
            tail = " — remaining jobs cancelled."
        else:
            streak = payload.get("streak", 0)
            limit  = payload.get("limit", 3)
            tail = f" ({streak}/{limit} consecutive timeouts — queue still running)"
        msg = html.Div([
            html.Div(addr, className="small fw-bold mb-1"),
            html.Div(f"{detail}{tail}", className="small text-danger"),
        ])
        return True, "Lookup failed", "danger", msg

    bbl  = payload["bbl"]
    more = payload.get("more") or 0
    data = _unwrap_agent_data(((lookup_status or {}).get(bbl) or {}).get("data"))

    parts = [html.Div(addr, className="small fw-bold mb-1")]

    counts = []
    nl = len(data.get("landlords") or [])
    nc = len(data.get("corporate_entities") or [])
    if nl:
        counts.append(f"{nl} landlord{'s' if nl != 1 else ''}")
    if nc:
        counts.append(f"{nc} corp entit{'ies' if nc != 1 else 'y'}")
    if counts:
        parts.append(
            html.Div(" · ".join(counts), className="small text-secondary mb-1")
        )

    top_landlord = ((data.get("landlords") or [{}])[0] or {}).get("name")
    if top_landlord:
        parts.append(html.Div(top_landlord, className="small fst-italic mb-1"))

    flags = data.get("flags") or []
    if flags:
        parts.append(
            html.Div(
                f"⚠ {flags[0]}",
                className="small text-warning mb-1",
                style={"whiteSpace": "normal"},
            )
        )

    if more:
        parts.append(
            html.Div(
                f"+{more} more building{'s' if more != 1 else ''}",
                className="small text-secondary mt-2",
            )
        )

    return True, "Owner details found", "success", html.Div(parts)


# ── Settings modal ──────────────────────────────────────────────────────


@callback(
    Output("settings-modal", "is_open"),
    Input("settings-btn", "n_clicks"),
    State("settings-modal", "is_open"),
    prevent_initial_call=True,
)
def toggle_settings_modal(n, is_open):
    return not is_open if n else is_open


# ── Discovered owners ────────────────────────────────────────────────────


def _all_discovered_owners(_lookup_status=None):
    """Every primary landlord we've ever stored, across all sessions. Sourced
    from the SQLite cache so a fresh app boot still surfaces past discoveries.
    The argument is intentionally unused — callers pass `lookup_status` so
    Dash re-triggers this when a new lookup completes."""
    del _lookup_status   # silence "unused" hint; only here to drive re-renders
    return agent_cache.get_discovered_owners()


_ALL_OWNERS_PAGE = 10


def _owner_card(o):
    has_email = bool(o.get("email"))
    bs = o.get("buildings", [])
    building_lines = [
        html.Small(b["address"], className="text-secondary d-block",
                   style={"fontSize": "10px"})
        for b in bs[:3]
    ]
    if len(bs) > 3:
        building_lines.append(
            html.Small(f"+{len(bs)-3} more", className="text-secondary",
                       style={"fontSize": "10px"})
        )
    contact_bits = [b for b in [o.get("phone"), o.get("email")] if b]
    # Clickable content area — clicking it selects every property this owner
    # holds. Kept as a sibling (not parent) of the Shortlist button so the
    # button's click doesn't bubble up and trigger selection.
    occupation = (o.get("occupation") or "").strip()
    name_row = [html.Small(o.get("name") or "(unnamed)",
                           className="fw-semibold",
                           style={"fontSize": "11px"})]
    if occupation:
        name_row.append(
            html.Small(
                occupation,
                className="text-warning ms-1",
                style={
                    "fontSize": "9px",
                    "fontStyle": "italic",
                    "textTransform": "lowercase",
                },
                title="Occupation (from ContactOut profile)",
            )
        )
    content_div = html.Div(
        [
            html.Div(name_row, className="d-flex align-items-baseline gap-1"),
            *building_lines,
            html.Small(" · ".join(contact_bits),
                       className="text-info d-block",
                       style={"fontSize": "10px"}) if contact_bits else None,
        ],
        id={"type": "all-owner-row", "id": o.get("owner_key")},
        n_clicks=0,
        style={
            "flex": "1", "minWidth": 0, "overflow": "hidden",
            "cursor": "pointer",
        },
    )
    row_children = [content_div]
    if (coowner_count := o.get("coowner_count") or 0) > 0:
        row_children.append(
            dbc.Button(
                f"Co · {coowner_count}",
                id={"type": "all-owner-coowners-btn", "id": o.get("owner_key")},
                size="sm",
                color="info",
                outline=True,
                n_clicks=0,
                style={"fontSize": "10px", "padding": "1px 5px", "flexShrink": "0"},
                title="Show co-owners on shared buildings",
            )
        )
    if o.get("email_sent_at"):
        row_children.append(
            html.Span(
                "✓ email sent",
                className="badge bg-success",
                style={
                    "fontSize": "9px",
                    "padding": "3px 6px",
                    "flexShrink": "0",
                    "alignSelf": "center",
                    "fontWeight": "500",
                },
                title="An outreach email has been sent to this owner.",
            )
        )
    elif has_email:
        row_children.append(
            dbc.Button(
                "+ Shortlist",
                id={"type": "all-owner-shortlist-btn", "id": o.get("owner_key")},
                size="sm",
                color="success",
                outline=True,
                style={"fontSize": "10px", "padding": "1px 5px", "flexShrink": "0"},
            )
        )
    return html.Div(
        row_children,
        className="d-flex align-items-start gap-1 mb-1 p-1 rounded",
        style={"background": "#1e1e3a"},
    )


def _coowner_row(co: dict):
    """Compact sub-row under an expanded owner card. Indented + blue accent
    so it's visually subordinate to its primary owner."""
    name = co.get("name") or "(unnamed)"
    contact_bits = [b for b in [co.get("email"), co.get("phone")] if b]
    shared = co.get("shared_bbls") or []
    return html.Div(
        [
            html.Small(name, className="fw-semibold d-block",
                       style={"fontSize": "10px"}),
            html.Small(
                " · ".join(contact_bits),
                className="text-info d-block",
                style={"fontSize": "9px"},
            ) if contact_bits else None,
            html.Small(
                f"shared on {len(shared)} building{'s' if len(shared) != 1 else ''}",
                className="text-secondary d-block",
                style={"fontSize": "9px"},
            ) if shared else None,
        ],
        className="mb-1 p-1 rounded",
        style={
            "background": "#161628",
            "marginLeft": "12px",
            "borderLeft": "2px solid #4fc3f7",
            "paddingLeft": "6px",
        },
    )


def _owner_subsection_header(label: str, count: int, color_cls: str):
    return html.Div(
        f"{label} · {count}",
        className=f"small fw-bold {color_cls} mb-1 mt-2",
        style={
            "fontSize": "10px",
            "textTransform": "uppercase",
            "letterSpacing": "0.04em",
            "paddingBottom": "2px",
            "borderBottom": "1px solid #2a2a4a",
        },
    )


def _owner_card_with_coowners(o: dict, coowners_expanded: set[str]):
    """Render an owner card plus, if the user has expanded it, the inline
    list of co-owners pulled lazily from the DB."""
    card = _owner_card(o)
    if o.get("owner_key") not in coowners_expanded:
        return [card]
    co_list = agent_cache.get_coowners(o.get("name") or "")
    if not co_list:
        return [card, html.Div(
            "(no co-owners on shared buildings)",
            className="small text-secondary",
            style={
                "fontSize": "9px", "marginLeft": "12px",
                "paddingLeft": "6px", "borderLeft": "2px solid #4fc3f7",
                "marginBottom": "4px",
            },
        )]
    return [card] + [_coowner_row(co) for co in co_list]


@callback(
    Output("all-owners-items", "children"),
    Output("all-owners-count", "children"),
    Output("all-owners-toggle-btn", "children"),
    Output("all-owners-toggle-btn", "style"),
    Input("lookup-status", "data"),
    Input("all-owners-expanded", "data"),
    Input("coowners-expanded-keys", "data"),
    Input("email-sent-signal", "data"),
)
def render_all_owners(lookup_status, expanded, coowners_expanded_keys, _email_sent_signal):
    owners = _all_discovered_owners(lookup_status)
    total = len(owners)
    btn_hidden = {"fontSize": "10px", "display": "none"}
    btn_visible = {"fontSize": "10px", "display": "inline-block"}
    if not owners:
        return html.P("No lookups yet.", className="small text-secondary"), "", "", btn_hidden

    # Split by contact reachability. Owners we have an email for are far more
    # useful (clickable + shortlistable + email-blastable) so they sit first.
    with_email    = [o for o in owners if o.get("email")]
    without_email = [o for o in owners if not o.get("email")]

    if expanded:
        we_show, ne_show = with_email, without_email
    else:
        we_show, ne_show = with_email[:_ALL_OWNERS_PAGE], without_email[:_ALL_OWNERS_PAGE]

    coowners_expanded = set(coowners_expanded_keys or [])

    items: list = []
    if with_email:
        items.append(_owner_subsection_header("Email found", len(with_email), "text-success"))
        for o in we_show:
            items.extend(_owner_card_with_coowners(o, coowners_expanded))
    if without_email:
        items.append(_owner_subsection_header("No email found", len(without_email), "text-secondary"))
        for o in ne_show:
            items.extend(_owner_card_with_coowners(o, coowners_expanded))

    # Show the "Show all" toggle only when at least one section is truncated.
    truncated = (not expanded) and (
        len(with_email) > _ALL_OWNERS_PAGE or len(without_email) > _ALL_OWNERS_PAGE
    )
    if not truncated and not expanded:
        return items, str(total), "", btn_hidden
    btn_label = "Hide" if expanded else f"Show all ({total})"
    return items, str(total), btn_label, btn_visible


@callback(
    Output("coowners-expanded-keys", "data"),
    Input({"type": "all-owner-coowners-btn", "id": ALL}, "n_clicks"),
    State("coowners-expanded-keys", "data"),
    prevent_initial_call=True,
)
def toggle_coowners(clicks, current):
    trig = ctx.triggered_id
    if not isinstance(trig, dict) or trig.get("type") != "all-owner-coowners-btn":
        return no_update
    # Pattern-matched inputs occasionally fire on re-render with no real
    # n_clicks; require at least one actual click.
    if not any(c for c in (clicks or []) if c):
        return no_update
    key = trig.get("id")
    expanded = list(current or [])
    if key in expanded:
        expanded.remove(key)
    else:
        expanded.append(key)
    return expanded


@callback(
    Output("all-owners-expanded", "data"),
    Input("all-owners-toggle-btn", "n_clicks"),
    State("all-owners-expanded", "data"),
    prevent_initial_call=True,
)
def toggle_all_owners(n, expanded):
    return not expanded if n else expanded


# ── Selection mutations ──────────────────────────────────────────────────


@callback(
    Output("selected-buildings", "data"),
    Output("bbox-store", "data", allow_duplicate=True),
    Output("subway-selection-store", "data", allow_duplicate=True),
    Input("buildings-layer", "clickData"),
    Input("bbox-store", "data"),
    Input("subway-selection-store", "data"),
    Input("radius-slider", "value"),
    Input("clear-selection-btn", "n_clicks"),
    Input({"type": "deselect-btn", "id": ALL}, "n_clicks"),
    Input({"type": "all-owner-row", "id": ALL}, "n_clicks"),
    State("selected-buildings", "data"),
    State("lookup-status", "data"),
    prevent_initial_call=True,
)
def update_selection(
    click_data, bbox, subway_sel, radius_km, _clear, _deselects,
    owner_clicks, current, lookup_status,
):
    trig = ctx.triggered_id
    current = list(current or [])

    # Clear button: empty selection AND clear bbox/subway visuals.
    if trig == "clear-selection-btn":
        return [], None, None

    # × on a specific card: remove that one building.
    if isinstance(trig, dict) and trig.get("type") == "deselect-btn":
        bid = trig.get("id")
        return [x for x in current if x != bid], no_update, no_update

    # Click on an owner row in the discovered-owners list: REPLACE selection
    # with every building that owner is the primary landlord of. Guard against
    # the spurious initial-render click pattern-matched inputs sometimes fire
    # with no real n_clicks.
    if isinstance(trig, dict) and trig.get("type") == "all-owner-row":
        if not any(c for c in (owner_clicks or []) if c):
            return no_update, no_update, no_update
        owners = _all_discovered_owners(lookup_status)
        owner = next(
            (o for o in owners if o.get("owner_key") == trig.get("id")), None
        )
        if not owner:
            return no_update, no_update, no_update
        bids = []
        for b in owner.get("buildings", []):
            props = FEATURES_BY_BBL.get(b["bbl"])
            if props:
                bids.append(props["id"])
        return bids, None, None

    # BBox drag committed: REPLACE selection with buildings in bbox.
    # BBox cleared by JS background-click reset (bbox=None): clear selection.
    if trig == "bbox-store":
        if bbox:
            return _building_ids_in_bbox(bbox), no_update, no_update
        return [], no_update, no_update

    # Subway stop click committed: REPLACE with buildings in radius.
    # Subway cleared by background-click reset (subway_sel=None): clear selection.
    if trig == "subway-selection-store":
        if subway_sel:
            return (
                _building_ids_in_radius(subway_sel["lat"], subway_sel["lng"], radius_km),
                no_update, no_update,
            )
        return [], no_update, no_update

    # Radius slider while a subway selection is active: refresh in-radius set.
    if trig == "radius-slider" and subway_sel:
        return (
            _building_ids_in_radius(subway_sel["lat"], subway_sel["lng"], radius_km),
            no_update, no_update,
        )

    # Building marker click on the map: toggle that building.
    if trig == "buildings-layer" and click_data:
        bid = (click_data.get("properties") or {}).get("id")
        if not bid:
            return no_update, no_update, no_update
        if bid in current:
            return [x for x in current if x != bid], no_update, no_update
        return [*current, bid], no_update, no_update

    return no_update, no_update, no_update


@callback(
    Output("selected-building-layer", "data"),
    Input("selected-buildings", "data"),
)
def highlight_selected(selected_ids):
    if not selected_ids:
        return EMPTY_GEOJSON
    features = []
    for bid in selected_ids:
        props = FEATURES_BY_ID.get(bid)
        if not props:
            continue
        # Recover the original geometry from ALL_FEATURES
        f = next((g for g in ALL_FEATURES if g["properties"]["id"] == bid), None)
        if f:
            features.append(f)
    return {"type": "FeatureCollection", "features": features}


# ── Bulk lookup / add-to-shortlist on the selection ─────────────────────


@callback(
    Output("lookup-status", "data", allow_duplicate=True),
    Output("task-batch", "data", allow_duplicate=True),
    Output("lookup-queue", "data", allow_duplicate=True),
    Output("job-bbls", "data", allow_duplicate=True),
    Input("bulk-lookup-btn", "n_clicks"),
    State("selected-buildings", "data"),
    State("lookup-status", "data"),
    State("task-batch", "data"),
    State("lookup-queue", "data"),
    State("job-bbls", "data"),
    prevent_initial_call=True,
)
def bulk_lookup_selected(n, selected_ids, status, batch, queue, job_bbls):
    if not n or not selected_ids:
        return no_update, no_update, no_update, no_update
    status = dict(status or {})
    batch = dict(batch or {})
    queue = list(queue or [])
    bbls = list(job_bbls or [])
    added = 0
    for bid in selected_ids:
        props = FEATURES_BY_ID.get(bid)
        if not props:
            continue
        bbl = _bbl_for(props)
        if not bbl:
            continue
        new_status, new_batch, new_queue = _queue_lookup_for_bbl(bbl, status, batch, queue)
        if new_status is None:
            continue
        status = new_status
        batch  = new_batch
        queue  = new_queue
        if bbl not in bbls:
            bbls.append(bbl)
        added += 1
    if added == 0:
        return no_update, no_update, no_update, no_update
    return status, batch, queue, bbls


# ── Owner shortlist ─────────────────────────────────────────────────────


def _merge_owners_into_shortlist(shortlist, new_owners):
    by_key = {o.get("owner_key"): o for o in shortlist}
    for o in new_owners:
        key = o["owner_key"]
        if key in by_key:
            existing = by_key[key]
            seen = {b["bbl"] for b in existing.get("buildings", [])}
            for b in o["buildings"]:
                if b["bbl"] not in seen:
                    existing["buildings"].append(b)
                    seen.add(b["bbl"])
        else:
            shortlist.append(o)
            by_key[key] = o
    return shortlist


@callback(
    Output("shortlist-store", "data"),
    Input("bulk-shortlist-btn", "n_clicks"),
    Input("modal-shortlist-btn", "n_clicks"),
    Input({"type": "all-owner-shortlist-btn", "id": ALL}, "n_clicks"),
    State("selected-buildings", "data"),
    State("modal-building-id", "data"),
    State("lookup-status", "data"),
    State("shortlist-store", "data"),
    prevent_initial_call=True,
)
def add_owners_to_shortlist(_bulk, _modal, _per_owner, selected_ids, modal_bid, lookup_status, shortlist):
    trig = ctx.triggered_id
    shortlist = list(shortlist or [])
    if trig == "bulk-shortlist-btn":
        candidates = list(selected_ids or [])
        new_owners = _group_owners_from_buildings(candidates, lookup_status or {})
    elif trig == "modal-shortlist-btn":
        candidates = [modal_bid] if modal_bid else []
        new_owners = _group_owners_from_buildings(candidates, lookup_status or {})
    elif isinstance(trig, dict) and trig.get("type") == "all-owner-shortlist-btn":
        if not any(_per_owner or []):
            return no_update
        owner_key = trig["id"]
        all_owners = _all_discovered_owners(lookup_status)
        target = next((o for o in all_owners if o.get("owner_key") == owner_key), None)
        if not target:
            return no_update
        new_owners = [target]
    else:
        return no_update
    if not new_owners:
        return no_update
    return _merge_owners_into_shortlist(shortlist, new_owners)


@callback(
    Output("shortlist-items", "children"),
    Output("shortlist-count", "children"),
    Input("shortlist-store", "data"),
)
def render_shortlist(shortlist):
    if not shortlist:
        return html.P("No owners yet.", className="small text-secondary"), ""
    items = []
    for o in shortlist:
        bs = o.get("buildings", [])
        building_lines = [
            html.Small(b["address"], className="text-secondary d-block",
                       style={"fontSize": "10px"})
            for b in bs[:3]
        ]
        if len(bs) > 3:
            building_lines.append(
                html.Small(f"+{len(bs)-3} more", className="text-secondary",
                           style={"fontSize": "10px"})
            )
        contact_bits = [b for b in [o.get("phone"), o.get("email")] if b]
        items.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Small(o.get("name") or "(unnamed)",
                                       className="fw-semibold d-block",
                                       style={"fontSize": "11px"}),
                            *building_lines,
                            html.Small(" · ".join(contact_bits),
                                       className="text-info d-block",
                                       style={"fontSize": "10px"}) if contact_bits else None,
                        ],
                        style={"flex": "1", "minWidth": 0, "overflow": "hidden"},
                    ),
                    dbc.Button(
                        "×",
                        id={"type": "remove-owner-btn", "id": o.get("owner_key")},
                        size="sm",
                        color="danger",
                        outline=True,
                        style={"fontSize": "11px", "padding": "1px 5px", "flexShrink": "0"},
                    ),
                ],
                className="d-flex align-items-start gap-1 mb-1 p-1 rounded",
                style={"background": "#1e1e3a"},
            )
        )
    return items, str(len(shortlist))


@callback(
    Output("shortlist-store", "data", allow_duplicate=True),
    Input({"type": "remove-owner-btn", "id": ALL}, "n_clicks"),
    State("shortlist-store", "data"),
    prevent_initial_call=True,
)
def remove_owner_from_shortlist(n_clicks_list, shortlist):
    if not any(n_clicks_list) or not shortlist:
        return no_update
    remove_key = ctx.triggered_id["id"]
    return [o for o in shortlist if o.get("owner_key") != remove_key]


@callback(
    Output("sidebar-mode-store", "data", allow_duplicate=True),
    Input("open-email-btn", "n_clicks"),
    prevent_initial_call=True,
)
def enter_draft_mode(_n):
    return "draft"


@callback(
    Output("sidebar-mode-store", "data", allow_duplicate=True),
    Input("draft-back-btn", "n_clicks"),
    prevent_initial_call=True,
)
def exit_draft_mode(_n):
    return "shortlist"


@callback(
    Output("sidebar-shortlist-view", "style"),
    Output("sidebar-draft-view", "style"),
    Input("sidebar-mode-store", "data"),
)
def render_sidebar_mode(mode):
    shortlist_show = {"display": "block", "height": "100%", "overflowY": "auto"}
    shortlist_hide = {"display": "none"}
    draft_show = {"display": "flex", "flexDirection": "column", "height": "100%"}
    draft_hide = {"display": "none"}
    if mode == "draft":
        return shortlist_hide, draft_show
    return shortlist_show, draft_hide


@callback(
    Output("draft-llm-btn", "children"),
    Input("draft-body", "value"),
)
def llm_button_label(body):
    return "Refine" if (body or "").strip() else "Generate"


@callback(
    Output("draft-previews", "children"),
    Input("shortlist-store", "data"),
    Input("draft-subject", "value"),
    Input("draft-body", "value"),
)
def render_previews(shortlist, subject, body):
    if not shortlist:
        return html.P(
            "Add owners to shortlist to see previews.",
            className="small text-secondary",
        )
    items = []
    for o in shortlist:
        bs = o.get("buildings", [])
        addresses_str = "; ".join(b["address"] for b in bs)
        ctx_vars = _build_owner_ctx(o)
        subject_preview = _interpolate(subject or "", ctx_vars)
        body_preview = _interpolate(body or "", ctx_vars)
        items.append(
            dbc.Card(
                [
                    dbc.CardHeader(
                        [
                            html.Small(
                                f"{o.get('name')} · {len(bs)} building{'s' if len(bs)!=1 else ''}",
                                className="fw-bold d-block",
                            ),
                            html.Small(
                                addresses_str,
                                className="text-secondary d-block",
                                style={"fontSize": "10px"},
                            ),
                            dbc.Input(
                                id={"type": "owner-email", "id": o.get("owner_key")},
                                placeholder="owner@example.com",
                                type="email",
                                size="sm",
                                value=o.get("email") or "",
                                className="mt-1",
                                debounce=True,
                            ),
                        ]
                    ),
                    dbc.CardBody(
                        [
                            html.Div(
                                [
                                    html.Span("Subject: ", className="fw-bold"),
                                    html.Span(subject_preview or html.Em("(empty)")),
                                ],
                                className="small mb-2",
                                style={"fontSize": "11px"},
                            ),
                            html.Pre(
                                body_preview,
                                style={
                                    "fontSize": "11px",
                                    "whiteSpace": "pre-wrap",
                                    "marginBottom": 0,
                                },
                            ),
                        ]
                    ),
                ],
                className="mb-2",
                color="dark",
            )
        )
    return items


@callback(
    Output("draft-subject", "value"),
    Output("draft-body", "value"),
    Output("llm-status-store", "data"),
    Input("draft-llm-btn", "n_clicks"),
    State("draft-prompt", "value"),
    State("draft-subject", "value"),
    State("draft-body", "value"),
    running=[
        (Output("draft-llm-btn", "disabled"), True, False),
    ],
    prevent_initial_call=True,
)
def run_llm_draft(_n, prompt, subject, body):
    try:
        result = _llm_draft_email((prompt or "").strip(), subject or "", body or "")
    except Exception as e:
        return no_update, no_update, f"LLM error: {e}"
    _save_last_template(result["subject"], result["body"])
    return result["subject"], result["body"], None


@callback(
    Output("draft-llm-status", "children"),
    Input("llm-status-store", "data"),
)
def render_llm_status(msg):
    return msg or ""


_EMAIL_VALIDATE_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@callback(
    Output("draft-send-summary", "children"),
    Input("shortlist-store", "data"),
    Input({"type": "owner-email", "id": ALL}, "value"),
)
def render_send_summary(shortlist, email_values):
    total = len(shortlist or [])
    if total == 0:
        return "Add owners to the shortlist before sending."
    filled = sum(
        1 for v in (email_values or [])
        if v and _EMAIL_VALIDATE_RE.match(v.strip())
    )
    skipped = total - filled
    if filled == 0:
        return "No owners have a valid email — none will be sent."
    parts = [f"Will send {filled} email{'s' if filled != 1 else ''}"]
    if skipped > 0:
        parts.append(f"{skipped} skipped (no email)")
    return " · ".join(parts)


@callback(
    Output("draft-send-status", "children"),
    Output("email-sent-signal", "data"),
    Output("shortlist-store", "data", allow_duplicate=True),
    Input("draft-send-btn", "n_clicks"),
    State("shortlist-store", "data"),
    State({"type": "owner-email", "id": ALL}, "value"),
    State({"type": "owner-email", "id": ALL}, "id"),
    State("draft-subject", "value"),
    State("draft-body", "value"),
    running=[(Output("draft-send-btn", "disabled"), True, False)],
    prevent_initial_call=True,
)
def send_draft(_n, shortlist, email_values, email_ids, subject_tpl, body_tpl):
    if not shortlist:
        return dbc.Alert("Shortlist is empty.", color="warning", duration=4000), no_update, no_update
    email_user = os.getenv("EMAIL_USER", "").strip()
    email_pass = os.getenv("EMAIL_APP_PASSWORD", "").strip()
    if not email_user or not email_pass:
        return dbc.Alert(
            "Set EMAIL_USER and EMAIL_APP_PASSWORD in your .env file.",
            color="danger",
        ), no_update, no_update
    if not (subject_tpl or "").strip() or not (body_tpl or "").strip():
        return dbc.Alert(
            "Subject and body must be non-empty before sending.",
            color="warning",
            duration=5000,
        ), no_update, no_update
    sender_name = os.getenv("SENDER_NAME", "Sam Shersher")

    # Map current owner-email input values by owner_key (input ids are dicts).
    owner_email_by_key = {
        eid.get("id"): (val or "").strip()
        for eid, val in zip(email_ids or [], email_values or [])
    }

    queue = []
    for owner in shortlist:
        key = owner.get("owner_key")
        addr = owner_email_by_key.get(key, "")
        if not addr or not _EMAIL_VALIDATE_RE.match(addr):
            continue
        queue.append((owner, addr))

    if not queue:
        return dbc.Alert(
            "No valid recipient emails — fill in the per-owner email fields above.",
            color="warning",
            duration=5000,
        ), no_update, no_update

    sent, failed, errors = 0, 0, []
    sent_keys: set[str] = set()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(email_user, email_pass)
            for owner, to_addr in queue:
                ctx = _build_owner_ctx(owner)
                subject = _interpolate(subject_tpl, ctx).strip() or "Inquiry"
                body = _interpolate(body_tpl, ctx)

                msg = MIMEMultipart()
                msg["From"] = f"{sender_name} <{email_user}>"
                msg["To"] = to_addr
                msg["Subject"] = subject
                msg["Reply-To"] = email_user
                msg.attach(MIMEText(body, "plain", _charset="utf-8"))

                try:
                    server.sendmail(email_user, [to_addr], msg.as_string())
                    sent += 1
                    sent_keys.add(owner.get("owner_key"))
                    agent_cache.mark_email_sent(owner.get("name") or "")
                except Exception as e:
                    failed += 1
                    errors.append(f"{owner.get('name', '?')}: {e}")
    except smtplib.SMTPAuthenticationError:
        return dbc.Alert(
            "Gmail rejected the login. Use an App Password "
            "(Google Account → Security → App Passwords), not your normal password.",
            color="danger",
        ), no_update, no_update
    except Exception as e:
        return dbc.Alert(f"SMTP error: {e}", color="danger"), no_update, no_update

    if sent:
        signal = int(time.time())
        next_shortlist = [o for o in shortlist if o.get("owner_key") not in sent_keys]
    else:
        signal = no_update
        next_shortlist = no_update

    if failed:
        return dbc.Alert(
            [
                html.Div(f"Sent {sent} · Failed {failed}"),
                html.Small("; ".join(errors[:3]), className="d-block mt-1"),
            ],
            color="warning",
            duration=10000,
        ), signal, next_shortlist
    return dbc.Alert(
        f"Sent {sent} email{'s' if sent != 1 else ''} from {email_user}.",
        color="success",
        duration=6000,
    ), signal, next_shortlist


# ── Subway stop click → radius selection ──────────────────────────────────

@callback(
    Output("subway-selection-store", "data"),
    Output("radius-label", "children"),
    Input("subway-layer", "clickData"),
    Input("clear-selection-btn", "n_clicks"),
    State("radius-slider", "value"),
    prevent_initial_call=True,
)
def handle_subway_click(click_data, clear_clicks, radius_km):
    if ctx.triggered_id == "clear-selection-btn":
        return None, "Subway-stop radius (click a stop to use)"
    if not click_data:
        return no_update, no_update
    coords = (click_data.get("geometry") or {}).get("coordinates", [])
    if len(coords) < 2:
        return no_update, no_update
    lng, lat = coords[0], coords[1]
    name = (click_data.get("properties") or {}).get("name", "station")
    sel = {"lat": lat, "lng": lng, "name": name}
    return sel, f"Radius: {name}"


@callback(
    Output("selection-shapes", "children"),
    Output("selection-status", "children"),
    Input("bbox-store", "data"),
    Input("subway-selection-store", "data"),
    Input("radius-slider", "value"),
)
def update_selection_shapes(bbox, subway_sel, radius_km):
    triggered = [t["prop_id"] for t in (ctx.triggered or [])]
    print(f"[update_selection_shapes] triggered={triggered} bbox={bbox} subway_sel={subway_sel} radius_km={radius_km}", flush=True)
    shapes = []
    status_parts = []
    if bbox:
        print(f"  → rendering Rectangle bounds={bbox}", flush=True)
        bounds = [[bbox["min_lat"], bbox["min_lng"]], [bbox["max_lat"], bbox["max_lng"]]]
        shapes.append(dl.Rectangle(
            bounds=bounds,
            color="#4fc3f7",
            fillColor="#4fc3f7",
            fillOpacity=0.08,
            weight=2,
            dashArray="5",
            interactive=False,
            className="non-interactive-shape",
        ))
        status_parts.append("⬛ bbox")
    if subway_sel:
        print(f"  → rendering Circle center=({subway_sel['lat']},{subway_sel['lng']}) r={radius_km}km", flush=True)
        lat, lng = subway_sel["lat"], subway_sel["lng"]
        shapes.append(dl.Circle(
            center=[lat, lng],
            radius=(radius_km or 0.5) * 1000,
            color="#81C784",
            fillColor="#81C784",
            fillOpacity=0.12,
            weight=2,
            dashArray="6",
            interactive=False,
            className="non-interactive-shape",
        ))
        status_parts.append(f"◉ {subway_sel['name']}")
    status = " · ".join(status_parts) if status_parts else ""
    return shapes, status


# ── Polling: bbox drag + background-click reset from JS ───────────────────

app.clientside_callback(
    """
    function(n) {
        /* One-time map setup */
        if (!window._mapSetupDone) {
            var map = window._mainLeafletMap;
            if (!map) return [
                window.dash_clientside.no_update,
                window.dash_clientside.no_update
            ];
            window._mapSetupDone = true;
            console.warn('━━━━━━━ MAPSETUP v7 LOADED ━━━━━━━ thresholds: move=20px commit=100px,150ms');

            /* Disable Leaflet dragging — we handle mouse ourselves */
            map.dragging.disable();
            map.boxZoom.disable();
            map.doubleClickZoom.disable();

            /* Allow fractional zoom so ±small deltas aren't rounded away.
               With default zoomSnap:1, setZoom(12-0.5)=11.5 rounds back to 12
               (zoom-out appears broken). Setting zoomSnap:0 disables that rounding. */
            map.options.zoomSnap = 0;

            /* Scroll/trackpad → pan; pinch (ctrl+scroll on Mac) → zoom */
            map.getContainer().addEventListener('wheel', function(e) {
                e.preventDefault();
                if (e.ctrlKey || e.metaKey) {
                    /* Proportional delta matching Leaflet's own 1 level / 120px rate.
                       animate:false prevents queued animations from conflicting. */
                    var delta = -e.deltaY * 0.025;
                    map.setZoom(map.getZoom() + delta, {animate: false});
                } else {
                    map.panBy([e.deltaX * 0.8, e.deltaY * 0.8], {animate: false});
                }
            }, {passive: false});

            /* Drag → bbox / click background → reset.
               We do NOT use map.on('click') for reset: with dragging disabled,
               Leaflet fires 'click' after every mouseup including after drags,
               which would immediately clear the bbox we just created.
               Instead we detect drag vs click in mouseup ourselves, and check
               whether the target has leaflet-interactive to skip feature clicks. */
            var startPx = null, isDragging = false, overlay = null;

            map.getContainer().addEventListener('mousedown', function(e) {
                if (e.button !== 0) return;
                var rect = map.getContainer().getBoundingClientRect();
                var cx = e.clientX - rect.left;
                var cy = e.clientY - rect.top;
                /* Save only pixel positions; defer lat/lng conversion until mouseup
                   so both start and end use the SAME map state. Otherwise a map pan
                   between mousedown and mouseup (popup auto-pan, trackpad scroll)
                   makes startLL stale and produces a huge phantom bbox. */
                startPx = {x: e.clientX, y: e.clientY, ox: cx, oy: cy, t: Date.now()};
                isDragging = false;

                overlay = document.createElement('div');
                overlay.style.cssText = 'position:absolute;pointer-events:none;'
                    + 'background:rgba(79,195,247,0.10);border:2px dashed #4fc3f7;'
                    + 'z-index:1000;display:none;border-radius:2px;';
                map.getContainer().appendChild(overlay);
            }, true); /* capture phase — fires before Leaflet marker stopPropagation */

            document.addEventListener('mousemove', function(e) {
                if (!startPx) return;
                var dx = e.clientX - startPx.x, dy = e.clientY - startPx.y;
                /* 400 px² = 20 px radius — only show overlay once user has clearly moved */
                if (!isDragging && dx*dx + dy*dy > 400) {
                    isDragging = true;
                    if (overlay) overlay.style.display = 'block';
                }
                if (isDragging && overlay) {
                    var rect = map.getContainer().getBoundingClientRect();
                    var cx = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
                    var cy = Math.max(0, Math.min(e.clientY - rect.top,  rect.height));
                    var x = Math.min(startPx.ox, cx), y = Math.min(startPx.oy, cy);
                    overlay.style.left   = x + 'px';
                    overlay.style.top    = y + 'px';
                    overlay.style.width  = Math.abs(cx - startPx.ox) + 'px';
                    overlay.style.height = Math.abs(cy - startPx.oy) + 'px';
                }
            });

            document.addEventListener('mouseup', function(e) {
                if (!startPx) return;
                if (overlay) { overlay.remove(); overlay = null; }

                /* Commit a bbox only if ALL of:
                   (a) isDragging was set during mousemove (overlay was shown),
                   (b) end-displacement ≥ 100 px,
                   (c) gesture lasted ≥ 150 ms.
                   This eliminates trackpad-click drift, which can register a single
                   batched mousemove of 50+ px even when the user perceives a click. */
                var dx = e.clientX - startPx.x, dy = e.clientY - startPx.y;
                var dt = Date.now() - startPx.t;
                var realDrag = isDragging
                    && (dx * dx + dy * dy) > 10000
                    && dt > 150;


                if (realDrag) {
                    /* Convert BOTH endpoints using the current map state so a
                       view change between mousedown and mouseup doesn't inflate
                       the bbox. */
                    var rect = map.getContainer().getBoundingClientRect();
                    var sCx = startPx.x - rect.left, sCy = startPx.y - rect.top;
                    var eCx = e.clientX  - rect.left, eCy = e.clientY  - rect.top;
                    var sLL = map.containerPointToLatLng(L.point(sCx, sCy));
                    var eLL = map.containerPointToLatLng(L.point(eCx, eCy));
                    window._pendingBbox = {
                        min_lat: Math.min(sLL.lat, eLL.lat),
                        max_lat: Math.max(sLL.lat, eLL.lat),
                        min_lng: Math.min(sLL.lng, eLL.lng),
                        max_lng: Math.max(sLL.lng, eLL.lng),
                    };
                    console.log('[JS→ pendingBbox queued]', window._pendingBbox);
                } else {
                    /* Click: reset only when target is map background, not a feature.
                       Leaflet marks all interactive layers with leaflet-interactive.
                       Selection shapes are explicitly interactive=False so clicks
                       on the bbox/circle pass through and trigger reset. */
                    var t = e.target;
                    var onFeature = t && (
                        t.classList.contains('leaflet-interactive') ||
                        (t.closest && t.closest('.leaflet-interactive'))
                    );
                    if (!onFeature) {
                        window._pendingReset = true;
                        console.log('[JS→ pendingReset queued]');
                    } else {
                        console.log('[JS→ click on feature, no reset] target=', t && t.tagName, t && t.className);
                    }
                }

                startPx = null; isDragging = false;
            });
        }

        /* Poll for pending updates */
        var bboxOut  = window.dash_clientside.no_update;
        var resetOut = window.dash_clientside.no_update;

        if (window._pendingBbox) {
            bboxOut = window._pendingBbox;
            window._pendingBbox = null;
            console.log('[poll→ writing bbox-store]', bboxOut);
        }
        if (window._pendingReset) {
            bboxOut  = null;   /* clear bbox */
            resetOut = null;   /* clear subway selection */
            window._pendingReset = false;
            console.log('[poll→ writing bbox-store=null, subway-sel=null (reset)]');
        }
        return [bboxOut, resetOut];
    }
    """,
    Output("bbox-store", "data"),
    Output("subway-selection-store", "data", allow_duplicate=True),
    Input("sel-poll", "n_intervals"),
    prevent_initial_call=True,
)


def _interpolate(template: str, variables: dict) -> str:
    return re.sub(
        r"\{\{(\w+)\}\}",
        lambda m: str(variables.get(m.group(1), m.group(0))),
        template,
    )


_LLM_SYSTEM_PROMPT = (
    "You draft short cold-outreach emails sent to NYC landlords on behalf of "
    "a renter looking for a rent-stabilized apartment. The output is a "
    "template: it must use double-brace variables like {{owner_name}} and "
    "{{address}} so the app can fan it out per recipient. "
    f"Available variables: {', '.join('{{' + v + '}}' for v in AVAILABLE_TEMPLATE_VARS)}. "
    "Use {{property_label}} (not {{address}}) in the subject line — it "
    "resolves to a single street address when the owner has one building "
    "and a generic 'your N properties' when they have more. Sign the email "
    "with {{sender_name}}. "
    "Keep the body under 120 words, polite, friendly, and direct. "
    "Respond with strict JSON of the form "
    '{"subject": "...", "body": "..."} — no preamble, no markdown fences, '
    "no commentary."
)


def _llm_draft_email(prompt: str, current_subject: str, current_body: str) -> dict:
    """Call Claude to generate or refine an email template.

    Returns {"subject": str, "body": str}. Raises on API failure or unparseable output.
    """
    client = Anthropic()
    instruction = prompt.strip() if prompt else ""
    if current_body.strip():
        body_msg = (
            "Refine this email template. Preserve the {{double_brace}} variables "
            "where they still make sense.\n\n"
            f"Current subject: {current_subject!r}\n"
            f"Current body:\n{current_body}"
        )
        user_msg = (
            f"{body_msg}\n\nInstruction: {instruction}" if instruction
            else f"{body_msg}\n\nNo specific instruction — polish wording, tighten phrasing, "
                 "and improve flow without changing the intent."
        )
    else:
        base = (
            "Draft a new email template. Use {{double_brace}} variables to personalize "
            "per recipient."
        )
        user_msg = (
            f"{base}\n\nInstruction: {instruction}" if instruction
            else f"{base}\n\nNo specific instruction — write a friendly, concise "
                 "inquiry asking whether anything is currently available or "
                 "expected to open up."
        )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=_LLM_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    # Strip optional ```json fences defensively.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    parsed = json.loads(text)
    return {"subject": str(parsed.get("subject", "")), "body": str(parsed.get("body", ""))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run the landlord-lookup agent's browser visibly (headless=false). "
             "Useful for demos or debugging. Defaults to headless.",
    )
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--debug", action="store_true", default=True)
    args = parser.parse_args()
    AGENT_HEADED = args.headed
    app.run(debug=args.debug, port=args.port)
