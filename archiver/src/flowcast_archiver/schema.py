"""One long-format schema shared by every archived forecast product."""

from __future__ import annotations

import pandas as pd
import pyarrow as pa

SCHEMA = pa.schema(
    [
        ("dataset", pa.string()),  # e.g. marfc_nwps, hefs, nwm_short_range
        ("location_id", pa.string()),  # native id: NWS lid, NWM reach, or product site name
        ("usgs_site", pa.string()),  # co-located USGS gauge when known
        ("variable", pa.string()),  # stage_ft, flow_cfs, pool_elev_ft, water_temp_max_f, ...
        ("issue_time", pa.timestamp("s", tz="UTC")),
        ("valid_time", pa.timestamp("s", tz="UTC")),
        ("lead_h", pa.float32()),
        ("member", pa.int16()),  # ensemble member / HEFS trace year; null if deterministic
        ("quantile", pa.float32()),  # for products published as intervals; null otherwise
        ("qualifier", pa.string()),  # product-specific tag (HEFS qualifier, release scenario, rating id)
        ("value", pa.float32()),
        ("fetched_at", pa.timestamp("s", tz="UTC")),
    ]
)

COLUMNS = SCHEMA.names

# Rows are unique on these columns; readers should dedupe on them because an
# issuance can be archived twice if the state file is ever lost.
KEY_COLUMNS = ["dataset", "location_id", "variable", "issue_time", "valid_time", "member", "quantile", "qualifier"]


def to_table(df: pd.DataFrame) -> pa.Table:
    df = df.copy()
    for col in COLUMNS:
        if col not in df:
            df[col] = None
    df = df[COLUMNS]
    for col in ("issue_time", "valid_time", "fetched_at"):
        df[col] = pd.to_datetime(df[col], utc=True).dt.floor("s")
    df["lead_h"] = (df["valid_time"] - df["issue_time"]).dt.total_seconds() / 3600.0
    return pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False)
