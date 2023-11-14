import logging
import os
import shutil
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

if len(logging.getLogger().handlers) > 0:
    # The Lambda environment pre-configures a handler logging to stderr. If a handler is already configured,
    # `.basicConfig` does not execute. Thus we set the level directly.
    logging.getLogger().setLevel(logging.INFO)
else:
    logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

import handlers.forecast as forecast
import handlers.retrain as retrain
import handlers.update as update
import handlers.access as access

TMP_DIR = '/tmp/fc'
IS_LAMBDA_ENV = 'LAMBDA_TASK_ROOT' in os.environ.keys()

def garbage_collect():
  if not os.path.exists(TMP_DIR): return

  log.info('collecting garbage...')
  with os.scandir(TMP_DIR) as entries:
    for entry in entries:
      log.info(f'deleting {entry.path}')
      if entry.is_file():
        os.unlink(entry.path)
      else:
        shutil.rmtree(entry.path)
  log.info(f'...finished, {len(list(os.scandir(TMP_DIR)))} files remain')

def handle(handler, *args):
  exec_start_time = datetime.now()
  log.info(f'starting execution at {exec_start_time}')

  if IS_LAMBDA_ENV: garbage_collect()
  else: log.info('non-lambda environment, skipping garbage collection')

  try:
    res = handler(*args)
  except Exception as e:
    log.exception(e)
    res = { 'statusCode': 500, 'error': repr(e) }

  exec_end_time = datetime.now()
  log.info(f'completed execution at {exec_end_time} (took {(exec_end_time - exec_start_time).seconds}s)')

  return res

def handle_forecast(event, context):
  return handle(forecast.handler, event, context)

def handle_retrain(event, context):
  return handle(retrain.handler, event, context)

def handle_update(event, context):
  return handle(update.handler, event, context)

def handle_access(event, context):
  return handle(access.handler, event, context)

# import utils.s3 as s3
# import utils.constants as constants

if __name__ == '__main__':
  # s3.fetch_jumpstart_data(constants.USGS_SITE, 'hist', 1599466380)
  # handle_forecast(None, None)
  # handle_retrain(None, None)
  handle_update(None, None)
  # print(handle_access(None, None))
