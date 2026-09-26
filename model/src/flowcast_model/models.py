"""Model variants on top of NeuralHydrology's model zoo (which has no registry, so variants are applied by class swap).

`make_residual`: the forecast branch predicts the change from the last observed flow. The value of a hindcast input
(e.g. `qobs_mm_h_shift1`, the flow observed 1 h before the issue time) at the last hindcast step is added to the
location of every forecast-step output (CMAL/GMM `mu`, regression `y_hat`). Inputs and target share the same
normalization (the lagged feature is the target shifted by one hour), so the offset is added in normalized space.
Where the input is missing (masked in training, or no observation) the offset is zero and the model sees the
missing-input flag, so it learns both regimes.
"""

from __future__ import annotations

import torch
from neuralhydrology.modelzoo.handoff_forecast_lstm import HandoffForecastLSTM


class ResidualHandoffForecastLSTM(HandoffForecastLSTM):
    residual_from: str = ""

    def forward(self, data):
        pred = super().forward(data)
        last = data["x_d_hindcast"][self.residual_from][:, -1, 0]
        last = torch.nan_to_num(last, nan=0.0)
        L = self.cfg.forecast_seq_length
        for key in ("mu", "y_hat"):
            if key in pred and pred[key] is not None:
                offset = torch.zeros_like(pred[key])
                offset[:, -L:, :] = last[:, None, None]
                pred[key] = pred[key] + offset
        return pred


def make_residual(model: torch.nn.Module, feature: str) -> torch.nn.Module:
    if not isinstance(model, HandoffForecastLSTM):
        raise NotImplementedError("residual forecasts are implemented for handoff_forecast_lstm")
    model.__class__ = ResidualHandoffForecastLSTM
    model.residual_from = feature
    return model


def apply_variants(model: torch.nn.Module, options: dict | None) -> torch.nn.Module:
    options = options or {}
    if options.get("residual_from"):
        model = make_residual(model, options["residual_from"])
    return model
