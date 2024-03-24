USGS_SITE = '01427510'
WATER_CONDITION_FEATURES = {'00010': 'watertemp', '00060': 'streamflow'}
ATMOSPHERIC_WEATHER_FEATURES = {'temp': 'airtemp', 'precip': 'precip', 'cloudcover': 'cloudcover', 'snow': 'snow', 'snowdepth': 'snowdepth'}
TIMESERIES_FREQUENCY = '1h'
FORECAST_HORIZON = 24*7 # hours
MAX_HISTORY_REACHBACK_YEARS = 8
CONFIDENCE_INTERVAL = 0.90
FEATURES_TO_FORECAST = ['streamflow', 'watertemp']
FEATURE_COLS = {
  'streamflow': ['precip', 'cloudcover', 'airtemp', 'streamflow', 'snow', 'snowdepth'],
  'watertemp': ['precip', 'cloudcover', 'airtemp', 'streamflow', 'watertemp', 'snow', 'snowdepth']
}
