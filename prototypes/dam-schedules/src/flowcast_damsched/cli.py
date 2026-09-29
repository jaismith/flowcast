"""flowcast-damsched: fetch the prototype sources, link each dam to its below-dam gauge, and check.

    flowcast-damsched run --out output/damsched            # all sources
    flowcast-damsched run --out output/damsched --only swpa

Writes records.parquet (all sources, one schema), raw payloads under raw/, and checks.md.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from flowcast_pipeline.usgs import WaterDataClient

from . import coverage, link, validate
from .dams import LCRA, SWPA
from .fetch import Fetcher
from .schema import EASTERN, Kind, hourly
from .sources import lcra, release_calendar, safewaters, swpa

log = logging.getLogger("flowcast_damsched")


def _gauge(client: WaterDataClient, lat: float, lon: float, cache: dict) -> dict | None:
    key = (round(lat, 4), round(lon, 4))
    if key not in cache:
        try:
            cache[key] = link.below_dam_gauge(lat, lon, client)
        except (requests.RequestException, KeyError, IndexError) as exc:
            log.warning("linking %s failed: %s", key, exc)
            cache[key] = None
    return cache[key]


def _fmt(x: float, nd: int = 2) -> str:
    return "" if pd.isna(x) else f"{x:.{nd}f}"


def run_swpa(fetcher: Fetcher, client: WaterDataClient, links: dict) -> tuple[pd.DataFrame, list[str]]:
    frames, projects = [], None
    lines = ["## SWPA projected generation vs observed release (last 7 days, nameplate MW -> cfs)", "",
             "Reference: the dam's CWMS hourly outflow where published, else the nearest active USGS gauge downstream.", "",
             "| Project | Reference | km | Lag h | r | Obs/sched volume | MAE sched (cfs) | MAE persistence | MAE persistence + sched change | n h |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    dates = []
    for recs, meta in swpa.collect(fetcher):
        frames.append(recs)
        projects = meta["projects"]
        dates.append((meta["date"], meta["title_date"]))
    df = pd.concat(frames, ignore_index=True)
    mismatch = [d for d, t in dates if t and t != d]
    cap = projects.set_index("project")
    for name, dam in SWPA.items():
        mw = hourly(df, name, Kind.SCHEDULED_GENERATION)
        if mw.empty:
            continue
        cfs = swpa.mw_to_cfs(mw, cap.at[name, "capacity_mw"], cap.at[name, "full_power_cfs"])
        if dam.cwms:
            ref, km, max_lag = f"CWMS {dam.cwms[0]} {dam.cwms[1].split('.')[0]} outflow", "0", 2
            obs = validate.cwms_hourly(*dam.cwms, cfs.index.min(), cfs.index.max() + pd.Timedelta(hours=3))
        else:
            g = _gauge(client, dam.lat, dam.lon, links)
            if g is None:
                lines.append(f"| {name} | none (no CWMS outflow, no active USGS gauge within 30 km) | | | | | | | | |")
                continue
            ref, km, max_lag = f"USGS {g['site']} {g['name'][:28]}", f"{g['km']:.1f}", 12
            obs = validate.gauge_hourly(client, g["site"], cfs.index.min(), cfs.index.max())
        res = validate.hourly_check(cfs, obs, "America/Chicago", max_lag_h=max_lag) if not obs.empty else None
        if res is None:
            lines.append(f"| {name} | {ref} | {km} | no overlapping data | | | | | | |")
            continue
        lines.append(f"| {name} | {ref} | {km} | {res.lag_h} | {_fmt(res.r)} | {_fmt(res.bias)} | {res.mae:.0f} | {res.mae_persistence:.0f} | {res.mae_persist_delta:.0f} | {res.n} |")
    lines += ["", f"Pages fetched: {len(dates)}; body dates {min(d for d, _ in dates)} to {max(d for d, _ in dates)}; "
              f"title date differs from body date on {len(mismatch)} of {len(dates)} pages.", ""]
    return df, lines


def run_safewaters(fetcher: Fetcher, client: WaterDataClient, links: dict) -> tuple[pd.DataFrame, list[str], list[dict]]:
    frames, metas = [], []
    for recs, meta in safewaters.collect(fetcher):
        frames.append(recs)
        metas.append(meta)
    df = pd.concat(frames, ignore_index=True)
    coords = next(m["coords"] for m in metas if "coords" in m)
    sched = df[df["kind"] == Kind.SCHEDULED_RELEASE]
    now = df["fetched_at"].max()
    horizon = sched.groupby("dam")["valid_end"].max().sub(now).dt.total_seconds() / 3600
    lines = ["## Brookfield Safe Waters", "",
             f"- Facility pages: {len(metas)}; with a parsed interval/daily schedule: {sched['dam'].str.split(':').str[0].nunique()}; "
             f"left as notice (other table layouts): {df.loc[df['kind'] == Kind.NOTICE, 'dam'].nunique()}.",
             f"- Schedule horizon past fetch time (h): median {horizon.median():.0f}, max {horizon.max():.0f}; "
             f"{int((horizon <= 0).sum())} facilities show only expired rows.",
             f"- Operator-reported values in the embedded data: {int((df['kind'] == Kind.OBSERVED_RELEASE).sum())} flows, "
             f"{int((df['kind'] == Kind.POOL_ELEVATION).sum())} pool elevations.",
             f"- Facilities with long-term PDF/XLSX calendars: {sum(bool(m['long_term_files']) for m in metas)}.", "",
             "Operator-reported current flow vs the linked gauge's latest hourly value, and the past part of each short-term schedule vs the gauge:", "",
             "| Facility | Gauge | km | Reported now (cfs) | Gauge now (cfs) | Past sched h | Lag h | r | MAE sched | MAE persistence | MAE persistence + sched change |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    observed = df[df["kind"] == Kind.OBSERVED_RELEASE].groupby("dam")["value"].first()
    for slug in sorted(set(sched["dam"].str.split(":").str[0]) | set(observed.index)):
        if slug not in coords:
            continue
        g = _gauge(client, *coords[slug], links)
        if g is None:
            continue
        s = hourly(sched, slug, Kind.SCHEDULED_RELEASE)
        start = min(s.index.min() if len(s) else now, now - pd.Timedelta(days=2))
        obs = validate.gauge_hourly(client, g["site"], start, now)
        past = s[s.index <= (obs.dropna().index.max() if not obs.dropna().empty else now)]
        res = validate.hourly_check(past, obs, "America/New_York", max_lag_h=8) if len(past) >= 12 and not obs.empty else None
        gnow = obs.dropna().iloc[-1] if not obs.dropna().empty else float("nan")
        cells = [f"{res.lag_h}", _fmt(res.r), f"{res.mae:.0f}", f"{res.mae_persistence:.0f}", f"{res.mae_persist_delta:.0f}"] if res else [""] * 5
        lines.append(f"| {slug} | {g['site']} {g['name'][:30]} | {g['km']:.1f} | {_fmt(observed.get(slug, float('nan')), 0)} | {_fmt(gnow, 0)} | {len(past)} | " + " | ".join(cells) + " |")
    lines.append("")
    return df, lines, metas


def run_calendar(fetcher: Fetcher, client: WaterDataClient, links: dict) -> tuple[pd.DataFrame, list[str]]:
    spec = release_calendar.FIFE_BROOK_2026
    got = fetcher.get(spec.url)
    days = release_calendar.parse_pdf(got.body)
    recs = release_calendar.to_records(days, spec, got)
    site = "01168500"  # Deerfield at Charlemont; the gauge right below Fife Brook (near Rowe) stopped in 2011.
    first, last = pd.Timestamp("2026-04-01"), min(pd.Timestamp.now(tz=EASTERN).tz_localize(None).normalize() - pd.Timedelta(days=1), pd.Timestamp("2026-10-31"))
    obs = validate.gauge_hourly(client, site, first.tz_localize(EASTERN), last.tz_localize(EASTERN) + pd.Timedelta(days=1))
    # Releases run 11:30-14:30 at Fife Brook and reach Charlemont (12 km straight-line) roughly 2 h later.
    table = validate.calendar_check(recs, obs, "America/New_York", window=(12, 19), threshold_cfs=0.0, min_rise_cfs=250.0, first=first, last=last)
    sched, seen = table["scheduled"], table["observed"]
    lines = ["## Fife Brook 2026 whitewater calendar (PDF, issued 2026-03-16) vs USGS 01168500 Deerfield at Charlemont", "",
             f"- Calendar release days parsed: {len(recs)} (Apr-Oct).",
             f"- Days checked: {len(table)} ({first.date()} to {last.date()}).",
             f"- Scheduled days with an observed release (rise >= 250 cfs, 12:00-19:00 ET): {int((sched & seen).sum())} of {int(sched.sum())}.",
             f"- Unscheduled days with a similar rise (generation, storms): {int((~sched & seen).sum())} of {int((~sched).sum())}.", ""]
    missed = table[sched & ~seen]["date"].astype(str).tolist()
    if missed:
        lines.append(f"- Scheduled days without a visible release: {', '.join(missed)}")
        lines.append("")
    return recs, lines


def run_lcra(fetcher: Fetcher, client: WaterDataClient, links: dict) -> tuple[pd.DataFrame, list[str]]:
    frames = [recs for recs, _ in lcra.collect(fetcher)]
    df = pd.concat(frames, ignore_index=True)
    lines = ["## LCRA reported hourly dam discharge vs the gauge below (last 14 days)", "",
             "| Dam | Gauge | km | Lag h | r | Gauge/reported volume | MAE (cfs) | n h |", "|---|---|---|---|---|---|---|---|"]
    for name, dam in LCRA.items():
        rep = df[(df["dam"] == name) & (df["kind"] == Kind.OBSERVED_RELEASE)].set_index("valid_start")["value"].sort_index()
        g = _gauge(client, dam.lat, dam.lon, links)
        if g is None or rep.empty:
            lines.append(f"| {name} | none active within 30 km | | | | | | |")
            continue
        obs = validate.gauge_hourly(client, g["site"], rep.index.min(), rep.index.max())
        res = validate.hourly_check(rep, obs, "America/Chicago", max_lag_h=8) if not obs.empty else None
        if res is None:
            lines.append(f"| {name} | {g['site']} | {g['km']:.1f} | no overlapping data | | | | |")
            continue
        lines.append(f"| {name} | {g['site']} {g['name'][:32]} | {g['km']:.1f} | {res.lag_h} | {_fmt(res.r)} | {_fmt(res.bias)} | {res.mae:.0f} | {res.n} |")
    notices = df[df["kind"] == Kind.NOTICE]
    lines += ["", f"Gate-operation notices: {len(notices)}, e.g. \"{notices['note'].iloc[0][:160]}\"" if len(notices) else "", ""]
    return df, lines


RUNNERS = {"swpa": run_swpa, "safewaters": run_safewaters, "calendar": run_calendar, "lcra": run_lcra}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="flowcast-damsched")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch, link and check")
    r.add_argument("--out", type=Path, default=Path("output/damsched"))
    r.add_argument("--only", choices=sorted(RUNNERS), action="append")
    c = sub.add_parser("coverage", help="share of training-basin storage reachable by schedules, release gauges and storage data")
    c.add_argument("--manifest", type=Path, required=True, help="training dataset manifest.json (its 'basins' list)")
    c.add_argument("--cache", type=Path, default=Path("output/damsched/coverage-cache"))
    c.add_argument("--out", type=Path, default=Path("output/damsched"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd == "coverage":
        args.cache.mkdir(parents=True, exist_ok=True)
        sites = json.loads(args.manifest.read_text())["basins"]
        dams, pairs = coverage.dam_coverage(args.cache, sites)
        args.out.mkdir(parents=True, exist_ok=True)
        dams.drop(columns="geometry").to_csv(args.out / "coverage_dams.csv", index=False)
        text = coverage.summarize(dams, pairs)
        (args.out / "coverage.md").write_text(text + "\n")
        print(text)
        return

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(raw_dir=out / "raw")
    client = WaterDataClient()
    links: dict = {}
    frames, report = [], [f"# Dam schedule prototypes: checks ({datetime.now(EASTERN):%Y-%m-%d %H:%M} ET)", ""]
    for name in args.only or list(RUNNERS):
        try:
            result = RUNNERS[name](fetcher, client, links)
        except Exception:
            # One broken source must not stop the others; this is where a repair agent would be woken.
            log.exception("%s failed", name)
            report += [f"## {name}: FAILED (see log)", ""]
            continue
        frames.append(result[0])
        report += result[1]
        log.info("%s: %d records", name, len(result[0]))
    records = pd.concat(frames, ignore_index=True)
    records["kind"] = records["kind"].astype(str)
    records.to_parquet(out / "records.parquet")
    (out / "checks.md").write_text("\n".join(report) + "\n")
    print("\n".join(report))


if __name__ == "__main__":
    main()
