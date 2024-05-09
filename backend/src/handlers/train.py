import pandas as pd
import numpy as np
import logging

log = logging.getLogger(__name__)

from utils import s3, constants, utils

def handler(usgs_site: str):
  # load df
  archive = s3.fetch_archive_data(usgs_site)
  log.info(f'loaded archive ({archive.shape[0]} obs)')

  # only use historical observations for training, filter
  log.info(f'dropping forecasted entries')
  historical = archive[archive['type'] == 'hist']

  # todo remove when neuralprophet fixes empty regressor bug
  historical['snow'][0] = 0.01
  historical['snowdepth'][0] = 0.01

  for feature in constants.FEATURES_TO_FORECAST:
    create_model(pd.DataFrame(historical), usgs_site, feature)

  return { 'statusCode': 200 }

def create_model(data: pd.DataFrame, usgs_site: str, feature: str):
  historical = utils.prep_archive_for_training(data, feature)
  log.info(f'dataset ready for training: {historical}')

  # this is an expensive import, we'll only do it when this handler is called
  from neuralprophet import NeuralProphet
  from neuralprophet.logger import MetricsLogger

  # create new model
  model = NeuralProphet(
    growth='off',
    yearly_seasonality=True,
    daily_seasonality=False,
    weekly_seasonality=False,
    n_lags=constants.FORECAST_HORIZON*2,
    n_forecasts=constants.FORECAST_HORIZON,
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
    err = (predictions[f'yhat{i}'] - predictions['y']).dropna()
    metrics.loc[i, 'mae'] = sum(abs(err)) / err.shape[0]
    metrics.loc[i, 'mse'] = sum(np.square(err)) / err.shape[0]
  metrics['rmse'] = np.sqrt(metrics['mse'])

  log.info(f'test metrics by horizon:\n{metrics.loc[metrics.index % 6 == 0]}')

  # save model
  s3.save_model(model, usgs_site, feature)
