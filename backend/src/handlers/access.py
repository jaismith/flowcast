import os
from decimal import Decimal
from aws_lambda_powertools.utilities.typing import LambdaContext
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEventV2
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, CORSConfig
import pandas as pd
from botocore.exceptions import ClientError

from utils import claude, db_v2

app = APIGatewayRestResolver(cors=CORSConfig(allow_origin='*'))

@app.get('/forecast')
def get_forecast():
  query_params = app.current_event.query_string_parameters or {}
  usgs_site = query_params.get('usgs_site')
  if not usgs_site:
    return { 'message': 'missing required query param: usgs_site' }, 400

  # optional params
  start_ts_param = query_params.get('start_ts')
  end_ts_param = query_params.get('end_ts')

  # defaults: last 10 days back to forecast horizon window
  default_start_ts = int(pd.Timestamp.now().timestamp()) - 10 * 24 * 3600
  start_ts = int(start_ts_param) if start_ts_param is not None else default_start_ts
  end_ts = int(end_ts_param) if end_ts_param is not None else None

  # Historical observations
  hist_items = db_v2.get_hist_entries_after(usgs_site, start_ts)
  hist_df = pd.DataFrame(hist_items)
  if not hist_df.empty:
    hist_df = hist_df.set_index(pd.to_datetime(hist_df['timestamp'].apply(pd.to_numeric), unit='s'))
    if end_ts is not None:
      end_dt = pd.to_datetime(end_ts, unit='s')
      hist_df = hist_df[hist_df.index <= end_dt]
  else:
    hist_df = pd.DataFrame([])

  # Latest forecast (combined water + atmospheric) in v2 format
  latest_fcst = db_v2.get_latest_forecast(usgs_site)
  # Build v2 response bundle
  historical_series = {
    'timestamps': [],
    'watertemp': [],
    'streamflow': [],
    'airtemp': [],
    'precip': [],
    'cloudcover': [],
    'snow': [],
    'snowdepth': []
  }
  if not hist_df.empty:
    # Ensure numeric arrays in time order
    sorted_hist = hist_df.sort_index()
    historical_series['timestamps'] = (sorted_hist.index.astype('int64') // 10**9).astype(int).tolist()
    for col in ['watertemp', 'streamflow', 'airtemp', 'precip', 'cloudcover', 'snow', 'snowdepth']:
      if col in sorted_hist.columns:
        historical_series[col] = pd.to_numeric(sorted_hist[col], errors='coerce').where(~sorted_hist[col].isnull(), None).tolist()

  if not latest_fcst:
    return {
      'forecast': {
        'origin_timestamp': None,
        'created_at': None,
        'horizon_hours': 0,
        'water_forecast': {
          'watertemp': { 'values': [], 'timestamps': [], 'confidence_intervals': { '5th': [], '95th': [] } },
          'streamflow': { 'values': [], 'timestamps': [], 'confidence_intervals': { '5th': [], '95th': [] } }
        },
        'atmospheric_forecast': { 'timestamps': [], 'airtemp': [], 'precip': [], 'cloudcover': [], 'snow': [], 'snowdepth': [] },
        'historical': historical_series
      }
    }, 200

  # Helper: recursively convert Decimal -> native int/float
  def to_native_numbers(obj):
    if isinstance(obj, dict):
      return { k: to_native_numbers(v) for k, v in obj.items() }
    if isinstance(obj, list):
      return [to_native_numbers(v) for v in obj]
    if isinstance(obj, Decimal):
      # Preserve integers when possible, use float otherwise
      try:
        if obj == obj.to_integral_value():
          return int(obj)
      except Exception:
        pass
      return float(obj)
    return obj

  # Extract v2 forecast payload and coerce numbers
  data = latest_fcst.get('data', {})
  water_fcst = to_native_numbers(data.get('water_forecast', {}))
  atmos_fcst = to_native_numbers(data.get('atmospheric_forecast', {}))

  origin_timestamp_raw = latest_fcst.get('timestamp')
  created_at_raw = latest_fcst.get('created_at')
  horizon_hours_raw = latest_fcst.get('horizon_hours', 0)

  def to_int_or_none(v):
    if v is None:
      return None
    if isinstance(v, Decimal):
      try:
        return int(v)
      except Exception:
        return int(float(v))
    return int(v)

  origin_timestamp = to_int_or_none(origin_timestamp_raw)
  created_at = to_int_or_none(created_at_raw)
  horizon_hours = to_int_or_none(horizon_hours_raw) or 0

  response_bundle = {
    'origin_timestamp': origin_timestamp,
    'created_at': created_at,
    'horizon_hours': horizon_hours,
    'water_forecast': water_fcst,
    'atmospheric_forecast': atmos_fcst,
    'historical': historical_series
  }

  return { 'forecast': response_bundle }, 200

@app.get('/site')
def get_site():
  query_params = app.current_event.query_string_parameters
  usgs_site = query_params.get('usgs_site')
  return { 'site': db_v2.get_site(usgs_site) }, 200

@app.get('/sites')
def get_sites():
  return { 'sites': db_v2.get_sites() }, 200

@app.post('/site/register')
def register_site():
  WEBSOCKET_API_ENDPOINT = os.environ['WEBSOCKET_API_ENDPOINT']

  query_params = app.current_event.query_string_parameters
  usgs_site = query_params.get('usgs_site')

  try:
    site_info = db_v2.register_new_site(usgs_site)
  except ClientError as e:
    if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
      return { 'message': 'Site is already (or currently being) onboarded.' }
    else:
      raise

  return { 'site': site_info, 'progress_url': WEBSOCKET_API_ENDPOINT }

@app.get('/report')
def get_report():
  query_params = app.current_event.query_string_parameters
  usgs_site = query_params.get('usgs_site')

  date = pd.Timestamp.today().date().isoformat()

  report = db_v2.get_report(usgs_site, date)
  if report is None:
    # Generate once and persist; guard against upstream errors
    generated = claude.get_report(usgs_site)
    db_v2.save_report(usgs_site, date, generated)
    # Normalize shape for client
    return { 'report': { 'report': generated } }, 200

  # If pulled from DB, it is an item dict; normalize to expected shape
  if isinstance(report, dict):
    text = report.get('report')
    return { 'report': { 'report': text } }, 200

  # Fallback: if storage shape changes and returns plain string
  return { 'report': { 'report': str(report) } }, 200

def handler(event: APIGatewayProxyEventV2, context: LambdaContext):
  return app.resolve(event, context)
