from datetime import datetime, timedelta
import pandas as pd

from utils import db, constants

def handler(event, _context):
  print(f'received evt: {event}')

  query_params = event.get('queryStringParameters', {})
  start_ts = int(query_params.get('start_ts', (datetime.now() - timedelta(hours=14 * 24)).timestamp()))
  historical_fcst_horizon = int(query_params.get('historical_fcst_horizon', 0))

  if start_ts >= datetime.now().timestamp():
    hist = []
  else:
    hist = db.get_hist_entries_after(constants.USGS_SITE, start_ts)
    hist = pd.DataFrame(hist)
    hist = hist.set_index(pd.to_datetime(hist['timestamp'].apply(pd.to_numeric), unit='s'))

  last_fcst_entry = db.get_latest_fcst_entry(constants.USGS_SITE)
  fcst = db.get_entire_fcst(constants.USGS_SITE, last_fcst_entry['origin'])
  fcst = pd.DataFrame(fcst)
  fcst = fcst.set_index(pd.to_datetime(fcst['timestamp'].apply(pd.to_numeric), unit='s'))

  historical_fcsts = []
  if (historical_fcst_horizon > 0):
    historical_fcsts = db.get_fcsts_with_horizon_after(constants.USGS_SITE, historical_fcst_horizon, start_ts)
    historical_fcsts = pd.DataFrame(fcst)
    historical_fcsts = fcst.set_index(pd.to_datetime(fcst['timestamp'].apply(pd.to_numeric), unit='s'))

  df = pd.concat([hist, fcst, historical_fcsts]).sort_index()
  df = df[df['timestamp'] > start_ts]

  return {
    'statusCode': 200,
    'headers': {
      'Access-Control-Allow-Origin': '*'
    },
    'body': df.to_json(orient='records')
  }
