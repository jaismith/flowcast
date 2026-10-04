"""Model registry versions from training runs (production-architecture.md §3.2).

A version is self-contained, so a forecast run never reads the training cube:

    flow/{v}/seeds/{run}/        config.yml, flowcast.yml, basins.txt, train_data/train_data_scaler.yml, model_epochNNN.pt
    flow/{v}/calibration/        flow_calibration.csv/.json, basin_stats.parquet (PR #51 standard path)
    flow/{v}/statics.parquet     every static the seeds read, plus area, lon, below_dam, band elevations, for all
                                 the version's basins (from the run's training cube; time-invariant)
    flow/{v}/outflows.json       below-dam gauges per basin, the cube's own `gauged_outflow_sites` (the gauges whose
                                 summed discharge is the trained `gauged_outflow_mm_h`)
    temp/{v}/...                 the same, with calibration/calibration.csv and temp_gauges.json (upstream and
                                 dam-release temperature gauges per basin, chosen as `tempcube.gauge_temperatures` does)
    snow/{v}/params.json         SNOW-17 parameters
    {family}/{v}/manifest.json   SHA-256 of every file, source runs, cube manifest ids, git SHA, notes

A new model (e.g. the upstream-gauge ensemble) is promoted the same way; `required_features` lists what it reads,
`check_servable` refuses a version whose inputs the live cube can't build, and `check_gauge_lists` refuses one whose
gauge lists differ from the ones the model was trained with.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import subprocess
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
from flowcast_model.config import load_run
from flowcast_model.cube import FROZEN_TEST_START, Cube
from flowcast_pipeline.snow.params import MeltMode, PrecipSplit, RainOnSnow, SnowParams

from . import predict
from .registry import ModelRegistry, sha256_file

log = logging.getLogger(__name__)

EXTRA_STATICS = ["area_km2", "lat", "lon", "below_dam", "elevation_m", "forest_frac", "developed_frac", "frac_snow", "nid_n_major",
                 *[f"band_elev_m_band{k}" for k in range(4)], "gauged_outflow_n", "has_upstream_tw", "outflow_tw_n"]
# Gauge lists come from the full v1.3 cube: its `gauged_outflow_sites` and `upstream_slot_site` are the post-Sep-27
# (regulation fix) lists every cube derived from it was built with; derived cubes (e.g. temp126v2) don't carry them.
GAUGE_CUBE = "s3://flowcast-training-257129854363-us-east-2/v1.3/full/trainval.zarr"
# Live inputs the serving code builds (inputs.py); anything else a version reads must be added there first.
SERVABLE_DYNAMIC = {
    "qobs_mm_h", "gauged_outflow_mm_h", "q_flat_run_h", "q_constant_release", "tw_c", "upstream_tw_c", "outflow_tw_c",
    "mrms_precip_mm_h", "snodas_swe_mm", *[f"snodas_band_swe_mm_band{k}" for k in range(4)],
    *[f"hrrr_an_{v}" for v in ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2")],
    *[f"aorc_{v}" for v in ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2", "spfh_2m_gkg")],
    "doy_sin", "doy_cos", "solar_sin", "solar_cos",
}
SERVABLE_FORECAST = {*[f"gefs_{v}" for v in ("precip_mm_h", "temp_2m_c", "dewpoint_2m_c", "pressure_kpa", "wind_speed_10m", "sw_down_wm2", "lw_down_wm2")], "flowfc_qobs_mm_h"}


def snow_params_from(d: dict) -> SnowParams:
    d = dict(d)
    d["adc"] = tuple(d["adc"])
    d["melt_mode"], d["precip_split"], d["rain_on_snow"] = MeltMode(d["melt_mode"]), PrecipSplit(d["precip_split"]), RainOnSnow(d["rain_on_snow"])
    return SnowParams(**d)


def check_servable(version_root: Path) -> None:
    need = predict.required_features(version_root)
    missing = sorted(set(need["dynamic"]) - SERVABLE_DYNAMIC - SERVABLE_FORECAST) + sorted(set(need["forecast"]) - SERVABLE_FORECAST)
    if missing:
        raise ValueError(f"{version_root}: the live cube can't build {missing}; add them to flowcast_serve.inputs before promoting")


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _sync_run(s3, uri: str, dest: Path) -> str:
    """Copy one training run's serving files (final checkpoint only) from `s3://bucket/runs/<run>` to `dest`."""
    bucket, prefix = uri.removeprefix("s3://").split("/", 1)
    prefix = prefix.rstrip("/") + "/run/"
    dest.mkdir(parents=True, exist_ok=True)
    for rel in ("config.yml", "flowcast.yml", "basins.txt", "train_data/train_data_scaler.yml"):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, prefix + rel, str(dest / rel))
    ckpts = sorted(o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "model_epoch") for o in p.get("Contents", []))
    for key in ckpts:  # final_epoch reads the file names; only the chosen checkpoint is kept
        (dest / key.rsplit("/", 1)[-1]).touch()
    _, options = load_run(dest)
    epoch = predict.final_epoch(dest, options.hindcast.epoch)
    for p in dest.glob("model_epoch*.pt"):
        p.unlink()
    s3.download_file(bucket, f"{prefix}model_epoch{epoch:03d}.pt", str(dest / f"model_epoch{epoch:03d}.pt"))
    return f"{uri} epoch {epoch}"


def build_version(family: str, version: str, runs: list[str], cube: str, out: Path, calibration: Path | None = None,
                  gauge_cube: str = GAUGE_CUBE, notes: str = "") -> Path:
    s3 = boto3.client("s3")
    root = out / family / version
    if root.exists():
        shutil.rmtree(root)
    sources = [_sync_run(s3, r, root / "seeds" / r.rstrip("/").rsplit("/", 1)[-1]) for r in runs]
    check_servable(root)
    seeds = predict.seed_dirs(root)
    basins = sorted(set.intersection(*[set((d / "basins.txt").read_text().split()) for d in seeds]))
    names = sorted(set(predict.required_features(root)["static"]) | set(EXTRA_STATICS))
    c = Cube([cube])
    have = [n for n in names if c.has(n)]
    statics = c.load_static(basins, have)
    statics.index = statics.index.astype(str)
    statics.to_parquet(root / "statics.parquet")
    if calibration is not None:
        shutil.copytree(calibration, root / "calibration")
    (root / "outflows.json").write_text(json.dumps(outflow_gauges(gauge_cube, basins)))
    if family == "temp":
        (root / "temp_gauges.json").write_text(json.dumps(temperature_gauges(gauge_cube, basins)))
    check_gauge_lists(root, gauge_cube)
    write_manifest(root, {"family": family, "version": version, "sources": sources, "cube": cube, "gauge_cube": gauge_cube, "basins": len(basins),
                          "notes": notes})
    return root


def rebuild_gauges(registry: ModelRegistry, family: str, source_version: str, version: str, out: Path, gauge_cube: str = GAUGE_CUBE,
                   notes: str = "") -> Path:
    """A new version identical to `source_version` (seeds, calibration, statics, temp_gauges.json) except for
    `outflows.json`, rebuilt from the cube's `gauged_outflow_sites`. No retraining: the weights were trained on those lists."""
    src, manifest = registry.fetch(family, source_version)
    root = out / family / version
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(src, root, ignore=shutil.ignore_patterns("manifest.json", ".verified"))
    basins = list(pd.read_parquet(root / "statics.parquet").index.astype(str))
    (root / "outflows.json").write_text(json.dumps(outflow_gauges(gauge_cube, basins)))
    check_gauge_lists(root, gauge_cube)
    kept = {k: manifest[k] for k in ("sources", "cube", "basins") if k in manifest}
    write_manifest(root, {"family": family, "version": version, **kept, "gauge_cube": gauge_cube, "rebuilt_from": source_version,
                          "notes": notes or f"{source_version} with outflows.json rebuilt from the gauge cube's gauged_outflow_sites"})
    return root


def site_list(value: object) -> list[str]:
    """A cube `*_sites` entry ("01417000,01425000", space- or comma-separated; "" for none) as a list."""
    return [s for s in str(value).replace(",", " ").split() if s]


def outflow_gauges(gauge_cube: str, basins: list[str]) -> dict[str, list[str]]:
    """Per basin: the below-dam gauges whose summed discharge is the cube's `gauged_outflow_mm_h`."""
    c = Cube([gauge_cube])
    st = c.stores[0]
    if "gauged_outflow_sites" not in st:
        raise KeyError(f"{gauge_cube} has no gauged_outflow_sites; use the full cube it was derived from ({GAUGE_CUBE})")
    lists = {str(b): site_list(v) for b, v in zip(c.basins, st["gauged_outflow_sites"].values, strict=True)}
    missing = sorted(set(basins) - set(lists))
    if missing:
        raise KeyError(f"{gauge_cube} has no gauge lists for {missing}")
    return {b: lists[b] for b in basins}


def gauge_list_mismatches(statics: pd.DataFrame, outflows: dict[str, list[str]], trained: dict[str, list[str]],
                          temp_gauges: dict[str, dict] | None = None) -> list[str]:
    """Every way a version's serving lists differ from what its model was trained with: the cube's outflow lists, and
    the trained statics that count them (`gauged_outflow_n`, `has_upstream_tw`, `outflow_tw_n`)."""
    problems = []
    for b in statics.index.astype(str):
        row = statics.loc[b]
        served = sorted(outflows.get(b, []))
        if served != sorted(trained.get(b, [])):
            problems.append(f"{b}: outflows {served}, trained with {sorted(trained.get(b, []))}")
        if "gauged_outflow_n" in row and int(row["gauged_outflow_n"]) != len(served):
            problems.append(f"{b}: {len(served)} outflow gauges, trained gauged_outflow_n {int(row['gauged_outflow_n'])}")
        if temp_gauges is None:
            continue
        tg = temp_gauges.get(b, {})
        if "has_upstream_tw" in row and bool(tg.get("upstream")) != bool(row["has_upstream_tw"]):
            problems.append(f"{b}: upstream temperature gauge {tg.get('upstream')}, trained has_upstream_tw {int(row['has_upstream_tw'])}")
        if "outflow_tw_n" in row and len(tg.get("outflow", [])) != int(row["outflow_tw_n"]):
            problems.append(f"{b}: release temperature gauges {tg.get('outflow', [])}, trained outflow_tw_n {int(row['outflow_tw_n'])}")
        if not set(tg.get("outflow", [])) <= set(trained.get(b, [])):
            problems.append(f"{b}: release temperature gauges {tg.get('outflow', [])} aren't among its outflow gauges {sorted(trained.get(b, []))}")
    return problems


def check_gauge_lists(root: Path, gauge_cube: str = GAUGE_CUBE) -> None:
    """Refuse a version whose `outflows.json` / `temp_gauges.json` differ from the lists its model was trained with."""
    statics = pd.read_parquet(root / "statics.parquet")
    statics.index = statics.index.astype(str)
    outflows = json.loads((root / "outflows.json").read_text()) if (root / "outflows.json").exists() else {}
    temp_gauges = json.loads((root / "temp_gauges.json").read_text()) if (root / "temp_gauges.json").exists() else None
    problems = gauge_list_mismatches(statics, outflows, outflow_gauges(gauge_cube, list(statics.index)), temp_gauges)
    if problems:
        raise ValueError(f"{root}: {len(problems)} serving gauge-list mismatches with the training cube:\n  " + "\n  ".join(problems))


def snow_version(version: str, params: SnowParams, out: Path, notes: str = "") -> Path:
    root = out / "snow" / version
    root.mkdir(parents=True, exist_ok=True)
    (root / "params.json").write_text(json.dumps(dataclasses.asdict(params), indent=1, default=int))
    write_manifest(root, {"family": "snow", "version": version, "sources": ["flowcast_pipeline.snow.params.NORTHEAST"], "notes": notes})
    return root


def write_manifest(root: Path, meta: dict) -> dict:
    files = {str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file() and p.name != "manifest.json"}
    manifest = {**meta, "git_sha": _git_sha(), "created": pd.Timestamp.now(tz="UTC").isoformat(), "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def upload(registry: ModelRegistry, root: Path, family: str, version: str) -> None:
    if registry.read_json(f"models/{family}/{version}/manifest.json") is not None:
        raise FileExistsError(f"models/{family}/{version} exists; versions are immutable")
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            registry.s3.upload_file(str(p), registry.bucket, registry._key(f"models/{family}/{version}/{p.relative_to(root)}"))
    registry.s3.upload_file(str(root / "manifest.json"), registry.bucket, registry._key(f"models/{family}/{version}/manifest.json"))


def temperature_gauges(source_cube: str, basins: list[str], train_end: str = "2019-10-01") -> dict[str, dict]:
    """Per temperature basin: the upstream gauge (`upstream_tw_c`) and dam-release gauges (`outflow_tw_c`), chosen as
    `flowcast_model.tempcube.gauge_temperatures` chose them from the source cube (most training-year hours of water
    temperature among the basin's upstream slots; release sites that record any). Reads training years only."""
    c = Cube([source_cube])
    all_basins = [str(b) for b in c.basins]
    st = c.stores[0]
    pos = {b: i for i, b in enumerate(all_basins)}
    slots = st["upstream_slot_site"].values
    outflow_sites = st["gauged_outflow_sites"].values
    cand = set()
    for b in basins:
        i = pos[b]
        cand |= {str(s) for s in slots[i] if str(s) in pos and str(s) != b}
        cand |= {s for s in str(outflow_sites[i]).replace(",", " ").split() if s in pos and s != b}
    train_end = pd.Timestamp(train_end)
    train_hours, any_tw = {}, {}
    for g in sorted(cand):
        tw = c.load_dynamic(g, ["tw_c"], pd.Timestamp("2000-01-01"), FROZEN_TEST_START - pd.Timedelta(hours=1))["tw_c"]
        ok = np.isfinite(tw.to_numpy())
        train_hours[g] = int(ok[tw.index < train_end].sum())
        any_tw[g] = bool(ok.any())
    out = {}
    for b in basins:
        i = pos[b]
        best, best_hours = None, 0
        for s in slots[i]:
            s = str(s)
            if s in train_hours and train_hours[s] > best_hours:
                best, best_hours = s, train_hours[s]
        rel = [s for s in str(outflow_sites[i]).replace(",", " ").split() if any_tw.get(s)]
        out[b] = {"upstream": best, "outflow": rel}
    return out
