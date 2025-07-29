# Forecast-Centric Storage Implementation Plan

## Overview
This document provides a detailed implementation plan for transitioning Flowcast from the current time-series storage to a forecast-centric approach. With current forecast sizes of only ~20KB (4.9% of DynamoDB's 400KB limit), this implementation can be entirely within DynamoDB without requiring S3 complexity.

## Phase 1: Core Implementation

### 1.1 Database Schema Updates

#### New DynamoDB Table Structure
```typescript
// infra/lib/flowcast.ts - Add new table
const forecastDataV2Table = new ddb.Table(this, 'flowcast-data-v2', {
  tableName: 'flowcast-data-v2',
  billingMode: ddb.BillingMode.PAY_PER_REQUEST,
  partitionKey: { name: 'usgs_site#type', type: ddb.AttributeType.STRING },
  sortKey: { name: 'timestamp', type: ddb.AttributeType.NUMBER },
  removalPolicy: cdk.RemovalPolicy.RETAIN,
  pointInTimeRecovery: true
});

// Add GSI for forecast origin queries
forecastDataV2Table.addGlobalSecondaryIndex({
  indexName: 'forecast_origin_index',
  partitionKey: { name: 'usgs_site#type', type: ddb.AttributeType.STRING },
  sortKey: { name: 'origin_timestamp', type: ddb.AttributeType.NUMBER }
});
```

### 1.2 Updated Database Utilities

#### New Forecast Storage Functions
```python
# backend/src/utils/db_v2.py
import json
from datetime import datetime
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb')
data_table_v2 = dynamodb.Table('flowcast-data-v2')

def push_forecast_entry(usgs_site: str, origin_timestamp: int, forecast_data: dict):
    """
    Store a complete forecast as a single database entry
    """
    item = {
        'usgs_site': usgs_site,
        'type': 'forecast',
        'usgs_site#type': f'{usgs_site}#forecast',
        'origin_timestamp': origin_timestamp,
        'timestamp': origin_timestamp,  # For sort key compatibility
        'forecast_created_at': int(datetime.now().timestamp()),
        'forecast_horizon_hours': len(forecast_data['watertemp']['values']),
        'forecast_data': forecast_data
    }
    
    data_table_v2.put_item(Item=item)

def get_latest_forecast(usgs_site: str):
    """
    Retrieve the most recent complete forecast
    """
    response = data_table_v2.query(
        KeyConditionExpression=Key('usgs_site#type').eq(f'{usgs_site}#forecast'),
        ScanIndexForward=False,
        Limit=1
    )
    
    if response['Items']:
        return response['Items'][0]
    return None

def get_forecast_by_origin(usgs_site: str, origin_timestamp: int):
    """
    Retrieve a specific forecast by origin timestamp
    """
    response = data_table_v2.query(
        IndexName='forecast_origin_index',
        KeyConditionExpression=Key('usgs_site#type').eq(f'{usgs_site}#forecast') & 
                              Key('origin_timestamp').eq(origin_timestamp)
    )
    
    if response['Items']:
        return response['Items'][0]
    return None

def get_forecasts_in_range(usgs_site: str, start_origin: int, end_origin: int):
    """
    Retrieve all forecasts within a date range
    """
    response = data_table_v2.query(
        IndexName='forecast_origin_index',
        KeyConditionExpression=Key('usgs_site#type').eq(f'{usgs_site}#forecast') & 
                              Key('origin_timestamp').between(start_origin, end_origin)
    )
    
    return response['Items']
```

### 1.3 Updated Forecast Generation

#### Modified Forecast Handler
```python
# backend/src/handlers/forecast_v2.py
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
        db_v2.push_site_onboarding_log(usgs_site, f'🔮 Started forecasting for site {usgs_site}')

    # Get latest historical data
    last_hist_entries = db_v2.get_n_most_recent_hist_entries(usgs_site, constants.FORECAST_HORIZON*2)
    last_hist_origin = last_hist_entries[0]['timestamp']
    
    # Get weather forecast data
    last_fcst_entries = db_v2.get_entire_fcst(usgs_site, last_hist_origin)
    
    # Check if forecast already exists
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
        feature_fcst = forecast_feature(source_df, feature, usgs_site, is_onboarding)
        
        # Extract forecast data
        fcst_mask = feature_fcst['type'] == 'fcst'
        fcst_data = feature_fcst[fcst_mask]
        
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
    db_v2.push_forecast_entry(usgs_site, last_hist_origin, complete_forecast)
    
    if is_onboarding:
        db_v2.push_site_onboarding_log(usgs_site, f'\tFinished forecasting at {utils.get_current_local_time()}')
        db_v2.update_site_status(usgs_site, db_v2.SiteStatus.ACTIVE)

    return { 'statusCode': 200 }

def forecast_feature(data: pd.DataFrame, feature: str, usgs_site: str, is_onboarding: bool):
    # Existing forecast_feature implementation remains the same
    # ... (keep existing logic)
    pass
```

### 1.4 Data Access Layer Updates

#### New Data Retrieval Functions
```python
# backend/src/utils/data_access.py
import pandas as pd
import numpy as np
from typing import Dict, List, Optional

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

def get_forecast_for_time_range(usgs_site: str, start_time: int, end_time: int) -> Optional[pd.DataFrame]:
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

def compare_forecasts(usgs_site: str, origin_timestamps: List[int]) -> Dict:
    """
    Compare multiple forecasts for analysis
    """
    forecasts = {}
    
    for origin_ts in origin_timestamps:
        forecast_item = db_v2.get_forecast_by_origin(usgs_site, origin_ts)
        if forecast_item:
            forecasts[origin_ts] = get_forecast_as_dataframe(forecast_item)
    
    return forecasts
```

## Phase 2: Migration Utilities

### 2.1 Data Migration Script
```python
# backend/src/utils/migration.py
import logging
from typing import List, Dict
import pandas as pd

log = logging.getLogger(__name__)

def migrate_forecast_data(usgs_site: str, start_date: str, end_date: str):
    """
    Migrate existing forecast data to new format
    """
    log.info(f'Starting migration for site {usgs_site} from {start_date} to {end_date}')
    
    # Get existing forecast data
    existing_forecasts = db.get_fcsts_with_horizon_after(usgs_site, 0, start_date)
    
    # Group by origin timestamp
    forecasts_by_origin = {}
    for fcst in existing_forecasts:
        origin = fcst['origin']
        if origin not in forecasts_by_origin:
            forecasts_by_origin[origin] = []
        forecasts_by_origin[origin].append(fcst)
    
    # Convert each group to new format
    migrated_count = 0
    for origin, fcst_entries in forecasts_by_origin.items():
        try:
            new_forecast = convert_forecast_group_to_new_format(fcst_entries)
            db_v2.push_forecast_entry(usgs_site, origin, new_forecast)
            migrated_count += 1
            log.info(f'Migrated forecast for origin {origin}')
        except Exception as e:
            log.error(f'Failed to migrate forecast for origin {origin}: {e}')
    
    log.info(f'Migration complete. Migrated {migrated_count} forecasts.')
    return migrated_count

def convert_forecast_group_to_new_format(fcst_entries: List[Dict]) -> Dict:
    """
    Convert a group of forecast entries to the new format
    """
    # Convert to DataFrame
    df = pd.DataFrame(fcst_entries)
    df = df.set_index(pd.to_datetime(df['timestamp'].apply(pd.to_numeric), unit='s')).sort_index()
    
    # Extract forecast data
    forecast_data = {
        'watertemp': {'values': [], 'timestamps': [], 'confidence_intervals': {'5th': [], '95th': []}},
        'streamflow': {'values': [], 'timestamps': [], 'confidence_intervals': {'5th': [], '95th': []}}
    }
    
    weather_forecast = {
        'airtemp': [], 'precip': [], 'cloudcover': [], 'snow': [], 'snowdepth': [], 'timestamps': []
    }
    
    # Populate forecast data
    for feature in ['watertemp', 'streamflow']:
        if feature in df.columns:
            forecast_data[feature]['values'] = df[feature].tolist()
            forecast_data[feature]['timestamps'] = df.index.astype(np.int64) // 10**9
            
            if f'{feature}_5th' in df.columns:
                forecast_data[feature]['confidence_intervals']['5th'] = df[f'{feature}_5th'].tolist()
            if f'{feature}_95th' in df.columns:
                forecast_data[feature]['confidence_intervals']['95th'] = df[f'{feature}_95th'].tolist()
    
    # Populate weather forecast
    for weather_var in ['airtemp', 'precip', 'cloudcover', 'snow', 'snowdepth']:
        if weather_var in df.columns:
            weather_forecast[weather_var] = df[weather_var].tolist()
    
    weather_forecast['timestamps'] = df.index.astype(np.int64) // 10**9
    
    return {
        'forecast_data': forecast_data,
        'weather_forecast': weather_forecast
    }
```

## Phase 3: Testing and Validation

### 3.1 Unit Tests
```python
# backend/tests/test_forecast_storage.py
import pytest
import pandas as pd
from unittest.mock import Mock, patch
from utils import db_v2, data_access

def test_forecast_storage_and_retrieval():
    """Test storing and retrieving a complete forecast"""
    usgs_site = "01427510"
    origin_timestamp = 1690948800
    
    # Mock forecast data
    forecast_data = {
        'forecast_data': {
            'watertemp': {
                'values': [62.1, 62.3, 62.5],
                'timestamps': [1690952400, 1690956000, 1690959600],
                'confidence_intervals': {
                    '5th': [61.2, 61.4, 61.6],
                    '95th': [63.2, 63.4, 63.6]
                }
            }
        },
        'weather_forecast': {
            'airtemp': [55.3, 54.3, 53.4],
            'precip': [0.0, 0.0, 0.0],
            'cloudcover': [0.0, 0.0, 0.0],
            'snow': [0.0, 0.0, 0.0],
            'snowdepth': [0.0, 0.0, 0.0],
            'timestamps': [1690952400, 1690956000, 1690959600]
        }
    }
    
    with patch('utils.db_v2.data_table_v2') as mock_table:
        # Test storage
        db_v2.push_forecast_entry(usgs_site, origin_timestamp, forecast_data)
        mock_table.put_item.assert_called_once()
        
        # Test retrieval
        mock_table.query.return_value = {'Items': [{'origin_timestamp': origin_timestamp, 'forecast_data': forecast_data}]}
        retrieved = db_v2.get_forecast_by_origin(usgs_site, origin_timestamp)
        assert retrieved['origin_timestamp'] == origin_timestamp
        assert retrieved['forecast_data'] == forecast_data

def test_dataframe_conversion():
    """Test converting forecast data to DataFrame"""
    forecast_item = {
        'forecast_data': {
            'watertemp': {
                'values': [62.1, 62.3],
                'timestamps': [1690952400, 1690956000],
                'confidence_intervals': {
                    '5th': [61.2, 61.4],
                    '95th': [63.2, 63.4]
                }
            }
        },
        'weather_forecast': {
            'airtemp': [55.3, 54.3],
            'precip': [0.0, 0.0],
            'cloudcover': [0.0, 0.0],
            'snow': [0.0, 0.0],
            'snowdepth': [0.0, 0.0],
            'timestamps': [1690952400, 1690956000]
        }
    }
    
    df = data_access.get_forecast_as_dataframe(forecast_item)
    assert len(df) == 2
    assert 'watertemp' in df.columns
    assert 'airtemp' in df.columns
    assert df['watertemp'].iloc[0] == 62.1
```

## Deployment Checklist

### Pre-Deployment
- [ ] Create new DynamoDB table with proper indexes
- [ ] Implement new database utilities
- [ ] Update forecast handler with new storage logic
- [ ] Add data access layer functions
- [ ] Write comprehensive unit tests
- [ ] Create migration utilities

### Deployment
- [ ] Deploy new table and code to staging
- [ ] Run migration on staging data
- [ ] Validate data integrity
- [ ] Deploy to production
- [ ] Run migration on production data
- [ ] Switch traffic to new format

### Post-Deployment
- [ ] Monitor performance metrics
- [ ] Validate forecast accuracy
- [ ] Clean up old table and code
- [ ] Update documentation

## Risk Mitigation

1. **Data Loss**: Implement dual-write during transition period
2. **Performance**: Monitor DynamoDB read/write capacity
3. **Size Limits**: Monitor item sizes as features are added (currently only 4.9% of limit)
4. **Rollback Plan**: Keep old code and table until validation complete

## Success Metrics

1. **Storage Efficiency**: 30-40% reduction in forecast data storage
2. **Query Performance**: 50% reduction in database round trips
3. **Data Integrity**: Zero orphaned forecast entries
4. **Development Velocity**: Simplified forecast comparison and analysis
5. **Scalability**: Maintain under 20% of DynamoDB item size limit for future growth