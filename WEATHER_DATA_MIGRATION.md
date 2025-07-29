# Weather Data Migration Guide

## Overview

This guide explains the changes made to separate weather forecast data from water forecast data, allowing for safe cleanup of old forecast entries without losing critical weather data.

## Problem Statement

Previously, the `forecast_v2` handler depended on old 'fcst' entries to get weather data (airtemp, precip, cloudcover, snow, snowdepth). These entries were mixed with water forecast data, making it impossible to clean up old forecasts without breaking the system.

## Solution

We've implemented a separate storage mechanism for weather data:
- Weather data is now stored with type `'weather'` instead of `'fcst'`
- The `update` handler stores weather data in both formats during transition
- The `forecast_v2` handler can read from either format

## Changes Made

### 1. New Functions in `db_v2.py`

```python
# Store weather data separately
push_weather_data(usgs_site, origin_timestamp, weather_entries)

# Retrieve weather data
get_weather_data(usgs_site, origin_timestamp)
```

### 2. Updated `update.py` Handler

Now stores weather data in both formats:
- Old format: `db.push_fcst_entries(fcst_rows)` - for backward compatibility
- New format: `db_v2.push_weather_data(...)` - for forecast_v2

### 3. Updated `forecast_v2.py` Handler

Reads weather data from new format first, falls back to old format:
```python
# Try new format first
weather_data = db_v2.get_weather_data(usgs_site, last_hist_origin)

# Fall back to old format if needed
if not weather_data:
    last_fcst_entries = db_v2.get_entire_fcst(usgs_site, last_hist_origin)
```

## Deployment Steps

### Phase 1: Deploy Infrastructure (Required First)

```bash
cd infra
cdk deploy
```

This creates the new DynamoDB table with proper schema.

### Phase 2: Deploy Updated Code

Deploy the updated Lambda functions with the new handlers.

### Phase 3: Populate Weather Data

The `update` handler will automatically start storing weather data in the new format. 
Wait for at least one update cycle to complete for each site.

### Phase 4: Verify Weather Data

Check that weather data is being stored correctly:

```python
import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('flowcast-data-v2')

# Check for weather entries
response = table.query(
    KeyConditionExpression=Key('usgs_site#type').eq('01427510#weather')
)
print(f"Found {len(response['Items'])} weather entries")
```

### Phase 5: Clean Up Old Forecasts

Once weather data is confirmed in the new format, you can safely clean up old 'fcst' entries:

```python
from utils.cleanup import cleanup_old_forecast_entries

# Clean up for a specific site
deleted = cleanup_old_forecast_entries('01427510')
print(f"Deleted {deleted} old forecast entries")
```

## Data Structure

### Old Format ('fcst' entries)
- Multiple rows per forecast (one per timestamp)
- Mixed weather and water data
- Type: 'fcst'

### New Weather Format ('weather' entries)
- Single entry per origin timestamp
- Contains only weather data
- Type: 'weather'
- Follows same schema pattern as water forecasts
- Structure:
  ```json
  {
    "usgs_site": "01427510",
    "type": "weather",
    "usgs_site#type": "01427510#weather",
    "origin_timestamp": 1704085200,
    "timestamp": 1704085200,
    "weather_created_at": 1704089000,
    "weather_horizon_hours": 168,
    "weather_data": {
      "timestamps": [...],
      "airtemp": [...],
      "precip": [...],
      "cloudcover": [...],
      "snow": [...],
      "snowdepth": [...]
    }
  }
  ```

### New Forecast Format ('forecast' entries)
- Single entry per forecast
- Contains only water forecast data
- Type: 'forecast'

## Rollback Plan

If issues occur:

1. The old 'fcst' entries remain untouched until explicitly cleaned
2. The `forecast_v2` handler falls back to old format automatically
3. Simply revert the Lambda code if needed

## Benefits

1. **Clean Separation**: Weather data (from external APIs) is separate from water forecasts (our predictions)
2. **Safe Cleanup**: Can delete old water forecasts without losing weather data
3. **Better Performance**: Fewer database entries to query
4. **Future Flexibility**: Can optimize weather data storage independently

## Testing

Run the test script after deployment:

```bash
cd backend
poetry run python test_weather_storage.py
```

Note: The test will fail if the DynamoDB table doesn't exist yet. This is expected before infrastructure deployment.

## Timeline

1. **Immediate**: Deploy infrastructure and code
2. **After 1 update cycle**: Weather data will be in new format
3. **After verification**: Clean up old 'fcst' entries
4. **Long term**: Remove backward compatibility code

## Important Notes

- Weather data comes from external APIs (Visual Crossing, etc.) during the `update` handler
- This data is required for water forecasting models
- The separation allows us to manage these different data types appropriately
- Old 'fcst' entries can be safely deleted once weather data is confirmed in new format