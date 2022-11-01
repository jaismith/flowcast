from math import nan
import pandas as pd

from utils.db import engine

def handler(_event, _context):
  # todo - would be more performant to filter these columns and format data in the sql query, but
  # since the table is relatively small and will always be the same size, this is ok for now
  forecast = pd.read_sql(
    '''
    SELECT * FROM forecast
    ''',
    engine
  )

  output = []
  forecast_idx = 1
  for idx in forecast.index:
    obs = {}
    obs['ds'] = forecast['ds'][idx]
    obs['temp'] = forecast['y'][idx]

    # the first forecast value is yhat1, second is yhat2, etc.
    if pd.isnull(obs['temp']):
      obs['temp_pred'] = forecast[f'yhat{forecast_idx}'][idx]
      forecast_idx += 1
    else:
      obs['temp_pred'] = nan

    output += obs

  forecast_json = forecast.to_json()
  return { 'statusCode': 200, 'body': forecast_json }
