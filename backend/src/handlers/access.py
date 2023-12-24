import pandas as pd

from utils import db, constants

def handler(_event, _context):
  hist = db.get_n_most_recent_hist_entries(constants.USGS_SITE, 24*30)
  hist = pd.DataFrame(hist)
  hist = hist.set_index(pd.to_datetime(hist['timestamp'].apply(pd.to_numeric), unit='s'))

  last_fcst_entry = db.get_latest_fcst_entry(constants.USGS_SITE)
  fcst = db.get_entire_fcst(constants.USGS_SITE, last_fcst_entry['origin'])
  fcst = pd.DataFrame(fcst)
  fcst = fcst.set_index(pd.to_datetime(fcst['timestamp'].apply(pd.to_numeric), unit='s'))

  df = pd.concat([hist, fcst]).sort_index()
  return { 'statusCode': 200, 'body': df.to_json(orient='records') }
