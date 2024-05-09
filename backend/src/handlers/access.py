from datetime import datetime, timedelta
import pandas as pd
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEventV2

from utils import db

def handler(event: APIGatewayProxyEventV2, _context):
  query_params = event.get('queryStringParameters', {})
  usgs_site = query_params.get('usgs_site')
  start_ts = int(query_params.get('start_ts', (datetime.now() - timedelta(hours=14 * 24)).timestamp()))
  historical_fcst_horizon = int(query_params.get('historical_fcst_horizon', 0))

  if start_ts >= datetime.now().timestamp():
    print('start date is in the future, skipping historical query')
    hist = pd.Dataframe([])
  else:
    hist = db.get_hist_entries_after(usgs_site, start_ts)
    hist = pd.DataFrame(hist)
    hist = hist.set_index(pd.to_datetime(hist['timestamp'].apply(pd.to_numeric), unit='s'))
    print(f'retrieved {len(hist.index)} historical observations')

  last_fcst_entry = db.get_latest_fcst_entry(usgs_site)
  fcst = db.get_entire_fcst(usgs_site, last_fcst_entry['origin'])
  fcst = pd.DataFrame(fcst)
  fcst = fcst.set_index(pd.to_datetime(fcst['timestamp'].apply(pd.to_numeric), unit='s'))
  print(f'retrieved {len(fcst.index)} current forecasted observations')

  historical_fcsts = pd.DataFrame([])
  if (historical_fcst_horizon > 0):
    print('historical forecast horizon provided, fetching historical forecasts')
    historical_fcsts = db.get_fcsts_with_horizon_after(usgs_site, historical_fcst_horizon, start_ts)
    historical_fcsts = pd.DataFrame(historical_fcsts)
    if (historical_fcsts.shape[0] > 0):
      historical_fcsts = historical_fcsts.set_index(pd.to_datetime(historical_fcsts['timestamp'].apply(pd.to_numeric), unit='s'))
    print(f'retrieved {len(historical_fcsts.index)} historical forecast observations')

  df = pd.concat([hist, fcst, historical_fcsts]).sort_index()
  df = df[df['timestamp'] > start_ts]

  return {
    'statusCode': 200,
    'headers': {
      'Access-Control-Allow-Origin': '*'
    },
    'body': df.to_json(orient='records')
  }
