from prophet.serialize import model_from_json
from sqlalchemy import create_engine
import json
import pandas as pd
import requests
import os
from dotenv import load_dotenv
load_dotenv()

from utils.db import HOST, PORT, USER, PASS, DBNAME, cur

def handler(_event, _context):
  # retrieve model
  cur.execute('''
    SELECT (model) FROM saved_models WHERE location = 'callicoon'
  ''')
  model_json = cur.fetchall()[0][0]
  model = model_from_json(json.dumps(model_json))

  res = requests.get('https://api.weatherapi.com/v1/forecast.json?key={}&q=Callicoon, NY&days=3&aqi=no&alerts=no'.format(os.environ['WEATHER_KEY']))

  forecast_raw = []
  for day in res.json()['forecast']['forecastday']:
    forecast_raw += day['hour']

  forecast = {}
  for hour in forecast_raw:
    airtemp = hour['temp_c']
    cloudcover = hour['cloud'] * (4 / 100)
    precip = 0
    if (hour['will_it_rain']): precip = 2
    elif (hour['will_it_snow']): precip = 3

    forecast[pd.to_datetime(hour['time'])] = {
      'airtemp': airtemp,
      'cloudcover': cloudcover,
      'precip': precip
    }

  forecast_df = pd.DataFrame.from_dict(forecast, orient='index')
  forecast_df = forecast_df.resample('15min').interpolate('linear')

  forecast_df = forecast_df.reset_index()
  forecast_df = forecast_df.rename(columns={'index': 'ds'})
  forecast_df['ds'] = forecast_df['ds'].dt.tz_localize(None)

  print(forecast_df.head(20))

  pred = model.predict(forecast_df)
  print(pred)

  # create engine
  engine = create_engine(f'postgresql://{USER}:{PASS}@{HOST}:{PORT}/{DBNAME}')

  try:
    pred.to_sql('forecast', engine, if_exists='replace')
    print('updated forecast')
  except Exception as e:
    print('error updating forecast', e)

  return { 'statusCode': 200 }
