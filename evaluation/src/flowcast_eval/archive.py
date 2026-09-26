"""Read the baseline forecast archive (the `flowcast-archiver` stack) as harness forecasts.

The archiver writes `normalized/{dataset}/month=YYYY-MM/{run_id}.parquet` in its own long schema
(`dataset, location_id, usgs_site, variable, issue_time, valid_time, lead_h, member, quantile, qualifier,
value, fetched_at`). This module maps that onto the interchange format in `schema`:

| archive dataset          | harness model                                          | notes                              |
|--------------------------|--------------------------------------------------------|------------------------------------|
| `marfc_rvf`              | `marfc_rvf`                                            | IEM RVF bulletins (2000+); flow is stage through the current USGS rating |
| `marfc_nwps`             | `marfc`                                                | NWPS JSON; MARFC's own flow        |
| `hefs`                   | `hefs`                                                 | 65-member ensemble (trace years)   |
| `nwm_short_range`        | `nwm_short_range`                                      | via NWPS                           |
| `nwm_medium_range`       | `nwm_medium_range_mem1`, `nwm_medium_range_ensemble`   | members 1-6; the `mean` rows are dropped |
| `nwm_medium_range_blend` | `nwm_medium_range_blend`                               |                                    |
| `flowcast_legacy`        | `flowcast_legacy`                                      | the old flowcast `/forecast`       |

Only `flow_cfs` (discharge) and `stage_ft` (stage) are read. An issuance can be archived twice (both RVF
PILs, or a lost state file), so rows are deduplicated per model, variable, issue, valid time and member.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from flowcast_pipeline.lake import Lake
from flowcast_pipeline.usgs.params import site_id, site_number

from .schema import normalize_forecasts

log = logging.getLogger(__name__)

DATASET_MODELS = {
    "marfc_rvf": "marfc_rvf",
    "marfc_nwps": "marfc",
    "hefs": "hefs",
    "nwm_short_range": "nwm_short_range",
    "nwm_medium_range": "nwm_medium_range",
    "nwm_medium_range_blend": "nwm_medium_range_blend",
    "flowcast_legacy": "flowcast_legacy",
}
VARIABLES = {"flow_cfs": ("discharge", "ft3/s"), "stage_ft": ("stage", "ft")}
FORWARD_DATASETS = ["marfc_nwps", "hefs", "nwm_short_range", "nwm_medium_range", "nwm_medium_range_blend", "flowcast_legacy"]


def archive_keys(archive: Lake, dataset: str, since: pd.Timestamp | None = None) -> list[str]:
    keys = [k for k in archive.list(f"normalized/{dataset}/") if k.endswith(".parquet")]
    if since is not None:
        first = f"month={since:%Y-%m}"
        keys = [k for k in keys if k.split("/")[2] >= first]
    return keys


def _read_rows(archive: Lake, key: str, usgs: str) -> pd.DataFrame:
    table = pq.read_table(pa.BufferReader(archive.read(key)))
    mask = pc.and_(pc.equal(table["usgs_site"], usgs), pc.is_in(table["variable"], pa.array(list(VARIABLES))))
    return table.filter(mask).to_pandas()


def read_archive_raw(archive: Lake, dataset: str, site: str, since: pd.Timestamp | None = None, workers: int = 16) -> pd.DataFrame:
    """Archive rows for one USGS site (flow and stage), in the archiver's schema."""
    keys = archive_keys(archive, dataset, since)
    usgs = site_number(site)
    with ThreadPoolExecutor(workers) as pool:
        frames = [f for f in pool.map(lambda k: _read_rows(archive, k, usgs), keys) if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if since is not None:
        df = df[df["issue_time"] >= since]
    return df


def to_forecasts(raw: pd.DataFrame, site: str) -> pd.DataFrame:
    """Archiver rows -> normalized interchange forecasts (see the table in the module docstring)."""
    if raw.empty:
        return normalize_forecasts(pd.DataFrame(columns=["site_id", "variable", "model", "issue_time", "valid_time", "value"]))
    df = raw[raw["variable"].isin(list(VARIABLES)) & raw["dataset"].isin(list(DATASET_MODELS))].copy()
    df["model"] = df["dataset"].map(DATASET_MODELS)
    df["unit"] = df["variable"].map(lambda v: VARIABLES[v][1])
    df["variable"] = df["variable"].map(lambda v: VARIABLES[v][0])
    df["site_id"] = site_id(site)
    df["member"] = pd.to_numeric(df["member"], errors="coerce").astype("Int64")

    medium = df["model"] == "nwm_medium_range"
    members = df[medium & df["member"].notna()]
    ensemble = members.assign(model="nwm_medium_range_ensemble")
    mem1 = members[members["member"] == 1].assign(model="nwm_medium_range_mem1", member=pd.NA)
    df = pd.concat([df[~medium], ensemble, mem1], ignore_index=True)
    df = df.sort_values("fetched_at", kind="stable").drop_duplicates(["model", "variable", "issue_time", "valid_time", "member"], keep="last")
    cols = ["site_id", "variable", "model", "issue_time", "valid_time", "value", "unit", "member", "qualifier"]
    return normalize_forecasts(df[cols])


def read_archive(archive: Lake, datasets: list[str], site: str, since: pd.Timestamp | None = None) -> pd.DataFrame:
    frames = []
    for dataset in datasets:
        raw = read_archive_raw(archive, dataset, site, since)
        log.info("archive %s: %d rows for %s", dataset, len(raw), site)
        if not raw.empty:
            frames.append(to_forecasts(raw, site))
    if not frames:
        return to_forecasts(pd.DataFrame(), site)
    return pd.concat(frames, ignore_index=True)


def archive_summary(archive: Lake) -> dict[str, dict]:
    """Newest issuance and cursor per dataset, from the archiver's state file."""
    data = archive.read("_state/state.json")
    if data is None:
        return {}
    state = json.loads(data)
    seen, cursors = state.get("seen", {}), state.get("cursors", {})
    return {
        dataset: {"cursor": cursors.get(dataset), "recent_issuances": len(seen.get(dataset, {}))}
        for dataset in sorted(set(seen) | set(cursors))
    }
