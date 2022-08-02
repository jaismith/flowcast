import pickle
import pandas as pd
import requests
import os
from dotenv import load_dotenv
load_dotenv()

from utils.db import engine, cur

def handler(_event, _context):
  # retrieve model
  cur.execute('''
    SELECT (model) FROM saved_models WHERE location = 'callicoon'
  ''')
  model_pickle = cur.fetchall()[0][0]
  model = pickle.loads(model_pickle)

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

    forecast[pd.to_datetime(int(hour['time_epoch']), unit='s', origin='unix').tz_localize(None)] = {
      'airtemp': airtemp,
      'cloudcover': cloudcover,
      'precip': precip
    }

  forecast_df = pd.DataFrame.from_dict(forecast, orient='index')

  # add the last three days of historical observations to the prediction frame
  # load observations df
  last_four_days = pd.read_sql(
    '''
    SELECT * FROM historical_obs
    WHERE ds > (CURRENT_DATE - INTERVAL '4 DAY')
    ''',
    engine,
    index_col='index'
  )

  last_four_days = last_four_days.drop(columns=['107337_00065'])
  last_four_days = last_four_days.rename(columns={'107338_00010': 'y'})
  last_four_days['ds'] = pd.to_datetime(last_four_days['ds']).dt.tz_convert(None)

  # resample to 1h
  last_four_days = last_four_days.set_index('ds')
  last_four_days = last_four_days.resample('30min').interpolate('linear')
  forecast_df = forecast_df.resample('30min').interpolate('linear')
  last_four_days = last_four_days.reset_index()
  forecast_df = forecast_df.reset_index()
  last_four_days = last_four_days.rename(columns={'index': 'ds'})
  forecast_df = forecast_df.rename(columns={'index': 'ds'})

  # remove overlap between last four days and forecast df
  forecast_df = forecast_df[~forecast_df['ds'].isin(last_four_days['ds'])]

  print('=== historical ===\n', last_four_days)
  print('=== forecast ===\n', forecast_df)

  future = model.make_future_dataframe(
    df=last_four_days,
    regressors_df=forecast_df,
    n_historic_predictions=True
  )
  pred = model.predict(future)
  print(pred)

  try:
    pred.to_sql('forecast', engine, if_exists='replace')
    print('updated forecast')
  except Exception as e:
    print('error updating forecast', e)

  return { 'statusCode': 200 }
