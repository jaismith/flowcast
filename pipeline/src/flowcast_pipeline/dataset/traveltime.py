"""Travel time to the basin outlet for every AORC cell, and isochrone zones built from it.

Per cell: channel time along NHDPlusV2 flowlines from the nearest point on the nearest flowline down to the
outlet gauge (mean-annual EROM velocities; waterbody flowlines at the v1.2 wave celerity), plus an
overland/hillslope term, the straight-line distance from the cell centre to that flowline at
`HILLSLOPE_VELOCITY_MS`.

Zones:

* coarse: 0-6 h, 6-24 h, 24 h+;
* hourly: one bin per hour out to `HOURLY_CAP_H`, with everything beyond lumped into a final bin.

Zone weights are the basin's AORC cell weights restricted to the cells in each zone, so zone means use the same
area weighting as the basin means.
"""

import json
import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import scipy.sparse as sp
from shapely import STRtree
from shapely.geometry import shape

from .grids import Grid
from .upstream import Network, _get, site_feature

log = logging.getLogger(__name__)

NLDI_SITE = "https://api.water.usgs.gov/nldi/linked-data/nwissite"
HILLSLOPE_VELOCITY_MS = 0.1  # overland + small-channel flow not represented by NHDPlus flowlines
COARSE_EDGES_H = (0.0, 6.0, 24.0)  # bins [0,6), [6,24), [24, inf)
HOURLY_CAP_H = 72  # bins [0,1) ... [71,72), then [72, inf)
N_COARSE = len(COARSE_EDGES_H)
N_HOURLY = HOURLY_CAP_H + 1
EQUAL_AREA = "EPSG:5070"


def flowlines(cache: Path, site: str) -> gpd.GeoDataFrame:
    path = cache / "flowlines" / f"{site}.json"
    if path.exists():
        data = json.loads(path.read_text())
    else:
        resp = _get(f"{NLDI_SITE}/USGS-{site}/navigation/UT/flowlines", {"distance": 9999, "f": "json"})
        data = resp.json() if resp.status_code == 200 else {"features": []}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    feats = data.get("features", [])
    if not feats:
        return gpd.GeoDataFrame({"comid": []}, geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(
        {"comid": [int(f["properties"]["nhdplus_comid"]) for f in feats]}, geometry=[shape(f["geometry"]) for f in feats], crs="EPSG:4326"
    )


def downstream_end_hours(net: Network, comids: list[int], outlet: int, outlet_measure: float) -> dict[int, float]:
    """Hours from each flowline's downstream end to the outlet gauge; flowlines that don't drain to it are left out."""
    f_out = net._frac(outlet, outlet_measure)
    up_end = {outlet: net._hours(outlet, 1.0 - f_out)}  # outlet flowline's upstream end -> gauge
    down_end = {outlet: -net._hours(outlet, f_out)}  # its downstream end lies below the gauge
    by_hs = net.by_hydroseq
    for c in comids:
        if c in down_end or c not in net.v.index:
            continue
        chain, cur = [], c
        while cur not in down_end:
            dn = net.v.at[cur, "dnhydroseq"]
            if dn <= 0 or dn not in by_hs.index or len(chain) > 20_000:
                chain = None
                break
            chain.append(cur)
            cur = int(by_hs.loc[dn])
            if cur not in net.v.index:
                chain = None
                break
        if chain is None:
            continue
        # cur is known; walk back up the chain: a flowline's downstream end is its downstream neighbour's upstream end.
        for x in reversed(chain):
            dn = int(by_hs.loc[net.v.at[x, "dnhydroseq"]])
            base = up_end[dn] if dn in up_end else down_end[dn] + net._hours(dn, 1.0)
            down_end[x] = base
            up_end[x] = base + net._hours(x, 1.0)
    return down_end


def cell_travel_hours(cells: np.ndarray, grid: Grid, lines: gpd.GeoDataFrame, down_end: dict[int, float], net: Network) -> np.ndarray:
    """Travel hours to the outlet for flat grid cell indices `cells` (NaN if no usable flowline)."""
    lines = lines[lines["comid"].isin(list(down_end))]
    if lines.empty:
        return np.full(len(cells), np.nan)
    iy, ix = np.divmod(cells, grid.nx)
    pts = gpd.GeoSeries(gpd.points_from_xy(grid.x0 + grid.dx * ix, grid.y0 + grid.dy * iy), crs=grid.crs).to_crs(EQUAL_AREA)
    geoms = lines.to_crs(EQUAL_AREA).geometry.to_numpy()
    tree = STRtree(geoms)
    nearest = tree.nearest(pts.to_numpy())
    out = np.empty(len(cells))
    comids = lines["comid"].to_numpy()
    for k, (p, j) in enumerate(zip(pts.to_numpy(), nearest)):
        line = geoms[j]
        c = int(comids[j])
        frac_from_down = 1.0 - line.project(p, normalized=True)  # NHDPlus flowlines are digitised upstream -> downstream
        channel = down_end[c] + net._hours(c, frac_from_down)
        hill = line.distance(p) / HILLSLOPE_VELOCITY_MS / 3600.0
        out[k] = max(channel, 0.0) + hill
    return out


def zone_index(hours: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    coarse = np.searchsorted(np.asarray(COARSE_EDGES_H), hours, side="right") - 1
    hourly = np.minimum(np.floor(hours), HOURLY_CAP_H).astype(int)
    return np.clip(coarse, 0, N_COARSE - 1), np.clip(hourly, 0, N_HOURLY - 1)


def zone_weights(
    basins: list[str], w_basins: sp.csr_matrix, grid: Grid, cache: Path, net: Network
) -> tuple[sp.csr_matrix, sp.csr_matrix, pd.DataFrame]:
    """(coarse weights: basin*3 + k rows, hourly weights: basin*73 + k rows, per-basin travel-time summary)."""
    rows_c, cols_c, vals_c, rows_h, cols_h, vals_h, summary = [], [], [], [], [], [], []
    for b, site in enumerate(basins):
        lo, hi = w_basins.indptr[b], w_basins.indptr[b + 1]
        cells, w = w_basins.indices[lo:hi], w_basins.data[lo:hi]
        feat = site_feature(cache, site)
        lines = flowlines(cache, site)
        hours = np.full(len(cells), np.nan)
        if feat and feat.get("comid") and int(feat["comid"]) in net.v.index and not lines.empty:
            down = downstream_end_hours(net, lines["comid"].tolist(), int(feat["comid"]), float(feat["measure"] or 50.0))
            hours = cell_travel_hours(cells, grid, lines, down, net)
        ok = np.isfinite(hours)
        if not ok.any():  # no NHDPlus network for this gauge: the whole basin is one near-outlet zone
            hours = np.zeros(len(cells))
            ok = np.ones(len(cells), bool)
        hours = np.where(ok, hours, np.nanmedian(hours[ok]))
        ci, hi_ = zone_index(hours)
        rows_c.append(b * N_COARSE + ci), cols_c.append(cells), vals_c.append(w)
        rows_h.append(b * N_HOURLY + hi_), cols_h.append(cells), vals_h.append(w)
        wsum = w.sum()
        summary.append(
            {
                "basin": site,
                "tt_mean_h": float(np.sum(w * hours) / wsum),
                "tt_p90_h": float(np.quantile(hours, 0.9)),
                "tt_max_h": float(hours.max()),
                "tt_missing_frac": float(1 - ok.mean()),
                "n_flowlines": len(lines),
            }
        )
        if (b + 1) % 50 == 0:
            log.info("travel-time zones %d/%d", b + 1, len(basins))
    shape_ = w_basins.shape[1]
    wc = sp.csr_matrix((np.concatenate(vals_c), (np.concatenate(rows_c), np.concatenate(cols_c))), shape=(len(basins) * N_COARSE, shape_))
    wh = sp.csr_matrix((np.concatenate(vals_h), (np.concatenate(rows_h), np.concatenate(cols_h))), shape=(len(basins) * N_HOURLY, shape_))
    return wc, wh, pd.DataFrame(summary).set_index("basin")
