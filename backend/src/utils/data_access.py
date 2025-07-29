import pandas as pd
import numpy as np
from typing import Dict, List, Optional
import logging

log = logging.getLogger(__name__)

def get_forecast_as_dataframe(forecast_item: Dict) -> pd.DataFrame:
    """
    Convert forecast item to pandas DataFrame for analysis
    """
    forecast_data = forecast_item['forecast_data']
    weather_forecast = forecast_item['weather_forecast']
    
    # Create DataFrame from forecast data
    df_data = {}
    
    for feature in forecast_data:
        df_data[f'{feature}'] = forecast_data[feature]['values']
        df_data[f'{feature}_5th'] = forecast_data[feature]['confidence_intervals']['5th']
        df_data[f'{feature}_95th'] = forecast_data[feature]['confidence_intervals']['95th']
    
    # Add weather data
    for weather_var in weather_forecast:
        if weather_var != 'timestamps':
            df_data[weather_var] = weather_forecast[weather_var]
    
    # Create index from timestamps
    timestamps = pd.to_datetime(weather_forecast['timestamps'], unit='s')
    
    df = pd.DataFrame(df_data, index=timestamps)
    return df

def get_forecast_for_time_range(usgs_site: str, start_time: int, end_time: int, db_v2) -> Optional[pd.DataFrame]:
    """
    Get forecast data for a specific time range
    """
    # Find the most recent forecast that covers the time range
    latest_forecast = db_v2.get_latest_forecast(usgs_site)
    if not latest_forecast:
        return None
    
    forecast_df = get_forecast_as_dataframe(latest_forecast)
    
    # Filter to requested time range
    start_dt = pd.to_datetime(start_time, unit='s')
    end_dt = pd.to_datetime(end_time, unit='s')
    
    mask = (forecast_df.index >= start_dt) & (forecast_df.index <= end_dt)
    return forecast_df[mask]

def compare_forecasts(usgs_site: str, origin_timestamps: List[int], db_v2) -> Dict:
    """
    Compare multiple forecasts for analysis
    """
    forecasts = {}
    
    for origin_ts in origin_timestamps:
        forecast_item = db_v2.get_forecast_by_origin(usgs_site, origin_ts)
        if forecast_item:
            forecasts[origin_ts] = get_forecast_as_dataframe(forecast_item)
    
    return forecasts

def get_latest_forecast_summary(usgs_site: str, db_v2) -> Optional[Dict]:
    """
    Get a summary of the latest forecast
    """
    latest_forecast = db_v2.get_latest_forecast(usgs_site)
    if not latest_forecast:
        return None
    
    forecast_df = get_forecast_as_dataframe(latest_forecast)
    
    summary = {
        'origin_timestamp': latest_forecast['origin_timestamp'],
        'forecast_created_at': latest_forecast['forecast_created_at'],
        'forecast_horizon_hours': latest_forecast['forecast_horizon_hours'],
        'forecast_start': forecast_df.index.min().isoformat(),
        'forecast_end': forecast_df.index.max().isoformat(),
        'features': {}
    }
    
    # Add summary statistics for each feature
    for feature in ['watertemp', 'streamflow']:
        if feature in forecast_df.columns:
            summary['features'][feature] = {
                'mean': float(forecast_df[feature].mean()),
                'min': float(forecast_df[feature].min()),
                'max': float(forecast_df[feature].max()),
                'std': float(forecast_df[feature].std())
            }
    
    return summary

def validate_forecast_data(forecast_item: Dict) -> bool:
    """
    Validate that a forecast item has the expected structure
    """
    try:
        # Check required top-level keys
        required_keys = ['forecast_data', 'weather_forecast']
        for key in required_keys:
            if key not in forecast_item:
                log.error(f'Missing required key: {key}')
                return False
        
        # Check forecast_data structure
        forecast_data = forecast_item['forecast_data']
        for feature in ['watertemp', 'streamflow']:
            if feature not in forecast_data:
                log.error(f'Missing feature: {feature}')
                return False
            
            feature_data = forecast_data[feature]
            required_feature_keys = ['values', 'timestamps', 'confidence_intervals']
            for key in required_feature_keys:
                if key not in feature_data:
                    log.error(f'Missing feature key {key} for {feature}')
                    return False
            
            # Check confidence intervals
            ci = feature_data['confidence_intervals']
            if '5th' not in ci or '95th' not in ci:
                log.error(f'Missing confidence interval keys for {feature}')
                return False
        
        # Check weather_forecast structure
        weather_forecast = forecast_item['weather_forecast']
        required_weather_keys = ['airtemp', 'precip', 'cloudcover', 'snow', 'snowdepth', 'timestamps']
        for key in required_weather_keys:
            if key not in weather_forecast:
                log.error(f'Missing weather key: {key}')
                return False
        
        # Check that all arrays have the same length
        lengths = []
        for feature in ['watertemp', 'streamflow']:
            lengths.append(len(forecast_data[feature]['values']))
            lengths.append(len(forecast_data[feature]['timestamps']))
        
        for weather_var in ['airtemp', 'precip', 'cloudcover', 'snow', 'snowdepth']:
            lengths.append(len(weather_forecast[weather_var]))
        
        if len(set(lengths)) > 1:
            log.error(f'Inconsistent array lengths: {lengths}')
            return False
        
        return True
        
    except Exception as e:
        log.error(f'Error validating forecast data: {e}')
        return False