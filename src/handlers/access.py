import pandas as pd

from utils.db import engine

def handler(_event, _context):
  forecast = pd.read_sql(
    'forecast',
    engine,
    index_col='index'
  )

  forecast_json = forecast.to_json()

  return { 'statusCode': 200, 'body': forecast_json }
