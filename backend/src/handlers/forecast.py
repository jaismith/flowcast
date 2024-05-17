import pandas as pd
import numpy as np
import logging

log = logging.getLogger(__name__)

from utils import s3, db, constants, utils

def handler(event, _context):
  usgs_site = event['usgs_site']

  # get latest hist
  log.info(f'retrieving most recent historical data for site {usgs_site}')
  # include 10 row buffer in case any rows are invalid
  last_hist_entries = db.get_n_most_recent_hist_entries(usgs_site, constants.FORECAST_HORIZON*2)

  last_hist_origin = last_hist_entries[0]['timestamp']
  log.info(f'retrieving weather forecast data for site {usgs_site} at {last_hist_origin}')
  last_fcst_entries = db.get_entire_fcst(usgs_site, last_hist_origin)

  if (last_fcst_entries[0][constants.FEATURES_TO_FORECAST[0]] is not None):
    log.warning(f'forecast already exists for most recent weather data. perhaps the update task failed?')
    return { 'statusCode': 200 }

  fcst_df = pd.DataFrame(last_fcst_entries)
  hist_df = pd.DataFrame(last_hist_entries)
  source_df = pd.concat([fcst_df[fcst_df['timestamp'] > hist_df['timestamp'].max()], hist_df])
  source_df = source_df.set_index(pd.to_datetime(source_df['timestamp'].apply(pd.to_numeric), unit='s')).sort_index()

  data = source_df
  for feature in constants.FEATURES_TO_FORECAST:
    feature_fcst = forecast_feature(data, feature, usgs_site)
    for col in [feature, f'{feature}_5th', f'{feature}_95th']:
      data[data['type'] == 'fcst'][col] = feature_fcst[feature_fcst['type'] == 'fcst'][col]

  mask = data['type'] == 'fcst'
  for feature in constants.FEATURES_TO_FORECAST:
    mask &= data[feature].notnull()
  updates = data[mask]
  fcst_rows = utils.generate_fcst_rows(updates, pd.Timestamp.fromtimestamp(int(last_hist_origin)), usgs_site, True)

  log.info('pushing new fcst entries to db')
  logging.getLogger('boto3.dynamodb.table').setLevel(logging.DEBUG)
  db.push_fcst_entries(fcst_rows)

  return { 'statusCode': 200 }

def forecast_feature(data: pd.DataFrame, feature: str, usgs_site: str):
  df = data.drop(columns=data.columns.difference(constants.FEATURE_COLS[feature]))
  df = df.reset_index()
  df = df.rename(columns={'timestamp': 'ds'})

  # convert decimals to floats
  df[constants.FEATURE_COLS[feature]] = df[constants.FEATURE_COLS[feature]].apply(pd.to_numeric, downcast='float')

  df = df.rename(columns={feature: 'y'})
  # todo - remove once neuralprophet issue is resolved
  df.loc[0, 'snow'] = 0.01
  df.loc[0, 'snowdepth'] = 0.01
  log.info(f'dataset ready for inference:\n{df}')

  # load model
  model = s3.load_model(usgs_site, feature)

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
  data[f'{feature}_5th'] = np.nan
  data[f'{feature}_95th'] = np.nan
  data[f'{feature}'] = data[f'{feature}'].combine_first(yhat['origin-0'])
  data[f'{feature}_5th'] = data[f'{feature}_5th'].combine_first(yhat['origin-0 5.0%'])
  data[f'{feature}_95th'] = data[f'{feature}_95th'].combine_first(yhat['origin-0 95.0%'])

  return data
