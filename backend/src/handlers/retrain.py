import pandas as pd
from scipy.signal import savgol_filter
from neuralprophet import NeuralProphet
from neuralprophet.logger import MetricsLogger
import logging

log = logging.getLogger(__name__)

from utils import s3, constants

def handler(_event, _context):
  # load df
  archive = s3.fetch_archive_data()
  log.info(f'loaded archive ({archive.shape[0]} obs)')

  # only use historical observations for training, filter
  log.info(f'dropping forecasted entries')
  historical = archive[archive['usgs_site#type'] == '01427510#hist']#archive[archive['type'] == 'hist']

  # ! temporarily drop streamflow to simplify (eventually will need two models, one targeting streamflow, one targeting watertemp, then will need a two part forecast step)
  log.info('dropping non-feature columns, reindexing, renaming ds col')
  feature_cols = ['precip', 'snow', 'snowdepth', 'cloudcover', 'airtemp', 'watertemp']
  historical = historical.drop(columns=historical.columns.difference(feature_cols))
  historical = historical.reset_index()
  historical = historical.rename(columns={'timestamp': 'ds'})
  historical['ds'] = pd.to_datetime(historical['ds']).dt.tz_convert(None)

  # convert decimals to floats
  historical[feature_cols] = historical[feature_cols].apply(pd.to_numeric, downcast='float')

  historical = historical.rename(columns={'watertemp': 'y'})
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
