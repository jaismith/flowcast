import logging
import pandas as pd
from datetime import datetime, timezone

from utils import usgs, weather, db, utils, s3
from utils.constants import TIMESERIES_FREQUENCY, MAX_HISTORY_REACHBACK_YEARS, USGS_SITE, FORECAST_HORIZON

log = logging.getLogger(__name__)

def handler(_event, _context):
  # get most recent entry
  last_obs = db.get_latest_hist_entry('01427510')
  if last_obs is None:
    last_obs = {'timestamp': (datetime.now(timezone.utc) - pd.Timedelta(days=MAX_HISTORY_REACHBACK_YEARS * 365)).timestamp()}
  last_obs_ts = pd.to_datetime(int(last_obs['timestamp']), unit='s', utc=True)

  log.info(f'last observation timestamped {last_obs_ts}')

  start_dt = last_obs_ts + pd.Timedelta(minutes=1)
  if (datetime.now(timezone.utc) - start_dt).days > 180:
    log.warn(f'start date {start_dt} is too far in the past for direct weather queries, checking s3')
    s3.verify_jumpstart_archive_exists('01427510', 'hist', int(start_dt.timestamp()))

  # fetch usgs data
  water_conditions = usgs.fetch_observations(start_dt, USGS_SITE)
  # usgs records in celsius, convert watertemp to fahrenheit
  water_conditions['watertemp'] = water_conditions['watertemp'].apply(lambda c: (c * 9/5) + 32)

  # fetch weather data
  site_location = usgs.get_site_coords(USGS_SITE)
  atmospheric_conditions_hist, atmospheric_conditions_fcst = weather.fetch_observations(start_dt, site_location, '01427510')

  # merge and resample
  hist_conditions = utils.merge_dfs([water_conditions, atmospheric_conditions_hist])
  hist_conditions = utils.resample_df(hist_conditions, TIMESERIES_FREQUENCY)
  atmospheric_conditions_fcst = utils.resample_df(atmospheric_conditions_fcst, TIMESERIES_FREQUENCY)
  origin_ts = hist_conditions.index.max() if hist_conditions.shape[0] > 0 else atmospheric_conditions_fcst.index.min() - pd.Timedelta(hours=1)
  atmospheric_conditions_fcst = atmospheric_conditions_fcst[(atmospheric_conditions_fcst.index > origin_ts)
      & (atmospheric_conditions_fcst.index <= origin_ts + pd.Timedelta(hours=FORECAST_HORIZON))]
  log.info(f'merged and resampled data\n=== historical ===\n{hist_conditions}\n=== forecasted ===\n{atmospheric_conditions_fcst}')

  # generate rows to send to db
  utils.convert_floats_to_decimals(hist_conditions)
  utils.convert_floats_to_decimals(atmospheric_conditions_fcst)
  hist_rows = utils.generate_hist_rows(hist_conditions)
  fcst_rows = utils.generate_fcst_rows(atmospheric_conditions_fcst, origin_ts)

  # push to ddb
  log.info('pushing entries to ddb')
  # a viewing the debug level logs from ddb during upload is helpful
  logging.getLogger('boto3.dynamodb.table').setLevel(logging.DEBUG)
  db.push_hist_entries(hist_rows)
  db.push_fcst_entries(fcst_rows)

  return { 'statusCode': 200 }
