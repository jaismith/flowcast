import pandas as pd
import numpy as np
import requests
import logging
from datetime import datetime
import xml.etree.ElementTree as ET

from utils.utils import to_iso
from utils.constants import WATER_CONDITION_FEATURES

log = logging.getLogger(__name__)

def get_site_coords(usgs_site: str):
  url = f'https://nwis.waterservices.usgs.gov/nwis/site/?sites={usgs_site}&format=mapper'
  log.info(f'querying usgs site at {url}')
  res = requests.get(url)

  root = ET.fromstring(res.content)
  site = root.find('sites')[0]
  lat = site.get('lat')
  lng = site.get('lng')

  return (lat, lng)

def fetch_observations(start_dt: datetime, usgs_site: str):
  # fetch most recent available obs from nwis
  url = f'https://nwis.waterservices.usgs.gov/nwis/iv/?format=json&sites={usgs_site}&parameterCd={",".join(WATER_CONDITION_FEATURES.keys())}&siteStatus=all&startDT={to_iso(start_dt)}'
  log.info(f'querying usgs instantaneous values at {url}')
  res = requests.get(url)

  water = pd.DataFrame()
  data = res.json()
  for series in data['value']['timeSeries']:
    code = series['variable']['variableCode'][0]['value']
    values = series['values'][0]['value']

    if (len(values)) == 0:
      water[WATER_CONDITION_FEATURES[code]] = np.nan
      continue

    df = pd.DataFrame(values)
    df = df.set_index(pd.to_datetime(df['dateTime'], utc=True))
    df = df.drop(['qualifiers', 'dateTime'], axis=1)
    df['value'] = df['value'].astype('float64')
    df = df.rename(columns={'value': WATER_CONDITION_FEATURES[code]})
    df.index.name = None

    water = water.join(df, how='outer')

  log.info(f'retrieved {water.shape[0]} new usgs obs')

  return water
