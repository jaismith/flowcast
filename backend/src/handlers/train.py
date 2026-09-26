import pandas as pd
import numpy as np
import logging

from utils import s3, constants, utils, db

log = logging.getLogger(__name__)

def handler(usgs_site: str, is_onboarding: bool):
  if is_onboarding:
    db.update_site_status(usgs_site, db.SiteStatus.TRAINING_MODELS)
    db.push_site_onboarding_log(usgs_site, f'🧠 Started training feature models for site {usgs_site} at {utils.get_current_local_time()}')

  # load df
  archive = s3.fetch_archive_data(usgs_site)
  log.info(f'loaded archive ({archive.shape[0]} obs)')
  if is_onboarding:
    db.push_site_onboarding_log(usgs_site, '\tloaded latest snapshot')

  # only use actual observations for training, filter
  log.info('dropping forecasted entries')
  historical = archive[archive['type'] == 'actual'].copy()

  # todo: remove when neuralprophet fixes empty regressor bug
  if 'snow' in historical.columns and len(historical) > 0:
    historical.loc[historical.index[0], 'snow'] = 0.01
  if 'snowdepth' in historical.columns and len(historical) > 0:
    historical.loc[historical.index[0], 'snowdepth'] = 0.01

  for feature in constants.FEATURES_TO_FORECAST:
    if is_onboarding:
      db.push_site_onboarding_log(usgs_site, f'\tfitting model for {feature}')
    create_model(pd.DataFrame(historical), usgs_site, feature)

  if is_onboarding:
    db.push_site_onboarding_log(usgs_site, f'\tfinished training feature models at {utils.get_current_local_time()}')

  return { 'statusCode': 200 }

def create_model(data: pd.DataFrame, usgs_site: str, feature: str):
  historical = utils.prep_archive_for_training(data, feature)
  # Fill regressor gaps to avoid excessive row drops
  reg_cols = [c for c in constants.FEATURE_COLS[feature] if c != feature and c in historical.columns]
  if 'ds' in historical.columns and len(reg_cols) > 0:
    hist_idx = historical.set_index('ds')
    hist_idx[reg_cols] = (hist_idx[reg_cols]
                          .interpolate(method='time', limit_direction='both')
                          .fillna(method='ffill')
                          .fillna(method='bfill'))
    historical = hist_idx.reset_index()

  log.info(f'dataset ready for training: {historical}')

  # this is an expensive import, we'll only do it when this handler is called
  from neuralprophet import NeuralProphet
  from neuralprophet.logger import MetricsLogger

  # create new model with lags sized to data length to prevent window errors
  total_rows = len(historical)
  n_forecasts = int(constants.FORECAST_HORIZON)
  # ensure lags are smaller than available rows after accounting for forecast horizon
  safe_max_lags = max(1, total_rows - n_forecasts - 1)
  desired_lags = int(constants.FORECAST_HORIZON) * 2
  n_lags = max(24, min(desired_lags, safe_max_lags))

  model = NeuralProphet(
    growth='off',
    yearly_seasonality=True,
    daily_seasonality=False,
    weekly_seasonality=False,
    n_lags=n_lags,
    n_forecasts=n_forecasts,
    ar_layers=[64] * 4,
    learning_rate=0.003,
    quantiles=[
      round(((1 - constants.CONFIDENCE_INTERVAL) / 2), 2),
      round((constants.CONFIDENCE_INTERVAL + (1 - constants.CONFIDENCE_INTERVAL) / 2), 2)
    ],
    drop_missing=True
  )
  model.metrics_logger = MetricsLogger(save_dir='/tmp/fc')
  # add future regressors for all features which influence what is being forecast, minus the forecast feature
  for feat in filter(lambda f: f != feature, constants.FEATURE_COLS[feature]):
    model.add_future_regressor(feat)

  train, test = model.split_df(historical, freq='H', valid_p=0.2)

  # fit model
  model.fit(train, freq='H')
  # test model
  model.test(test)

  # evaluate model
  log.info('generating metrics...')
  logging.getLogger('py.warnings').setLevel('ERROR') # hide predict warnings
  predictions = model.predict(test)
  metrics = pd.DataFrame(0, index=np.arange(1, constants.FORECAST_HORIZON + 1), columns=['mae', 'mse'])
  for i in range(1, constants.FORECAST_HORIZON + 1):
    err = (predictions[f'yhat{i}'] - predictions['y']).dropna(1)
    metrics.loc[i, 'mae'] = sum(abs(err)) / err.shape[0]
    metrics.loc[i, 'mse'] = sum(np.square(err)) / err.shape[0]
  metrics['rmse'] = np.sqrt(metrics['mse'])

  log.info(f'test metrics by horizon:\n{metrics.loc[metrics.index % 6 == 0]}')

  # save model
  s3.save_model(model, usgs_site, feature)
