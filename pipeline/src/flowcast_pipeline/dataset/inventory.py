"""Which gauges have instantaneous discharge / water temperature on the USGS Water Data API, and for how long."""

import pandas as pd

from ..usgs.client import WaterDataClient
from ..usgs.params import Parameter
from .config import REGION_BBOX

COLUMNS = ["monitoring_location_id", "parameter_code", "begin_utc", "end_utc", "last_modified"]


def continuous_inventory(client: WaterDataClient, parameter: Parameter, bbox=REGION_BBOX) -> pd.DataFrame:
    """One row per gauge: first and last instantaneous value (UTC) for `parameter` inside `bbox`."""
    rows = client._features(
        "time-series-metadata",
        {
            "parameter_code": parameter.value,
            "computation_period_identifier": "Points",
            "bbox": ",".join(str(v) for v in bbox),
            "properties": ",".join(COLUMNS),
        },
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["site", "begin", "end"])
    df["site"] = df["monitoring_location_id"].str.split("-", n=1).str[1]
    df["begin"] = pd.to_datetime(df["begin_utc"], utc=True, format="ISO8601")
    df["end"] = pd.to_datetime(df["end_utc"], utc=True, format="ISO8601")
    # A gauge can have several IV series (e.g. multiple sensors); keep its full span.
    return df.groupby("site").agg(begin=("begin", "min"), end=("end", "max")).reset_index()
