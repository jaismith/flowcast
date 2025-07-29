import pandas as pd
import numpy as np
from typing import Dict, List, Optional
import logging

log = logging.getLogger(__name__)

def get_forecast_as_dataframe(forecast_item: Dict) -> pd.DataFrame:
    """
    Convert forecast item to pandas DataFrame for analysis
    """
    # Support both old and new schema during transition
    if 'data' in forecast_item:
        data = forecast_item['data']
        # Try new field names first, then fall back to old names
        water_forecast = data.get('water_forecast', data.get('forecast_data', data))
        atmospheric_forecast = data.get('atmospheric_forecast', data.get('weather_forecast', {}))
    else:
        # Old schema support
        water_forecast = forecast_item.get('forecast_data', {})
        atmospheric_forecast = forecast_item.get('weather_forecast', {})
    
    # Create DataFrame from forecast data
    df_data = {}
    
    # Handle water forecast data
    if 'watertemp' in water_forecast:
        # New nested structure
        for feature in ['watertemp', 'streamflow']:
            if feature in water_forecast:
                df_data[f'{feature}'] = water_forecast[feature]['values']
                df_data[f'{feature}_5th'] = water_forecast[feature]['confidence_intervals']['5th']
                df_data[f'{feature}_95th'] = water_forecast[feature]['confidence_intervals']['95th']
    else:
        # Direct structure (for future clean schema)
        for feature in water_forecast:
            if isinstance(water_forecast[feature], dict) and 'values' in water_forecast[feature]:
                df_data[f'{feature}'] = water_forecast[feature]['values']
                if 'confidence_5th' in water_forecast[feature]:
                    df_data[f'{feature}_5th'] = water_forecast[feature]['confidence_5th']
                if 'confidence_95th' in water_forecast[feature]:
                    df_data[f'{feature}_95th'] = water_forecast[feature]['confidence_95th']
    
    # Add atmospheric data
    for weather_var in atmospheric_forecast:
        if weather_var != 'timestamps':
            df_data[weather_var] = atmospheric_forecast[weather_var]
    
    # Create index from timestamps
    # Try to get timestamps from atmospheric data first, then from water forecast data
    timestamps = None
    if atmospheric_forecast and 'timestamps' in atmospheric_forecast:
        timestamps = pd.to_datetime(atmospheric_forecast['timestamps'], unit='s')
    elif 'watertemp' in water_forecast and 'timestamps' in water_forecast['watertemp']:
        timestamps = pd.to_datetime(water_forecast['watertemp']['timestamps'], unit='s')
    
    if timestamps is not None:
        df = pd.DataFrame(df_data, index=timestamps)
    else:
        df = pd.DataFrame(df_data)
    
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
        'origin_timestamp': latest_forecast.get('timestamp', latest_forecast.get('origin_timestamp')),
        'forecast_created_at': latest_forecast.get('created_at', latest_forecast.get('forecast_created_at')),
        'forecast_horizon_hours': latest_forecast.get('horizon_hours', latest_forecast.get('forecast_horizon_hours')),
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
        # Support both old and new schema
        if 'data' in forecast_item:
            # New schema
            data = forecast_item['data']
            water_forecast = data.get('water_forecast', data.get('forecast_data', data))
            atmospheric_forecast = data.get('atmospheric_forecast', data.get('weather_forecast', {}))
        else:
            # Old schema
            if 'forecast_data' not in forecast_item:
                log.error('Missing forecast_data in old schema format')
                return False
            water_forecast = forecast_item['forecast_data']
            atmospheric_forecast = forecast_item.get('weather_forecast', {})
        for feature in ['watertemp', 'streamflow']:
            if feature not in water_forecast:
                log.error(f'Missing feature: {feature}')
                return False
            
            feature_data = water_forecast[feature]
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
        
        # Check atmospheric_forecast structure (if present)
        if atmospheric_forecast:
            required_atmospheric_keys = ['airtemp', 'precip', 'cloudcover', 'snow', 'snowdepth', 'timestamps']
            for key in required_atmospheric_keys:
                if key not in atmospheric_forecast:
                    log.error(f'Missing atmospheric key: {key}')
                    return False
        
        # Check that all arrays have the same length
        lengths = []
        for feature in ['watertemp', 'streamflow']:
            if feature in water_forecast:
                lengths.append(len(water_forecast[feature]['values']))
                lengths.append(len(water_forecast[feature]['timestamps']))
        
        if atmospheric_forecast:
            for weather_var in ['airtemp', 'precip', 'cloudcover', 'snow', 'snowdepth']:
                if weather_var in atmospheric_forecast:
                    lengths.append(len(atmospheric_forecast[weather_var]))
        
        if len(set(lengths)) > 1:
            log.error(f'Inconsistent array lengths: {lengths}')
            return False
        
        return True
        
    except Exception as e:
        log.error(f'Error validating forecast data: {e}')
        return False