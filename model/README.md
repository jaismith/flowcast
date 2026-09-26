# flowcast-model

The flowcast v2 streamflow model (rebuild plan §3, §5, milestones 1.4–1.5): a multi-basin hourly NeuralHydrology
`HandoffForecastLSTM` with a CMAL head, a streaming Zarr dataset for the training cube, an EC2 Spot training
launcher, and hindcast scoring through `evaluation/`.

```bash
uv sync
uv run pytest                                   # offline tests (dataset acceptance, trainer resume, launcher with moto)

# local
uv run flowcast-model train --config configs/slice50.yml --cube data/slice50/trainval.zarr --run-dir runs/x
uv run flowcast-model hindcast --run-dir runs/x --out runs/x/hindcast [--extra-issues marfc_issues.parquet]
uv run flowcast-model score --forecasts runs/*/hindcast --cube data/slice50/trainval.zarr --out results/x

# EC2 Spot (AWS credentials in the environment)
uv run flowcast-train setup
uv run flowcast-train launch --sweep configs/sweeps/slice50.yml --only base h256 --retry-minutes 60
uv run flowcast-train status | cost | fetch --run-id <id> | kill --run-id <id>
```

## Streaming dataset (`dataset.py`, `cube.py`)

`ZarrCubeDataset` is registered as NeuralHydrology dataset `flowcast_zarr`.

- **Same samples as stock.** It reuses NeuralHydrology's own per-basin preprocessing and `__getitem__`, so samples
  match the stock loader exactly. `tests/test_dataset.py` compares every sample, the normalization statistics and
  the sample index against NeuralHydrology's `generic` dataset, in plain, forecast and validation modes.
- **Bounded memory.** Basins are read one at a time. Statistics come from one streaming pass. The sample index is
  one int32 array per basin. `BasinBlockBatchSampler` draws batches from K basins at a time, and each worker keeps
  an LRU cache of about 2K basins, so RAM stays flat as the basin count grows.
- **Masked optional inputs.** `optional_inputs` may be missing without invalidating a sample; the model masks
  them with `nan_handling_method`. `group_dropout` masks whole hindcast input groups during training (lagged
  observed flow about 50% of the time, per plan §3; AORC 30%, so the model also runs on real-time analyses).
- **Archived forecasts.** Forecast products (`<p>_init`, `<p>_lead`, optional `<p>_member`) feed the forecast
  branch from the latest init available at the sample's issue time, after a per-product latency. Coarser leads
  fill the hours they cover.
- **Cube layout.** It reads the training cube v1 layout: per-variable `(basin, time)` arrays; band and month
  dimensions expanded into features such as `aorc_band_temp_2m_c_band2`; `static_all(basin, attribute)`; and
  separate `trainval.zarr` and `test.zarr` stores.
- **Frozen test guard.** Nothing at or after 2022-10-01 is ever read unless `allow_frozen_test` is set.

## Forecast forcing in v1: analysis-as-forecast training, GEFS at issue time

Archived GEFS forecasts start in Oct 2020 and HRRR forecasts in Jul 2018, so the WY2001–2019 training years
have almost none. Training the forecast branch on 2018+ would put GEFS only in the validation years, which
leaks. v1 therefore follows the plan's fallback, the same "perfect prog" approach as the LightGBM baseline:

- **Training.** The forecast branch sees future AORC (analysis as forecast) for all training years.
- **`operational` hindcasts:**
  - AORC is masked in the hindcast branch, because it isn't available in real time. HRRR analysis and MRMS
    take over; the model learned this through AORC group dropout.
  - Archived GEFS members from the latest 00Z run usable at issue time (6 h latency) go into the
    forecast-branch AORC slots, normalized with AORC's training statistics.
  - These are fair, operational forecasts.
- **`perfect_forcing` hindcasts.** Future AORC stays as the forecast. This is optimistic: it bounds what a perfect
  weather forecast would buy. Results are always labelled `run_type=perfect_forcing`.
- **Scoring window.** Both modes are scored on WY2021–2022, the validation years with archived GEFS.
- **Known limitations:**
  - GEFS and AORC differ in bias and resolution, and the model never saw GEFS errors in training.
  - HRRR 48 h forecasts are not used yet.
  - Gauged dam outflow is a hindcast-only input; its persistence over the forecast window (plan §3) is not
    added yet.
- **Next.** The GEFSv12 reforecast (2000–2019, being added to the dataset) removes the perfect-prog mismatch: the
  forecast branch can then be trained on real forecasts.

## Launcher (`launcher/`)

Each run is one persistent Spot instance with interruption behavior `stop`. On every boot, a systemd unit runs
the job:

1. pull the code tarball (the exact working tree) and the dataset;
2. resume from the latest local or S3 checkpoint;
3. train, syncing checkpoints to S3 after every epoch;
4. hindcast the validation years in every mode;
5. upload everything;
6. cancel the Spot request and terminate itself.

**Runtime limits.** Three independent limits stop anything from outliving `--max-hours`:

- the Spot request's `ValidUntil`;
- an on-instance watchdog, which stops training gracefully 15 min before the deadline and terminates the
  instance at it;
- EventBridge Scheduler one-shot schedules that cancel the request and terminate the instance.

**Tags and placement.** Everything is tagged `project=flowcast component=training`. `--instance-type auto` places
runs on the cheapest free GPU Spot slots across regions, based on the G/VT Spot quota. A cross-region dataset is
cached on the instance's EBS root (one transfer per instance), or copied once with `--replicate-dataset`.

**Measured speed** (720 h + 168 h sequences, batch 256, CMAL):

| instance | s/update |
|---|---|
| g5.xlarge / g5.2xlarge (A10G) | 0.045 / 0.043 |
| g6.xlarge (L4) | 0.049 |
| c8g.4xlarge (CPU) | 0.87 |

4 vCPUs are enough to keep the GPU fed.

## Scoring

- **`flowcast-model score`.** Runs `evaluation/` per basin on the validation years: persistence,
  recession-persistence, climatology, and the NWM v3.0 retrospective (reach IDs from the NLDI). It writes
  cross-basin medians.
- **Callicoon.** `flowcast-eval strong-baselines --extra-forecasts <hindcast dir>` puts the model in the
  strong-baseline table: LightGBM and ARX with GEFS QPF, and MARFC at its own bulletin times. For that, hindcast
  Callicoon with `--extra-issues` at MARFC's issue times.
