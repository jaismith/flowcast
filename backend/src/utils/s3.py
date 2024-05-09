import boto3
from boto3.dynamodb.types import TypeDeserializer
import logging
import json
import re
import os
import pandas as pd
import gzip
from io import BytesIO

JUMPSTART_BUCKET_NAME = os.environ['JUMPSTART_BUCKET_NAME']
ARCHIVE_BUCKET_NAME = os.environ['ARCHIVE_BUCKET_NAME']
MODEL_BUCKET_NAME = os.environ['MODEL_BUCKET_NAME']

TMP_MODEL_DIR = '/tmp/fc/model'

log = logging.getLogger(__name__)

s3 = boto3.resource('s3')

jumpstart_bucket = s3.Bucket(JUMPSTART_BUCKET_NAME)
archive_bucket = s3.Bucket(ARCHIVE_BUCKET_NAME)
model_bucket = s3.Bucket(MODEL_BUCKET_NAME)

ddb_deserializer = TypeDeserializer()

def get_available_site_data(usgs_site: str, type: str, start_ts: int):
  available_site_data = list(filter(lambda o: o.key.startswith(f'{usgs_site}_{type}'),
      jumpstart_bucket.objects.all()))
  return list(filter(lambda o: int(o.key.split('_')[2].split('.')[0]) <= start_ts, available_site_data))

def verify_jumpstart_archive_exists(usgs_site: str, type: str, start_ts: int):
  available_site_data = get_available_site_data(usgs_site, type, start_ts)
  if (len(available_site_data) < 1):
    log.error(f'could not load jumpstart data for site {usgs_site}, maybe it doesn\'t exist?')
    raise Exception()

def fetch_jumpstart_data(usgs_site: str, type: str, start_ts: int):
  available_site_data = get_available_site_data(usgs_site, type, start_ts)
  if len(available_site_data) == 0: raise Exception('missing jumpstart data')

  data = None
  for obj in available_site_data:
    log.info(f'loading jumpstart file {obj.key}')
    obj_body = obj.get()['Body'].read().decode('utf-8')

    if data is None:
      data = json.loads(obj_body)
    else:
      data['days'] += json.loads(obj_body)['days']

  log.info(f'retrieved {len(data["days"])} days of jumpstart data')
  return data

def fetch_archive_data(usgs_site: str):
  objects = archive_bucket.objects.all()
  timestamps = list(obj.key.split('/')[0] for obj in objects)

  latest_timestamp = max(timestamps)
  data_filter = re.compile(f'{latest_timestamp}/AWSDynamoDB/([^/]+)/data/(.+).json.gz')
  export_keys = list(obj.key for obj in objects if data_filter.match(obj.key))

  data = []
  for key in export_keys:
    object = s3.Object(ARCHIVE_BUCKET_NAME, key)
    with gzip.GzipFile(fileobj=object.get()['Body']) as gzipfile:
      for line in gzipfile.readlines():
        data.append(ddb_deserializer.deserialize({'M': json.loads(line)['Item']}))

  archive = pd.DataFrame(data)
  # select relevant usgs site, todo: optimize?
  archive = archive[archive['usgs_site'] == usgs_site]
  archive = archive.set_index(pd.to_datetime(archive['timestamp'].astype(int), unit='s', utc=True))

  return archive

def save_model(model, usgs_site, feature):
  # this is an expensive import, we'll only do it when this method is called
  from neuralprophet import save

  model_data = BytesIO()
  save(model, model_data)

  # s3 goes pretty hard on the debug logging here
  logging.getLogger('s3transfer').setLevel(logging.INFO)

  model_object = model_bucket.Object(key=f'{usgs_site}_{feature}_model.np')
  return model_object.put(Body=model_data.getvalue())

def load_model(usgs_site, feature):
  # this is an expensive import, we'll only do it when this method is called
  from neuralprophet import load

  os.makedirs(TMP_MODEL_DIR, exist_ok=True)
  model_object = model_bucket.Object(key=f'{usgs_site}_{feature}_model.np')
  try:
    model_data = BytesIO(model_object.get()['Body'].read())
  except Exception as e:
    log.warning(f'unable to load existing model: {e}')
    return None

  return load(model_data)
