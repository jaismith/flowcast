from zoneinfo import ZoneInfo
import pandas as pd
from decimal import Decimal
from datetime import datetime, timezone
import logging
import pytz

log = logging.getLogger(__name__)

# https://facebook.github.io/prophet/docs/additional_topics.html
def stan_init(m):
  """Retrieve parameters from a trained model.

  Retrieve parameters from a trained model in the format
  used to initialize a new Stan model.

  Parameters
  ----------
  m: A trained model of the Prophet class.

  Returns
  -------
  A Dictionary containing retrieved parameters of m.

  """
  res = {}
  for pname in ['k', 'm', 'sigma_obs']:
    res[pname] = m.params[pname][0][0]
  for pname in ['delta', 'beta']:
    res[pname] = m.params[pname][0]
  return res

# def obs_to_entries(data):
#   """Convert an observation into a datatable entry.

#   Parameters
#   ---
#   data (pd.DataFrame):
#     site: int
#     ds: timestamp (pd.DateTime)
#     airtemp: float
#     cloudcover: float
#     precip: float
#     gageheight: float
#     watertemp: float
#   data_type: 'historical' or 'forecast'

#   Returns
#   ---
#   A dictionary containing the datatable entry.
#   """

#   data['ds'] = data['ds'].astype(np.int64) // 10**9
#   data.rename(columns={
#     'ds': 'timestamp',
#     'site': 'usgs_site'
#   }, inplace=True)
#   data['usgs_site#data_type'] = data['usgs_site'] + '#' + data['data_type']

#   # floats -> decimal
#   to_decimal = lambda x: Decimal(x).quantize(Decimal('1.00'))
#   data['airtemp'] = data['airtemp'].apply(to_decimal)
#   data['cloudcover'] = data['cloudcover'].apply(to_decimal)
#   data['precip'] = data['precip'].apply(to_decimal)
#   data['gageheight'] = data['gageheight'].apply(to_decimal)
#   data['watertemp'] = data['watertemp'].apply(to_decimal)

#   return data

def to_iso(dt: datetime):
  return dt.isoformat(timespec='seconds').replace('+00:00', 'Z')

def merge_dfs(dfs: [pd.DataFrame]):
  if len(dfs) < 2:
    raise RuntimeError('need at least two dfs to perform merge')

  df = pd.merge(dfs[0], dfs[1], left_index=True, right_index=True, how='outer')
  if len(dfs) > 2:
    for add_df in dfs[2:]:
      df = pd.merge(df, add_df, left_index=True, right_index=True, how='outer')

  return df

def resample_df(df: pd.DataFrame, freq: str):
  if df.shape[0] == 0: return df

  # before running interpolation, ensure all cols are numeric
  df = df.apply(pd.to_numeric)

  oidx = df.index
  nidx = pd.date_range(oidx.min().round(freq), oidx.max().round(freq), freq=freq)
  df = df.reindex(oidx.union(nidx)).interpolate('time', limit_direction='both').reindex(nidx)
  df = df.dropna()

  return df

def generate_hist_rows(hist_df: pd.DataFrame):
  new_hist = []
  for ts, row in hist_df.iterrows():
    usgs_site = '01427510'
    new_hist.append({
        'usgs_site': usgs_site,
        'type': 'hist',
        'usgs_site#type': f'{usgs_site}#hist',
        'timestamp': int(ts.timestamp()),
        'origin#timestamp': f'{int(ts.timestamp())}#{int(ts.timestamp())}',
        **row
    })

  return new_hist

def generate_fcst_rows(fcst_df: pd.DataFrame, origin_ts: pd.Timestamp, skip_meta: bool = False):
  new_fcst = []
  for ts, row in fcst_df.iterrows():
      usgs_site = '01427510'
      origin = int(origin_ts.timestamp())
      timestamp = int(ts.timestamp())
      horizon = timestamp - origin

      item = {}
      if not skip_meta:
        item = {
          'usgs_site': usgs_site,
          'type': 'fcst',
          'usgs_site#type': f'{usgs_site}#fcst',
          'origin': origin,
          'timestamp': timestamp,
          'horizon': horizon,
          'horizon#timestamp': f'{horizon}#{timestamp}',
          'origin#timestamp': f'{origin}#{timestamp}',
          'watertemp': None,
          'streamflow': None
        }

      new_fcst.append({
          **item,
          **row
      })

  return new_fcst

def convert_floats_to_decimals(df: pd.DataFrame):
  for col in df.columns:
    if pd.api.types.is_float_dtype(df[col]) or (df[col].shape[0] > 0 and isinstance(df[col][0], float)):
      df[col] = df[col].apply(lambda x: Decimal(x).quantize(Decimal('1.00')))

def prep_archive_for_training(archive: pd.DataFrame) -> pd.DataFrame:
  # only use historical observations for training, filter
  log.info(f'dropping forecasted entries')
  training = archive[archive['type'] == 'hist']

  # ! temporarily drop streamflow to simplify (eventually will need two models,
  # one targeting streamflow, one targeting watertemp, then will need a two part forecast step)
  log.info('dropping non-feature columns, reindexing, renaming ds col')
  feature_cols = ['precip', 'snow', 'snowdepth', 'cloudcover', 'airtemp', 'watertemp']
  training = training.drop(columns=training.columns.difference(feature_cols))
  training = training.reset_index()
  training = training.rename(columns={'timestamp': 'ds'})
  training['ds'] = pd.to_datetime(training['ds']).dt.tz_convert(None)

  # convert decimals to floats
  training[feature_cols] = training[feature_cols].apply(pd.to_numeric, downcast='float')

  training = training.rename(columns={'watertemp': 'y'})

  return training

def timestamp_exists_in_timezone(posix_timestamp, tz_name):
  try:
    tz = ZoneInfo(tz_name)
    # Convert POSIX timestamp to a timezone-aware datetime object in UTC
    utc_time = datetime.fromtimestamp(posix_timestamp, timezone.utc)
    # Convert the UTC datetime object to the specified timezone
    utc_time.astimezone(tz)
    return True
  except Exception as e:
    return False
