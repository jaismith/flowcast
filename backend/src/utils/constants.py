USGS_SITE = '01427510'
WATER_CONDITION_FEATURES = {'00010': 'watertemp', '00060': 'streamflow'}
ATMOSPHERIC_WEATHER_FEATURES = {'temp': 'airtemp', 'precip': 'precip', 'cloudcover': 'cloudcover', 'snow': 'snow', 'snowdepth': 'snowdepth'}
TIMESERIES_FREQUENCY = '1h'
FORECAST_HORIZON = 24*7 # hours
MAX_HISTORY_REACHBACK_YEARS = 3
CONFIDENCE_INTERVAL = 0.90
