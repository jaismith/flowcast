# Decision record

What we tried for the v2 model, what it measured, and what we decided. Code for dropped experiments was removed from
`main` in the Oct 2026 cleanup ([#59](https://github.com/jaismith/flowcast/pull/59)); the PRs and commits below
still have it.

Unless noted, numbers are median CRPS skill vs persistence over the scored basins (552 for full runs, 50 for
`slice50`), operational hindcasts (GEFS forecast weather), validation years WY2021–2022. "+0.010" means 0.010 more
skill than the comparison. The frozen test years (WY2023+) have not been read.

## What ships

- **Flow:** `model/configs/full_v2_snodas.yml`, seeds 42, 43 and 44, pooled with equal weights from their saved CMAL
  mixtures, all 11 GEFS members. Skill +0.32 / +0.56 / +0.64 / +0.63 / +0.52 at 1 / 6 / 24 / 72 / 168 h.
- **Flow post-processing:** the per-lead, state-dependent log-space stretch (`flowcast-model flow-calibrate`).
- **Water temperature:** `model/configs/temp_v2_nodrop.yml`, with the plain per-lead offset and spread calibration
  in `temp-score`.

## Flow model

- **Hidden size and the slice50 sweep.** Tried hidden 128 vs 256, a 336 h hindcast, less lagged-flow dropout, no
  lagged flow, no AORC dropout, persistence inputs and an NSE head. Hidden 256 was best at every lead (+0.11 at 24 h
  over 128); dropping lagged flow or AORC dropout cost 0.2–0.3 at 24 h. **Adopted** h256 with both dropouts.
  Commits `85cbc7c`, `39fbf36` (PR [#19](https://github.com/jaismith/flowcast/pull/19)).
- **Residual forecasts** (predict the change from the last observed flow). 1 h skill −1.40 → +0.31 on slice50,
  better in 50/50 basins, no cost beyond 3 days. **Adopted.** Commit `3d88808` (#19).
- **Basin-mixed batches** (each batch mixes many basins; before, an epoch saw one 16-basin block). Better at every
  lead in 72–90% of basins. **Adopted** as the default. Commits `30d5871`, `cd3b510` (#19).
- **Training precision.** bf16 at batch 512 broke down with NaN losses in two of three full runs, and batch-512 runs
  trailed fp32 batch 256 by about 0.005. **Full runs use fp32, batch 256.** Commits `445e047`, `c78f9cf` (#19).
- **CMAL scale floor** (`min_scale 1e-3`, about the USGS reporting step). The non-finite gradients came from the
  CMAL scale collapsing to its 1e-5 floor on flat, quantized low flows. **Adopted**, with a constant-release
  indicator input for long flat regulated releases. Commits `895fb85`, `c27bdf6` (#19).
- **Forecast-trained forcing (v1.1)**: train the forecast branch on the GEFSv12 reforecast instead of future AORC.
  Worse at 1–6 h (Callicoon 1 h −0.58), +0.03 only at 168 h; combined with the residual model the gains didn't
  stack. **Not adopted** for flow (the temperature model does train on the reforecast). Commits `cc54849`, `aa7eb8e`.
- **Upstream gauges (v1.2).** +0.00 to +0.02 at 3–24 h in gauged basins, small losses at 2–7 days (38 of 50 basins
  worse at 168 h). **Not adopted**; "gauged basins only, unlagged" is still on the idea list. Commit `88b47a1`,
  dataset PR [#21](https://github.com/jaismith/flowcast/pull/21).
- **Upstream gauges (10% dropout) and flood emphasis, 50-basin pilot** (Oct 2026; 42 gauged + 8 ungauged basins,
  3 seeds per arm, vs matched baselines). At the full training budget, upstream inputs gain +0.095 / +0.042 / +0.029 /
  +0.011 at 1 / 6 / 12 / 24 h (48 / 48 / 48 / 43 of 50 basins), with no cost at 2–7 days and none in ungauged basins.
  Loss weighting toward high flows and rising limbs (`dataset.flow_weight`) on top: +0.006 to +0.016 at 3–12 h,
  crest downturns 28% → 15%, peaks ×1.03–1.05 (×1.08–1.10 at the slice budget), −0.003 at 5–7 days. Flood-window
  oversampling (`dataset.flood_oversample`) at full budget: same CRPS as the weighting, the fewest false alarms
  (24 h false-alarm ratio 0.21 vs 0.27), but downturns only to 21%, peaks level, and the same 5–7-day cost.
  **Recommended for the national retrain:** upstream inputs plus the weighting. bf16 broke down on these long runs,
  so the trainer now falls back to fp32 by itself (`train.amp_fallback_skip`). PR [#61](https://github.com/jaismith/flowcast/pull/61).
- **SNOW-17 snow-module features and elevation-band forcings.** −0.05 to −0.08 with the full feature set, about
  −0.01 with snow states only. **Not adopted.** The module itself (PR [#12](https://github.com/jaismith/flowcast/pull/12),
  `pipeline/.../snow/`) stays for possible snow and melt map layers. Configs: commit `cb44684`.
- **Travel-time zones (v1.3).** Neutral on 3 slice seeds and in a clean 2-seed full test (two-seed ensembles −0.001
  to +0.002 at 6–72 h; the seed pairs disagree in sign), and −0.012 to −0.025 at flood peaks. The earlier "zone gain" was the training recipe.
  **Dropped.** Hourly travel-time bins were stopped untested (AORC-only, so unusable operationally). Commits
  `b7b2bef`, `e7f4ab5`; data PRs [#21](https://github.com/jaismith/flowcast/pull/21), [#22](https://github.com/jaismith/flowcast/pull/22).
- **SNODAS snowpack input.** +0.025 / +0.027 / +0.027 at 48 / 72 / 120 h in the 111 snowiest basins in Dec–May
  (83–88 better), +0.014 to +0.031 over deep snow with a warm forecast; −0.024 at 1 h. **Adopted** (the final recipe).
  PRs [#25](https://github.com/jaismith/flowcast/pull/25), [#27](https://github.com/jaismith/flowcast/pull/27).
- **All 11 GEFS members instead of 5.** +0.005 at 24 h, +0.010 at 72 h, +0.018 at 168 h on the same weights.
  **Adopted.** PR [#43](https://github.com/jaismith/flowcast/pull/43).
- **Three-seed ensemble, equal weights.** +0.017 to +0.027 over the previous best single model (`full-resid-mixed`) at
  6–168 h, in 486–529 basins; level at 1 h. Seed 43's flood-peak damping is within normal seed spread, and dropping it
  costs CRPS at every lead. **Adopted.** Pooling: PR [#47](https://github.com/jaismith/flowcast/pull/47).
- **Calendar inputs** (clock hour, day of week, for hydropeaking). +0.010 at 1 h within seed noise, nothing at
  3–168 h, nothing extra at regulated basins. **Not adopted.** PR [#44](https://github.com/jaismith/flowcast/pull/44) (closed).
- **Mixed forcing** (train on GEFSv12 reforecast instead of future AORC for 25% or 60% of samples). On slice50,
  +0.006 to +0.015 at 3–72 h; at full scale within seed noise at 6–120 h, +0.017 only at 168 h, and 72 h flood peaks
  ×0.69–0.72. **Not adopted.** PR [#50](https://github.com/jaismith/flowcast/pull/50) (code removed in the cleanup).
- **Quantile-mapped GEFS precipitation** (map GEFS rain onto AORC's distribution at hindcast time). Peaks +5–8%,
  24 h coverage +11 points, but CRPS −0.002 to −0.005 at 24–168 h. **Not adopted.** PR #50 (code removed).
- **Dam releases: oracle and NYC storage.** Perfect future releases add +0.05 to +0.11 at 2–7 days at the four
  NYC-downstream gauges; reservoir storage alone recovers none of it (−0.021 at 24 h) and hurts temperature slightly.
  **Storage not adopted**; release schedules are being forward-archived for a fine-tune once about 12 months exist.
  PR [#34](https://github.com/jaismith/flowcast/pull/34) (storage code and configs removed in the cleanup).
- **Reservoir fill and observed releases (v1.4,** 175 basins, 2 seeds). ±0.004 at every lead, below seed noise;
  −0.002 to −0.015 at 6–12 h where releases duplicate gauged outflow. **Not adopted.** PRs
  [#37](https://github.com/jaismith/flowcast/pull/37), [#39](https://github.com/jaismith/flowcast/pull/39) (dataset builder kept).
- **Output dropout and flow forecast shrinkage.** The flow model's forecast changes are calibrated (slope 1.01 on
  WY2021–2022, 1.05 in training), unlike temperature's. **No retrain** without dropout for flow. No code.
- **Per-site fine-tuning** (5 basins) **and per-site conformal calibration.** Small gains, almost all at 1 h; within
  ±0.05 and mostly not significant at 24–72 h; conformal calibration made forecasts worse. **Not adopted.** Commits
  `4db3c40`, `553122d` (the trainer's `init_from` / `select_until` options stay).
- **Per-cluster fine-tuning pilot** (16 clusters, seed 42). Failed 3 of 4 criteria: +0.024 at 1 h but +0.005 at
  6–12 h (bar +0.010), and flood peaks slightly lower. Only regulated clusters gained (+0.010 to +0.016 at 6–12 h).
  **No rollout**; regulated-group and per-site follow-ups are proposed. PR [#48](https://github.com/jaismith/flowcast/pull/48) (closed).

## Short-range blends

- **National persistence blend.** Cross-validated weight on the LSTM is 0.9–1.0 from 1 h: no gain once the model is
  residual. **Not adopted.** Commit `2f3d89c` (`model/src/flowcast_model/blend.py`).
- **LSTM + LightGBM blend at Callicoon, 1–24 h.** 1 h skill +0.18 (LSTM) and +0.55 (LightGBM) → +0.57 blended;
  +0.63 at 24 h. **Recommended for sites with a LightGBM model; not yet confirmed as adopted.** `blend.py` stays.

## Calibration

- **Flow-range stretch** (log-space, per lead and forecast state; median kept from 9 h). +0.016 at 1 h in 438 of 552
  basins, neutral beyond (largest change +0.0009); 90% coverage moves toward nominal. **Adopted:** on by default in
  the standard scoring path. PR [#51](https://github.com/jaismith/flowcast/pull/51).
- **Flood-tail boost** (extra upper-tail widening above the 80th percentile). Flood-peak 90% coverage 52% → 67% at
  24 h, but threshold-weighted CRPS above Q99 +3–5% and the false-alarm ratio 61% → 72%. **Deferred** as a product
  decision; code removed in the cleanup (PR #51 has it).
- **Warm-up temperature correction** (daily-max offset that grows with the forecast air warm-up). Closed 25–29% of
  the v1 heat-onset bias. Adopted Oct 1, then **retired** Oct 2: the no-dropout model is better without it, and it
  adds only 0.1–0.5% on top. Code removed in the cleanup (PR #51 has it).
- **Per-lead temperature offset and spread** (fitted on the other validation year). Daily-max bias −0.5 → about
  0 °C, Callicoon day-1 RMSE 0.91 → 0.75 °C. **Adopted.** PR [#24](https://github.com/jaismith/flowcast/pull/24),
  all-years fit in #51.

## Water temperature

- **v1** (hourly multi-site LSTM, 126 basins, 7 days). Callicoon day-1 daily-max RMSE 0.75 °C vs 1.16 for
  air2stream on GEFS; better than air2stream in 96–98 of 98 basins. Superseded by v2. PR [#24](https://github.com/jaismith/flowcast/pull/24).
- **Coherent sample paths** for daily maxima (independent draws per hour made maxima warm and far too narrow,
  80% coverage 0.30–0.48). **Adopted.** Commit `46ec88d`.
- **v2 arm 1** (v1 on the final flow recipe's data: SNODAS, 11 GEFS members, the 3-seed flow forecast). Beats v1 at
  every daily-max lead through day 5 in 94–102 of 111–113 basins. **Adopted** as the base (`temp_v2.yml`).
  PR [#52](https://github.com/jaismith/flowcast/pull/52).
- **Heat-weight loss and forecast warm-up inputs** (v2 arms 2 and 3). Both left the onset bias where arm 1 had it
  and were slightly worse overall (−0.002 to −0.030). **Not adopted.** PR #52 (code removed in the cleanup).
- **No output dropout.** Dropout before the CMAL head shrank every forecast change by about 13%. Without it the
  onset bias falls from −1.00 / −1.69 to −0.30 / −0.85 °C at days 1 / 5, and uncorrected daily-max CRPS is 9.6 / 6.6
  / 3.8% better than arm 1 with the warm-up correction at days 1 / 3 / 5 (two seeds). **Adopted:**
  `temp_v2_nodrop.yml`, PR [#58](https://github.com/jaismith/flowcast/pull/58).
- **β-NLL loss.** Worse on every measure at both weights: shrinkage slope 1.18–1.22, CRPS 7–11% worse.
  **Not adopted.** PR [#57](https://github.com/jaismith/flowcast/pull/57) (closed).

## Scoring and benchmarks

- **Exact mixture CRPS** (`score --mixtures`): the national 3-seed score in 21 min instead of 3+ h, validated
  against sampling. **Adopted.** PR [#49](https://github.com/jaismith/flowcast/pull/49).
- **MARFC scoring fix.** The NWM-retrospective lookup missed every bulletin not issued on the hour, so only about 35
  of 855 bulletins were scored. Corrected, the final model beats MARFC by +0.16 / +0.24 / +0.30 / +0.26 / +0.24 at
  6 / 12 / 24 / 48 / 72 h (was published as +0.64 at 24 h and level at 48–72 h). PR [#55](https://github.com/jaismith/flowcast/pull/55);
  the skill page was republished Oct 3.
- **Benchmark study** (NWS RFC bulletins at 181 forecast points; operational NWM at 552 basins). CRPS 19–38% below
  the RFC forecast's error at 6–72 h and 27–74% below NWM; the RFCs still win on single-value flood peaks (94% vs
  85% of the peak at 24 h). Fetchers kept. PR [#56](https://github.com/jaismith/flowcast/pull/56).

## Training infrastructure

- **Faster basin loads** (concurrent array reads, float16 forecast normalization, same batches), **loader-wait
  logging** and a **160 GiB root volume** (a full hindcast with mixtures filled 100 GiB). **Adopted.** PR #50.
- **Background prefetch of the next training block.** Bit-identical batches but no speedup on the 16 GB PC.
  **Not adopted**; revisit after a RAM upgrade. PR [#46](https://github.com/jaismith/flowcast/pull/46) (closed).
- **Spot watcher Lambda (tick)** with per-job regions and a role scoped to every job region. **Adopted.** PRs
  [#30](https://github.com/jaismith/flowcast/pull/30), [#42](https://github.com/jaismith/flowcast/pull/42),
  [#53](https://github.com/jaismith/flowcast/pull/53).
