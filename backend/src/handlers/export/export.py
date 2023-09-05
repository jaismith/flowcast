import boto3
import logging
import os
from datetime import datetime, timedelta

TABLE_ARN = os.environ['DATA_TABLE_ARN']
BUCKET_NAME = os.environ['ARCHIVE_BUCKET_NAME']

logging.basicConfig(level=logging.INFO)
client = boto3.client('dynamodb')

def handler(_event, _context):
  logging.info(f'requesting export from table {TABLE_ARN} to archive-bucket')

  dt = datetime.now()

  client.export_table_to_point_in_time(
    TableArn=TABLE_ARN,
    ExportTime=dt - timedelta(minutes=5),
    S3Bucket=BUCKET_NAME,
    S3Prefix=f'{dt.timestamp()}',
    ExportFormat='DYNAMODB_JSON'
  )

  return { 'statusCode': 200 }
