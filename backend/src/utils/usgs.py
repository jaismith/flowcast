import pandas as pd
import requests
import math
import logging
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

from utils.constants import WATER_CONDITION_FEATURES

log = logging.getLogger(__name__)

def get_site_info(usgs_site: str):
  url = f'https://nwis.waterservices.usgs.gov/nwis/site/?sites={usgs_site}&format=mapper'
  log.info(f'querying usgs site at {url}')
  res = requests.get(url)

  root = ET.fromstring(res.content)
  site = root.find('sites')[0]

  return {
    'sno': site.get('sno'),
    'sna': site.get('sna'),
    'cat': site.get('cat'),
    'lat': site.get('lat'),
    'lng': site.get('lng'),
    'agc': site.get('agc')
  }

def get_site_coords(usgs_site: str):
  site_info = get_site_info(usgs_site)
  return (site_info['lat'], site_info['lng'])

def fetch_observations(start_dt: datetime, usgs_site: str):
  hours_to_retrieve = (int) (math.ceil((datetime.now(timezone.utc) - start_dt).total_seconds() / 3600) + 1)

  # fetch most recent available obs from nwis
  url = f'https://nwis.waterservices.usgs.gov/nwis/iv/?format=json&sites={usgs_site}&parameterCd={",".join(WATER_CONDITION_FEATURES.keys())}&siteStatus=all&period=PT{hours_to_retrieve}H'
  log.info(f'querying usgs instantaneous values at {url}')
  res = requests.get(url)

  water = pd.DataFrame(columns=WATER_CONDITION_FEATURES.values())
  data = res.json()
  for series in data['value']['timeSeries']:
    code = series['variable']['variableCode'][0]['value']
    values = series['values'][0]['value']

    df = pd.DataFrame(values)
    df = df.set_index(pd.to_datetime(df['dateTime'], utc=True))
    df = df.drop(['qualifiers', 'dateTime'], axis=1)
    df['value'] = df['value'].astype('float64')
    df.index.name = None

    if water.shape[0] == 0:
      water[WATER_CONDITION_FEATURES[code]] = df['value']
    else:
      water[WATER_CONDITION_FEATURES[code]] = water[WATER_CONDITION_FEATURES[code]].combine_first(df['value'])

  # trim offset
  water = water[water.index >= start_dt]

  log.info(f'retrieved {water.shape[0]} new usgs obs')

  return water
