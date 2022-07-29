from sqlalchemy import create_engine
import pandas as pd
from scipy.signal import savgol_filter
from prophet import Prophet
from prophet.serialize import model_to_json, model_from_json
import json

from utils.db import HOST, PORT, USER, PASS, DBNAME, cur, conn
from utils.utils import stan_init

def handler(_event, _context):
  # create engine
  engine = create_engine(f'postgresql://{USER}:{PASS}@{HOST}:{PORT}/{DBNAME}')

  # with open('output/model.json') as f:
  #   model = model_from_json(f.read())

  # model_json = model_to_json(model)
  # cur.execute('''
  #   CREATE TABLE IF NOT EXISTS saved_models (
  #     location TEXT NOT NULL,
  #     last_updated TIMESTAMP NOT NULL DEFAULT NOW(),
  #     model JSON NOT NULL,
  #     PRIMARY KEY (location)
  #   )
  # ''')

  # cur.execute('''
  #   INSERT INTO saved_models(location, model)
  #   VALUES(%s, %s) RETURNING last_updated;
  # ''', ('callicoon', model_json))

  # model_updated = cur.fetchone()[0]
  # conn.commit()

  # print(f'model updated at {model_updated.ctime()}')

  # load df
  observations = pd.read_sql('historical_obs', engine)
  observations = observations.drop(columns=['index'])
  print(observations.head())

  # prep for prophet
  # to make this data better suited for regression, we'll run it through savitzky golay to smooth
  observations[observations.columns[2:]] = observations[observations.columns[2:]].apply(lambda d: savgol_filter(d, 25, 3))

  # for readability, rename 107337_00065 -> gageheight, 107338_00010 -> watertemp
  observations = observations.rename(columns={'107337_00065': 'gageheight', '107338_00010': 'watertemp'})

  # [temp] to simplify, drop gageheight and reduce num observations
  observations = observations.drop(columns=['gageheight'])

  # water temp -> y, index -> ds
  observations = observations.reset_index()
  observations = observations.drop(columns=['index'])
  observations = observations.rename(columns={'watertemp': 'y'})
  print(observations.head())

  # remove timezone
  observations['ds'] = observations['ds'].dt.tz_localize(None)

  # get prior learned params
  prior = cur.execute('''
    SELECT (model) FROM saved_models WHERE location = 'callicoon'
  ''')
  saved_model_json = cur.fetchall()[0][0]
  saved_model = model_from_json(json.dumps(saved_model_json))

  # train model
  model = Prophet(yearly_seasonality=True, daily_seasonality=True, weekly_seasonality=False)
  model.add_regressor('airtemp', standardize=False)
  model.add_regressor('cloudcover', standardize=False)
  model.add_regressor('precip', standardize=False)

  # fit
  print('fitting model...')
  model.fit(observations, init=stan_init(saved_model))

  # # save model
  # with open('output/model.json', 'w+') as f:
  #   f.write(model_to_json(model))

  # update db
  cur.execute('''
    UPDATE saved_models
      SET model = %s,
          last_updated = DEFAULT
    WHERE location = 'callicoon'
  ''', (model_to_json(model),))
  conn.commit()

  return { 'statusCode': 200 }
