import os
from aws_lambda_powertools.utilities.typing import LambdaContext
from aws_lambda_powertools.utilities.data_classes import APIGatewayProxyEventV2
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, CORSConfig
import pandas as pd
from botocore.exceptions import ClientError

from utils import usgs, forecast, claude, db

app = APIGatewayRestResolver(cors=CORSConfig(allow_origin='*'))

@app.get('/forecast')
def get_forecast():
  query_params = app.current_event.query_string_parameters
  usgs_site = query_params.get('usgs_site')
  start_ts = query_params.get('start_ts')
  historical_fcst_horizon = query_params.get('historical_fcst_horizon')
  df = forecast.get_forecast(usgs_site, start_ts, historical_fcst_horizon)

  return '{"forecast": ' + df.to_json(orient='records') + '}', 200

@app.get('/site')
def get_site():
  query_params = app.current_event.query_string_parameters
  usgs_site = query_params.get('usgs_site')
  return { 'site': usgs.get_site_info(usgs_site) }, 200

@app.post('/site/register')
def register_site():
  WEBSOCKET_API_ENDPOINT = os.environ['WEBSOCKET_API_ENDPOINT']

  query_params = app.current_event.query_string_parameters
  usgs_site = query_params.get('usgs_site')

  try:
    site_info = db.register_new_site(usgs_site)
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

  report = db.get_report(usgs_site, date)
  if report is None:
    report = claude.get_report(usgs_site)
    db.save_report(usgs_site, date, report)

  return { 'report': report }

def handler(event: APIGatewayProxyEventV2, context: LambdaContext):
  return app.resolve(event, context)
