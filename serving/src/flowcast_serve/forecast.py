"""One forecast run: a list of sites, one synoptic issue (production-architecture.md §3-5).

1. Read `models/production.json`, fetch and verify the flow, temp and snow versions (cached while warm).
2. Pull new USGS readings for every gauge the sites read; extract the hindcast window of HRRR analysis and MRMS and
   the GEFS init for every site at once (tiles shared), archiving both in the lake.
3. Per site: live cube -> 3 flow seeds -> calibrated 132-member pool; flow member medians -> live cube with the flow
   forecast -> 2 temperature seeds -> calibrated hourly and daily highs; SNOW-17 snowmelt estimate.
4. Write the lake record (`forecasts/live/`), the immutable page JSON, `events/` + `flowcast.forecast.issued`, the
   site's live.json, and the run record. A failing site doesn't stop the others; the next cycle or visit retries.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
from flowcast_model.hindcast import daily_maxima
from flowcast_model.units import to_cfs
from flowcast_pipeline.lake import Lake
from flowcast_pipeline.snow import HRUSet
from flowcast_pipeline.snow.params import SnowParams

from . import (
    bundles,
    config,
    forcing,
    inputs,
    light,
    livecube,
    predict,
    snodas_live,
    snowmelt,
    usgs_inputs,
)
from .calibration import (
    calibrate_flow,
    calibrate_temperature_daily,
    calibrate_temperature_hourly,
)
from .control import Control
from .issues import check_live, iso, latest_issue, parse_issue, utcnow
from .promote import snow_params_from
from .registry import ModelRegistry, ServedSite, served_sites

log = logging.getLogger(__name__)


@dataclass
class Models:
    pointer: dict[str, str]
    flow: Path
    temp: Path | None
    snow: Path | None
    flow_statics: list[str]
    temp_statics: list[str]
    temp_basins: set[str]
    temp_cal: pd.DataFrame | None
    snow_params: SnowParams | None


def load_models(registry: ModelRegistry) -> Models:
    pointer = registry.production()
    flow, _ = registry.fetch("flow", pointer["flow"])
    temp = registry.fetch("temp", pointer["temp"])[0] if pointer.get("temp") else None
    snow = registry.fetch("snow", pointer["snow"])[0] if pointer.get("snow") else None
    temp_basins = set(pd.read_parquet(temp / "statics.parquet").index) if temp else set()
    temp_cal = pd.read_csv(temp / "calibration" / "calibration.csv") if temp else None
    params = snow_params_from(json.loads((snow / "params.json").read_text())) if snow else None
    return Models(pointer, flow, temp, snow, predict.required_features(flow)["static"], predict.required_features(temp)["static"] if temp else [],
                  temp_basins, temp_cal, params)


def hrus_for(lake: Lake, basin: str, work: Path) -> HRUSet | None:
    root = work / "hrus" / f"USGS-{basin}"
    if not (root / "hrus.parquet").exists():
        keys = lake.list(f"sites/USGS-{basin}/serving/hrus/")
        if not keys:
            return None
        for k in keys:
            path = root / k.rsplit("/", 1)[-1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(lake.read(k))
    return HRUSet.load(root)


def site_info(lake: Lake, basin: str) -> dict:
    data = lake.read(f"sites/USGS-{basin}/serving/site.json")
    return json.loads(data) if data else {}


def refresh_inputs(lake: Lake, models: Models, sites: list[ServedSite], issue: pd.Timestamp, now: pd.Timestamp) -> dict:
    """USGS readings and analysis forcing for every site; GEFS for the issue's init. Returns provenance."""
    t0 = time.monotonic()
    metas = {s.site_id: inputs.basin_meta(models.flow, models.temp, s.usgs_id) for s in sites}
    gauges: dict[str, list[str]] = {"discharge": [], "water_temperature": []}
    gauges["stage"] = []
    for s in sites:
        for var, ids in inputs.gauges_for(metas[s.site_id], s.usgs_id in models.temp_basins and s.has_temperature).items():
            gauges[var] += ids
        # the site's own readings for live.json, whatever the models read
        gauges["water_temperature"].append(s.usgs_id)
        gauges["stage"].append(s.usgs_id)
    failed = usgs_inputs.refresh(lake, gauges, now)
    t_usgs = time.monotonic() - t0
    basins = [s.usgs_id for s in sites]
    start = issue - pd.Timedelta(days=inputs.HISTORY_DAYS + 1)
    latest = {}
    for prefix, source in forcing.ANALYSIS.items():
        try:
            weights = {b: forcing.load_weights(lake, b, source) for b in basins}
            out = forcing.extract_analysis(source, weights, start, issue)
            for b, frame in out.items():
                forcing.store_analysis(lake, prefix, b, frame)
            latest[prefix] = max((f.dropna(how="all").index.max() for f in out.values() if f.notna().any().any()), default=None)
        except Exception:  # dynamical.org down or late: the lake archive is used as it is (masked groups)
            log.exception("%s extraction failed; using the archive", source)
    t_an = time.monotonic() - t0 - t_usgs
    init = forcing.gefs_init_for(issue)
    if init is not None:
        need = [b for b in basins if forcing.load_gefs(lake, init, b) is None]
        if need:
            basin_w = {b: forcing.load_weights(lake, b, "gefs_forecast") for b in need}
            band_w = {b: w for b in need if (w := _try_weights(lake, b, "gefs_forecast_bands")) is not None}
            vals, leads = forcing.extract_gefs("gefs_forecast", basin_w, init)
            bands, _ = forcing.extract_gefs("gefs_forecast_bands", band_w, init) if band_w else ({}, leads)
            for b in need:
                forcing.store_gefs(lake, init, b, vals[b], bands.get(b), leads)
    t_gefs = time.monotonic() - t0 - t_usgs - t_an
    return {"usgs_failed": failed, "latest": latest, "gefs_init": init, "seconds": {"usgs": round(t_usgs, 1), "analysis": round(t_an, 1), "gefs": round(t_gefs, 1)}}


def _try_weights(lake: Lake, basin: str, source: str):
    try:
        return forcing.load_weights(lake, basin, source)
    except FileNotFoundError:
        return None


def forecast_site(lake: Lake, data: bundles.DataBucket, models: Models, site: ServedSite, issue: pd.Timestamp, issue_key: str, trigger: str,
                  prov: dict, work: Path, entry: dict) -> dict:
    basin = site.usgs_id
    t0 = time.monotonic()
    init = prov["gefs_init"]
    if init is None:
        raise RuntimeError("no GEFS 00Z init within 30 h of the issue; forecast delayed")
    g = forcing.load_gefs(lake, init, basin)
    gefs = inputs.gefs_product(init, g["leads"], g["basin"][0])
    meta = inputs.basin_meta(models.flow, models.temp, basin)
    with_temp = basin in models.temp_basins and site.has_temperature
    index = inputs.window(issue)
    obs = inputs.observed(lake, meta, index, issue, with_temp)
    frc = inputs.forcing(lake, basin, index, issue)
    masked = [name for name, cols in (("lagged_flow", ["qobs_mm_h"]), ("hrrr", ["hrrr_an_temp_2m_c"]), ("mrms", ["mrms_precip_mm_h"]), ("snodas", ["snodas_swe_mm"]))
              if not np.isfinite((pd.concat([obs, frc], axis=1)[cols].loc[issue - pd.Timedelta(hours=6):issue]).to_numpy(float)).any()]
    if with_temp and not np.isfinite(obs["tw_c"].loc[issue - pd.Timedelta(days=2):issue].to_numpy(float)).any():
        with_temp = False  # the gauge stopped reporting temperature: flow-only this issue
    work_site = work / issue_key / basin
    flow_lb = inputs.build(lake, meta, issue, inputs.statics(models.flow, basin, models.flow_statics), False, {"gefs": gefs}, obs, frc)
    flow_cube = livecube.write(work_site / "flow.zarr", flow_lb)
    outs = [predict.predict_seed(d, flow_cube, basin, issue, seed=k) for k, d in enumerate(predict.seed_dirs(models.flow))]
    raw_mm = np.concatenate([o.samples for o in outs], axis=1)
    flow_cfs = calibrate_flow(to_cfs(raw_mm, "mm/h", meta.area_km2), basin, models.flow / "calibration")
    t_flow = time.monotonic() - t0

    temp_h = temp_d = None
    n_temp = 0
    if with_temp:
        n = outs[0].n_samples
        per_member = np.concatenate([o.samples.reshape(o.samples.shape[0], len(o.members), n) for o in outs], axis=2)  # [lead, member, seeds*draws]
        fm = np.median(per_member, axis=2).T  # [member, lead] mm/h, as tempcube built `flowfc_qobs_mm_h`
        temp_lb = inputs.build(lake, meta, issue, inputs.statics(models.temp, basin, models.temp_statics), True,
                               {"gefs": gefs, "flowfc": inputs.flowfc_product(issue, fm)}, obs, frc)
        temp_cube = livecube.write(work_site / "temp.zarr", temp_lb)
        touts = [predict.predict_seed(d, temp_cube, basin, issue, seed=100 + k) for k, d in enumerate(predict.seed_dirs(models.temp))]
        raw_t = np.concatenate([o.samples for o in touts], axis=1)
        n_temp = raw_t.shape[1]
        temp_h = calibrate_temperature_hourly(raw_t, models.temp_cal)
        maxima, days, lead_days = daily_maxima(raw_t[None], pd.DatetimeIndex([issue.tz_convert(None)]), site.timezone)
        temp_d = (calibrate_temperature_daily(maxima[0], lead_days, models.temp_cal), days[0], lead_days)
    t_temp = time.monotonic() - t0 - t_flow

    snodas = snodas_live.load_daily(lake, basin, issue - pd.Timedelta(days=10), issue)
    swe = float(snodas["swe_mm"].iloc[-1]) if len(snodas) else None
    melt_bins = None
    hrus = hrus_for(lake, basin, work)
    if hrus is not None and models.snow_params is not None and "bands" in g:
        st = pd.read_parquet(models.flow / "statics.parquet").loc[basin]
        band_elev = np.array([st[f"band_elev_m_band{k}"] for k in range(4)], dtype=float)
        try:
            melt = snowmelt.estimate(hrus, models.snow_params, float(st["elevation_m"]), band_elev, frc, snodas, g["basin"][0], g["bands"], g["leads"], init, issue)
            melt_bins = melt["bins_6h_mm"]
        except Exception:
            log.exception("%s: snowmelt estimate failed; left out", basin)
    t_snow = time.monotonic() - t0 - t_flow - t_temp

    info = site_info(lake, basin)
    prebuilt = lake.read(f"sites/USGS-{basin}/serving/static.json")
    static_doc = json.loads(prebuilt) if prebuilt else None
    # flood flows: the NWS stages through the USGS rating (static.json), else NWPS's own flows
    categories = (static_doc or {}).get("flood_categories") or info.get("flood_categories", [])
    weather = bundles.weather_block(issue, init, g["basin"][0], g["leads"], frc["mrms_precip_mm_h"].loc[:issue], swe, melt_bins)
    latest = prov["latest"]
    q_obs = obs["qobs_m3s"].dropna()
    in_doc = {"obs_latest": iso(q_obs.index[-1].to_pydatetime()) if len(q_obs) else None, "hrrr_latest": iso(latest.get("hrrr_an")), "mrms_latest": iso(latest.get("mrms")),
              "gefs_init": iso(init), "snodas_date": str(snodas["day"].iloc[-1].date()) if len(snodas) else None, "masked": masked}
    created = utcnow()
    doc = bundles.forecast_document(site, issue, issue_key, created, trigger, {k: models.pointer.get(k) for k in ("flow", "temp", "snow")}, flow_cfs,
                                    temp_h, temp_d, weather, in_doc, categories, flow_cfs.shape[1], n_temp)
    data.put(f"{bundles.site_prefix(site.site_id)}/forecasts/{issue_key}.json", doc, bundles.IMMUTABLE, retention="30d")
    record = _record(issue, flow_cfs, temp_h)
    lake.write_parquet(f"forecasts/live/{site.site_id}/{issue_key}.parquet", record)
    lake.write_parquet(f"forecasts/live/{site.site_id}/{issue_key}.mix.parquet", _mixtures(outs))
    event = bundles.event_summary(site, issue_key, doc)
    lake.write(f"events/{site.site_id}/{issue_key}.json", json.dumps(event).encode(), "application/json")
    if static_doc is None:
        static_doc = bundles.static_document(site, entry, inputs.statics(models.flow, basin, models.flow_statics), categories, info.get("nws_lid"))
    static_doc.pop("has_temperature", None)
    static_doc["has_temp"] = with_temp
    data.put(f"{bundles.site_prefix(site.site_id)}/static.json", static_doc, "public, max-age=300")
    return {"event": event, "seconds": {"flow": round(t_flow, 1), "temp": round(t_temp, 1), "snow": round(t_snow, 1), "total": round(time.monotonic() - t0, 1)},
            "with_temp": with_temp, "masked": masked}


def _record(issue: pd.Timestamp, flow_cfs: np.ndarray, temp_h: np.ndarray | None) -> pd.DataFrame:
    rows = []
    for variable, x in (("discharge", flow_cfs), ("water_temperature", temp_h)):
        if x is None:
            continue
        q = np.percentile(x, bundles.Q, axis=1)
        leads = np.arange(1, x.shape[0] + 1)
        frame = pd.DataFrame({"variable": variable, "issue_time": issue, "lead_h": leads.astype(float), "valid_time": issue + pd.to_timedelta(leads, unit="h")})
        for p, v in zip(bundles.Q, q):
            frame[f"q{p:02d}"] = v.astype(np.float32)
        rows.append(frame)
    return pd.concat(rows, ignore_index=True)


def _mixtures(outs: list[predict.SeedOutput]) -> pd.DataFrame:
    frames = []
    for o in outs:
        L, M, K, _ = o.mixture.shape
        f = pd.DataFrame({"seed": o.run, "lead_h": np.repeat(np.arange(1, L + 1, dtype=float), M), "member": np.tile(o.members, L)})
        flat = o.mixture.reshape(L * M, K, 4)
        for j, name in enumerate(("pi", "mu", "b", "tau")):
            for c in range(K):
                f[f"{name}{c}"] = flat[:, c, j]
        frames.append(f)
    return pd.concat(frames, ignore_index=True)


def run(site_ids: list[str], issue_key: str, trigger: str, locked: bool, settings: config.Settings | None = None, requested: str | None = None) -> dict:
    settings = settings or config.Settings()
    issue = pd.Timestamp(parse_issue(issue_key))
    check_live(issue.to_pydatetime())
    now = pd.Timestamp(utcnow())
    if issue.to_pydatetime() > latest_issue(now.to_pydatetime()):
        raise ValueError(f"issue {issue_key} is not available yet (inputs land 1.5 h after the issue time)")
    lake = Lake(settings.lake_uri)
    bucket = settings.lake_uri.removeprefix("s3://").split("/", 1)[0]
    registry = ModelRegistry(bucket, cache=Path(settings.work_dir) / "models")
    models = load_models(registry)
    control = Control(settings.table)
    data = bundles.DataBucket(settings.data_bucket, settings.data_prefix)
    all_sites = served_sites()
    index = {e["id"]: e for e in light.index_entries(lake)}
    sites = []
    for sid in site_ids:
        site = all_sites.get(sid)
        if site is None:
            log.warning("%s is not a servable site; skipped", sid)
            continue
        if locked or control.acquire(sid, issue_key, models.pointer["flow"], trigger):
            sites.append(site)
    if not sites:
        return {"issue": issue_key, "ran": []}
    work = Path(settings.work_dir) / "runs"
    prov = refresh_inputs(lake, models, sites, issue, now)
    results, events = {}, []
    for site in sites:
        t = time.monotonic()
        try:
            res = forecast_site(lake, data, models, site, issue, issue_key, trigger, prov, work, index.get(site.site_id, {}))
            control.finish(site.site_id, issue_key, models.pointer["flow"], True, seconds=time.monotonic() - t, has_temp=res["with_temp"])
            events.append(res["event"])
            results[site.site_id] = {"ok": True, **{k: v for k, v in res.items() if k != "event"}}
            light.publish_site(lake, data, control, site, utcnow(), refresh=False)
        except Exception as err:
            log.exception("%s %s failed", site.site_id, issue_key)
            control.finish(site.site_id, issue_key, models.pointer["flow"], False, error=repr(err), seconds=time.monotonic() - t)
            results[site.site_id] = {"ok": False, "error": repr(err)[:300]}
        finally:
            shutil.rmtree(work / issue_key / site.usgs_id, ignore_errors=True)
    if events:
        entries = [{"Source": "flowcast.forecast", "DetailType": "flowcast.forecast.issued", "Detail": json.dumps(e), "EventBusName": settings.event_bus} for e in events]
        for i in range(0, len(entries), 10):
            boto3.client("events").put_events(Entries=entries[i : i + 10])
    wake_latency = None
    if trigger == "wake" and requested:
        wake_latency = (utcnow() - pd.Timestamp(requested).to_pydatetime()).total_seconds()
    _metrics(settings, trigger, results, prov, wake_latency)
    return {"issue": issue_key, "trigger": trigger, "inputs": {k: (iso(v) if hasattr(v, "tzinfo") else v) for k, v in prov.items() if k != "latest"}, "results": results,
            "wake_latency_s": wake_latency}


def _metrics(settings: config.Settings, trigger: str, results: dict, prov: dict, wake_latency: float | None) -> None:
    """Embedded metrics without dimensions: the account stays inside CloudWatch's 10 free custom metrics."""
    metrics = {"SitesFailed": sum(not r["ok"] for r in results.values())}
    names = [{"Name": "SitesFailed", "Unit": "Count"}]
    if wake_latency is not None:
        metrics["WakeLatency"] = round(wake_latency, 1)
        names.append({"Name": "WakeLatency", "Unit": "Seconds"})
    log.info(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{"Namespace": settings.metrics_namespace, "Dimensions": [[]], "Metrics": names}]},
                         **metrics, "trigger": trigger, "sites_forecast": sum(r["ok"] for r in results.values()), "input_seconds": prov["seconds"]}))
