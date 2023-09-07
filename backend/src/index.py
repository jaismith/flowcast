import logging
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

import handlers.forecast as forecast
import handlers.retrain as retrain
import handlers.update as update
# import handlers.access as access

def handle_forecast(event, context):
  try:
    return forecast.handler(event, context)
  except Exception as e:
    log.error(e)
    return { 'statusCode': 500, 'error': repr(e) }

def handle_retrain(event, context):
  try:
    return retrain.handler(event, context)
  except Exception as e:
    log.error(e)
    return { 'statusCode': 500, 'error': repr(e) }

def handle_update(event, context):
  try:
    return update.handler(event, context)
  except Exception as e:
    log.error(e)
    return { 'statusCode': 500, 'error': repr(e) }

# def handle_access(event, context):
#   try:
#     return access.handler(event, context)
#   except Exception as e:
#     log.error(e)
#     return { 'statusCode': 500, 'error': repr(e) }

# import utils.s3 as s3
# import utils.constants as constants

# if __name__ == '__main__':
  # s3.fetch_jumpstart_data(constants.USGS_SITE, 'hist', 1599466380)
  # handle_forecast(None, None)
  # handle_retrain(None, None)
  # handle_update(None, None)
#   # handle_access(None, None)
