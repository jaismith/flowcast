import boto3
import logging
import os
from datetime import datetime, timedelta, timezone

from utils import db, utils

TABLE_ARN = os.environ['DATA_TABLE_ARN']
BUCKET_NAME = os.environ['ARCHIVE_BUCKET_NAME']

logging.basicConfig(level=logging.INFO)
ddb_client = boto3.client('dynamodb')
s3_client = boto3.client('s3')

def get_nested(dictionary, keys, default=None):
  for key in keys:
    try:
      dictionary = dictionary[key]
    except KeyError:
      return default
  return dictionary

def handler(event, _context):
  usgs_site = event['usgs_site']
  is_onboarding = event['is_onboarding']
  export_job_arn = get_nested(event, ['Result', 'Payload', 'exportJobArn'])

  response = None
  if (export_job_arn is None):
    logging.info(f'requesting export from table {TABLE_ARN} to archive-bucket')
    if is_onboarding:
      db.update_site_status(usgs_site, db.SiteStatus.EXPORTING_SNAPSHOT)
      db.push_site_onboarding_log(usgs_site, f'💾 Exporting database snapshot for training at {utils.get_current_local_time()} (this may take a few minutes)')
    now = datetime.now()
    response = ddb_client.export_table_to_point_in_time(
      TableArn=TABLE_ARN,
      ExportTime=now,
      S3Bucket=BUCKET_NAME,
      S3Prefix=f'{now.timestamp()}',
      ExportFormat='DYNAMODB_JSON'
    )
    export_job_arn = response['ExportDescription']['ExportArn']
  else:
    response = ddb_client.describe_export(
      ExportArn=export_job_arn
    )

  status = response['ExportDescription']['ExportStatus']

  if status == 'IN_PROGRESS':
    return { 'statusCode': 202, 'exportJobArn': export_job_arn }
  elif status == 'FAILED' or status == 'CANCELLED':
    return { 'statusCode': 500 }
  else:
    delete_old_exports(BUCKET_NAME)
    if is_onboarding: db.push_site_onboarding_log(usgs_site, f'\tfinished exporting snapshot at {utils.get_current_local_time()}')
    return { 'statusCode': 200 }

def delete_old_exports(bucket_name, retention_days=7):
  cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)
  response = s3_client.list_objects_v2(Bucket=bucket_name)

  if 'Contents' in response:
    for item in response['Contents']:
      item_date_str = item['Key'].split('/')[0]
      try:
        item_date = datetime.fromtimestamp(float(item_date_str), tz=timezone.utc)
        if item_date < cutoff_date:
          logging.info(f"Deleting {item['Key']}")
          s3_client.delete_object(Bucket=bucket_name, Key=item['Key'])
      except ValueError:
        logging.warning(f"Skipping invalid date format: {item['Key']}")
