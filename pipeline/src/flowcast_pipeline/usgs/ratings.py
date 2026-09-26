"""Stage-discharge rating curves from the USGS Water Data STAC `ratings` collection (NWIS RDB files)."""

import io
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

_HEADER_KV = re.compile(r'(\w+)="?([^"\s]*)"?')


def parse_rdb(text: str) -> tuple[pd.DataFrame, dict[str, dict[str, str]]]:
    """Parse an NWIS RDB file into (table, header) where header maps `//KEY` lines to their key=value pairs."""
    header: dict[str, dict[str, str]] = {}
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            content = line.lstrip("# ").strip()
            if content.startswith("//"):
                key, _, rest = content[2:].partition(" ")
                header.setdefault(key, {}).update(dict(_HEADER_KV.findall(rest)))
        elif line.strip():
            body.append(line)
    if len(body) < 2:
        raise ValueError("RDB file has no table")
    # body[1] is the RDB column-format line (e.g. "16N\t16N\t1S").
    table = pd.read_csv(io.StringIO("\n".join([body[0], *body[2:]])), sep="\t", dtype=str)
    for col in table.columns:
        if col != "STOR":
            table[col] = pd.to_numeric(table[col], errors="coerce")
    return table, header


@dataclass(frozen=True)
class RatingCurve:
    """A monotone stage (ft) -> discharge (ft3/s) table. Values outside the table's range map to NaN."""

    site_id: str
    kind: str
    stage_ft: np.ndarray
    discharge_cfs: np.ndarray
    rating_id: str | None = None
    retrieved: str | None = None
    header: dict[str, dict[str, str]] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_rdb(cls, site_id: str, kind: str, text: str) -> "RatingCurve":
        table, header = parse_rdb(text)
        table = table.dropna(subset=["INDEP", "DEP"]).sort_values("INDEP")
        stage = table["INDEP"].to_numpy(float)
        flow = table["DEP"].to_numpy(float)
        if np.any(np.diff(flow) < 0):
            raise ValueError(f"rating for {site_id} is not monotone in discharge")
        retrieved = None
        for line in text.splitlines()[:10]:
            if "RETRIEVED:" in line:
                retrieved = line.split("RETRIEVED:", 1)[1].strip()
        return cls(
            site_id=site_id,
            kind=kind,
            stage_ft=stage,
            discharge_cfs=flow,
            rating_id=header.get("RATING", {}).get("ID"),
            retrieved=retrieved,
            header=header,
        )

    def stage_to_discharge(self, stage_ft) -> np.ndarray:
        return np.interp(np.asarray(stage_ft, float), self.stage_ft, self.discharge_cfs, left=np.nan, right=np.nan)

    def discharge_to_stage(self, discharge_cfs) -> np.ndarray:
        # EXSA tables can hold repeated discharges at the bottom; np.interp needs strictly increasing x.
        flow, idx = np.unique(self.discharge_cfs, return_index=True)
        return np.interp(np.asarray(discharge_cfs, float), flow, self.stage_ft[idx], left=np.nan, right=np.nan)
