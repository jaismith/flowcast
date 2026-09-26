"""Basin selection for training cube v1 (rebuild plan §5.3).

Eligible: CAMELSH basins in HUC2 01/02/04/05 with 50-25,000 km2 drainage, at least 10 years of USGS
instantaneous discharge since WY2001, and still reporting in 2026 (so every basin has frozen-test data).
From those, ~500 are drawn by stratified sampling over HUC2 x regulation class so regulated basins are
learned from many dams, with water-temperature gauges and longer records preferred inside each stratum.
"""

import numpy as np
import pandas as pd

from .config import ALWAYS_INCLUDE, AREA_KM2, HUC2, MIN_RECORD_YEARS, RECORD_SINCE, TARGET_BASINS

ACTIVE_SINCE = pd.Timestamp("2026-06-01", tz="UTC")


def regulation_class(attrs: pd.DataFrame) -> pd.Series:
    """none / minor / major from GAGES-II dam counts and normal storage (ML per km2)."""
    major = (attrs["MAJ_NDAMS_2009"] > 0) | (attrs["STOR_NOR_2009"] >= 100)
    minor = ~major & (attrs["NDAMS_2009"] > 0)
    return pd.Series(np.select([major, minor], ["major", "minor"], "none"), index=attrs.index)


def eligible(attrs: pd.DataFrame, inv_q: pd.DataFrame, camelsh_info: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Basins meeting the plan's criteria. Record length counts CAMELSH hourly flow (2000-2024, which is the
    historical target source) except for always-included sites, which are pulled in full from the USGS API."""
    df = attrs.copy()
    df["HUC02"] = df["HUC02"].astype(str).str.zfill(2)
    df = df[df["HUC02"].isin(HUC2) & df["DRAIN_SQKM"].between(*AREA_KM2)]
    df = df.join(inv_q.set_index("site")[["begin", "end"]], how="inner")
    years = [str(y) for y in range(RECORD_SINCE.year, 2025)]
    df["camelsh_years"] = camelsh_info.set_index("STAID")[years].sum(axis=1).reindex(df.index).fillna(0) / 8766.0
    df["usgs_years"] = (df["end"].clip(upper=now) - df["begin"].clip(lower=RECORD_SINCE)).dt.days / 365.25
    forced = df.index.isin(ALWAYS_INCLUDE)
    df["record_years"] = np.where(forced, df["usgs_years"], df["camelsh_years"] + (now - pd.Timestamp("2025-01-01", tz="UTC")).days / 365.25)
    active = (df["end"] >= ACTIVE_SINCE) | forced
    return df[(df["record_years"] >= MIN_RECORD_YEARS) & active]


def select(candidates: pd.DataFrame, has_temperature: set[str], n: int | None = TARGET_BASINS, seed: int = 20260926) -> pd.DataFrame:
    df = candidates.copy()
    n = len(df) if n is None else n
    df["regulation_class"] = regulation_class(df)
    df["has_temperature"] = df.index.isin(list(has_temperature))
    df["forced"] = df.index.isin(ALWAYS_INCLUDE)
    rng = np.random.default_rng(seed)
    df["tiebreak"] = rng.random(len(df))
    df = df.sort_values(["forced", "has_temperature", "record_years", "tiebreak"], ascending=False)

    strata = df.groupby(["HUC02", "regulation_class"])
    quota = (strata.size() / len(df) * n).round().astype(int).clip(lower=1)
    if n >= len(df):
        quota = strata.size()
    chosen = [df[df["forced"]]]
    for key, group in strata:
        take = group[~group["forced"]].head(max(quota[key] - int(group["forced"].sum()), 0))
        chosen.append(take)
    out = pd.concat(chosen)
    out = out[~out.index.duplicated()]
    return out.drop(columns="tiebreak").sort_index()


def early_slice(selected: pd.DataFrame, regulation: pd.DataFrame, n: int = 50, seed: int = 7) -> list[str]:
    """A diverse subset published first: forced sites, then regulated, snowy and other basins spread over HUC2s."""
    df = selected.join(regulation[["below_dam", "gauged_outflow_n"]])
    rng = np.random.default_rng(seed)
    picks = list(df.index[df["forced"]])
    snow = df["SNOW_PCT_PRECIP"].astype(float)
    pools = [
        df[df["below_dam"] > 0],
        df[df["gauged_outflow_n"] > 0],
        df[snow >= snow.quantile(0.8)],
        df[df["regulation_class"] == "none"],
        df,
    ]
    quotas = [8, 6, 10, 6, n]
    for pool, quota in zip(pools, quotas):
        pool = pool[~pool.index.isin(picks)]
        # Round-robin over HUC2 so every region is represented.
        order = pool.assign(r=rng.random(len(pool))).sort_values("r").groupby("HUC02", group_keys=False).apply(
            lambda g: g.assign(k=range(len(g)))
        ).sort_values(["k", "r"])
        picks += list(order.index[: min(quota, n - len(picks))])
        if len(picks) >= n:
            break
    return sorted(picks[:n])
