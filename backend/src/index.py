import logging

logging.basicConfig(level=logging.INFO)

# from handlers.forecast import handler as handle_forecast
from handlers.retrain import handler as handle_retrain
from handlers.update import handler as handle_update
# from handlers.access import handler as handle_access
