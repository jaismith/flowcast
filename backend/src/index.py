import logging
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)

import handlers.forecast as forecast
import handlers.retrain as retrain
import handlers.update as update
# import handlers.access as access

def handle_forecast(event, context):
  try:
    return forecast.handler(event, context)
  except Exception as e:
    return { 'statusCode': 500, 'error': repr(e) }

def handle_retrain(event, context):
  try:
    return retrain.handler(event, context)
  except Exception as e:
    return { 'statusCode': 500, 'error': repr(e) }

def handle_update(event, context):
  try:
    return update.handler(event, context)
  except Exception as e:
    return { 'statusCode': 500, 'error': repr(e) }

# def handle_access(event, context):
#   try:
#     return access.handler(event, context)
#   except Exception as e:
#     return { 'statusCode': 500, 'error': repr(e) }

if __name__ == '__main__':
#   # handle_forecast(None, None)
#   # handle_retrain(None, None)
  handle_update(None, None)
#   # handle_access(None, None)
