# import json
import os
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
import requests

from dotenv import load_dotenv
load_dotenv()

import utils.utils as utils
import utils.s3 as s3
from utils.constants import ATMOSPHERIC_WEATHER_FEATURES

log = logging.getLogger(__name__)

VISUAL_CROSSING_API_KEY = os.environ['VISUAL_CROSSING_API_KEY']

def fetch_observations(start_dt: datetime, location: (float, float), usgs_site: str):
  end_dt = datetime.now(timezone.utc) + timedelta(days=3)

  data = None
  if (end_dt - start_dt).days > 180:
    data = s3.fetch_jumpstart_data(usgs_site, 'hist', int(start_dt.timestamp()))
  else:
    url = f'https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/{location[0]}%2C{location[1]}/{utils.to_iso(start_dt)}/{utils.to_iso(end_dt)}?unitGroup=us&include=hours&key={VISUAL_CROSSING_API_KEY}&contentType=json'
    log.info(f'querying visual crossing at {url}')
    res = requests.get(url)
    data = res.json()

  atmos = pd.concat(pd.DataFrame(day['hours']) for day in data['days'])

  atmos = atmos.set_index(pd.to_datetime(atmos['datetimeEpoch'], unit='s', utc=True))
  atmos = atmos.drop(columns=atmos.columns.difference(list(ATMOSPHERIC_WEATHER_FEATURES.keys()) + ['source']))
  atmos = atmos.rename(columns=ATMOSPHERIC_WEATHER_FEATURES)
  atmos.index.name = None

  # visual crossing always returns starting at the beginning of the day, trim to target range
  atmos = atmos[atmos.index >= start_dt]

  atmos_hist = atmos[atmos['source'] == 'obs'].drop(columns=['source'])
  atmos_fcst = atmos[atmos['source'] == 'fcst'].drop(columns=['source'])

  for df in [atmos_hist, atmos_fcst]:
    for col in df.columns:
      df[col] = df[col].astype('float64')

  log.info(f'retrieved {atmos_hist.shape[0] + atmos_fcst.shape[0]} new atmospheric obs')

  return atmos_hist, atmos_fcst
