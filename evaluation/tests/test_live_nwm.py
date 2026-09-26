"""Checks the NWM fast path against h5py on a real archive file. Run with `pytest -m live`."""

import fsspec
import h5py
import numpy as np
import pytest
import requests

from flowcast_eval.nwm import CFS_PER_CMS, OPS_BUCKET, discover_layout, read_with_layout

pytestmark = pytest.mark.live

URL = f"{OPS_BUCKET}/nwm.20250601/medium_range_mem1/nwm.t00z.medium_range.channel_rt_1.f024.conus.nc"
CALLICOON_REACH = 2617456


def test_fast_path_matches_h5py():
    layout = discover_layout(URL, CALLICOON_REACH)
    fast = read_with_layout(URL, layout, requests.Session())
    with fsspec.filesystem("https").open(URL, block_size=2**20) as f, h5py.File(f, "r") as h:
        i = int(np.flatnonzero(h["feature_id"][:] == CALLICOON_REACH)[0])
        slow = float(h["streamflow"][i]) * 0.01 * CFS_PER_CMS
    assert fast == pytest.approx(slow, rel=1e-6)
