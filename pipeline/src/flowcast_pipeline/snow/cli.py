"""flowcast-snow: build HRUs, run the snow module on a forcing file, benchmark throughput."""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import shape

from .dataset_step import snow_features_for_basin
from .features import LSTM_FEATURES, band_states
from .hru import HRUSet, build_hrus, fetch_nldi_basin
from .model import AORC_NAMES, AORC_RADIATION_LABEL, forcing_from_aorc, run_snow
from .params import NORTHEAST, HruParams
from .snow17 import N_OUT, day_number_from_march21, initial_state, snow17_kernel
from .terrain import flat_lut


def _subbasins_from_geojson(path: Path, id_field: str) -> dict[str, shapely.Geometry]:
    data = json.loads(path.read_text())
    return {str(f["properties"][id_field]): shape(f["geometry"]) for f in data["features"]}


def cmd_build_hrus(args) -> None:
    if args.geojson:
        subbasins = _subbasins_from_geojson(Path(args.geojson), args.id_field)
    else:
        subbasins = {args.site: fetch_nldi_basin(args.site)}
    hrus = build_hrus(subbasins, n_bands=args.n_bands, n_aspects=args.n_aspects, forest_frac=args.forest_frac)
    out = hrus.save(args.out)
    print(f"{hrus.n} HRUs -> {out}")


def cmd_run(args) -> None:
    hrus = HRUSet.load(args.hrus)
    forcing = pd.read_parquet(args.forcing)
    if "time" in forcing.columns:
        forcing = forcing.set_index("time")
    feats = snow_features_for_basin(hrus, forcing, spinup=args.spinup)
    feats.to_parquet(args.out)
    print(f"{len(feats)} h x {len(LSTM_FEATURES)} features -> {args.out}")
    if args.band_states:
        radiation_label = None
        if any(name in forcing for name in AORC_NAMES):
            forcing = forcing_from_aorc(forcing)
            radiation_label = AORC_RADIATION_LABEL
        result = run_snow(hrus, forcing, radiation_label=radiation_label)
        band_states(result).to_dataframe().reset_index().to_parquet(args.band_states)
        print(f"band states -> {args.band_states}")


def cmd_benchmark(args) -> None:
    """Kernel and end-to-end throughput on synthetic hourly forcing."""
    nh, nt = args.hrus, args.years * 8766
    rng = np.random.default_rng(0)
    times = pd.date_range("2000-10-01 01:00", periods=nt, freq="h")
    doy = times.dayofyear.to_numpy()
    ta = (-2 - 10 * np.cos(2 * np.pi * (doy - 20) / 365))[:, None] + rng.normal(0, 3, (nt, nh))
    px = np.where(rng.random((nt, nh)) < 0.07, rng.gamma(0.8, 2.0, (nt, nh)), 0.0)
    fr = np.clip((1.5 - ta) / 2.0, 0, 1)
    ea = np.full((nt, nh), 5.0)
    pa = np.full((nt, nh), 950.0)
    wind = np.full((nt, nh), 3.0)
    sw = np.full((nt, nh), 150.0)
    hp = HruParams.build(NORTHEAST, np.full(nh, 42.0), np.zeros(nh))
    idn = day_number_from_march21(times)
    small = slice(0, 48)
    snow17_kernel(ta[small], px[small], fr[small], ea[small], pa[small], wind[small], sw[small], idn[small], 1,
                  hp.values, hp.adc, hp.flags, initial_state(nh), np.zeros((N_OUT, 48, nh), np.float32))  # fmt: skip
    out = np.zeros((N_OUT, nt, nh), np.float32)
    t0 = time.perf_counter()
    snow17_kernel(ta, px, fr, ea, pa, wind, sw, idn, 1, hp.values, hp.adc, hp.flags, initial_state(nh), out)
    kernel_s = time.perf_counter() - t0
    print(f"kernel: {nh} HRUs x {nt} h = {nh * nt / 1e6:.0f}M HRU-steps in {kernel_s:.2f} s "
          f"({nh * nt / kernel_s / 1e6:.0f}M steps/s)")  # fmt: skip

    n_basin_hru = 8
    table = pd.DataFrame(
        {
            "hru_id": [f"b:{i}" for i in range(n_basin_hru)],
            "subbasin_id": "b",
            "band_id": [f"b:b{i // 2 + 1}" for i in range(n_basin_hru)],
            "band": [i // 2 + 1 for i in range(n_basin_hru)],
            "elev_mean": np.linspace(300, 900, n_basin_hru),
            "area_km2": 10.0,
            "lat": 42.0,
            "lon": -74.5,
            "svf": 0.97,
            "forest_frac": 0.0,
        }
    )
    hrus = HRUSet(table, flat_lut(n_basin_hru))
    forcing = pd.DataFrame(
        {"precip": px[:, 0], "air_temperature": ta[:, 0], "specific_humidity": 0.003, "surface_pressure": 95000.0,
         "wind_speed": 3.0, "shortwave_down": 150.0},
        index=times,
    )  # fmt: skip
    t0 = time.perf_counter()
    snow_features_for_basin(hrus, forcing)
    basin_s = time.perf_counter() - t0
    print(f"end to end: one basin ({n_basin_hru} HRUs, {args.years} years hourly) in {basin_s:.1f} s "
          f"-> 500 basins ~ {500 * basin_s / 60:.0f} min on one core")  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowcast-snow", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-hrus", help="sub-basins x elevation bands x aspect classes with terrain factors")
    b.add_argument("--site", help="USGS site; basin polygon from the NLDI")
    b.add_argument("--geojson", help="sub-basin FeatureCollection instead of --site")
    b.add_argument("--id-field", default="subbasin_id")
    b.add_argument("--n-bands", type=int, default=4)
    b.add_argument("--n-aspects", type=int, default=2, choices=[1, 2, 4])
    b.add_argument("--forest-frac", type=float, default=0.0)
    b.add_argument("--out", required=True)
    b.set_defaults(func=cmd_build_hrus)
    r = sub.add_parser("run", help="LSTM features from an hourly basin-mean forcing Parquet (AORC or canonical names)")
    r.add_argument("--hrus", required=True)
    r.add_argument("--forcing", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--spinup")
    r.add_argument("--band-states", help="also write per-band states (long Parquet) for maps")
    r.set_defaults(func=cmd_run)
    k = sub.add_parser("benchmark")
    k.add_argument("--hrus", type=int, default=500)
    k.add_argument("--years", type=int, default=26)
    k.set_defaults(func=cmd_benchmark)
    args = parser.parse_args(argv)
    if args.cmd == "build-hrus" and not (args.site or args.geojson):
        parser.error("build-hrus needs --site or --geojson")
    args.func(args)


if __name__ == "__main__":
    main()
