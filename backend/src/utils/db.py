from datetime import datetime
import boto3
from boto3.dynamodb.conditions import Key, Attr

# * ddb

print('initializing ddb client')

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('flowcast-data')

def get_latest_hist_entry(usgs_site):
  res = table.query(
    KeyConditionExpression=Key('usgs_site#type')
        .eq(f'{usgs_site}#hist'),
    ScanIndexForward=False,
    Limit=1
  )

  try:
    return res['Items'][0]
  except IndexError:
    return None

def get_latest_fcst_entry(usgs_site):
  res = table.query(
    KeyConditionExpression=Key('usgs_site#type')
        .eq(f'{usgs_site}#fcst'),
    FilterExpression=Attr('watertemp').exists(), # avoid retrieving partial forecasts during update
    ScanIndexForward=False,
    Limit=1
  )

  try:
    return res['Items'][0]
  except IndexError:
    return None

def get_entire_fcst(usgs_site, origin):
  res = table.query(
    KeyConditionExpression=Key('usgs_site#type')
        .eq(f'{usgs_site}#fcst') & Key('origin#timestamp')
        .begins_with(str(origin))
  )

  return res['Items']

def get_hist_entries_after(usgs_site, start_ts):
  res = table.query(
    KeyConditionExpression=Key('usgs_site#type')
        .eq(f'{usgs_site}#hist') & Key('origin#timestamp').gte(f'{start_ts}'),
  )

  return res['Items']

def get_n_most_recent_hist_entries(usgs_site, n):
  res = table.query(
    KeyConditionExpression=Key('usgs_site#type')
        .eq(f'{usgs_site}#hist'),
    ScanIndexForward=False,
    Limit=n
  )

  return res['Items']

def get_fcsts_with_horizon_after(usgs_site, horizon, start_ts):
  res = table.query(
    IndexName='fcst_horizon_aware_index',
    KeyConditionExpression=Key('usgs_site#type')
        .eq(f'{usgs_site}#fcst') & Key('horizon#timestamp')
        .between(f'{horizon}#{start_ts}', f'{horizon}#{datetime.now().timestamp()}')
  )

  return res['Items']

def push_hist_entries(entries: list[dict]):
  with table.batch_writer() as batch:
    for entry in entries:
      batch.put_item(Item=entry)

def push_fcst_entries(entries: list[dict]):
  with table.batch_writer() as batch:
    for entry in entries:
      batch.put_item(Item=entry)
