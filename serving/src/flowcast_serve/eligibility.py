"""Which USGS gauges flowcast can forecast: one rule, evaluated here and published per gauge
(`/data/v1/gauges/index.json` and 5-degree tiles), so the site selector never re-implements it.
Rule and rationale: docs/site-eligibility.md in the project store; contract: serving/schema/gauges.schema.json.

Status per gauge:
- `model_basin`: one of the flow model's training basins; forecast on visit today.
- `eligible`: meets every criterion; forecastable once on-the-fly onboarding ships (until then the API answers
  `not_supported`).
- `ineligible`: fails at least one criterion; `reasons` lists them.
Temperature (`has_temp`) needs a recent 00010 series, for any status.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd
from flowcast_pipeline.usgs.client import OGC_BASE, WaterDataClient

from .usgs_inputs import LiveClient

LOWER48_BBOX = (-125.0, 24.4, -66.9, 49.5)


@dataclass(frozen=True)
class Rule:
    # Discharge must be reporting: the model's lagged-flow input and the residual anchor need it at every issue.
    active_days: float = 7.0
    # Stream gauges only: lakes, estuaries, tidal reaches, canals and ditches don't have a basin-runoff response.
    site_types: tuple[str, ...] = ("ST",)
    # The training basins' drainage-area range (pipeline/dataset/config.py AREA_KM2).
    area_km2: tuple[float, float] = (50.0, 25_000.0)
    # Daily discharge history for the static attributes the model reads (baseflow index, mean runoff) and for the
    # calibration's flow climatology: the training selection required 10 years.
    min_record_years: float = 10.0
    # HUC2 regions the model was trained in: 01 New England, 02 Mid-Atlantic, 04 Great Lakes, 05 Ohio.
    training_huc2: tuple[str, ...] = ("01", "02", "04", "05")
    # Outside the training region: "refuse" (not eligible) or "label" (eligible, marked in_training_region false).
    outside_region: str = "refuse"
    # Water temperature: a 00010 series reporting within this many days.
    temp_active_days: float = 7.0


RULE = Rule()
REASONS = {
    "no_recent_discharge": "discharge (00060) not reported in the last {active_days:g} days",
    "not_a_stream": "not a stream gauge (lake, estuary, tidal, canal or ditch)",
    "no_drainage_area": "no drainage area on record",
    "area_out_of_range": "drainage area outside the training range ({lo:g}-{hi:g} km2)",
    "short_record": "fewer than {years:g} years of daily discharge",
    "outside_training_region": "outside the region the model was trained on (HUC2 01, 02, 04, 05)",
}


def series_inventory(client: WaterDataClient, parameter: str, period: str, bbox=LOWER48_BBOX) -> pd.DataFrame:
    rows = client._features("time-series-metadata", {"parameter_code": parameter, "computation_period_identifier": period, "bbox": ",".join(map(str, bbox)),
                                                      "properties": "monitoring_location_id,begin_utc,end_utc"})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["begin", "end"])
    df["begin"] = pd.to_datetime(df["begin_utc"], utc=True, format="ISO8601")
    df["end"] = pd.to_datetime(df["end_utc"], utc=True, format="ISO8601")
    return df.groupby("monitoring_location_id").agg(begin=("begin", "min"), end=("end", "max"))


def locations(client: WaterDataClient, ids: list[str], batch: int = 150) -> pd.DataFrame:
    """Monitoring-location attributes (with coordinates) for `ids`."""
    rows = []
    props = "monitoring_location_name,site_type_code,drainage_area,hydrologic_unit_code,state_code"
    for k in range(0, len(ids), batch):
        url, query = f"{OGC_BASE}/collections/monitoring-locations/items", {"f": "json", "limit": 10000, "id": ",".join(ids[k : k + batch]), "properties": props}
        while url:
            payload = client._get(url, query).json()
            for f in payload.get("features", []):
                xy = (f.get("geometry") or {}).get("coordinates") or [math.nan, math.nan]
                rows.append({"id": f["id"], **f["properties"], "lon": xy[0], "lat": xy[1]})
            url = next((link["href"] for link in payload.get("links", []) if link.get("rel") == "next"), None)
            query = None
    return pd.DataFrame(rows).set_index("id")


def evaluate(iv_q: pd.DataFrame, dv_q: pd.DataFrame, iv_tw: pd.DataFrame, locs: pd.DataFrame, model_basins: set[str], now: pd.Timestamp, rule: Rule = RULE) -> pd.DataFrame:
    """One row per gauge with a discharge series: status, reasons, has_temp, in_training_region."""
    df = locs.join(iv_q.rename(columns={"begin": "iv_begin", "end": "iv_end"}), how="left").join(dv_q.rename(columns={"begin": "dv_begin", "end": "dv_end"}), how="left")
    area_km2 = df["drainage_area"].astype(float) * 2.589988110336
    record = ((df["dv_end"].fillna(df["iv_end"]) - df["dv_begin"].fillna(df["iv_begin"])).dt.days / 365.25).fillna(0.0)
    huc2 = df["hydrologic_unit_code"].fillna("").astype(str).str[:2]
    in_region = huc2.isin(rule.training_huc2)
    checks = {
        "no_recent_discharge": ~(df["iv_end"] >= now - pd.Timedelta(days=rule.active_days)),
        "not_a_stream": ~df["site_type_code"].isin(rule.site_types),
        "no_drainage_area": area_km2.isna(),
        "area_out_of_range": area_km2.notna() & ~area_km2.between(*rule.area_km2),
        "short_record": record < rule.min_record_years,
        "outside_training_region": ~in_region if rule.outside_region == "refuse" else pd.Series(False, index=df.index),
    }
    reasons = pd.Series([[] for _ in range(len(df))], index=df.index)
    for name, failed in checks.items():
        for i in df.index[failed.fillna(True)]:
            reasons[i].append(name)
    tw_ok = df.index.isin(iv_tw.index[iv_tw["end"] >= now - pd.Timedelta(days=rule.temp_active_days)])
    is_model = df.index.str.removeprefix("USGS-").isin(model_basins)
    # Model basins met the static criteria at training time; what can lapse since is the discharge record itself.
    for i, m in zip(df.index, is_model, strict=True):
        if m:
            reasons[i] = [r for r in reasons[i] if r == "no_recent_discharge"]
    status = [("model_basin" if not r else "ineligible") if m else ("eligible" if not r else "ineligible") for m, r in zip(is_model, reasons, strict=True)]
    return pd.DataFrame({
        "name": df["monitoring_location_name"], "lat": df["lat"].round(5), "lon": df["lon"].round(5), "area_km2": area_km2.round(1), "huc2": huc2,
        "record_years": record.round(1), "status": status, "reasons": list(reasons), "model_basin": is_model,
        "has_q": (df["iv_end"] >= now - pd.Timedelta(days=rule.active_days)).fillna(False).to_numpy(), "has_temp": tw_ok, "in_training_region": in_region,
    }, index=df.index)


def build(model_basins: set[str], now: pd.Timestamp, client: WaterDataClient | None = None) -> pd.DataFrame:
    """The rule evaluated for every lower-48 gauge with a discharge series (about 20 USGS requests)."""
    client = client or LiveClient(timeout_s=120.0, max_retries=5)
    iv_q = series_inventory(client, "00060", "Points")
    dv_q = series_inventory(client, "00060", "Daily")
    iv_tw = series_inventory(client, "00010", "Points")
    ids = sorted(set(iv_q.index) | set(dv_q.index[dv_q["end"] >= now - pd.Timedelta(days=365)]))
    return evaluate(iv_q, dv_q, iv_tw, locations(client, ids), model_basins, now)


def index_flags(table: pd.DataFrame, entries: list[dict]) -> list[dict]:
    """The site index's entries with `forecastable` / `not_forecastable_reason` from the rule (a model basin whose
    gauge stopped reporting discharge can't be forecast)."""
    out = []
    for e in entries:
        reasons = list(table.loc[e["id"], "reasons"]) if e["id"] in table.index else ["no_recent_discharge"]
        out.append({**e, "forecastable": not reasons, "not_forecastable_reason": reasons[0] if reasons else None})
    return out


def tile_key(lon: float, lat: float) -> str:
    return f"{int(math.floor(lon / 5.0) * 5)}_{int(math.floor(lat / 5.0) * 5)}"


def documents(table: pd.DataFrame, generated: str, rule: Rule = RULE) -> tuple[dict, dict[str, dict]]:
    """(index, {tile key: tile}) for `/data/v1/gauges/`."""
    tiles: dict[str, list] = {}
    for gid, r in table.iterrows():
        if not (math.isfinite(r["lon"]) and math.isfinite(r["lat"])):
            continue
        tiles.setdefault(tile_key(r["lon"], r["lat"]), []).append({
            "id": gid, "name": r["name"], "lat": r["lat"], "lon": r["lon"], "area_km2": None if pd.isna(r["area_km2"]) else float(r["area_km2"]),
            "eligibility": {"status": str(r["status"]), "forecast_now": r["status"] == "model_basin", "reasons": [str(x) for x in r["reasons"]]},
            "model_basin": bool(r["model_basin"]), "has_q": bool(r["has_q"]), "has_temp": bool(r["has_temp"]), "in_training_region": bool(r["in_training_region"]), "record_years": float(r["record_years"]),
        })
    counts = table.groupby(["in_training_region", "status"]).size()
    index = {
        "schema": "flowcast.gauges/v1", "generated": generated, "tile_deg": 5, "tile_url": "/data/v1/gauges/tiles/{key}.json",
        "rule": {**rule.__dict__, "reasons": {k: v.format(active_days=rule.active_days, lo=rule.area_km2[0], hi=rule.area_km2[1], years=rule.min_record_years) for k, v in REASONS.items()}},
        "tiles": {k: len(v) for k, v in sorted(tiles.items())},
        "counts": {"lower48": {s: int(n) for s, n in table["status"].value_counts().items()},
                   "training_region": {s: int(counts.get((True, s), 0)) for s in ("model_basin", "eligible", "ineligible")}},
    }
    index["ids_url"] = "/data/v1/gauges/ids.json"
    return index, {k: {"schema": "flowcast.gauges.tile/v1", "key": k, "gauges": v} for k, v in tiles.items()}


def id_map(tiles: dict[str, dict], generated: str) -> dict:
    """`/data/v1/gauges/ids.json`: every catalog gauge's tile, so a direct link resolves without the map view."""
    return {"schema": "flowcast.gauges.ids/v1", "generated": generated, "tiles": {g["id"]: k for k, t in sorted(tiles.items()) for g in t["gauges"]}}
