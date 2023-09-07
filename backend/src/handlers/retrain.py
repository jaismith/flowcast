import pandas as pd
from neuralprophet import NeuralProphet
from neuralprophet.logger import MetricsLogger
import logging

log = logging.getLogger(__name__)

from utils import s3, constants, utils

def handler(_event, _context):
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
    ar_layers=[64, 64, 64, 64],
    learning_rate=0.003,
    quantiles=[
      round(((1 - constants.CONFIDENCE_INTERVAL) / 2), 2),
      round((constants.CONFIDENCE_INTERVAL + (1 - constants.CONFIDENCE_INTERVAL) / 2), 2)
    ]
  )
  model.metrics_logger = MetricsLogger(save_dir='/tmp')
  for feature in ['snow', 'precip', 'snowdepth', 'cloudcover', 'airtemp']:
    model.add_future_regressor(feature)

  # fit model
  model.fit(historical, freq='H')

  # save model
  s3.save_model(model, constants.USGS_SITE)

  return { 'statusCode': 200 }
