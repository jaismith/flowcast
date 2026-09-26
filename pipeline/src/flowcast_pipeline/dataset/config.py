"""Constants for training cube v1 (rebuild plan §5.3)."""

import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

VERSION = "v1"
HUC2 = ("01", "02", "04", "05")
AREA_KM2 = (50.0, 25_000.0)
MIN_RECORD_YEARS = 10.0
RECORD_SINCE = pd.Timestamp("2000-10-01", tz="UTC")
# All eligible basins are used (~550); extraction cost is set by source tiles touched, not basin count.
TARGET_BASINS: int | None = None
# CAMELSH hourly flow covers 2000-2024; the USGS API supplies everything from here on (one overlap year for QA).
USGS_TARGETS_FROM = pd.Timestamp("2024-01-01", tz="UTC")

# Target site and its gauged sub-basins are always included.
ALWAYS_INCLUDE = (
    "01427510",  # Delaware at Callicoon (demo)
    "01427207",  # Delaware at Lordville (temperature head-to-head)
    "01425000",  # WB Delaware at Stilesville (below Cannonsville)
    "01417000",  # EB Delaware at Downsville (below Pepacton)
    "01421000",  # EB Delaware at Fishs Eddy
    "01426500",  # WB Delaware at Hale Eddy
    "01420500",  # Beaver Kill at Cooks Falls
    "01423000",  # WB Delaware at Walton
    "01413500",  # EB Delaware at Margaretville
    "01415000",  # Tremper Kill near Andes
)

TIME_START = pd.Timestamp("2000-01-01T00:00", tz="UTC")
TIME_END = pd.Timestamp("2026-09-30T23:00", tz="UTC")


@dataclass(frozen=True)
class Split:
    name: str
    start: pd.Timestamp
    end: pd.Timestamp


SPLITS = (
    Split("train", pd.Timestamp("2000-10-01T00:00", tz="UTC"), pd.Timestamp("2019-09-30T23:00", tz="UTC")),
    Split("validation", pd.Timestamp("2019-10-01T00:00", tz="UTC"), pd.Timestamp("2022-09-30T23:00", tz="UTC")),
    Split("test", pd.Timestamp("2022-10-01T00:00", tz="UTC"), TIME_END),
)

# Physically separate stores so training jobs never download the frozen test years.
# The test store starts early enough to feed a 720 h lookback plus 90-day antecedent features.
STORES = {
    "trainval": (TIME_START, pd.Timestamp("2022-09-30T23:00", tz="UTC")),
    "test": (pd.Timestamp("2022-06-01T00:00", tz="UTC"), TIME_END),
}

REGION_BBOX = (-98.5, 36.0, -66.5, 50.0)  # lon_min, lat_min, lon_max, lat_max

# Same region as the NOAA open-data buckets and the GPU training instances; set via the environment.
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
TAGS = {"project": "flowcast", "component": "dataset"}
BUCKET = os.environ.get("FLOWCAST_DATASET_BUCKET", "flowcast-dataset-257129854363")


def work_dir() -> Path:
    return Path(os.environ.get("FLOWCAST_DATASET_DIR", "/data/flowcast-dataset"))


def hourly_index(start: pd.Timestamp = TIME_START, end: pd.Timestamp = TIME_END) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="h")
