import boto3
import logging
import os
from datetime import datetime, timedelta

TABLE_ARN = os.environ['DATA_TABLE_ARN']
BUCKET_NAME = os.environ['ARCHIVE_BUCKET_NAME']

logging.basicConfig(level=logging.INFO)
client = boto3.client('dynamodb')

def handler(event, _context):
  export_job_arn = event['Result']['payload']['exportJobArn'] if 'exportJobArn' in event.keys() else None

  response = None
  if (export_job_arn is None):
    logging.info(f'requesting export from table {TABLE_ARN} to archive-bucket')
    now = datetime.now()
    response = client.export_table_to_point_in_time(
      TableArn=TABLE_ARN,
      ExportTime=now - timedelta(minutes=5),
      S3Bucket=BUCKET_NAME,
      S3Prefix=f'{now.timestamp()}',
      ExportFormat='DYNAMODB_JSON'
    )
    export_job_arn = response['ExportDescription']['ExportArn']
  else:
    response = client.describe_export(
      ExportArn=export_job_arn
    )

  status = response['ExportDescription']['ExportStatus']

  if status == 'IN_PROGRESS':
    return { 'statusCode': 202, 'exportJobArn': export_job_arn }
  elif status == 'FAILED' or status == 'CANCELLED':
    return { 'statusCode': 500 }
  else:
    return { 'statusCode': 200 }
