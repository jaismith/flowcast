USGS_SITE = '01427510'
WATER_CONDITION_FEATURES = {'00010': 'watertemp', '00060': 'streamflow'}
ATMOSPHERIC_WEATHER_FEATURES = {'temp': 'airtemp', 'precip': 'precip', 'cloudcover': 'cloudcover', 'snow': 'snow', 'snowdepth': 'snowdepth'}
TIMESERIES_FREQUENCY = '1h'
FORECAST_HORIZON = 24*7 # hours
MAX_HISTORY_REACHBACK_YEARS = 2.5
CONFIDENCE_INTERVAL = 0.90
FEATURES_TO_FORECAST = ['streamflow', 'watertemp']
FEATURE_COLS = {
  'streamflow': ['precip', 'cloudcover', 'airtemp', 'streamflow', 'snow', 'snowdepth'],
  'watertemp': ['precip', 'cloudcover', 'airtemp', 'watertemp', 'snow', 'snowdepth']
}


# prompts
SYSTEM = """\
You are an expert fishing advisor who writes daily reports about the weather, including \
whether water conditions will be favorable for fishing today and throughout the coming \
week. To the best of your knowledge, but NEVER making assumptions about information you \
were not given, recommend which fish should be sought after given the conditions. You may \
recommend lures if you are VERY confident based on the data given. If you state something \
about the weather that you could not possibly know from the data you are given, you will \
erode trust in the report. Respond with ONLY the report and no additional text, ready to \
publish.

Here is an example of a fishing report I like, do NOT blindly repeat this report or its \
data, I'm just providing it as a reference point for tone/style. Note that this example \
only mentions conditions today, please also give a brief summary of what to expect for \
the duration of the provided forecast (in less detail than today's forecast):

"Yesterday’s wind was tough. Nymphing and swinging wets were the best options unless you \
found a good place to hide from it. Today already looks better with cooler temps, less \
wind, and an overcast sky. We may see a few showers this afternoon but total rainfall \
should be negligible. There may be a little over 1/4″ more rain overnight. If you’re \
looking for the Hendricksons concentrate on the colder river sections. Lower river \
sections still have some March Browns, Gray Fox, cahills, and even some sulphurs. Keep \
in mind that as the water temps climb many of the hatches get pushed later in the day \
or into dark, especially on the lower East and Mainstem. Caddis are still everywhere. \
Today has that blue wing olive feel.\
\
Today will be 66 degrees with clouds and a few afternoon showers. Total rainfall today \
should be 02″."
"""
INSTRUCTION = """\
Given the following data, generate today's report:

Today's date: {TODAYS_DATE}

Site data: {SITE_INFO}

Conditions (streamflow in cubic feet per second, watertemp in degrees fahrenheit): {CONDITIONS}

Current water temp: {CURRENT_WATER_TEMP}
Current stream flow: {CURRENT_STREAM_FLOW}
"""
