"""Skill page v0 (plan milestone 0.6): the nightly scoring job and the static page it publishes.

One run:

1. Restores the harness cache (NWM operational values, USGS daily values) from the lake, so only new
   NWM cycles are fetched from `noaa-nwm-pds`.
2. Reads hourly discharge and stage for the site from the lake's `obs/` (written by the hourly ingest).
3. Runs the harness: `site_scoreboard` (frozen-test baselines, NWM operational, temperature) and
   `score_forecasts` on the forecast archive: MARFC RVF bulletins since the frozen test began (discharge
   and stage), and every opponent the archiver has captured live (MARFC NWPS, HEFS, NWM, old flowcast).
4. Publishes `index.html`, `v1/sites/{site}/skill.json` and `v1/sites/{site}/scoreboard.md` to the web
   bucket, and a dated copy of the JSON and markdown under the lake's `metrics/`.
"""

from __future__ import annotations

import html
import io
import json
import logging
import os
import tarfile
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from flowcast_pipeline.ingest import ingest_health
from flowcast_pipeline.lake import Lake
from flowcast_pipeline.obs import obs_series
from flowcast_pipeline.sites import Site, get_site

from .archive import FORWARD_DATASETS, archive_summary, read_archive
from .baselines import Air2Stream
from .protocol import FROZEN_TEST
from .scoreboard import _md, _skill_table, render, score_forecasts, site_scoreboard, table

log = logging.getLogger(__name__)

CACHE_KEY = "cache/skill-page/cache.tar.gz"
CACHE_PARTS = ("nwm/operational", "usgs")
PAGE_CACHE_CONTROL = "public, max-age=900"
LEADS = [1, 6, 12, 24, 48, 72, 120, 168]
MARFC_LEADS = [6, 12, 18, 24, 36, 48, 60, 72]
GLANCE_LEADS = [6, 24, 72, 168]
PRELIMINARY_DAYS = 90
# With fewer verified issue days than this, skill is dominated by one or two situations (or undefined when
# persistence happens to be exact), so the page shows the archive count instead of scores.
MIN_VERIFIED_DAYS = 7
FORWARD_TITLES = {
    "marfc": "MARFC deterministic (NWPS)",
    "hefs": "HEFS ensemble",
    "nwm_short_range": "NWM short range (NWPS)",
    "nwm_medium_range_mem1": "NWM medium range member 1 (NWPS)",
    "nwm_medium_range_blend": "NWM medium range blend (NWPS)",
    "nwm_medium_range_ensemble": "NWM medium range ensemble (NWPS)",
    "flowcast_legacy": "Old flowcast model",
}


@dataclass(frozen=True)
class Config:
    site_id: str
    lake_uri: str
    archive_uri: str
    web_uri: str | None = None
    n_boot: int = 1000
    cache_dir: str | None = None
    include_nwm: bool = True

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            site_id=os.environ.get("SITE_ID", "USGS-01427510"),
            lake_uri=os.environ["LAKE_URI"],
            archive_uri=os.environ["ARCHIVE_URI"],
            web_uri=os.environ.get("WEB_URI"),
            n_boot=int(os.environ.get("N_BOOT", "1000")),
            cache_dir=os.environ.get("FLOWCAST_CACHE_DIR"),
        )


# ------------------------------------------------------------------ cache


def restore_cache(lake: Lake, cache_dir: Path) -> int:
    data = lake.read(CACHE_KEY)
    if data is None:
        return 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(cache_dir, filter="data")
    return len(data)


def save_cache(lake: Lake, cache_dir: Path) -> int:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for part in CACHE_PARTS:
            if (cache_dir / part).exists():
                tar.add(cache_dir / part, arcname=part)
    lake.write(CACHE_KEY, buf.getvalue(), "application/gzip")
    return buf.tell()


def load_air2stream(lake: Lake, site: Site) -> Air2Stream | None:
    data = lake.read(f"models/air2stream/{site.id}.json")
    return Air2Stream.from_dict(json.loads(data)) if data else None


# ------------------------------------------------------------------ scoring


def score_archive(archive: Lake, site: Site, q: pd.Series, stage: pd.Series, n_boot: int) -> dict[str, dict[str, pd.DataFrame]]:
    protocol = replace(FROZEN_TEST, n_boot=n_boot)
    results: dict[str, dict[str, pd.DataFrame]] = {}
    marfc = read_archive(archive, ["marfc_rvf"], site.id, since=protocol.test_window[0])
    results["marfc_rvf_discharge"] = score_forecasts(marfc, q, site, "discharge", protocol, name="marfc-rvf-wy2023+")
    if not stage.dropna().empty:
        results["marfc_rvf_stage"] = score_forecasts(marfc, stage, site, "stage", protocol, name="marfc-rvf-stage-wy2023+")
    forward = read_archive(archive, FORWARD_DATASETS, site.id)
    for model in sorted(forward["model"].unique()):
        log.info("forward archive: %s", model)
        results[f"forward_{model}"] = score_forecasts(forward[forward["model"] == model], q, site, "discharge", protocol, name=f"forward-{model}")
    return results


def run(config: Config) -> dict:
    started = time.monotonic()
    lake, archive = Lake(config.lake_uri), Lake(config.archive_uri)
    site = get_site(config.site_id)
    cache_dir = Path(config.cache_dir or os.environ.get("FLOWCAST_CACHE_DIR") or tempfile.mkdtemp(prefix="flowcast-cache-"))
    os.environ["FLOWCAST_CACHE_DIR"] = str(cache_dir)
    log.info("restored %d cache bytes", restore_cache(lake, cache_dir))

    q = obs_series(lake, site.id, "discharge")
    stage = obs_series(lake, site.id, "stage")
    if q.dropna().empty:
        raise RuntimeError(f"no discharge observations for {site.id} in {config.lake_uri}")
    model = load_air2stream(lake, site)

    with tempfile.TemporaryDirectory() as out:
        results = site_scoreboard(
            site.id, out, n_boot=config.n_boot, include_nwm=config.include_nwm, include_retrospective=False, obs=q, air2stream=model
        )
        meta = json.loads((Path(out) / "run.json").read_text())
    fitted = results.get("temperature", {}).get("model")
    if model is None and fitted is not None:
        lake.write(f"models/air2stream/{site.id}.json", json.dumps(fitted.to_dict()).encode(), "application/json")
    save_cache(lake, cache_dir)

    archived = score_archive(archive, site, q, stage, config.n_boot)
    now = pd.Timestamp.now(tz="UTC")
    health = {
        "obs_ingest": ingest_health(lake, now),
        "obs_last": {"discharge": _iso(q.dropna().index.max()), "stage": _iso(stage.dropna().index.max()) if not stage.dropna().empty else None},
        "archive": archive_summary(archive),
    }
    meta |= {"generated": f"{now:%Y-%m-%d %H:%M} UTC", "seconds": round(time.monotonic() - started, 1)}
    payload = build_payload(site, results, archived, meta, health)
    markdown = render(site, results, meta).replace("# Baseline scoreboard", "# Skill scoreboard", 1)
    markdown = markdown.replace("## Protocol", render_archive_md(archived) + "\n## Protocol", 1)
    page = render_html(payload)
    publish(lake, config.web_uri, site, payload, markdown, page, now)
    log.info("skill page built in %.0f s", time.monotonic() - started)
    return payload


def publish(lake: Lake, web_uri: str | None, site: Site, payload: dict, markdown: str, page: str, now: pd.Timestamp) -> None:
    body = json.dumps(payload, default=str).encode()
    lake.write(f"metrics/{site.id}/{now:%Y-%m-%d}/skill.json", body, "application/json")
    lake.write(f"metrics/{site.id}/{now:%Y-%m-%d}/scoreboard.md", markdown.encode(), "text/markdown")
    if web_uri:
        web = Lake(web_uri)
        web.write(f"v1/sites/{site.id}/skill.json", body, "application/json", PAGE_CACHE_CONTROL)
        web.write(f"v1/sites/{site.id}/scoreboard.md", markdown.encode(), "text/markdown; charset=utf-8", PAGE_CACHE_CONTROL)
        web.write("index.html", page.encode(), "text/html; charset=utf-8", PAGE_CACHE_CONTROL)


# ------------------------------------------------------------------ payload


def _iso(t) -> str | None:
    return None if t is None or pd.isna(t) else pd.Timestamp(t).isoformat()


def _records(df: pd.DataFrame | None) -> list[dict]:
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso"))


SECTION_SPECS = {
    # results key: (title, window note, metric, leads, sub-frames)
    "discharge": ("Reference baselines, frozen test WY2023–2026", "4 issues/day; baselines fitted on WY2001–2019", "crps", LEADS, ("scores", "vs_persistence")),
    "nwm_operational": ("NWM v3 operational (noaa-nwm-pds), Jan 2025 onward", "daily 00Z medium-range cycles; short range 6-hourly", "crps", LEADS, ("medium_scores", "medium_vs_persistence")),
    "temperature": ("Daily-maximum water temperature, frozen test", "1 issue/day at 12Z; leads in days", "rmse", [0, 24, 72, 168], ("scores", "vs_persistence")),
}


def build_payload(site: Site, results: dict, archived: dict, meta: dict, health: dict) -> dict:
    sections = []
    for key, (title, note, metric, leads, (scores_key, paired_key)) in SECTION_SPECS.items():
        if key not in results:
            continue
        frames = results[key]
        info = frames.get("info")
        sections.append(
            {
                "id": key, "title": title, "note": note, "metric": metric, "leads": leads,
                "scores": _records(frames[scores_key]), "vs_persistence": _records(frames[paired_key]),
                "info": _records(info)[0] if info is not None and not info.empty else {},
            }
        )
        if key == "nwm_operational":
            sections.append(
                {
                    "id": "nwm_short_range", "title": "NWM v3 short range (noaa-nwm-pds), Jan 2025 onward", "note": "6-hourly cycles, 1–18 h",
                    "metric": "mae", "leads": [1, 3, 6, 12, 18],
                    "scores": _records(frames["short_scores"]), "vs_persistence": _records(frames["short_vs_persistence"]), "info": {},
                }
            )
    for key, frames in archived.items():
        if key.startswith("marfc_rvf"):
            variable = key.removeprefix("marfc_rvf_")
            title = f"MARFC deterministic (CCRN6) from IEM RVF bulletins, WY2023 onward: {variable}"
            note = "bulletin stage; flow through the current USGS rating" if variable == "discharge" else "stage space, independent of the rating"
            metric, leads = "mae", MARFC_LEADS
        else:
            model = key.removeprefix("forward_")
            title = f"Forward archive: {FORWARD_TITLES.get(model, model)}"
            note = "captured live by flowcast-archiver"
            metric, leads = "crps", LEADS
        info = _records(frames["info"])[0] if not frames["info"].empty else {}
        scored = info.get("verified_days", 0) >= MIN_VERIFIED_DAYS
        sections.append(
            {
                "id": key, "title": title, "note": note, "metric": metric, "leads": leads,
                "scores": _records(frames["scores"]) if scored else [],
                "vs_persistence": _records(frames["vs_persistence"]) if scored else [],
                "info": info,
            }
        )
    return {
        "site": {"id": site.id, "name": site.name, "nws_lid": site.nws_lid, "nwm_reach": site.nwm_reach},
        "generated": meta["generated"],
        "fingerprint": meta.get("fingerprint"),
        "n_boot": meta.get("n_boot"),
        "seconds": meta.get("seconds"),
        "health": health,
        "glance": glance(sections),
        "sections": sections,
    }


def glance(sections: list[dict]) -> list[dict]:
    """One row per competitor: its skill vs same-time persistence at a few leads, with the section's window."""
    rows = []
    for s in sections:
        if s["id"] == "temperature" or not s["vs_persistence"]:
            continue
        paired = pd.DataFrame(s["vs_persistence"])
        paired = paired[paired["metric"] == s["metric"]]
        for model, g in paired.groupby("model", sort=False):
            by_lead = {float(r["lead_h"]): r for r in g.to_dict("records")}
            cells = {}
            for lead in GLANCE_LEADS:
                r = by_lead.get(float(lead))
                cells[str(lead)] = None if r is None else {k: r[k] for k in ("skill", "skill_lo", "skill_hi", "better")}
            info = s.get("info", {})
            rows.append({"section": s["id"], "model": model, "metric": s["metric"], "cells": cells, "verified_days": info.get("verified_days")})
    return rows


# ------------------------------------------------------------------ markdown


def render_archive_md(archived: dict) -> str:
    lines = []
    for key, frames in archived.items():
        info = frames["info"].iloc[0] if not frames["info"].empty else {}
        verified = int(info.get("verified_days", 0))
        if frames["scores"].empty or verified < MIN_VERIFIED_DAYS:
            lines += [f"## Archive: {key}", "", f"{int(info.get('issues', 0))} issues archived, {verified} issue days verified; scored from {MIN_VERIFIED_DAYS}.", ""]
            continue
        metric, leads, fmt = ("mae", MARFC_LEADS, "{:.2f}" if key.endswith("stage") else "{:.0f}") if key.startswith("marfc_rvf") else ("crps", LEADS, "{:.0f}")
        unit = "ft" if key.endswith("stage") else "ft3/s"
        first, last = pd.Timestamp(info["first"]), pd.Timestamp(info["last"])
        lines += [
            f"## Archive: {key}",
            "",
            f"{int(info['issues'])} issues from {first:%Y-%m-%d} to {last:%Y-%m-%d}, {int(info['verified_days'])} issue days with verifying obs. "
            "Baselines are issued at the same times.",
            "",
            f"### {metric.upper()} ({unit})",
            "",
            _md(table(frames["scores"], metric, leads, fmt=fmt)),
            "",
            "### KGE",
            "",
            _md(table(frames["scores"], "kge", leads, fmt="{:.3f}", ci=False)),
            "",
            f"### {metric.upper()} skill vs persistence",
            "",
            _md(_skill_table(frames["vs_persistence"], metric, leads)),
            "",
        ]
    return "\n".join(lines)


# ------------------------------------------------------------------ HTML

MODEL_LABELS = {
    "persistence": "Persistence",
    "recession_persistence": "Recession-persistence",
    "climatology": "Climatology",
    "nwm_medium_range_mem1": "NWM medium range, member 1",
    "nwm_medium_range_blend": "NWM medium range blend",
    "nwm_medium_range_ensemble": "NWM medium range ensemble (6)",
    "nwm_short_range": "NWM short range",
    "marfc_rvf": "MARFC (RVF bulletins)",
    "marfc": "MARFC (NWPS)",
    "hefs": "HEFS (65 members)",
    "flowcast_legacy": "Old flowcast model",
    "air2stream_clim_air": "air2stream, climatological air temp",
    "air2stream_obs_air": "air2stream, observed air temp (perfect forcing)",
}
UNITS = {"crps": "ft³/s", "mae": "ft³/s", "rmse": "°C"}

CSS = """
:root{--fg:#0f172a;--muted:#64748b;--line:#e2e8f0;--good:#047857;--good-bg:#ecfdf5;--bad:#b91c1c;--bad-bg:#fef2f2;--accent:#0369a1}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--fg);background:#f8fafc}
main{max-width:1100px;margin:0 auto;padding:32px 20px 64px}h1{font-size:28px;margin:0 0 4px}h2{font-size:19px;margin:40px 0 4px}
.sub{color:var(--muted);margin:0 0 20px}.note{color:var(--muted);font-size:13px;margin:0 0 10px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:12px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{font-weight:600;color:var(--muted);font-size:13px}
td small{display:block;font-size:11px;color:var(--muted)}td.good{background:var(--good-bg)}td.good small{color:var(--good)}td.bad{background:var(--bad-bg)}td.bad small{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.stat b{display:block;font-size:22px}.stat span{color:var(--muted);font-size:13px}
.pill{display:inline-block;font-size:11px;padding:1px 8px;border-radius:99px;background:#fef3c7;color:#92400e;margin-left:6px;vertical-align:middle}
a{color:var(--accent)}footer{margin-top:48px;color:var(--muted);font-size:13px}
"""


def _label(model: str) -> str:
    return MODEL_LABELS.get(model, model)


def _lead_label(lead: float, metric: str) -> str:
    return f"day {lead / 24:g}" if metric == "rmse" else f"{lead:g} h"


def _fmt(value, metric: str) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "–"
    return f"{value:.2f}" if metric == "rmse" or abs(value) < 10 else f"{value:,.0f}"


def _skill_cell(r: dict | None) -> tuple[str, str]:
    if r is None or r.get("skill") is None:
        return "", ""
    cls = "good" if r.get("better") is True else "bad" if r.get("better") is False else ""
    return f"{r['skill']:+.0%} vs persistence", cls


def _section_table(s: dict) -> str:
    metric, leads = s["metric"], s["leads"]
    scores = pd.DataFrame(s["scores"])
    if scores.empty:
        info = s.get("info", {})
        return (f'<p class="note">Accumulating: {info.get("issues", 0)} issues archived, {info.get("verified_days", 0)} issue days verified so far. '
                f"Scores appear once {MIN_VERIFIED_DAYS} issue days verify.</p>")
    scores = scores[scores["metric"] == metric]
    paired = pd.DataFrame(s["vs_persistence"])
    paired = paired[paired["metric"] == metric] if not paired.empty else paired
    leads = [lead for lead in leads if float(lead) in set(scores["lead_h"].astype(float))]
    head = "".join(f"<th>{_lead_label(lead, metric)}</th>" for lead in leads)
    rows = []
    for model in scores["model"].unique():
        cells = []
        for lead in leads:
            v = scores[(scores["model"] == model) & (scores["lead_h"].astype(float) == float(lead))]
            p = paired[(paired["model"] == model) & (paired["lead_h"].astype(float) == float(lead))] if not paired.empty else paired
            text, cls = _skill_cell(p.iloc[0].to_dict() if len(p) else None)
            value = _fmt(v["value"].iloc[0], metric) if len(v) else "–"
            cells.append(f'<td class="{cls}">{value}<small>{text}</small></td>')
        rows.append(f"<tr><td>{html.escape(_label(model))}</td>{''.join(cells)}</tr>")
    unit = "ft" if s["id"].endswith("stage") else UNITS.get(metric, "")
    return f"<table><thead><tr><th>{metric.upper()} ({unit})</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _glance_table(rows: list[dict]) -> str:
    head = "".join(f"<th>{lead} h</th>" for lead in GLANCE_LEADS)
    body = []
    for r in rows:
        if r["model"] == "persistence":
            continue
        cells = []
        for lead in GLANCE_LEADS:
            c = r["cells"].get(str(lead))
            if c is None:
                cells.append("<td>–</td>")
                continue
            cls = "good" if c["better"] is True else "bad" if c["better"] is False else ""
            lo = "" if c["skill_lo"] is None else f"[{c['skill_lo']:+.0%}, {c['skill_hi']:+.0%}]"
            cells.append(f'<td class="{cls}">{c["skill"]:+.0%}<small>{lo}</small></td>')
        days = r.get("verified_days")
        pill = f'<span class="pill">{days} days, preliminary</span>' if days is not None and days < PRELIMINARY_DAYS else ""
        body.append(f"<tr><td>{html.escape(_label(r['model']))} <small>{html.escape(r['section'])} · {r['metric'].upper()}</small>{pill}</td>{''.join(cells)}</tr>")
    return f"<table><thead><tr><th>Skill vs persistence issued at the same times</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _health_html(health: dict) -> str:
    ing = health.get("obs_ingest", {})
    missed = ing.get("missed_fraction")
    stats = [
        (f"{ing.get('days_covered', 0):g} d", "hourly obs ingest running (exit criterion: 7 d)"),
        ("–" if missed is None else f"{missed:.1%}", f"missed cycles, last 7 d ({ing.get('missed_cycles', 0)} of {ing.get('expected_cycles', 0)}; limit 1%)"),
        (health.get("obs_last", {}).get("discharge", "–")[:16].replace("T", " "), "latest discharge obs (UTC)"),
    ]
    grid = "".join(f'<div class="card stat"><b>{html.escape(str(v))}</b><span>{html.escape(t)}</span></div>' for v, t in stats)
    archive_rows = "".join(
        f"<tr><td>{html.escape(d)}</td><td>{html.escape(str(v.get('cursor') or '–'))[:16].replace('T', ' ')}</td><td>{v.get('recent_issuances', 0)}</td></tr>"
        for d, v in health.get("archive", {}).items()
    )
    archive = f'<div class="card"><table><thead><tr><th>Archived dataset</th><th>Newest issuance (UTC)</th><th>Issuances, last 45 d</th></tr></thead><tbody>{archive_rows}</tbody></table></div>'
    return f'<div class="grid">{grid}</div>{archive}'


def render_html(payload: dict) -> str:
    site = payload["site"]
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>flowcast skill: {html.escape(site['name'])}</title><style>{CSS}</style></head><body><main>",
        f"<h1>Forecast skill: {html.escape(site['name'])}</h1>",
        f"<p class=sub>USGS {html.escape(site['id'].removeprefix('USGS-'))} · NWS {html.escape(str(site['nws_lid']))} · NWM reach {site['nwm_reach']} · "
        f"updated {html.escape(payload['generated'])} · scoring fingerprint <code>{html.escape(str(payload['fingerprint']))}</code></p>",
        "<p class=note>Every forecast is scored against observed USGS discharge, together with reference baselines issued at exactly the same times (1 h obs latency). "
        "Skill = 1 − score/persistence. Green or red cells are significantly better or worse than persistence "
        f"(paired 7-day moving-block bootstrap, {payload['n_boot']} draws, 95%). Deterministic forecasts: CRPS = MAE.</p>",
        "<h2>At a glance</h2><p class=note>Each row uses its own section's window and issue times, so compare skill, not raw errors, across rows.</p>",
        f"<div class=card>{_glance_table(payload['glance'])}</div>",
    ]
    for s in payload["sections"]:
        info = s.get("info", {})
        detail = ""
        if info.get("issues") is not None and "verified_days" in info:
            detail = f" · {info['issues']} issues, {info['verified_days']} issue days verified"
            if info.get("first"):
                detail += f" ({str(info['first'])[:10]} to {str(info['last'])[:10]})"
        pill = '<span class="pill">preliminary</span>' if info.get("verified_days") is not None and info["verified_days"] < PRELIMINARY_DAYS else ""
        parts += [f"<h2>{html.escape(s['title'])}{pill}</h2><p class=note>{html.escape(s['note'] + detail)}</p>", f"<div class=card>{_section_table(s)}</div>"]
    parts += [
        "<h2>Pipeline health</h2>",
        _health_html(payload["health"]),
        f"<footer>Full tables with confidence intervals: <a href='v1/sites/{site['id']}/scoreboard.md'>scoreboard.md</a> · "
        f"<a href='v1/sites/{site['id']}/skill.json'>skill.json</a> · built in {payload.get('seconds')} s by the nightly flowcast skill job. "
        "Data: USGS Water Data API, NOAA NWM (noaa-nwm-pds), NWS NWPS and HEFS, IEM AFOS archive. Non-commercial.</footer>",
        "</main></body></html>",
    ]
    return "".join(parts)
