# DynamoDB Schema Design for Flowcast (IMPLEMENTED)

## Table Structure

**Table Name**: `flowcast-data-v2`
- **Partition Key**: `usgs_site#type` (String)
- **Sort Key**: `timestamp` (Number)

## Understanding the Schema

You're right to question the redundancy. Here's what we actually have and what it should be:

### Current Schema Issues

The current implementation has `origin_timestamp` and `timestamp` both set to the same value for forecasts, which is redundant and confusing.

### Proposed Clean Schema

We have **three distinct data types** in one table:

#### 1. Historical Observations (`type: 'hist'`)
One entry per observation timestamp with actual measured values.

```json
{
  "usgs_site": "01427510",
  "type": "hist",
  "usgs_site#type": "01427510#hist",
  "timestamp": 1704085200,  // When this observation was taken
  
  // Observed water conditions
  "watertemp": 15.2,
  "streamflow": 245.3,
  "gage_height": 3.45,
  
  // Observed weather conditions
  "airtemp": 18.5,
  "precip": 0.0,
  "cloudcover": 25.0
}
```

#### 2. Weather Forecasts (`type: 'weather'`)
External weather API predictions, stored at the time we fetched them.

```json
{
  "usgs_site": "01427510",
  "type": "weather",
  "usgs_site#type": "01427510#weather",
  "timestamp": 1704085200,  // When this forecast starts (origin)
  
  // Metadata
  "fetched_at": 1704085000,  // When we got this from the API
  "horizon_hours": 168,
  
  // Weather predictions for next 168 hours
  "data": {
    "timestamps": [1704085200, 1704088800, ...],  // Hourly timestamps
    "airtemp": [18.5, 19.0, ...],
    "precip": [0.0, 0.1, ...],
    "cloudcover": [25.0, 30.0, ...],
    "snow": [0.0, 0.0, ...],
    "snowdepth": [0.0, 0.0, ...]
  }
}
```

#### 3. Water Forecasts (`type: 'forecast'`)
Our ML model predictions for water conditions.

```json
{
  "usgs_site": "01427510",
  "type": "forecast",
  "usgs_site#type": "01427510#forecast",
  "timestamp": 1704085200,  // When this forecast starts (origin)
  
  // Metadata
  "created_at": 1704085300,  // When we generated this forecast
  "horizon_hours": 168,
  
  // Water condition predictions for next 168 hours
  "data": {
    "watertemp": {
      "timestamps": [1704085200, 1704088800, ...],
      "values": [15.2, 15.4, ...],
      "confidence_5th": [14.8, 15.0, ...],
      "confidence_95th": [15.6, 15.8, ...]
    },
    "streamflow": {
      "timestamps": [1704085200, 1704088800, ...],
      "values": [245.3, 248.1, ...],
      "confidence_5th": [240.0, 242.0, ...],
      "confidence_95th": [250.0, 254.0, ...]
    }
  }
}
```

## Key Design Principles

### 1. No Redundant Fields
- Remove `origin_timestamp` - just use `timestamp` as the sort key
- For forecasts, `timestamp` IS the origin timestamp

### 2. Clear Separation of Concerns
- **Historical**: What actually happened
- **Weather**: External predictions we consume
- **Forecast**: Our predictions we produce

### 3. Consistent Field Names
- `timestamp`: Primary temporal key for all types
- `created_at` / `fetched_at`: When we stored/retrieved the data
- `data`: The actual payload (not `forecast_data` or `weather_data`)
- `horizon_hours`: How far the forecast extends

### 4. Query Patterns

```python
# Get actual observation at specific time
db.query(
    KeyCondition='01427510#hist' AND timestamp = 1704085200
)

# Get latest observation
db.query(
    KeyCondition='01427510#hist',
    ScanIndexForward=False,
    Limit=1
)

# Get forecast that was made at specific time
db.query(
    KeyCondition='01427510#forecast' AND timestamp = 1704085200
)

# Get weather data fetched at specific time
db.query(
    KeyCondition='01427510#weather' AND timestamp = 1704085200
)
```

## Benefits of This Design

1. **Single Source of Truth**: For any given timestamp, we know exactly what to query
2. **No Redundancy**: No duplicate fields with same values
3. **Clear Semantics**: `timestamp` means "when" for all types
4. **Efficient Queries**: Partition by site+type, sort by time
5. **Extensible**: Easy to add new forecast types or data sources

## Migration Path

To clean up the current implementation:

1. Remove `origin_timestamp` field from forecast/weather entries
2. Rename `{type}_data` to just `data` for consistency
3. Rename `{type}_created_at` to `created_at` or `fetched_at`
4. Update query functions to use consistent return patterns

## Performance Considerations

- **Storage**: ~20KB per forecast (168 hours of data)
- **Query Cost**: Single query to get any specific forecast
- **Scalability**: Partition key spreads load across sites and types
- **Retention**: Easy to implement TTL on old forecasts/weather data

This schema is maintainable, performant, and follows DynamoDB best practices.