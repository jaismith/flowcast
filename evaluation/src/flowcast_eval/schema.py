"""The forecast interchange format the harness reads.

Any forecast source (flowcast models, reference baselines, and the NWS/HEFS/NWM archiver) writes
long-format Parquet with one row per (site, variable, model, issue_time, valid_time, member|quantile):

| column       | type                 | required | notes                                                            |
|--------------|----------------------|----------|------------------------------------------------------------------|
| `site_id`    | string               | yes      | `USGS-01427510` (bare `01427510` is normalized)                  |
| `variable`   | string               | yes      | see `VARIABLES`                                                  |
| `model`      | string               | yes      | e.g. `marfc`, `hefs`, `nwm_medium_range_mem1`, `persistence`     |
| `issue_time` | timestamp (UTC)      | yes      | when the forecast became available (or its nominal cycle time)   |
| `valid_time` | timestamp (UTC)      | yes      | daily variables: midnight UTC of the site-local calendar date    |
| `value`      | float                | yes      | in `unit`                                                        |
| `unit`       | string               | no       | defaults per variable; `m3/s`, `kcfs`, `cfs`, `degF` are converted |
| `lead_h`     | float                | no       | derived if absent: `valid_time - issue_time`; daily variables use 24 x (valid date - issue UTC date) |
| `member`     | int, null            | no       | ensemble member id; null for non-ensemble rows                   |
| `quantile`   | float in (0, 1), null| no       | quantile level; null unless the source publishes quantiles       |
| `run_type`   | string               | no       | `operational` (default), `perfect_forcing`, `simulation`          |
| `qualifier`  | string               | no       | free text (e.g. HEFS trace year, bulletin id)                    |

A row sets at most one of `member` / `quantile`; rows with neither are deterministic.
Common alternative column names (e.g. `source`, `lead_hours`, `ensemble_member`) are accepted via `COLUMN_ALIASES`.
"""

from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

REQUIRED = ["site_id", "variable", "model", "issue_time", "valid_time", "value"]
OPTIONAL = ["unit", "lead_h", "member", "quantile", "run_type", "qualifier"]
COLUMNS = REQUIRED + OPTIONAL

VARIABLES = {
    "discharge": "ft3/s",
    "stage": "ft",
    "water_temperature": "degC",
    "water_temperature_daily_max": "degC",
    "water_temperature_daily_mean": "degC",
}
DAILY_VARIABLES = {"water_temperature_daily_max", "water_temperature_daily_mean"}

COLUMN_ALIASES = {
    "source": "model",
    "system": "model",
    "site": "site_id",
    "location_id": "site_id",
    "monitoring_location_id": "site_id",
    "usgs_site": "site_id",
    "usgs_id": "site_id",
    "issued": "issue_time",
    "issue_time_utc": "issue_time",
    "reference_time": "issue_time",
    "valid": "valid_time",
    "valid_time_utc": "valid_time",
    "lead_hours": "lead_h",
    "lead": "lead_h",
    "ensemble_member": "member",
    "trace": "member",
    "units": "unit",
}
VARIABLE_ALIASES = {
    "flow": "discharge",
    "streamflow": "discharge",
    "q": "discharge",
    "gage_height": "stage",
    "water_temp": "water_temperature",
    "temperature": "water_temperature",
    "water_temp_max": "water_temperature_daily_max",
    "daily_max_water_temperature": "water_temperature_daily_max",
}
_CFS = {"ft3/s", "ft^3/s", "cfs", "ft3 s-1"}
UNIT_CONVERSIONS = {
    ("discharge", "m3/s"): lambda v: v * 35.314666721,
    ("discharge", "m3 s-1"): lambda v: v * 35.314666721,
    ("discharge", "kcfs"): lambda v: v * 1000.0,
    ("stage", "m"): lambda v: v / 0.3048,
    **{(var, "degF"): (lambda v: (v - 32.0) * 5.0 / 9.0) for var in VARIABLES if var.startswith("water_temperature")},
}


class ForecastFormatError(ValueError):
    pass


def normalize_forecasts(df: pd.DataFrame) -> pd.DataFrame:
    """Rename aliases, normalize ids/variables/units/times and derive `lead_h`. Returns the canonical columns."""
    df = df.rename(columns={k: v for k, v in COLUMN_ALIASES.items() if k in df.columns and v not in df.columns}).copy()
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ForecastFormatError(f"forecast frame is missing required columns {missing}")

    df["site_id"] = df["site_id"].astype(str).str.strip().map(lambda s: s if "-" in s else f"USGS-{s}")
    df["variable"] = df["variable"].astype(str).str.lower().replace(VARIABLE_ALIASES)
    unknown = set(df["variable"]) - set(VARIABLES)
    if unknown:
        raise ForecastFormatError(f"unknown variables {sorted(unknown)}; expected one of {sorted(VARIABLES)}")
    df["model"] = df["model"].astype(str)
    for col in ("issue_time", "valid_time"):
        df[col] = pd.to_datetime(df[col], utc=True)
    df["value"] = pd.to_numeric(df["value"], errors="coerce").astype(float)

    if "unit" not in df.columns:
        df["unit"] = df["variable"].map(VARIABLES)
    df["unit"] = df["unit"].fillna(df["variable"].map(VARIABLES)).astype(str)
    df.loc[df["unit"].isin(_CFS), "unit"] = "ft3/s"
    for (var, unit), convert in UNIT_CONVERSIONS.items():
        mask = (df["variable"] == var) & (df["unit"] == unit)
        if mask.any():
            df.loc[mask, "value"] = convert(df.loc[mask, "value"])
            df.loc[mask, "unit"] = VARIABLES[var]
    bad_units = df["unit"] != df["variable"].map(VARIABLES)
    if bad_units.any():
        pairs = df.loc[bad_units, ["variable", "unit"]].drop_duplicates().values.tolist()
        raise ForecastFormatError(f"unsupported units {pairs}")

    derived = (df["valid_time"] - df["issue_time"]).dt.total_seconds() / 3600.0
    daily = df["variable"].isin(DAILY_VARIABLES)
    if daily.any():
        days = (df.loc[daily, "valid_time"].dt.floor("D") - df.loc[daily, "issue_time"].dt.floor("D")).dt.days
        derived[daily] = 24.0 * days
    df["lead_h"] = pd.to_numeric(df["lead_h"], errors="coerce").fillna(derived) if "lead_h" in df.columns else derived
    df["member"] = pd.to_numeric(df["member"], errors="coerce").astype("Int64") if "member" in df.columns else pd.array([pd.NA] * len(df), dtype="Int64")
    df["quantile"] = pd.to_numeric(df["quantile"], errors="coerce").astype(float) if "quantile" in df.columns else np.nan
    if (df["member"].notna() & df["quantile"].notna()).any():
        raise ForecastFormatError("rows may set `member` or `quantile`, not both")
    if ((df["quantile"] <= 0) | (df["quantile"] >= 1)).any():
        raise ForecastFormatError("`quantile` must be in (0, 1)")
    df["run_type"] = df["run_type"].fillna("operational").astype(str) if "run_type" in df.columns else "operational"
    df["qualifier"] = df["qualifier"].astype("string") if "qualifier" in df.columns else pd.array([pd.NA] * len(df), dtype="string")
    return df[COLUMNS].reset_index(drop=True)


def read_forecasts(
    paths: str | Path | Iterable[str | Path],
    site_id: str | None = None,
    variable: str | None = None,
    models: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Read Parquet files or directories (hive partitions allowed) and return normalized forecasts."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    frames = []
    for path in paths:
        path = Path(path)
        dataset = ds.dataset(path, format="parquet", partitioning="hive" if path.is_dir() else None)
        frames.append(dataset.to_table().to_pandas())
    df = normalize_forecasts(pd.concat(frames, ignore_index=True))
    if site_id is not None:
        df = df[df["site_id"] == (site_id if "-" in site_id else f"USGS-{site_id}")]
    if variable is not None:
        df = df[df["variable"] == VARIABLE_ALIASES.get(variable, variable)]
    if models is not None:
        df = df[df["model"].isin(list(models))]
    return df.reset_index(drop=True)


def write_forecasts(df: pd.DataFrame, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    normalize_forecasts(df).to_parquet(path, index=False)
