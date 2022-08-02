from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
import pandas as pd
import requests
from ftplib import FTP
import os
import gzip
from ish_parser import ish_parser

from utils.db import engine, cur

# * config

noaa_limit = 500 # 500 obs
noaa_freq = 1 # 1 hour/obs
overall_freq = '30min'

def handler(_event, _context):
  # * upload existing
  # # load observations df
  # observations = pd.read_pickle('../output/observations.pickle')
  # # reindex, make ds a column
  # observations = observations.reset_index().rename(columns={'index': 'ds'})

  # # seed
  # try:
  #   observations.to_sql('historical_obs', engine, if_exists='fail')
  #   print('seeded with pickled observations...')
  # except Exception as e:
  #   print('table already exists, skipping seed...', e)

  def to_iso(dt): return dt.isoformat().replace('+00:00', 'Z')

  # get most recent entry
  cur.execute("""SELECT * FROM historical_obs ORDER BY ds DESC LIMIT 1""")
  last_obs = cur.fetchall()[0]
  last_idx = last_obs[0]
  start_dt = last_obs[1] + timedelta(minutes=1)
  print(f'last observation timestamped {last_obs[1].ctime()}')

  # fetch most recent available obs from nwis
  res = requests.get(f'https://nwis.waterservices.usgs.gov/nwis/iv/?format=json&sites=01427510&parameterCd=00010,00065&siteStatus=all&startDT={to_iso(start_dt)}')
  nwis = {}
  for series in res.json()['value']['timeSeries']:
    code = series['variable']['variableCode'][0]['value']
    values = series['values'][0]['value']
    nwis[code] = values
    print(f'retrieved {len(nwis[code])} new observations for variable {code}')

  if '00010' in nwis.keys():
    nwis_watertemp_df = pd.DataFrame(nwis['00010'])
    nwis_watertemp_df['dateTime'] = pd.to_datetime(nwis_watertemp_df['dateTime'])
    nwis_watertemp_df.set_index(nwis_watertemp_df.columns[2], inplace=True)
    nwis_watertemp_df.drop(columns=nwis_watertemp_df.columns[1], inplace=True)
    nwis_watertemp_df['value'] = nwis_watertemp_df['value'].astype('float64')
    nwis_watertemp_df.rename(columns={'value': '107338_00010'}, inplace=True)
  else:
    print('no new observations for 00010, no update needed')
    return { 'statusCode': 200 }

  if '00065' in nwis.keys():
    nwis_gageheight_df = pd.DataFrame(nwis['00065'])
    nwis_gageheight_df['dateTime'] = pd.to_datetime(nwis_gageheight_df['dateTime'])
    nwis_gageheight_df.set_index(nwis_gageheight_df.columns[2], inplace=True)
    nwis_gageheight_df.drop(columns=nwis_gageheight_df.columns[1], inplace=True)
    nwis_gageheight_df['value'] = nwis_gageheight_df['value'].astype('float64')
    nwis_gageheight_df.rename(columns={'value': '107337_00065'}, inplace=True)
  else:
    print('no new observations for 00065, no update needed')
    return { 'statusCode': 200 }

  # fetch most recent available obs from noaa
  ftp = FTP(os.environ['NCEI_HOST'])
  ftp.login('anonymous', os.environ['NCEI_EMAIL'])
  ftp.cwd('/pub/data/noaa')

  try:
    for year in range(start_dt.year, datetime.today().year + 1):
      print(f'retrieving ncei archive for {year}')
      ftp.retrbinary(f'RETR {year}/725145-54746-{year}.gz', open(f'/tmp/725145-54746-{year}.gz', 'wb').write)
    ncei_raw = ''
    for filename in [f for f in os.listdir('/tmp') if '725145-54746-' in f]:
      ncei_raw += gzip.open(f'/tmp/{filename}', 'rt').read()
  except Exception as e:
    print('error retrieving file', e)

  print(f'retrieved {len(ncei_raw)} new observations from noaa')

  parser = ish_parser()
  parser.loads(ncei_raw)

  reports = parser.get_reports()
  reports_dict = {}
  for r in reports:
    airtemp = float(r.air_temperature.get_numeric())
    cloudcover = None
    if 'GD1' in r.additional().keys():
      cloudcover_str = r.get_additional_field('GD1').sky_cover_summation['coverage'].get_numeric()
      if cloudcover_str == '': cloudcover = 0
      else: cloudcover = float(cloudcover_str)
    precip = 0
    if 'AU1' in r.additional().keys():
      precip_str = r.get_additional_field('AU1').present_weather_array['intensity']
      if precip_str == 'MISSING': precip = None
      elif precip_str == 'Light': precip = 2
      elif precip_str == 'Moderate': precip = 3
      elif precip_str == 'Heavy': precip = 4
      elif precip_str == 'Vicinity': precip = 1

    reports_dict[pd.to_datetime(r.datetime)] = {
      'airtemp': airtemp,
      'cloudcover': cloudcover,
      'precip': precip
    }
  noaa_df = pd.DataFrame.from_dict(reports_dict, orient='index')

  # extend noaa data with api.weather.gov
  weather_api_entries = {}
  end_dt = datetime.now(timezone.utc)
  start_dt = end_dt - timedelta(weeks=1)

  res = requests.get(f'https://api.weather.gov/stations/KMSV/observations?start={to_iso(start_dt)}&end={to_iso(end_dt)}')
  data = res.json()

  for feature in data['features']:
    ts = pd.to_datetime(feature['properties']['timestamp'])
    airtemp = feature['properties']['temperature']['value']

    precip = feature['properties']['precipitationLastHour']['value']
    if precip is not None:
      if precip > 0.3: precip = 4
      elif precip > 0.1: precip = 3
      elif precip > 0: precip = 2
      else: precip = 0

    def translate_cloudcover(s):
      if s == 'CLR' or s == 'SKC': return 0
      elif s == 'FEW': return 1
      elif s == 'SCT': return 2
      elif s == 'BKN': return 3
      elif s == 'OVC': return 4
    layers = list(translate_cloudcover(layer['amount'])
      for layer in feature['properties']['cloudLayers'])
    cloudcover = max(layers) if len(layers) else 0

    weather_api_entries[ts] = {
      'airtemp': airtemp,
      'cloudcover': cloudcover,
      'precip': precip
    }
  weather_api_df = pd.DataFrame.from_dict(weather_api_entries, orient='index')
  noaa_df = noaa_df.loc[noaa_df.index < weather_api_df.index.min()]
  noaa_df = pd.concat((noaa_df, weather_api_df))

  # interpolate & resample
  # noaa
  oidx = noaa_df.index
  nidx = pd.date_range(last_obs[1], oidx.max().round(overall_freq), freq=overall_freq)[1:]

  # reindex with index union
  noaa_df = noaa_df.reindex(oidx.union(nidx))

  # interpolate airtemp, cloudcover linearly
  noaa_df['airtemp'].interpolate('linear', inplace=True)
  noaa_df['cloudcover'].interpolate('linear', inplace=True)
  # interpolate precip with pad
  noaa_df['precip'].interpolate('pad', inplace=True)

  # reindex with new (consistent)
  noaa_df = noaa_df.reindex(nidx)

  # ncei
  nwis_gageheight_df.interpolate('linear', inplace=True)
  nwis_watertemp_df.interpolate('linear', inplace=True)

  print(f'=== noaa ===\n{noaa_df.tail(3)}\n=== nwis gageheight ===\n{nwis_gageheight_df.tail(3)}\n=== nwis watertemp ===\n{nwis_watertemp_df.tail(3)}')

  # merge
  observations = noaa_df.join(nwis_gageheight_df, how='outer').join(nwis_watertemp_df, how='outer')
  observations.dropna(axis='index', inplace=True)

  # reindex as continuation of existing obs
  observations = observations.reset_index().rename(columns={'index': 'ds'})
  observations = observations.set_index(pd.RangeIndex(start=last_idx + 1, stop=observations.shape[0] + last_idx + 1, step=1))

  print(f'pushing {observations.shape[0]} updated observations\n', observations)

  # push new records to db
  try:
    observations.to_sql('historical_obs', engine, if_exists='append')
    print('appended new observations')
  except Exception as e:
    print('error appending new observations', e)

  # update local pickle
  # observations = pd.read_sql('historical_obs', engine)
  # observations = observations.drop(columns=['index'])
  # observations.to_pickle('../output/observations.pickle')

  return { 'statusCode': 200 }

  # fetch most recent 

  # # verify upload
  # cur.execute("""SELECT * FROM historical_obs""")
  # query_results = cur.fetchall()
  # print(f'finished with {len(query_results)} total observations')

  # 
  # print(len(res.json()))

  # 'https://api.weather.gov/stations/KMSV/observations?start=2008-07-15T00:00:00.000Z&end=2022-07-20T00:00:00.000Z&limit=500'
