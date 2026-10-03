"""Model variants on top of NeuralHydrology's model zoo (which has no registry, so variants are applied by class swap).

`make_residual`: the forecast branch predicts the change from the last observed flow. The value of a hindcast input
(e.g. `qobs_mm_h_shift1`, the flow observed 1 h before the issue time) at the last hindcast step is added to the
location of every forecast-step output (CMAL/GMM `mu`, regression `y_hat`). Inputs and target share the same
normalization (the lagged feature is the target shifted by one hour), so the offset is added in normalized space.
Where the input is missing (masked in training, or no observation) the offset is zero and the model sees the
missing-input flag, so it learns both regimes.

`min_scale`: a floor on the CMAL/GMM scale `b` (normalized target units), added to the head's own tiny epsilon.
Observed flow is quantized at its reporting precision, so over flat stretches the likelihood rewards collapsing `b`
towards zero, and one step of change then overflows the gradient (model-results.md, "Training stability"). With
qobs normalized by about 0.11 mm/h, 1e-3 is about the reporting step at low flows.
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


def floor_scale(model: torch.nn.Module, min_scale: float) -> torch.nn.Module:
    heads = [getattr(model, name) for name in ("hindcast_head", "forecast_head", "head") if hasattr(model, name)]
    for head in heads:
        original = head.forward

        def forward(x, original=original):
            out = original(x)
            if "b" in out:
                out["b"] = out["b"] + min_scale
            return out

        head.forward = forward
    return model


def elementwise_cmal_loss(loss_obj, eps: float = 1e-8, weighted: bool = False):
    """Make a CMAL loss skip missing targets one step at a time instead of dropping every sample with any gap.

    NeuralHydrology's `MaskedCMALLoss` drops a sample whose forecast window has a single missing target. Water
    temperature records have many short gaps, so that discards much of the data. Here each missing step contributes
    nothing, and the per-sample sum over steps is averaged over samples with at least one target, which is the
    stock loss wherever windows are complete.

    `weighted`: each step's log-likelihood is multiplied by the sample's `loss_weight` (dataset option
    `loss_weight`), rescaled so the batch's mean weight over valid steps is 1 and the loss stays on the unweighted
    scale.
    """
    if weighted and "loss_weight" not in loss_obj._ground_truth_keys:
        loss_obj._ground_truth_keys = [*loss_obj._ground_truth_keys, "loss_weight"]

    def _get_loss(prediction, ground_truth, **kwargs):
        y = ground_truth["y"]
        valid = ~torch.isnan(y)
        m, b, t, p = prediction["mu"], prediction["b"], prediction["tau"], prediction["pi"]
        error = torch.where(valid, y, m) - m
        log_like = torch.log(t) + torch.log(1.0 - t) - torch.log(b) - torch.max(t * error, (t - 1.0) * error) / b
        mask = valid[..., 0].to(y.dtype)
        if weighted:
            w = ground_truth["loss_weight"][..., 0] * mask
            mask = w * (mask.sum() / w.sum().clamp(min=eps))
        step = torch.logsumexp(torch.log(p + eps) + log_like, dim=2) * mask
        n = valid[..., 0].any(dim=1).sum().clamp(min=1)
        return -step.sum() / n

    loss_obj._get_loss = _get_loss
    return loss_obj


def apply_variants(model: torch.nn.Module, options: dict | None) -> torch.nn.Module:
    options = options or {}
    if options.get("residual_from"):
        model = make_residual(model, options["residual_from"])
    if options.get("min_scale"):
        model = floor_scale(model, float(options["min_scale"]))
    return model
