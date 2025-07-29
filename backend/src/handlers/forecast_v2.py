import pandas as pd
import numpy as np
import logging
from datetime import datetime

log = logging.getLogger(__name__)

from utils import s3, db_v2, constants, utils

def handler(event, _context):
    usgs_site = event['usgs_site']
    is_onboarding = event['is_onboarding']

    if is_onboarding:
        db_v2.update_site_status(usgs_site, db_v2.SiteStatus.FORECASTING)
        db_v2.push_site_onboarding_log(usgs_site, f'🔮 Started forecasting for site {usgs_site} at {utils.get_current_local_time()}')

    # Get latest historical data
    log.info(f'retrieving most recent historical data for site {usgs_site}')
    last_hist_entries = db_v2.get_n_most_recent_hist_entries(usgs_site, constants.FORECAST_HORIZON*2)
    
    if not last_hist_entries:
        log.error(f'No historical data found for site {usgs_site}')
        return { 'statusCode': 500, 'error': 'No historical data available' }
    
    last_hist_origin = last_hist_entries[0]['timestamp']
    log.info(f'retrieving weather forecast data for site {usgs_site} at {last_hist_origin}')
    
    # Get weather forecast data (still using old format during transition)
    last_fcst_entries = db_v2.get_entire_fcst(usgs_site, last_hist_origin)
    
    if not last_fcst_entries:
        log.error(f'No weather forecast data found for site {usgs_site} at origin {last_hist_origin}')
        return { 'statusCode': 500, 'error': 'No weather forecast data available' }

    # Check if forecast already exists in new format
    existing_forecast = db_v2.get_forecast_by_origin(usgs_site, last_hist_origin)
    if existing_forecast:
        log.warning(f'Forecast already exists for origin time {last_hist_origin}')
        return { 'statusCode': 200 }

    # Prepare data for forecasting
    fcst_df = pd.DataFrame(last_fcst_entries)
    hist_df = pd.DataFrame(last_hist_entries)
    source_df = pd.concat([fcst_df[fcst_df['timestamp'] > hist_df['timestamp'].max()], hist_df])
    source_df = source_df.set_index(pd.to_datetime(source_df['timestamp'].apply(pd.to_numeric), unit='s')).sort_index()

    # Generate forecasts for each feature
    forecast_data = {
        'watertemp': {'values': [], 'timestamps': [], 'confidence_intervals': {'5th': [], '95th': []}},
        'streamflow': {'values': [], 'timestamps': [], 'confidence_intervals': {'5th': [], '95th': []}}
    }
    
    weather_forecast = {
        'airtemp': [], 'precip': [], 'cloudcover': [], 'snow': [], 'snowdepth': [], 'timestamps': []
    }

    for feature in constants.FEATURES_TO_FORECAST:
        if is_onboarding: 
            db_v2.push_site_onboarding_log(usgs_site, f'\tpredicting {feature} values')
        
        feature_fcst = forecast_feature(source_df, feature, usgs_site, is_onboarding)
        
        # Extract forecast data
        fcst_mask = feature_fcst['type'] == 'fcst'
        fcst_data = feature_fcst[fcst_mask]
        
        if len(fcst_data) == 0:
            log.error(f'No forecast data generated for feature {feature}')
            continue
        
        forecast_data[feature]['values'] = fcst_data[feature].tolist()
        forecast_data[feature]['timestamps'] = fcst_data.index.astype(np.int64) // 10**9
        forecast_data[feature]['confidence_intervals']['5th'] = fcst_data[f'{feature}_5th'].tolist()
        forecast_data[feature]['confidence_intervals']['95th'] = fcst_data[f'{feature}_95th'].tolist()
        
        # Extract weather forecast data (only once)
        if feature == constants.FEATURES_TO_FORECAST[0]:
            weather_forecast['airtemp'] = fcst_data['airtemp'].tolist()
            weather_forecast['precip'] = fcst_data['precip'].tolist()
            weather_forecast['cloudcover'] = fcst_data['cloudcover'].tolist()
            weather_forecast['snow'] = fcst_data['snow'].tolist()
            weather_forecast['snowdepth'] = fcst_data['snowdepth'].tolist()
            weather_forecast['timestamps'] = fcst_data.index.astype(np.int64) // 10**9

    # Store complete forecast
    complete_forecast = {
        'forecast_data': forecast_data,
        'weather_forecast': weather_forecast
    }
    
    log.info('Storing complete forecast to database')
    logging.getLogger('boto3.dynamodb.table').setLevel(logging.DEBUG)
    db_v2.push_forecast_entry(usgs_site, last_hist_origin, complete_forecast)
    
    if is_onboarding:
        db_v2.push_site_onboarding_log(usgs_site, f'\tfinished forecasting at {utils.get_current_local_time()}')
        db_v2.update_site_status(usgs_site, db_v2.SiteStatus.ACTIVE)

    return { 'statusCode': 200 }

def forecast_feature(data: pd.DataFrame, feature: str, usgs_site: str, is_onboarding: bool):
    """
    Forecast a specific feature using NeuralProphet
    This function remains largely the same as the original forecast_feature
    """
    df = data.drop(columns=data.columns.difference(constants.FEATURE_COLS[feature]))
    df = df.reset_index()
    df = df.rename(columns={'timestamp': 'ds'})

    # convert decimals to floats
    df[constants.FEATURE_COLS[feature]] = df[constants.FEATURE_COLS[feature]].apply(pd.to_numeric, downcast='float')

    df = df.rename(columns={feature: 'y'})
    # todo - remove once neuralprophet issue is resolved
    df.loc[0, 'snow'] = 0.01
    df.loc[0, 'snowdepth'] = 0.01
    log.info(f'dataset ready for inference:\n{df}')

    # load model
    model = s3.load_model(usgs_site, feature)

    # prep future
    future = model.make_future_dataframe(
        df=df[df['y'].notnull()],
        regressors_df=df[df['y'].isnull()].drop(columns=['y']),
        periods=constants.FORECAST_HORIZON
    )

    # predict
    # hide py.warnings (noisy pandas warnings during training)
    logging.getLogger('py.warnings').setLevel(logging.ERROR)
    pred = model.predict(df=future)
    yhat = model.get_latest_forecast(pred)

    yhat = yhat.set_index(yhat['ds'])
    utils.convert_floats_to_decimals(yhat)
    data[f'{feature}_5th'] = np.nan
    data[f'{feature}_95th'] = np.nan
    data[f'{feature}'] = data[f'{feature}'].combine_first(yhat['origin-0'])
    data[f'{feature}_5th'] = data[f'{feature}_5th'].combine_first(yhat['origin-0 5.0%'])
    data[f'{feature}_95th'] = data[f'{feature}_95th'].combine_first(yhat['origin-0 95.0%'])

    return data