import pandas as pd
import numpy as np
import logging

log = logging.getLogger(__name__)

from utils import s3, db, constants, utils

def handler(_event, _context):
  # get latest hist
  log.info(f'retrieving most recent historical data for site {constants.USGS_SITE}')
  # include 10 row buffer in case any rows are invalid
  last_hist_entries = db.get_n_most_recent_hist_entries(constants.USGS_SITE, constants.FORECAST_HORIZON*2)

  last_hist_origin = last_hist_entries[0]['timestamp']
  log.info(f'retrieving weather forecast data for site {constants.USGS_SITE} at {last_hist_origin}')
  last_fcst_entries = db.get_entire_fcst(constants.USGS_SITE, last_hist_origin)

  if (last_fcst_entries[0]['watertemp'] is not None):
    log.warning(f'forecast already exists for most recent weather data. perhaps the update task failed?')
    # return { 'statusCode': 200 }

  fcst_df = pd.DataFrame(last_fcst_entries)
  hist_df = pd.DataFrame(last_hist_entries)
  source_df = pd.concat([fcst_df[fcst_df['timestamp'] > hist_df['timestamp'].max()], hist_df])
  source_df = source_df.set_index(pd.to_datetime(source_df['timestamp'].apply(pd.to_numeric), unit='s')).sort_index()

  feature_cols = ['precip', 'cloudcover', 'airtemp', 'watertemp', 'snow', 'snowdepth']
  df = source_df.drop(columns=source_df.columns.difference(feature_cols))
  df = df.reset_index()
  df = df.rename(columns={'timestamp': 'ds'})

  # convert decimals to floats
  df[feature_cols] = df[feature_cols].apply(pd.to_numeric, downcast='float')

  df = df.rename(columns={'watertemp': 'y'})
  # todo - remove once neuralprophet issue is resolved
  df.loc[0, 'snow'] = 0.01
  df.loc[0, 'snowdepth'] = 0.01
  log.info(f'dataset ready for inference:\n{df}')

  # load model
  model = s3.load_model(constants.USGS_SITE)

  # prep future
  future = model.make_future_dataframe(
    df=df[df['y'].notnull()],
    regressors_df=df[df['y'].isnull()].drop(columns=['y']),
    periods=constants.FORECAST_HORIZON
  )

  # predict
  # hide py.warnings (noisy pandas warnings during training)
  logging.getLogger('py.warnings').setLevel(logging.ERROR)
  pred = model.predict(df=future)
  yhat = model.get_latest_forecast(pred)

  yhat = yhat.set_index(yhat['ds'])
  utils.convert_floats_to_decimals(yhat)
  source_df['watertemp_5th'] = np.nan
  source_df['watertemp_95th'] = np.nan
  source_df['watertemp'] = source_df['watertemp'].combine_first(yhat['origin-0'])
  source_df['watertemp_5th'] = source_df['watertemp_5th'].combine_first(yhat['origin-0 5.0%'])
  source_df['watertemp_95th'] = source_df['watertemp_95th'].combine_first(yhat['origin-0 95.0%'])
  updates = source_df[(source_df['type'] == 'fcst') & (source_df['watertemp'].notnull())]
  fcst_rows = utils.generate_fcst_rows(updates, pd.Timestamp.fromtimestamp(int(last_hist_origin)), True)

  log.info('pushing new fcst entries to db')
  logging.getLogger('boto3.dynamodb.table').setLevel(logging.DEBUG)
  db.push_fcst_entries(fcst_rows)

  return { 'statusCode': 200 }
