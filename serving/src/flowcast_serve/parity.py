"""Parity gates 1-2 (production-architecture.md §3.3), on validation years only (the trainval store ends 2022-09-30).

- `extraction`: the live extractor over a window vs the cube's HRRR analysis, MRMS and GEFS basin means.
- `model`: a live cube assembled from the training cube's arrays for one validation issue, run through the live
  predictor, vs the CMAL mixtures the training hindcast stored for that issue (every stored lead and member).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from flowcast_model.cube import FROZEN_TEST_START, Cube
from flowcast_pipeline.dataset.extract import Plan

from . import forcing, inputs, livecube, predict
from .registry import ModelRegistry


def _rel(a: np.ndarray, b: np.ndarray) -> dict:
    ok = np.isfinite(a) & np.isfinite(b)
    if not ok.any():
        return {"n": 0}
    sa, sb = float(np.sum(a[ok])), float(np.sum(b[ok]))
    return {"n": int(ok.sum()), "sum_rel_diff": abs(sa - sb) / max(abs(sb), 1e-9), "max_abs_diff": float(np.max(np.abs(a[ok] - b[ok]))),
            "nan_mismatch": int((np.isfinite(a) != np.isfinite(b)).sum())}


def extraction(basin: str, start: pd.Timestamp, end: pd.Timestamp, cube: str, plan_dir: Path) -> dict:
    if end >= FROZEN_TEST_START.tz_localize("UTC"):
        raise ValueError("parity windows stay in the validation years (before 2022-10-01)")
    c = Cube([cube])
    out = {}
    for prefix, source in forcing.ANALYSIS.items():
        w = forcing.SiteWeights.from_plan(Plan.load(plan_dir / f"{source}.pkl"), basin)
        live = forcing.extract_analysis(source, {basin: w}, start, end)[basin]
        cols = [f"{prefix}_{v}" for v in live.columns]
        ref = c.load_dynamic(basin, cols, start.tz_convert(None), end.tz_convert(None))
        ref.index = ref.index.tz_localize("UTC")
        out[prefix] = {col: _rel(live[col.removeprefix(prefix + "_")].reindex(ref.index).to_numpy(float), ref[col].to_numpy(float)) for col in cols}
    w = forcing.SiteWeights.from_plan(Plan.load(plan_dir / "gefs_forecast.pkl"), basin)
    names = [f"gefs_{v}" for v in inputs.GEFS_VARS]
    inits, leads, ref, _ = next(iter(c.load_forecast(basin, names, start.tz_convert(None), end.tz_convert(None)).values()))
    if len(inits):
        init = pd.Timestamp(inits[0], tz="UTC")
        live, _ = forcing.extract_gefs("gefs_forecast", {basin: w}, init)
        out["gefs"] = {"init": str(init), **{n: _rel(live[basin][0, :, :, j], ref[0, :, : , j].T) for j, n in enumerate(names)}}
    return out


def model(registry: ModelRegistry, family: str, basin: str, issue: pd.Timestamp, cube: str, mixture_dir: str) -> dict:
    if issue >= FROZEN_TEST_START.tz_localize("UTC") - pd.Timedelta(days=8):
        raise ValueError("parity issues stay in the validation years")
    pointer = registry.production()
    root, _ = registry.fetch(family, pointer[family])
    c = Cube([cube])
    need = predict.required_features(root)
    index = inputs.window(issue)
    dyn = [f for f in need["dynamic"] if c.has(f) and c.kind(f) == "dynamic"]
    dyn += [f"snodas_band_swe_mm_band{k}" for k in range(4) if c.has(f"snodas_band_swe_mm_band{k}") and f"snodas_band_swe_mm_band{k}" not in dyn]
    frame = c.load_dynamic(basin, dyn, index[0].tz_convert(None), min(index[-1].tz_convert(None), FROZEN_TEST_START - pd.Timedelta(hours=1)))
    frame = frame.reindex(index.tz_convert(None))
    frame.index = index
    products = {}
    fc_names = [f for f in need["forecast"] + [f"gefs_{v}" for v in inputs.GEFS_VARS] if c.has(f) and c.kind(f) == "forecast"]
    for product, (inits, leads, values, names) in c.load_forecast(basin, sorted(set(fc_names)), issue.tz_convert(None) - pd.Timedelta(days=2), issue.tz_convert(None)).items():
        prefix = product.removesuffix("_init")
        products[prefix] = livecube.Product(pd.DatetimeIndex(inits).tz_localize("UTC"), leads, {n: np.moveaxis(values[..., j], 2, 1) for j, n in enumerate(names)})
    statics = inputs.statics(root, basin, need["static"])
    with tempfile.TemporaryDirectory() as tmp:
        path = livecube.write(Path(tmp) / "parity.zarr", livecube.LiveBasin(basin, frame, statics, products))
        report = {}
        for seed in predict.seed_dirs(root):
            stored_dir = mixture_dir.replace("{run}", seed.name)
            model_name = "lstm_full_v2_snodas" if family == "flow" else "lstm_temp_v2"
            ref = pd.read_parquet(f"{stored_dir.rstrip('/')}/site_id=USGS-{basin}/{model_name}.parquet")
            ref["issue_time"] = pd.to_datetime(ref["issue_time"], utc=True)
            ref = ref[ref["issue_time"] == issue]
            if ref.empty:
                report[seed.name] = {"error": "issue not in the stored mixtures"}
                continue
            out = predict.predict_seed(seed, path, basin, issue)
            mix = out.mixture  # [lead, member, k, 4]
            k = mix.shape[2]
            diffs = {}
            for j, name in enumerate(("pi", "mu", "b", "tau")):
                live_v = np.stack([mix[int(r.lead_h) - 1, out.members.index(int(r.member)), :, j] for r in ref.itertuples()])
                stored = ref[[f"{name}{c_}" for c_ in range(k)]].to_numpy(float)
                diffs[name] = {"max_abs_diff": float(np.max(np.abs(live_v - stored))), "max_rel_diff": float(np.max(np.abs(live_v - stored) / np.maximum(np.abs(stored), 1e-6)))}
            report[seed.name] = {"rows": len(ref), **diffs}
    return report
