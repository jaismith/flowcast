import logging
import os
import sys
import shutil
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

if len(logging.getLogger().handlers) > 0:
    # The Lambda environment pre-configures a handler logging to stderr. If a handler is already configured,
    # `.basicConfig` does not execute. Thus we set the level directly.
    logging.getLogger().setLevel(logging.INFO)
else:
    logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

import handlers.forecast as forecast
import handlers.train as train
import handlers.update as update
import handlers.access as access
import handlers.export as export
import handlers.onboard as onboard

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

# this is run in fargate, and as such has slightly different parameters
def handle_train(usgs_site: str, is_onboarding: bool):
  return handle(train.handler, usgs_site, is_onboarding)

def handle_update(event, context):
  return handle(update.handler, event, context)

def handle_access(event, context):
  return handle(access.handler, event, context)

def handle_export(event, context):
  return handle(export.handler, event, context)

def handle_onboard_connect(event, context):
  return handle(onboard.connect, event, context)

def handle_onboard_disconnect(event, context):
  return handle(onboard.disconnect, event, context)

def handle_onboard_process_stream(event, context):
  return handle(onboard.process_stream, event, context)

def handle_onboard_failed(event, context):
  return handle(onboard.register_failure, event, context)

import utils.s3 as s3
import utils.constants as constants

if __name__ == '__main__':
  target = sys.argv[1]
  if target == 'jumpstart': log.debug(s3.fetch_jumpstart_data(constants.USGS_SITE, 'hist', 1599466380))
  elif target == 'forecast': log.debug(handle_forecast(None, None))
  elif target == 'train': log.debug(handle_train(None, None))
  elif target == 'update': log.debug(handle_update(None, None))
  elif target == 'access':
    from events import access as access_events
    log.debug(handle_access(access_events.event, None))
