import os
import boto3
import json
from aws_lambda_powertools.logging import Logger

from utils import db

logger = Logger()

@logger.inject_lambda_context(log_event=True)
def connect(event, _context):
  connection_id = event['requestContext']['connectionId']
  usgs_site = event['queryStringParameters']['usgs_site']

  db.add_site_subscription(usgs_site, connection_id)

  return { 'statusCode': 200 }

@logger.inject_lambda_context(log_event=True)
def disconnect(event, _context):
  connection_id = event['requestContext']['connectionId']
  usgs_site = event['queryStringParameters']['usgs_site']

  db.remove_site_subscriptions(usgs_site, [connection_id])

  return { 'statusCode': 200 }

@logger.inject_lambda_context(log_event=True)
def process_stream(event, _context):
  WEBSOCKET_API_ENDPOINT = os.environ['WEBSOCKET_API_ENDPOINT']
  apigatewaymanagementapi = boto3.client('apigatewaymanagementapi',
    endpoint_url=WEBSOCKET_API_ENDPOINT.replace('wss://', 'https://'))

  stale_subscriptions: list[str] = []
  last_usgs_site: str | None = None
  for record in event['Records']:
    if record['eventName'] == 'MODIFY':
      old_image = record['dynamodb']['OldImage']
      new_image = record['dynamodb']['NewImage']

      if (old_image['status'] == new_image['status']
          and old_image['onboarding_logs'] == new_image['onboarding_logs']):
        continue

      usgs_site = new_image['usgs_site']['S']
      last_usgs_site = usgs_site
      status = new_image['status']['S']
      onboarding_logs = [log['S'] for log in new_image['onboarding_logs']['L']]

      message = {
        'usgs_site': usgs_site,
        'status': status,
        'onboarding_logs': onboarding_logs
      }

      subscription_ids = new_image['subscription_ids']['SS']
      for subscription_id in subscription_ids:
        if subscription_id != 'placeholder':
          try:
            apigatewaymanagementapi.post_to_connection(
              ConnectionId=subscription_id,
              Data=json.dumps(message)
            )
          except apigatewaymanagementapi.exceptions.GoneException:
            stale_subscriptions.append(subscription_id)

  if len(stale_subscriptions) > 0 and last_usgs_site is not None:
    db.remove_site_subscriptions(last_usgs_site, stale_subscriptions)

  return { 'statusCode': 200 }

def register_failure(event, _context):
  usgs_site = None
  if 'usgs_site' in event:
    usgs_site = event['usgs_site']
  elif 'OriginalInput' in event and isinstance(event['OriginalInput'], dict) and 'usgs_site' in event['OriginalInput']:
    usgs_site = event['OriginalInput']['usgs_site']

  db.push_site_onboarding_log(usgs_site, '❌ Onboarding failed, please contact jksmithnyc@gmail.com for support')
  db.update_site_status(usgs_site, db.SiteStatus.FAILED)

  return { 'statusCode': 200 }
