from types import SimpleNamespace

import numpy as np

from flowcast_model import validation
from flowcast_model.validation import FlowcastValidator


def test_validator_keeps_a_bounded_number_of_basin_datasets(monkeypatch):
    built = []
    monkeypatch.setattr(validation, "get_dataset", lambda cfg, basin, **kw: built.append(basin) or SimpleNamespace(basin=basin))
    monkeypatch.setattr(validation, "issue_positions", lambda ds, *a: [0])
    cfg = SimpleNamespace(forecast_seq_length=168, predict_last_n=168, target_variables=["q"])
    scaler = {"xarray_feature_center": {"q": SimpleNamespace(values=np.float32(0))}, "xarray_feature_scale": {"q": SimpleNamespace(values=np.float32(1))}}
    v = FlowcastValidator(cfg, scaler)
    basins = [f"b{i:03d}" for i in range(FlowcastValidator.max_cached_basins + 36)]
    for b in basins:
        assert v._basin(b)[0].basin == b
    assert len(v._cache) == FlowcastValidator.max_cached_basins
    v._basin(basins[-1])
    assert built.count(basins[-1]) == 1
    v._basin(basins[0])
    assert built.count(basins[0]) == 2
