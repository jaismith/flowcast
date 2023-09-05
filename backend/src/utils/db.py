import boto3
from boto3.dynamodb.conditions import Key

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

def push_hist_entries(entries: [dict]):
  with table.batch_writer() as batch:
    for entry in entries:
      batch.put_item(Item=entry)

def push_fcst_entries(entries: [dict]):
  with table.batch_writer() as batch:
    for entry in entries:
      batch.put_item(Item=entry)
