import pandas as pd
from scipy.signal import savgol_filter
from neuralprophet import NeuralProphet
import pickle

from utils.db import engine, cur, conn

def handler(_event, _context):
  # with open('model.pickle', 'rb') as f:
  #   model = pickle.load(f)

  # cur.execute('''
  #   CREATE TABLE IF NOT EXISTS saved_models (
  #     location TEXT NOT NULL,
  #     last_updated TIMESTAMP NOT NULL DEFAULT NOW(),
  #     model BYTEA NOT NULL,
  #     PRIMARY KEY (location)
  #   )
  # ''')

  # model_pickle = pickle.dumps(model)
  # cur.execute('''
  #   INSERT INTO saved_models(location, model)
  #   VALUES(%s, %s)
  #   ON CONFLICT (location) DO UPDATE
  #     SET model = excluded.model
  #   RETURNING last_updated;
  # ''', ('callicoon', model_pickle))

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

  # remove timezone
  observations['ds'] = observations['ds'].dt.tz_localize(None)

  # ! dedupe
  observations = observations.drop_duplicates(subset=['ds'])

  # reindex to fill holes
  observations = observations.set_index('ds')
  observations = observations.reindex(pd.date_range(start=observations.index[0], end=observations.index[-1], freq='30min'), fill_value=None)
  observations = observations.interpolate('linear')
  observations = observations.reset_index()
  observations = observations.rename(columns={'index': 'ds'})

  print(observations.head())

  # with pd.option_context('display.max_seq_items', None):
  #   print(pd.date_range(start=observations.index[0], end=observations.index[-1], freq='15min').difference(observations['ds']))

  # get prior learned params
  # prior = cur.execute('''
  #   SELECT (model) FROM saved_models WHERE location = 'callicoon'
  # ''')
  # saved_model_json = cur.fetchall()[0][0]
  # saved_model = model_from_json(json.dumps(saved_model_json))

  # train model
  model = NeuralProphet(
    growth='off',
    yearly_seasonality=True,
    daily_seasonality=True,
    weekly_seasonality=False,
    n_lags=2*24*4,
    n_forecasts=2*24*2,
    num_hidden_layers=4,
    d_hidden=64,
    learning_rate=0.003
  )
  model.add_future_regressor('airtemp')
  model.add_future_regressor('cloudcover')
  model.add_future_regressor('precip')

  # fit
  print('fitting model...')
  model.fit(observations, freq='30min')#, init=stan_init(saved_model))

  # # save model
  # with open('model.pickle', 'wb') as f:
  #   pickle.dump(model, f)

  # update db
  cur.execute('''
    UPDATE saved_models
      SET model = %s,
          last_updated = DEFAULT
    WHERE location = 'callicoon'
    RETURNING last_updated
  ''', (pickle.dumps(model),))
  conn.commit()

  last_updated = cur.fetchone()[0]
  print(f'retrained and updated model at {last_updated.ctime()}')

  return { 'statusCode': 200 }
