import math
import os
import json
import re
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import pandas as pd
import requests
import dash
from dash import dcc, html, Input, Output, State, callback, ALL, ctx, no_update
import dash_leaflet as dl
import dash_bootstrap_components as dbc
from dash_extensions.javascript import assign
from dotenv import load_dotenv

load_dotenv()

AGENT_WEBHOOK_URL   = os.getenv("AGENT_WEBHOOK_URL", "").rstrip("/") or None
CALLBACK_PUBLIC_URL = os.getenv("CALLBACK_PUBLIC_URL", "").rstrip("/") or None
CALLBACK_LOCAL_URL  = os.getenv("CALLBACK_LOCAL_URL", "http://localhost:9000").rstrip("/")
SHARED_SECRET       = os.getenv("SHARED_SECRET") or None

# ── Data loading ───────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

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
            dbc.NavbarBrand("House Me", className="fw-bold me-3"),
            html.Span(id="nav-count", className="text-secondary small"),
        ],
        fluid=True,
    ),
    dark=True,
    color="dark",
    className="border-bottom border-secondary",
    style={"height": "50px", "minHeight": "50px"},
)

SIDEBAR = html.Div(
    [
        html.Div(
            [
                html.Div(
                    "Filters",
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
                dbc.Switch(
                    id="subway-toggle",
                    label="Subway stops",
                    value=True,
                    className="mb-1 small",
                ),
                dbc.Switch(
                    id="zip-toggle",
                    label="ZIP boundaries",
                    value=True,
                    className="mb-1 small",
                ),
                html.Div(id="selection-status", className="small text-warning mt-1"),
                html.Div(
                    id="radius-control",
                    children=[
                        html.Hr(className="my-2"),
                        html.Div(id="radius-label", className="small text-info mb-1"),
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
                        dbc.Button(
                            "Clear selection",
                            id="clear-selection-btn",
                            size="sm",
                            color="warning",
                            outline=True,
                            className="w-100 mt-1",
                        ),
                    ],
                    style={"display": "none"},
                ),
                html.Div(id="building-count", className="small text-secondary mt-1"),
                html.Div(
                    id="selected-list",
                    className="mt-2",
                    style={"maxHeight": "260px", "overflowY": "auto"},
                ),
                html.Div(
                    className="small text-secondary mt-2",
                    style={"opacity": "0.5", "fontSize": "10px"},
                    children="Drag to select area · Click stop for radius · Click map to reset",
                ),
            ],
            className="p-3 border-bottom border-secondary",
        ),
        html.Div(
            id="clicked-panel",
            className="p-3 border-bottom border-secondary",
            children=html.P(
                "Click a building for details",
                className="small text-secondary mb-0",
            ),
        ),
        html.Div(
            [
                html.Div(
                    [
                        html.Span("Shortlist", className="fw-bold small"),
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
                    children=html.P("None saved yet.", className="small text-secondary"),
                ),
                dbc.Button(
                    "Compose Email →",
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
    style={
        "width": "280px",
        "flexShrink": "0",
        "height": "calc(100vh - 50px)",
        "overflowY": "auto",
        "background": "#12122a",
        "borderLeft": "1px solid #222",
    },
)

MAP_AREA = dl.Map(
    id="main-map",
    center=[40.6501, -73.9496],
    zoom=12,
    scrollWheelZoom=False,
    style={"height": "calc(100vh - 50px)", "flex": "1"},
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

EMAIL_MODAL = dbc.Modal(
    [
        dbc.ModalHeader(dbc.ModalTitle("Email Blast")),
        dbc.ModalBody(
            [
                html.P(
                    [
                        "Template variables: ",
                        html.Code("{address}"),
                        ", ",
                        html.Code("{zip}"),
                        ", ",
                        html.Code("{block}"),
                        ", ",
                        html.Code("{lot}"),
                        ", ",
                        html.Code("{sender_email}"),
                    ],
                    className="small text-secondary mb-2",
                ),
                dcc.Textarea(
                    id="email-template",
                    value=(
                        "Hi,\n\n"
                        "I'm looking for a rent-stabilized apartment and came across "
                        "your building at {address} (ZIP {zip}).\n\n"
                        "Is anything currently available, or could you let me know "
                        "when something opens up?\n\n"
                        "Feel free to reply to {sender_email}.\n\n"
                        "Thank you,"
                    ),
                    style={
                        "width": "100%",
                        "height": "160px",
                        "fontFamily": "monospace",
                        "fontSize": "13px",
                    },
                    className="form-control mb-3",
                ),
                html.H6("Recipients", className="mt-2"),
                html.Small(
                    "Enter owner emails below — only buildings with an email will be sent.",
                    className="text-secondary",
                ),
                html.Div(id="email-previews", className="mt-3"),
                html.Hr(),
                dbc.Button(
                    "Send Blast", id="send-btn", color="danger", className="w-100 mt-1"
                ),
                html.Div(id="send-status", className="mt-2"),
            ]
        ),
    ],
    id="email-modal",
    size="xl",
    is_open=False,
    scrollable=True,
)

BUILDING_DETAIL_MODAL = dbc.Modal(
    [
        dbc.ModalHeader(dbc.ModalTitle(id="building-detail-title")),
        dbc.ModalBody(
            [
                html.Div(id="building-detail-body"),
                dbc.Button(
                    "Lookup Owner",
                    id="modal-lookup-btn",
                    size="sm",
                    color="primary",
                    className="mt-2",
                    n_clicks=0,
                    style={"display": "none"},
                ),
            ]
        ),
    ],
    id="building-detail-modal",
    is_open=False,
    size="md",
)

TASK_PROGRESS_BAR = html.Div(
    id="task-progress-bar",
    style={
        "position": "fixed",
        "top": "50px",
        "left": 0,
        "right": 0,
        "zIndex": 1500,
        "padding": "8px 16px",
        "background": "rgba(18,18,42,0.97)",
        "borderBottom": "1px solid #2a2a4a",
        "display": "none",
    },
    children=[
        html.Div(id="task-progress-label", className="small text-secondary mb-1"),
        dbc.Progress(
            id="task-progress-fill",
            value=0,
            striped=True,
            animated=True,
            style={"height": "8px"},
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

# ── App init ───────────────────────────────────────────────────────────────
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.DARKLY],
    suppress_callback_exceptions=True,
    title="Brooklyn RSB Finder",
)

app.layout = html.Div(
    [
        dcc.Store(id="shortlist-store", storage_type="local"),
        dcc.Store(id="bbox-store", data=None),
        dcc.Store(id="subway-selection-store", data=None),
        dcc.Store(id="modal-building-id", data=None),
        dcc.Store(id="lookup-status", storage_type="session", data={}),
        dcc.Store(id="lookup-toast-store"),
        dcc.Store(id="task-batch", data={"started": 0, "done": 0}),
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
        NAVBAR,
        TASK_PROGRESS_BAR,
        html.Div(
            [MAP_AREA, SIDEBAR],
            style={"display": "flex", "height": "calc(100vh - 50px)"},
        ),
        EMAIL_MODAL,
        BUILDING_DETAIL_MODAL,
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
    return dbc.Button("Lookup", color="primary", outline=True, **common)


def _selected_cards(features, lookup_status=None):
    if not features:
        return html.P(
            "No buildings in current selection.",
            className="small text-secondary mb-0",
        )
    status_map = lookup_status or {}
    cards = []
    for f in features[:SELECTED_CARD_LIMIT]:
        props = f["properties"]
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
                ],
                className="mb-1 p-2 rounded d-flex align-items-center gap-2",
                style={
                    "background": "#1a1a2e",
                    "border": "1px solid #2a2a4a",
                },
            )
        )
    if len(features) > SELECTED_CARD_LIMIT:
        cards.append(
            html.Div(
                f"Showing first {SELECTED_CARD_LIMIT:,} of {len(features):,}",
                className="small text-secondary mt-2",
            )
        )
    return cards


@callback(
    Output("buildings-layer", "data"),
    Output("building-count", "children"),
    Output("nav-count", "children"),
    Input("zip-filter", "value"),
    Input("bbox-store", "data"),
    Input("subway-selection-store", "data"),
    Input("radius-slider", "value"),
)
def update_buildings(selected_zips, bbox, subway_sel, radius_km):
    geojson = filter_geojson(selected_zips, bbox)
    if subway_sel:
        lat0 = subway_sel["lat"]
        lng0 = subway_sel["lng"]
        r_m = (radius_km or 0.5) * 1000
        m_lat = 111320.0
        m_lng = 111320.0 * math.cos(math.radians(lat0))
        geojson = {
            "type": "FeatureCollection",
            "features": [
                f for f in geojson["features"]
                if math.sqrt(
                    ((f["geometry"]["coordinates"][1] - lat0) * m_lat) ** 2
                    + ((f["geometry"]["coordinates"][0] - lng0) * m_lng) ** 2
                ) <= r_m
            ],
        }
    n = len(geojson["features"])
    total = len(ALL_FEATURES)
    label = f"{n:,} of {total:,} buildings"
    return geojson, label, label


@callback(
    Output("selected-list", "children"),
    Input("buildings-layer", "data"),
    Input("lookup-status", "data"),
)
def render_selected_list(geojson, lookup_status):
    features = ((geojson or {}).get("features") or [])
    return _selected_cards(features, lookup_status)


@callback(
    Output("subway-layer", "data"),
    Output("subway-routes-layer", "data"),
    Input("subway-toggle", "value"),
)
def toggle_subway(show):
    if show:
        return SUBWAY_GEOJSON, ROUTES_GEOJSON
    return EMPTY_GEOJSON, EMPTY_GEOJSON


@callback(
    Output("zip-layer", "data"),
    Input("zip-toggle", "value"),
)
def toggle_zip(show):
    return ZIP_GEOJSON if show else EMPTY_GEOJSON


@callback(
    Output("clicked-panel", "children"),
    Output("selected-building-layer", "data"),
    Input("buildings-layer", "clickData"),
)
def show_clicked(click_data):
    empty_panel = html.P(
        "Click a building for details", className="small text-secondary mb-0"
    )
    if not click_data or "properties" not in click_data:
        return empty_panel, EMPTY_GEOJSON
    p = click_data.get("properties", {})
    if not p.get("id"):
        return empty_panel, EMPTY_GEOJSON
    block = str(p.get("block", "")).zfill(5)
    lot   = str(p.get("lot",   "")).zfill(4)
    acris = f"https://a836-acris.nyc.gov/DS/DocumentSearch/BBL?ms_bbl=3{block}{lot}"
    panel = html.Div(
        [
            html.Strong(p.get("address"), className="d-block small"),
            html.Small(
                f"ZIP {p.get('zip')} · Block {p.get('block')} · Lot {p.get('lot')}",
                className="text-secondary d-block",
            ),
            html.Small(p.get("statuses", ""), className="text-secondary d-block"),
            html.Div(
                [
                    dbc.Button(
                        "+ Add to shortlist",
                        id="add-btn",
                        size="sm",
                        color="primary",
                        className="me-2 mt-2",
                    ),
                    html.A(
                        "ACRIS →",
                        href=acris,
                        target="_blank",
                        className="btn btn-outline-secondary btn-sm mt-2",
                    ),
                ]
            ),
        ]
    )
    geom = click_data.get("geometry")
    highlight = (
        {"type": "FeatureCollection",
         "features": [{"type": "Feature", "geometry": geom, "properties": p}]}
        if geom else EMPTY_GEOJSON
    )
    return panel, highlight


# Lookup for fast card-click → building props resolution
FEATURES_BY_ID  = {f["properties"]["id"]: f["properties"] for f in ALL_FEATURES}
FEATURES_BY_BBL = {
    _bbl_for(f["properties"]): f["properties"]
    for f in ALL_FEATURES
    if _bbl_for(f["properties"])
}


def _fire_enrichment_webhook(correlation_id: str, props: dict) -> None:
    """POST a job to the kiloclaw webhook. Fire-and-forget; runs in a thread
    so the Dash callback returns immediately."""
    if not AGENT_WEBHOOK_URL or not CALLBACK_PUBLIC_URL or not SHARED_SECRET:
        return
    payload = {
        "correlation_id": correlation_id,
        "bbl":     _bbl_for(props) or None,
        "address": props.get("address"),
        "zip":     props.get("zip"),
        "block":   props.get("block"),
        "lot":     props.get("lot"),
        "callback_url":  f"{CALLBACK_PUBLIC_URL}/agent-result",
    }
    try:
        requests.post(AGENT_WEBHOOK_URL, json=payload, timeout=8)
    except Exception:
        pass


def _fetch_enrichment_result(building_id: str):
    """Returns the agent's data dict if ready, None if still pending."""
    if not SHARED_SECRET:
        return None
    try:
        r = requests.get(
            f"{CALLBACK_LOCAL_URL}/result/{building_id}",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
            timeout=4,
        )
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("status") == "ready":
            return body.get("data")
    except Exception:
        return None
    return None


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

# Top-level metadata keys the modal already shows in the Building section,
# plus anything that must never be displayed (echoed-back auth, internal IDs).
INTERNAL_FIELDS = {
    "address", "bbl", "block", "lot", "zip",
    "correlation_id", "building_address", "search_address",
    "shared_secret",
}

NAMED_DICT_SECTIONS = [
    ("Recommended outreach", "recommended_outreach", False),
    ("Portfolio",            "portfolio",            False),
    ("Useful links",         "useful_links",         True),
]


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

    entities = data.get("corporate_entities")
    if isinstance(entities, list) and entities:
        sections.append(
            html.Div("Corporate entities", className="small fw-bold mt-2 mb-1")
        )
        sections.extend(_entity_cards(entities))

    for label, key, as_links in NAMED_DICT_SECTIONS:
        v = data.get(key)
        if isinstance(v, dict) and v:
            sections.append(
                html.Div(label, className="small fw-bold mt-2 mb-1")
            )
            sections.append(_render_dict_rows(v, as_links=as_links))

    handled = (
        {f[1] for f in STAT_FIELDS}
        | {"flags", "landlords", "corporate_entities"}
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


def _start_lookup_for_bbl(bbl, status, batch):
    """Common helper: mutates (copies of) status + batch when a new lookup
    should start for this BBL. Returns (new_status, new_batch) or (None, None)
    if the lookup shouldn't fire (already loading/done, or unknown BBL)."""
    if (status.get(bbl) or {}).get("status") in ("loading", "done"):
        return None, None
    props = FEATURES_BY_BBL.get(bbl)
    if not props:
        return None, None
    new_status = dict(status)
    new_status[bbl] = {"status": "loading", "address": props.get("address")}
    new_batch = dict(batch or {})
    new_batch["started"] = new_batch.get("started", 0) + 1
    new_batch.setdefault("done", 0)
    threading.Thread(
        target=_fire_enrichment_webhook, args=(bbl, props), daemon=True,
    ).start()
    return new_status, new_batch


@callback(
    Output("lookup-status", "data"),
    Output("task-batch", "data"),
    Input({"type": "lookup-btn", "id": ALL}, "n_clicks"),
    State("lookup-status", "data"),
    State("task-batch", "data"),
    prevent_initial_call=True,
)
def start_lookup(n_clicks_list, status, batch):
    if not any(n for n in (n_clicks_list or []) if n):
        return no_update, no_update
    trig = ctx.triggered_id
    if not trig or trig.get("type") != "lookup-btn":
        return no_update, no_update
    new_status, new_batch = _start_lookup_for_bbl(trig["id"], status or {}, batch or {})
    if new_status is None:
        return no_update, no_update
    return new_status, new_batch


@callback(
    Output("lookup-status", "data", allow_duplicate=True),
    Output("task-batch", "data", allow_duplicate=True),
    Input("modal-lookup-btn", "n_clicks"),
    State("modal-building-id", "data"),
    State("lookup-status", "data"),
    State("task-batch", "data"),
    prevent_initial_call=True,
)
def start_lookup_from_modal(n, building_id, status, batch):
    if not n or not building_id:
        return no_update, no_update
    props = FEATURES_BY_ID.get(building_id)
    if not props:
        return no_update, no_update
    bbl = _bbl_for(props)
    if not bbl:
        return no_update, no_update
    new_status, new_batch = _start_lookup_for_bbl(bbl, status or {}, batch or {})
    if new_status is None:
        return no_update, no_update
    return new_status, new_batch


@callback(
    Output("lookup-status", "data", allow_duplicate=True),
    Output("lookup-toast-store", "data"),
    Output("task-batch", "data", allow_duplicate=True),
    Input("lookup-poll", "n_intervals"),
    State("lookup-status", "data"),
    State("task-batch", "data"),
    prevent_initial_call=True,
)
def poll_lookups(_n, status, batch):
    status = dict(status or {})
    loading = [bbl for bbl, v in status.items() if (v or {}).get("status") == "loading"]
    if not loading:
        return no_update, no_update, no_update
    completed = []  # list of (bbl, address)
    for bbl in loading:
        data = _fetch_enrichment_result(bbl)
        if data is None:
            continue
        prev = status.get(bbl) or {}
        status[bbl] = {
            "status":  "done",
            "address": prev.get("address"),
            "data":    data,
        }
        completed.append((bbl, prev.get("address") or bbl))
    if not completed:
        return no_update, no_update, no_update
    new_batch = dict(batch or {})
    new_batch["done"]    = new_batch.get("done", 0) + len(completed)
    new_batch.setdefault("started", 0)
    last_bbl, last_addr = completed[-1]
    toast = {
        "bbl":     last_bbl,
        "address": last_addr,
        "more":    len(completed) - 1,
        "tick":    _n,
    }
    return status, toast, new_batch


@callback(
    Output("lookup-poll", "disabled"),
    Input("lookup-status", "data"),
)
def manage_lookup_poll(status):
    has_loading = any(
        (v or {}).get("status") == "loading" for v in (status or {}).values()
    )
    return not has_loading


# ── Task progress bar ────────────────────────────────────────────────────


_TASK_BAR_HIDDEN_STYLE = {"display": "none"}
_TASK_BAR_VISIBLE_STYLE = {
    "position": "fixed", "top": "50px", "left": 0, "right": 0, "zIndex": 1500,
    "padding": "8px 16px", "background": "rgba(18,18,42,0.97)",
    "borderBottom": "1px solid #2a2a4a", "display": "block",
}


@callback(
    Output("task-progress-bar", "style"),
    Output("task-progress-label", "children"),
    Output("task-progress-fill", "value"),
    Input("task-batch", "data"),
)
def render_task_progress(batch):
    started = (batch or {}).get("started", 0)
    done    = (batch or {}).get("done", 0)
    if started <= 0:
        return _TASK_BAR_HIDDEN_STYLE, no_update, no_update
    pct = int(100 * done / started) if started else 0
    word = "lookup" if started == 1 else "lookups"
    label = f"{done} / {started} owner {word} complete"
    return _TASK_BAR_VISIBLE_STYLE, label, pct


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
    Input("task-cleanup", "n_intervals"),
    prevent_initial_call=True,
)
def reset_task_batch(_n):
    return {"started": 0, "done": 0}, True


# ── Modal "Lookup Owner" button visibility ───────────────────────────────


_MODAL_BTN_HIDDEN  = {"display": "none"}
_MODAL_BTN_VISIBLE = {"display": "inline-block"}


@callback(
    Output("modal-lookup-btn", "style"),
    Input("modal-building-id", "data"),
    Input("lookup-status", "data"),
)
def manage_modal_lookup_btn(building_id, lookup_status):
    if not building_id:
        return _MODAL_BTN_HIDDEN
    props = FEATURES_BY_ID.get(building_id)
    if not props:
        return _MODAL_BTN_HIDDEN
    bbl = _bbl_for(props)
    state = ((lookup_status or {}).get(bbl) or {}).get("status", "idle")
    return _MODAL_BTN_VISIBLE if state == "idle" else _MODAL_BTN_HIDDEN


@callback(
    Output("lookup-toast", "is_open"),
    Output("lookup-toast-msg", "children"),
    Input("lookup-toast-store", "data"),
    State("lookup-status", "data"),
    prevent_initial_call=True,
)
def show_lookup_toast(payload, lookup_status):
    if not payload or not payload.get("bbl"):
        return no_update, no_update
    bbl  = payload["bbl"]
    addr = payload.get("address") or bbl
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
        parts.append(
            html.Div(top_landlord, className="small fst-italic mb-1")
        )

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

    return True, html.Div(parts)


@callback(
    Output("shortlist-store", "data"),
    Input("add-btn", "n_clicks"),
    State("buildings-layer", "clickData"),
    State("shortlist-store", "data"),
    prevent_initial_call=True,
)
def add_to_shortlist(_, click_data, shortlist):
    if not click_data:
        return no_update
    shortlist = shortlist or []
    p = click_data.get("properties", {})
    bid = p.get("id")
    if bid and not any(b.get("id") == bid for b in shortlist):
        shortlist = [*shortlist, p]
    return shortlist


@callback(
    Output("shortlist-items", "children"),
    Output("shortlist-count", "children"),
    Input("shortlist-store", "data"),
)
def render_shortlist(shortlist):
    if not shortlist:
        return html.P("None saved yet.", className="small text-secondary"), ""
    items = []
    for b in shortlist:
        items.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Small(
                                b.get("address", ""),
                                className="fw-semibold d-block",
                                style={"fontSize": "11px"},
                            ),
                            html.Small(
                                f"ZIP {b.get('zip', '')}",
                                className="text-secondary",
                                style={"fontSize": "10px"},
                            ),
                        ],
                        style={"flex": "1", "minWidth": 0, "overflow": "hidden"},
                    ),
                    dbc.Button(
                        "×",
                        id={"type": "remove-btn", "id": b.get("id")},
                        size="sm",
                        color="danger",
                        outline=True,
                        style={"fontSize": "11px", "padding": "1px 5px", "flexShrink": "0"},
                    ),
                ],
                className="d-flex align-items-center gap-1 mb-1 p-1 rounded",
                style={"background": "#1e1e3a"},
            )
        )
    return items, str(len(shortlist))


@callback(
    Output("shortlist-store", "data", allow_duplicate=True),
    Input({"type": "remove-btn", "id": ALL}, "n_clicks"),
    State("shortlist-store", "data"),
    prevent_initial_call=True,
)
def remove_from_shortlist(n_clicks_list, shortlist):
    if not any(n_clicks_list) or not shortlist:
        return no_update
    remove_id = ctx.triggered_id["id"]
    return [b for b in shortlist if b.get("id") != remove_id]


@callback(
    Output("email-modal", "is_open"),
    Input("open-email-btn", "n_clicks"),
    State("email-modal", "is_open"),
    prevent_initial_call=True,
)
def toggle_modal(n, is_open):
    return not is_open if n else is_open


@callback(
    Output("email-previews", "children"),
    Input("shortlist-store", "data"),
    Input("email-template", "value"),
)
def render_previews(shortlist, template):
    if not shortlist:
        return html.P("No buildings in shortlist.", className="small text-secondary")
    items = []
    for i, b in enumerate(shortlist):
        preview = _interpolate(template or "", {**b, "sender_email": "(your email)"})
        items.append(
            dbc.Card(
                [
                    dbc.CardHeader(
                        [
                            html.Small(b.get("address"), className="fw-bold d-block"),
                            dbc.Input(
                                id={"type": "owner-email", "id": b.get("id", str(i))},
                                placeholder="owner@example.com",
                                type="email",
                                size="sm",
                                className="mt-1",
                                debounce=True,
                            ),
                        ]
                    ),
                    dbc.CardBody(
                        html.Pre(
                            preview,
                            style={"fontSize": "11px", "whiteSpace": "pre-wrap", "marginBottom": 0},
                        )
                    ),
                ],
                className="mb-2",
                color="dark",
            )
        )
    return items


# @callback(
#     Output("send-status", "children"),
#     Input("send-btn", "n_clicks"),
#     State("shortlist-store", "data"),
#     State({"type": "owner-email", "id": ALL}, "value"),
#     State("email-template", "value"),
#     prevent_initial_call=True,
# )
# def send_emails(_, shortlist, email_values, template):
#     if not shortlist:
#         return dbc.Alert("Shortlist is empty.", color="warning", duration=4000)
#     email_user = os.getenv("EMAIL_USER", "")
#     email_pass = os.getenv("EMAIL_APP_PASSWORD", "")
#     if not email_user or not email_pass:
#         return dbc.Alert(
#             "Set EMAIL_USER and EMAIL_APP_PASSWORD in a .env file.", color="danger"
#         )
#     recipients = [(b, e) for b, e in zip(shortlist, email_values or []) if e]
#     if not recipients:
#         return dbc.Alert("No owner emails entered — fill them in above.", color="warning")
#     sent, failed = 0, 0
#     try:
#         with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
#             server.login(email_user, email_pass)
#             for b, to_addr in recipients:
#                 body = _interpolate(template or "", {**b, "sender_email": email_user})
#                 msg = MIMEMultipart()
#                 msg["From"] = email_user
#                 msg["To"] = to_addr
#                 msg["Subject"] = f"Apartment inquiry — {b.get('address', '')}"
#                 msg.attach(MIMEText(body, "plain"))
#                 try:
#                     server.sendmail(email_user, to_addr, msg.as_string())
#                     sent += 1
#                 except Exception:
#                     failed += 1
#     except Exception as e:
#         return dbc.Alert(f"SMTP error: {e}", color="danger")
#     color = "success" if not failed else "warning"
#     return dbc.Alert(f"Sent {sent} · Failed {failed}", color=color, duration=6000)


# ── Subway stop click → radius selection ──────────────────────────────────

@callback(
    Output("subway-selection-store", "data"),
    Output("radius-control", "style"),
    Output("radius-label", "children"),
    Input("subway-layer", "clickData"),
    Input("clear-selection-btn", "n_clicks"),
    State("radius-slider", "value"),
    prevent_initial_call=True,
)
def handle_subway_click(click_data, clear_clicks, radius_km):
    print(f"[handle_subway_click] triggered_id={ctx.triggered_id} click_data={click_data}", flush=True)
    if ctx.triggered_id == "clear-selection-btn":
        print("  → clearing subway selection", flush=True)
        return None, {"display": "none"}, ""
    if not click_data:
        return no_update, no_update, no_update
    coords = (click_data.get("geometry") or {}).get("coordinates", [])
    if len(coords) < 2:
        return no_update, no_update, no_update
    lng, lat = coords[0], coords[1]
    name = (click_data.get("properties") or {}).get("name", "station")
    sel = {"lat": lat, "lng": lng, "name": name}
    print(f"  → setting subway selection: {sel}", flush=True)
    return sel, {"display": "block"}, f"Radius: {name}"


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
        r"\{(\w+)\}",
        lambda m: str(variables.get(m.group(1), m.group(0))),
        template,
    )


if __name__ == "__main__":
    app.run(debug=True, port=8050)
