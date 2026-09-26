"""Site scoreboards: reference baselines and NWM scored under the hindcast protocol.

Sections (each written as CSV plus one `scoreboard.md`):

1. Discharge, frozen test (WY2023-2026): persistence, recession-persistence, climatology.
2. NWM v3.0 retrospective: simulation skill, and lead-independent skill vs persistence in WY2020-2022.
3. NWM operational archive (Jan 2025+): short range, medium range member 1, blend and the 6-member
   ensemble against baselines issued at the same cycle times.
4. Daily-max water temperature, frozen test: persistence, climatology, air2stream.
"""

import json
import logging
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

from flowcast_pipeline.obs import hourly_observations
from flowcast_pipeline.sites import Site, get_site
from flowcast_pipeline.usgs import Parameter, Statistic, WaterDataClient

from . import nwm
from .baselines import Air2Stream, Climatology, climatology, daily_persistence, fit_recession, persistence, recession_persistence
from .forcing import era5_daily_air_temperature
from .metrics import score
from .pairs import ForecastCube, lookup_obs, pairs_from_cube, pairs_from_long
from .protocol import FROZEN_TEST, NWM_OPERATIONAL, TEMPERATURE_DAILY, VALIDATION, HindcastProtocol
from .schema import normalize_forecasts, read_forecasts
from .scoring import add_flow_regime, add_season, score_pairs, table

log = logging.getLogger(__name__)

REPORT_LEADS_H = [1, 6, 12, 24, 48, 72, 120, 168]
SHORT_RANGE_LEADS_H = [1, 3, 6, 12, 18]
BASELINES = ["persistence", "recession_persistence", "climatology"]


def discharge_baselines(obs: pd.Series, site: Site, protocol: HindcastProtocol, issues: pd.DatetimeIndex) -> list[ForecastCube]:
    train = obs[protocol.train_window[0] : protocol.train_window[1]]
    leads = protocol.leads_h
    return [
        persistence(obs, issues, leads, site.id, latency_h=protocol.obs_latency_h),
        recession_persistence(obs, issues, leads, site.id, fit_recession(train), latency_h=protocol.obs_latency_h),
        climatology(Climatology.fit(train), issues, leads, site.id, "discharge"),
    ]


def _cube_from_series(name: str, series: pd.Series, site: Site, issues: pd.DatetimeIndex, leads, run_type: str) -> ForecastCube:
    """Lead-independent 'forecast' from a continuous series (e.g. a simulation)."""
    leads = np.asarray(leads, float)
    valid = issues.values[:, None] + (leads * 3600 * 1e9).astype("timedelta64[ns]")[None, :]
    values = lookup_obs(series, pd.DatetimeIndex(valid.ravel()).tz_localize("UTC"), tolerance="1min").reshape(len(issues), len(leads), 1)
    return ForecastCube(name, site.id, "discharge", issues, leads, values, run_type=run_type)


def section_discharge(obs: pd.Series, site: Site, protocol: HindcastProtocol, action_flow: float | None) -> dict[str, pd.DataFrame]:
    issues = protocol.issue_times(until=obs.index.max())
    pairs = pd.concat([pairs_from_cube(c, obs) for c in discharge_baselines(obs, site, protocol, issues)], ignore_index=True)
    scores, vs_persistence = score_pairs(pairs, protocol, reference="persistence")
    _, vs_climatology = score_pairs(pairs, protocol, reference="climatology")
    train = obs[protocol.train_window[0] : protocol.train_window[1]].dropna()
    q25, q90 = train.quantile(0.25), train.quantile(0.90)
    tagged = add_flow_regime(add_season(pairs), q25, q90, action_flow)
    report = replace(protocol, leads_h=(24.0, 72.0, 168.0))
    tagged = tagged[tagged["lead_h"].isin(report.leads_h)]
    season, season_paired = score_pairs(tagged, report, reference="persistence", by=["season"])
    regime, regime_paired = score_pairs(tagged, report, reference="persistence", by=["regime"])
    action = pd.DataFrame()
    if action_flow is not None and tagged["above_action"].any():
        action, _ = score_pairs(tagged[tagged["above_action"]], report, reference="persistence")
    return {
        "scores": scores,
        "vs_persistence": vs_persistence,
        "vs_climatology": vs_climatology,
        "season": season,
        "season_paired": season_paired,
        "regime": regime,
        "regime_paired": regime_paired,
        "above_action": action,
        "info": pd.DataFrame([{"issues": len(issues), "q25": q25, "q90": q90, "action_flow": action_flow}]),
    }


def section_nwm_retrospective(obs: pd.Series, site: Site, protocol: HindcastProtocol) -> dict[str, pd.DataFrame]:
    retro = nwm.retrospective(site.nwm_reach, start="2000-10-01", end="2023-02-01")
    rows = []
    for label, (start, end) in {
        "train WY2001-2019": ("2000-10-01", "2019-09-30T23:00"),
        "validation WY2020-2022": ("2019-10-01", "2022-09-30T23:00"),
    }.items():
        o = obs[start:end]
        sim = retro.reindex(o.index)
        hourly = score(sim.to_numpy(), o.to_numpy())
        daily = score(sim.resample("D").mean().to_numpy(), o.resample("D").mean().to_numpy())
        rows.append({"period": label, "hourly_nse": hourly["nse"], "hourly_kge": hourly["kge"], "hourly_pbias": hourly["pbias"], "daily_nse": daily["nse"], "daily_kge": daily["kge"], "n_hours": int(hourly["n"])})
    issues = protocol.issue_times(until=min(obs.index.max(), retro.index.max()))
    cubes = [*discharge_baselines(obs, site, protocol, issues), _cube_from_series("nwm_retrospective", retro, site, issues, protocol.leads_h, "simulation")]
    pairs = pd.concat([pairs_from_cube(c, obs) for c in cubes], ignore_index=True)
    scores, paired = score_pairs(pairs, protocol, reference="persistence")
    return {"simulation": pd.DataFrame(rows), "scores": scores, "vs_persistence": paired}


def section_nwm_operational(obs: pd.Series, site: Site, protocol: HindcastProtocol) -> dict[str, pd.DataFrame]:
    end = min(obs.index.max(), pd.Timestamp.now(tz="UTC").floor("D") - pd.Timedelta(days=1))
    daily_cycles = pd.date_range(protocol.test_window[0], end, freq="D")
    six_hourly = pd.date_range(protocol.test_window[0], end, freq="6h")

    def fetch(product: str, cycles: pd.DatetimeIndex) -> pd.DataFrame:
        return nwm.operational_forecasts(site.nwm_reach, site.id, product, cycles, protocol.leads_h)

    medium = normalize_forecasts(
        pd.concat(
            [
                fetch("medium_range_mem1", daily_cycles).assign(member=pd.NA),
                fetch("medium_range_blend", daily_cycles),
                nwm.medium_range_ensemble(site.nwm_reach, site.id, daily_cycles),
            ],
            ignore_index=True,
        )
    )
    out: dict[str, pd.DataFrame] = {}
    medium_issues = pd.DatetimeIndex(sorted(medium["issue_time"].unique()))
    pairs = pd.concat(
        [pairs_from_long(medium, obs, protocol.leads_h), *[pairs_from_cube(c, obs) for c in discharge_baselines(obs, site, protocol, medium_issues)]],
        ignore_index=True,
    )
    out["medium_scores"], out["medium_vs_persistence"] = score_pairs(pairs, protocol, reference="persistence")
    _, out["medium_vs_mem1"] = score_pairs(pairs, protocol, reference="nwm_medium_range_mem1")

    short = normalize_forecasts(fetch("short_range", six_hourly))
    short_proto = replace(protocol, leads_h=tuple(h for h in protocol.leads_h if h <= 18))
    short_issues = pd.DatetimeIndex(sorted(short["issue_time"].unique()))
    pairs = pd.concat(
        [pairs_from_long(short, obs, short_proto.leads_h), *[pairs_from_cube(c, obs) for c in discharge_baselines(obs, site, short_proto, short_issues)]],
        ignore_index=True,
    )
    out["short_scores"], out["short_vs_persistence"] = score_pairs(pairs, short_proto, reference="persistence")
    out["info"] = pd.DataFrame([{"medium_cycles": len(medium_issues), "short_cycles": len(short_issues), "first": medium_issues.min(), "last": medium_issues.max()}])
    return out


def _utc_midnight(s: pd.Series) -> pd.Series:
    idx = pd.DatetimeIndex(s.index)
    return s.set_axis(idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC"))


def section_temperature(client: WaterDataClient, site: Site, protocol: HindcastProtocol) -> dict[str, pd.DataFrame]:
    now = pd.Timestamp.now()
    tw = client.daily(site.id, Parameter.WATER_TEMPERATURE, "2000-10-01", now, statistic=Statistic.MAXIMUM).set_index("date")["value"]
    q = client.daily(site.id, Parameter.DISCHARGE, "2000-10-01", now, statistic=Statistic.MEAN).set_index("date")["value"]
    meta = client.monitoring_location(site.id)
    lon, lat = meta["geometry"]["coordinates"]
    air = era5_daily_air_temperature(lat, lon, "2000-10-01", f"{now - pd.Timedelta(days=7):%Y-%m-%d}", site.timezone)["tmax"]
    train_start, train_end = (t.tz_convert(None) for t in protocol.train_window)
    model = Air2Stream.fit(tw[train_start:train_end], air[train_start:train_end], q[train_start:train_end])
    last_day = min(tw.index.max(), air.index.max())
    issues = protocol.issue_times(until=pd.Timestamp(last_day, tz="UTC") + pd.Timedelta(hours=12))
    lead_days = (np.asarray(protocol.leads_h) / 24).astype(int)
    variable = "water_temperature_daily_max"
    cubes = [
        daily_persistence(tw, issues, lead_days, site.id, variable),
        climatology(Climatology.fit(tw[train_start:train_end]), issues, protocol.leads_h, site.id, variable),
        model.forecast(tw, None, q, issues, lead_days, site.id),
        model.forecast(tw, air, q, issues, lead_days, site.id),
    ]
    obs = _utc_midnight(tw)
    pairs = pd.concat([pairs_from_cube(c, obs) for c in cubes], ignore_index=True)
    scores, paired = score_pairs(pairs, protocol, reference="persistence")
    info = pd.DataFrame([{"air2stream_params": model.params.round(4).tolist(), "air2stream_train_rmse": model.rmse_train, "issues": len(issues), "obs_days_in_test": int(obs[protocol.test_window[0] : protocol.test_window[1]].notna().sum())}])
    return {"scores": scores, "vs_persistence": paired, "info": info}


# ---------------------------------------------------------------- rendering


def _hours(c) -> str:
    return f"{c:g} h" if isinstance(c, (int, float)) else str(c)


def _days(c) -> str:
    return f"day {c / 24:g}" if isinstance(c, (int, float)) else str(c)


def _md(df: pd.DataFrame, column_label=_hours) -> str:
    names = [n for n in df.index.names if n]
    header = "| " + " | ".join([" / ".join(names), *[column_label(c) for c in df.columns]]) + " |"
    sep = "|" + "---|" * (len(df.columns) + 1)
    label = lambda i: " / ".join(map(str, i)) if isinstance(i, tuple) else str(i)  # noqa: E731
    rows = ["| " + " | ".join([label(i), *[str(v) for v in r]]) + " |" for i, r in zip(df.index, df.to_numpy())]
    return "\n".join([header, sep, *rows])


def _skill_table(paired: pd.DataFrame, metric: str, leads, by: str | None = None) -> pd.DataFrame:
    p = paired[(paired["metric"] == metric) & paired["lead_h"].isin(leads)].copy()
    mark = p["better"].map({True: " ▲", False: " ▼"}).fillna("")
    p["cell"] = p.apply(lambda r: f"{r['skill']:+.2f} [{r['skill_lo']:+.2f}, {r['skill_hi']:+.2f}]", axis=1) + mark
    index = [by, "model"] if by else "model"
    return p.pivot_table(index=index, columns="lead_h", values="cell", aggfunc="first").fillna("–")


def render(site: Site, results: dict[str, dict[str, pd.DataFrame]], meta: dict) -> str:
    lines = [
        f"# Baseline scoreboard: {site.name} ({site.id})",
        "",
        f"Generated {meta['generated']} by `flowcast-eval scoreboard`; scoring fingerprint `{meta['fingerprint']}`.",
        "Discharge in ft3/s, temperature in degC. Brackets are 95% moving-block bootstrap intervals (7-day blocks, "
        f"{meta['n_boot']} draws). Skill = 1 - score/reference; ▲/▼ = significantly better/worse than the reference.",
        "",
    ]
    if "discharge" in results:
        d = results["discharge"]
        info = d["info"].iloc[0]
        lines += [
            "## 1. Discharge, frozen test WY2023-2026 (hourly, 4 issues/day)",
            "",
            f"{int(info['issues'])} issue times; baselines fitted on WY2001-2019; obs latency 1 h.",
            "",
            "### CRPS (ft3/s)",
            "",
            _md(table(d["scores"], "crps", REPORT_LEADS_H, fmt="{:.0f}")),
            "",
            "### KGE",
            "",
            _md(table(d["scores"], "kge", REPORT_LEADS_H, fmt="{:.3f}", ci=False)),
            "",
            "### NSE",
            "",
            _md(table(d["scores"], "nse", REPORT_LEADS_H, fmt="{:.3f}", ci=False)),
            "",
            "### CRPS skill vs persistence",
            "",
            _md(_skill_table(d["vs_persistence"], "crps", REPORT_LEADS_H)),
            "",
            "### CRPS skill vs climatology (CRPSS)",
            "",
            _md(_skill_table(d["vs_climatology"], "crps", REPORT_LEADS_H)),
            "",
            "### CRPS skill vs persistence by season (issue month)",
            "",
            _md(_skill_table(d["season_paired"], "crps", [24, 72, 168], by="season")),
            "",
            f"### CRPS skill vs persistence by flow regime (low < Q25 = {info['q25']:.0f} ft3/s, high > Q90 = {info['q90']:.0f} ft3/s)",
            "",
            _md(_skill_table(d["regime_paired"], "crps", [24, 72, 168], by="regime")),
            "",
        ]
        if not d["above_action"].empty:
            a = d["above_action"]
            n_pairs = int(a[(a["metric"] == "n") & (a["lead_h"] == 24) & (a["model"] == "persistence")]["value"].sum())
            lines += [
                f"### CRPS when the verifying flow is above NWS action stage (9 ft = {info['action_flow']:.0f} ft3/s)",
                "",
                f"Only {n_pairs} verifying hours at 24 h lead, from a handful of events, so these intervals are rough.",
                "",
                _md(table(a, "crps", [24, 72, 168], fmt="{:.0f}")),
                "",
            ]
    if "nwm_retrospective" in results:
        r = results["nwm_retrospective"]
        lines += [
            "## 2. NWM v3.0 retrospective (AORC-forced simulation, no data assimilation)",
            "",
            "Perfect-forcing simulation, so it is an optimistic reference for the NWM model itself, not a forecast.",
            "",
            _md(r["simulation"].set_index("period").round(3)),
            "",
            "### Lead-independent comparison, validation WY2020-2022: KGE",
            "",
            _md(table(r["scores"], "kge", REPORT_LEADS_H, ci=False)),
            "",
            "### CRPS skill vs persistence (the simulation's error is the same at every lead)",
            "",
            _md(_skill_table(r["vs_persistence"], "crps", REPORT_LEADS_H)),
            "",
        ]
    if "nwm_operational" in results:
        o = results["nwm_operational"]
        info = o["info"].iloc[0]
        lines += [
            f"## 3. NWM v3 operational forecasts, {info['first']:%Y-%m-%d} to {info['last']:%Y-%m-%d}",
            "",
            f"Medium range: {int(info['medium_cycles'])} daily 00Z cycles (member 1, blend, 6-member ensemble). "
            f"Short range: {int(info['short_cycles'])} 6-hourly cycles. Baselines are issued at the same cycle times. "
            "NWM output is available a few hours after its cycle time, so this slightly favors NWM.",
            "",
            "### Medium range: CRPS (ft3/s)",
            "",
            _md(table(o["medium_scores"], "crps", REPORT_LEADS_H, fmt="{:.0f}")),
            "",
            "### Medium range: KGE",
            "",
            _md(table(o["medium_scores"], "kge", REPORT_LEADS_H, fmt="{:.3f}", ci=False)),
            "",
            "### Medium range: CRPS skill vs persistence",
            "",
            _md(_skill_table(o["medium_vs_persistence"], "crps", REPORT_LEADS_H)),
            "",
            "### Short range: MAE (ft3/s) and KGE",
            "",
            _md(table(o["short_scores"], "mae", SHORT_RANGE_LEADS_H, fmt="{:.0f}")),
            "",
            _md(table(o["short_scores"], "kge", SHORT_RANGE_LEADS_H, fmt="{:.3f}", ci=False)),
            "",
        ]
    if "temperature" in results:
        t = results["temperature"]
        info = t["info"].iloc[0]
        lines += [
            "## 4. Daily-maximum water temperature, frozen test WY2023-2026 (1 issue/day at 12Z)",
            "",
            f"{int(info['issues'])} issues, {int(info['obs_days_in_test'])} observed days. air2stream fitted on WY2001-2019 daily maxima "
            f"(train RMSE {info['air2stream_train_rmse']:.2f} degC); `obs_air` uses ERA5 air temperature (perfect forcing), "
            "`clim_air` uses day-of-year air-temperature climatology (operationally fair). Leads are days after the issue date.",
            "",
            "### RMSE (degC)",
            "",
            _md(table(t["scores"], "rmse", list(TEMPERATURE_DAILY.leads_h), fmt="{:.2f}"), _days),
            "",
            "### CRPS (degC)",
            "",
            _md(table(t["scores"], "crps", list(TEMPERATURE_DAILY.leads_h), fmt="{:.2f}"), _days),
            "",
            "### RMSE skill vs persistence",
            "",
            _md(_skill_table(t["vs_persistence"], "rmse", list(TEMPERATURE_DAILY.leads_h)), _days),
            "",
        ]
    lines += ["## Protocol", "", "```json", json.dumps(meta["protocols"], indent=2, default=str), "```", ""]
    return "\n".join(lines)


def _write(out: Path, name: str, frames: dict[str, pd.DataFrame]) -> None:
    for key, frame in frames.items():
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            frame.to_csv(out / f"{name}_{key}.csv", index=False)


def site_scoreboard(site_id: str, out_dir: str, n_boot: int = 1000, include_nwm: bool = True, include_temperature: bool = True) -> dict:
    site = get_site(site_id)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    client = WaterDataClient()
    now = pd.Timestamp.now(tz="UTC")
    obs = hourly_observations(client, site.id, "discharge", "2000-10-01", now)
    action_flow = None
    if "action" in site.stage_thresholds_ft:
        action_flow = float(client.rating(site.id).stage_to_discharge(site.stage_thresholds_ft["action"]))

    frozen = replace(FROZEN_TEST, n_boot=n_boot)
    results: dict[str, dict[str, pd.DataFrame]] = {}
    log.info("discharge frozen test")
    results["discharge"] = section_discharge(obs, site, frozen, action_flow)
    protocols = {"discharge": asdict(frozen)}
    if include_nwm and site.nwm_reach:
        log.info("NWM retrospective")
        validation = replace(VALIDATION, n_boot=n_boot)
        results["nwm_retrospective"] = section_nwm_retrospective(obs, site, validation)
        log.info("NWM operational")
        operational = replace(NWM_OPERATIONAL, n_boot=n_boot)
        results["nwm_operational"] = section_nwm_operational(obs, site, operational)
        protocols |= {"nwm_retrospective": asdict(validation), "nwm_operational": asdict(operational)}
    if include_temperature and "water_temperature" in site.variables:
        log.info("temperature")
        temperature = replace(TEMPERATURE_DAILY, n_boot=n_boot)
        results["temperature"] = section_temperature(client, site, temperature)
        protocols["temperature"] = asdict(temperature)

    meta = {"generated": f"{now:%Y-%m-%d %H:%M} UTC", "fingerprint": frozen.fingerprint(), "n_boot": n_boot, "protocols": protocols, "obs_last": str(obs.dropna().index.max())}
    for name, frames in results.items():
        _write(out, name, frames)
    (out / "run.json").write_text(json.dumps(meta, indent=2, default=str))
    (out / "scoreboard.md").write_text(render(site, results, meta))
    return results


def site_or_stub(site_id: str) -> Site:
    """Registry entry for a site, or a bare `Site` for gauges that aren't forecast sites (e.g. training basins)."""
    try:
        return get_site(site_id)
    except KeyError:
        sid = site_id if "-" in site_id else f"USGS-{site_id}"
        return Site(id=sid, name=sid)


def score_forecasts(
    forecasts: pd.DataFrame,
    obs: pd.Series,
    site: Site,
    protocol: HindcastProtocol,
    nwm_reach: int | None = None,
    references: tuple[str, ...] = ("persistence",),
) -> dict[str, pd.DataFrame]:
    """Score normalized discharge forecasts for one site against baselines issued at exactly the same times.

    The protocol window is narrowed to the forecasts' issue range. With `nwm_reach`, the NWM v3.0 retrospective
    (a perfect-forcing simulation, lead-independent) joins as a reference wherever it covers the window.
    Returns `scores` and one `vs_<reference>` paired table per reference.
    """
    issues = pd.DatetimeIndex(sorted(forecasts["issue_time"].unique()))
    start, end = protocol.test_window
    issues = issues[(issues >= start) & (issues <= end)]
    if issues.empty:
        raise ValueError(f"no forecast issue times inside the {protocol.name} window {start} to {end}")
    forecasts = forecasts[forecasts["issue_time"].isin(issues)]
    protocol = protocol.with_window(f"{issues.min():%Y-%m-%dT%H:%M}", f"{issues.max():%Y-%m-%dT%H:%M}")
    cubes = discharge_baselines(obs, site, protocol, issues)
    if nwm_reach:
        retro = nwm.retrospective(int(nwm_reach), start=f"{issues.min() - pd.Timedelta(days=1):%Y-%m-%d}", end=f"{issues.max() + pd.Timedelta(days=8):%Y-%m-%d}")
        if not retro.dropna().empty:
            cubes.append(_cube_from_series("nwm_retrospective", retro, site, issues, protocol.leads_h, "simulation"))
    pairs = pd.concat([pairs_from_long(forecasts, obs, protocol.leads_h), *[pairs_from_cube(c, obs) for c in cubes]], ignore_index=True)
    out: dict[str, pd.DataFrame] = {}
    for ref in references:
        if ref not in set(pairs["model"]):
            continue
        scores, paired = score_pairs(pairs, protocol, reference=ref)
        out.setdefault("scores", scores)
        out[f"vs_{ref}"] = paired
    return out


def score_archived_forecasts(
    paths: list[str],
    site_id: str,
    variable: str,
    out_dir: str,
    obs: pd.Series | None = None,
    protocol: HindcastProtocol = FROZEN_TEST,
    nwm_reach: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Score interchange-format forecasts against baselines issued at exactly the same times (plan §8.3).

    `obs` defaults to USGS hourly observations; `protocol` fixes the fitting years and the allowed issue window
    (use `VALIDATION` to keep scoring out of the frozen test years).
    """
    if variable != "discharge":
        raise NotImplementedError("archived-forecast scoring currently covers discharge")
    site = site_or_stub(site_id)
    forecasts = read_forecasts(paths, site_id=site.id, variable=variable)
    if obs is None:
        obs = hourly_observations(WaterDataClient(), site.id, variable, "2000-10-01", protocol.test_window[1])
    obs = obs[: protocol.test_window[1]]
    results = score_forecasts(forecasts, obs, site, protocol, nwm_reach=nwm_reach)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for key, frame in results.items():
        frame.to_csv(out / f"{key}.csv", index=False)
    return results["scores"], results["vs_persistence"]
