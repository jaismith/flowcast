import pandas as pd
import numpy as np
import logging

log = logging.getLogger(__name__)

from utils import s3, constants, utils

def handler(_event, _context):
  # this is an expensive import, we'll only do it when this handler is called
  from neuralprophet import NeuralProphet
  from neuralprophet.logger import MetricsLogger

  # load df
  archive = s3.fetch_archive_data()
  log.info(f'loaded archive ({archive.shape[0]} obs)')

  historical = utils.prep_archive_for_training(archive)
  log.info(f'dataset ready for training: {historical}')

  # create new model
  model = NeuralProphet(
    growth='off',
    yearly_seasonality=True,
    daily_seasonality=True,
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
  for feature in ['snow', 'precip', 'snowdepth', 'cloudcover', 'airtemp']:
    model.add_future_regressor(feature)

  train, test = model.split_df(historical, freq='H', valid_p=0.2)

  # fit model
  model.fit(train, freq='H')
  # test model
  model.test(test)

  # tst = model.predict(test)

  # evalutate model
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
  s3.save_model(model, constants.USGS_SITE)

  return { 'statusCode': 200 }
