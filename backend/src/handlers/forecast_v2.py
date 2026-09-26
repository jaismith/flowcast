import pandas as pd
import numpy as np
import logging
from utils import s3, db_v2, constants, utils, weather, usgs

log = logging.getLogger(__name__)

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
    
    # Check if forecast already exists in new format
    existing_forecast = db_v2.get_forecast_by_origin(usgs_site, last_hist_origin)
    if existing_forecast:
        log.warning(f'Forecast already exists for origin time {last_hist_origin}')
        return { 'statusCode': 200 }

    # Fetch fresh atmospheric forecast data to use as regressors and to embed
    hist_df = pd.DataFrame(last_hist_entries)
    hist_max_ts = int(hist_df['timestamp'].max()) if not hist_df.empty else last_hist_origin
    # Use tz-aware timestamp (UTC) for weather fetch compatibility
    start_dt = pd.to_datetime(hist_max_ts, unit='s', utc=True) + pd.Timedelta(minutes=1)
    site_location = usgs.get_site_coords(usgs_site)
    _, atmos_fcst_df = weather.fetch_observations(start_dt, site_location, usgs_site)
    # Resample and filter strictly after last historical timestamp up to horizon
    atmos_fcst_df = utils.resample_df(atmos_fcst_df, constants.TIMESERIES_FREQUENCY)
    hist_max_dt_utc = pd.to_datetime(hist_max_ts, unit='s', utc=True)
    atmos_fcst_df = atmos_fcst_df[(atmos_fcst_df.index > hist_max_dt_utc) &
                                  (atmos_fcst_df.index <= hist_max_dt_utc + pd.Timedelta(hours=constants.FORECAST_HORIZON))]
    utils.convert_floats_to_decimals(atmos_fcst_df)
    last_fcst_entries = []
    for ts, row in atmos_fcst_df.iterrows():
        last_fcst_entries.append({
            'timestamp': int(ts.timestamp()),
            'airtemp': row.get('airtemp'),
            'precip': row.get('precip'),
            'cloudcover': row.get('cloudcover'),
            'snow': row.get('snow'),
            'snowdepth': row.get('snowdepth')
        })

    # Prepare data for forecasting
    fcst_df = pd.DataFrame(last_fcst_entries)
    hist_df = pd.DataFrame(last_hist_entries)
    source_df = pd.concat([fcst_df[fcst_df['timestamp'] > hist_df['timestamp'].max()], hist_df])
    source_df = source_df.set_index(pd.to_datetime(source_df['timestamp'].apply(pd.to_numeric), unit='s')).sort_index()
    # Ensure time index is named for downstream reset_index -> 'ds'
    source_df.index.name = 'ds'

    # Generate forecasts for each feature
    forecast_data = {
        'watertemp': {'values': [], 'timestamps': [], 'confidence_intervals': {'5th': [], '95th': []}},
        'streamflow': {'values': [], 'timestamps': [], 'confidence_intervals': {'5th': [], '95th': []}}
    }
    
    weather_forecast = {
        'airtemp': [], 'precip': [], 'cloudcover': [], 'snow': [], 'snowdepth': [], 'timestamps': []
    }

    # Determine forecast start threshold (strictly after last historical timestamp)
    hist_max_dt = pd.to_datetime(int(hist_df['timestamp'].max()), unit='s') if not hist_df.empty else None

    for feature in constants.FEATURES_TO_FORECAST:
        if is_onboarding: 
            db_v2.push_site_onboarding_log(usgs_site, f'\tpredicting {feature} values')
        
        feature_fcst = forecast_feature(source_df, feature, usgs_site, is_onboarding)
        
        # Extract forecast data (rows strictly after the last historical timestamp)
        if hist_max_dt is not None:
            fcst_data = feature_fcst[feature_fcst.index > hist_max_dt]
        else:
            fcst_data = feature_fcst
        
        if len(fcst_data) == 0:
            log.error(f'No forecast data generated for feature {feature}')
            continue
        
        forecast_data[feature]['values'] = fcst_data[feature].tolist()
        forecast_data[feature]['timestamps'] = ((fcst_data.index.astype(np.int64) // 10**9).tolist())
        forecast_data[feature]['confidence_intervals']['5th'] = fcst_data[f'{feature}_5th'].tolist()
        forecast_data[feature]['confidence_intervals']['95th'] = fcst_data[f'{feature}_95th'].tolist()
        
        # Extract weather forecast data (only once)
        if feature == constants.FEATURES_TO_FORECAST[0]:
            weather_forecast['airtemp'] = fcst_data['airtemp'].tolist()
            weather_forecast['precip'] = fcst_data['precip'].tolist()
            weather_forecast['cloudcover'] = fcst_data['cloudcover'].tolist()
            weather_forecast['snow'] = fcst_data['snow'].tolist()
            weather_forecast['snowdepth'] = fcst_data['snowdepth'].tolist()
            weather_forecast['timestamps'] = ((fcst_data.index.astype(np.int64) // 10**9).tolist())

    # Store complete forecast
    complete_forecast = {
        'water_forecast': forecast_data,
        'atmospheric_forecast': weather_forecast
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
    # Build model frame with 'ds' time column and required regressors/target
    keep_cols = [c for c in constants.FEATURE_COLS[feature] if c in data.columns]
    df = data.reset_index()  # index name was set to 'ds'
    df = df[['ds', *keep_cols]].copy()

    # convert decimals to floats (consistent dtypes for regressors)
    df[constants.FEATURE_COLS[feature]] = df[constants.FEATURE_COLS[feature]].apply(pd.to_numeric, downcast='float')

    df = df.rename(columns={feature: 'y'})
    # todo - remove once neuralprophet issue is resolved
    if 'snow' in df.columns:
        df['snow'] = df['snow'].astype(np.float32)
        df.loc[0, 'snow'] = np.float32(0.01)
    if 'snowdepth' in df.columns:
        df['snowdepth'] = df['snowdepth'].astype(np.float32)
        df.loc[0, 'snowdepth'] = np.float32(0.01)
    # helpful diagnostics
    num_hist = int(df['y'].notnull().sum())
    num_future = int(df['y'].isnull().sum())
    log.info(f'dataset ready for inference (hist={num_hist}, future={num_future}):\n{df}')

    # load model
    model = s3.load_model(usgs_site, feature)

    # prep future
    # Split into strictly historical rows (non-null y) and future regressors (null y)
    train_df = df[df['y'].notnull()].copy().sort_values('ds')
    future_regs = df[df['y'].isnull()].drop(columns=['y']).copy().sort_values('ds')

    # Enforce expected horizon length on future regressors
    target_horizon = int(constants.FORECAST_HORIZON)
    if len(future_regs) > target_horizon:
        log.info(f"regressors_df longer than horizon (len={len(future_regs)}), truncating to {target_horizon}")
        future_regs = future_regs.iloc[:target_horizon]
    elif len(future_regs) < target_horizon:
        log.warning(f"regressors_df shorter than horizon (len={len(future_regs)}<{target_horizon}); predictions may be shorter")

    log.info(f"make_future_dataframe inputs: train_rows={len(train_df)}, future_reg_rows={len(future_regs)}, periods={target_horizon}")

    future = model.make_future_dataframe(
        df=train_df,
        regressors_df=future_regs,
        periods=target_horizon
    )

    # predict
    # hide py.warnings (noisy pandas warnings during training)
    logging.getLogger('py.warnings').setLevel(logging.ERROR)
    pred = model.predict(df=future)
    yhat = model.get_latest_forecast(pred)
    log.info(f"prediction frames: future.shape={future.shape}, pred.shape={pred.shape}, yhat.shape={yhat.shape}")

    yhat = yhat.set_index(yhat['ds'])
    utils.convert_floats_to_decimals(yhat)
    data[f'{feature}_5th'] = np.nan
    data[f'{feature}_95th'] = np.nan
    data[f'{feature}'] = data[f'{feature}'].combine_first(yhat['origin-0'])
    data[f'{feature}_5th'] = data[f'{feature}_5th'].combine_first(yhat['origin-0 5.0%'])
    data[f'{feature}_95th'] = data[f'{feature}_95th'].combine_first(yhat['origin-0 95.0%'])

    return data