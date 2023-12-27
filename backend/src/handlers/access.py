import pandas as pd

from utils import db, constants

def handler(event, _context):
  query_params = event.get('queryStringParameters', {})
  horizon = query_params.get('horizon', 14 * 24)

  if horizon < constants.FORECAST_HORIZON:
    hist = []
  else:
    hist = db.get_n_most_recent_hist_entries(constants.USGS_SITE, horizon - constants.FORECAST_HORIZON)
    hist = pd.DataFrame(hist)
    hist = hist.set_index(pd.to_datetime(hist['timestamp'].apply(pd.to_numeric), unit='s'))

  last_fcst_entry = db.get_latest_fcst_entry(constants.USGS_SITE)
  fcst = db.get_entire_fcst(constants.USGS_SITE, last_fcst_entry['origin'])
  fcst = pd.DataFrame(fcst)
  fcst = fcst.set_index(pd.to_datetime(fcst['timestamp'].apply(pd.to_numeric), unit='s'))

  df = pd.concat([hist, fcst]).sort_index()
  return { 'statusCode': 200, 'body': df.to_json(orient='records') }
