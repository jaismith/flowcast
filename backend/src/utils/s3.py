import boto3
from boto3.dynamodb.types import TypeDeserializer
import logging
import json
import re
import os
import pandas as pd
import gzip
from neuralprophet import save, load

from utils import utils

JUMPSTART_BUCKET_NAME = os.environ['JUMPSTART_BUCKET_NAME']
ARCHIVE_BUCKET_NAME = os.environ['ARCHIVE_BUCKET_NAME']
MODEL_BUCKET_NAME = os.environ['MODEL_BUCKET_NAME']

TMP_MODEL_DIR = '/tmp/model'

log = logging.getLogger(__name__)

s3 = boto3.resource('s3')

jumpstart_bucket = s3.Bucket(JUMPSTART_BUCKET_NAME)
archive_bucket = s3.Bucket(ARCHIVE_BUCKET_NAME)
model_bucket = s3.Bucket(MODEL_BUCKET_NAME)

ddb_deserializer = TypeDeserializer()

def verify_jumpstart_archive_exists(usgs_site: str, type: str, start_ts: int):
  key = f'{usgs_site}_{type}_{start_ts}.json'

  try:
    jumpstart_bucket.Object(key).load()
  except Exception as e:
    log.error(f'could not load archive file {key}, maybe it doesn\'t exist?')
    raise e

def fetch_jumpstart_data(usgs_site: str, type: str, start_ts: int):
  available_site_data = list(filter(lambda o: o.key.startswith(f'{usgs_site}_{type}'),
      jumpstart_bucket.objects.all()))
  site_data_to_import = list(filter(lambda o: int(o.key.split('_')[2].split('.')[0]) < start_ts, available_site_data))

  if len(site_data_to_import) == 0: raise Exception('missing jumpstart data')

  data = None
  for obj in site_data_to_import:
    log.info(f'loading jumpstart file {obj.key}')
    obj_body = obj.get()['Body'].read().decode('utf-8')

    if data is None:
      data = json.loads(obj_body)
    else:
      data['days'] += json.loads(obj_body)['days']

  log.info(f'retrieved {len(data["days"])} days of jumpstart data')
  return data

def fetch_archive_data():
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
  archive = archive.set_index(pd.to_datetime(archive['timestamp'].astype(int), unit='s', utc=True))

  return archive

def save_model(model, usgs_site):
  os.makedirs(TMP_MODEL_DIR, exist_ok=True)
  new_model_path = TMP_MODEL_DIR + f'/{usgs_site}_new.np'
  save(model, new_model_path)

  model_data = None
  with open(new_model_path, 'rb') as model_file:
    model_data = model_file.read()

  model_object = model_bucket.Object(key=f'{usgs_site}_model.np')
  return model_object.put(Body=model_data)

def load_model(usgs_site):
  os.makedirs(TMP_MODEL_DIR, exist_ok=True)
  model_object = model_bucket.Object(key=f'{usgs_site}_model.np')
  model_data = None
  try:
    model_data = model_object.get()['Body'].read()
  except Exception as e:
    log.warning(f'unable to load existing model: {e}')
    return None

  old_model_path = TMP_MODEL_DIR + f'/{usgs_site}_old.np'
  with open(old_model_path, 'wb') as model_file:
    model_file.write(model_data)

  return load(old_model_path)
