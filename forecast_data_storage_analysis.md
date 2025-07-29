# Flowcast Data Storage Analysis: Current vs. Forecast-Centric Approach

## Executive Summary

This analysis examines the current time-series data storage format in Flowcast and evaluates the potential benefits of transitioning to a forecast-centric storage model. The current system stores individual timestamped observations, while the proposed approach would store complete forecasts as single database entries with origin timestamps.

## Current Data Storage Structure

### Database Schema
- **Table**: `flowcast-data` (DynamoDB)
- **Partition Key**: `usgs_site#type` (e.g., "01427510#hist", "01427510#fcst")
- **Sort Key**: `origin#timestamp` (e.g., "1690948800#1690948800")

### Current Data Types
1. **Historical Data (`type: 'hist'`)**:
   - One row per timestamp
   - Contains actual observations: `watertemp`, `streamflow`, `airtemp`, `precip`, `cloudcover`, `snow`, `snowdepth`
   - `origin#timestamp` = `timestamp` (same value)

2. **Forecast Data (`type: 'fcst'`)**:
   - One row per forecasted timestamp
   - Contains predicted values: `watertemp`, `streamflow`, `watertemp_5th`, `watertemp_95th`, etc.
   - `origin` = timestamp when forecast was generated
   - `timestamp` = the actual time being forecasted
   - `horizon` = `timestamp - origin` (hours into the future)

### Current Data Flow
1. **Update Process**: Fetches new historical data and weather forecasts, stores as individual rows
2. **Forecast Process**: 
   - Retrieves recent historical data and weather forecasts
   - Runs NeuralProphet models to predict water conditions
   - Stores each forecasted timestamp as a separate row
   - All forecasts from same origin time share the same `origin` value

### Current Query Patterns
- Historical data: Query by `usgs_site#type` = "site#hist"
- Forecast data: Query by `usgs_site#type` = "site#fcst" and `origin#timestamp` prefix
- Latest data: Get most recent entries for both types

## Proposed Forecast-Centric Storage Model

### New Schema Design
- **Table**: `flowcast-data-v2` (DynamoDB)
- **Partition Key**: `usgs_site#type` (e.g., "01427510#hist", "01427510#forecast")
- **Sort Key**: `timestamp` (for hist) or `origin_timestamp` (for forecasts)

### New Data Types
1. **Historical Data (`type: 'hist'`)**:
   - Unchanged: One row per timestamp
   - Same structure as current

2. **Forecast Data (`type: 'forecast'`)**:
   - **One row per complete forecast**
   - Contains entire forecast series as nested data
   - Structure:
     ```json
     {
       "usgs_site": "01427510",
       "type": "forecast",
       "usgs_site#type": "01427510#forecast",
       "origin_timestamp": 1690948800,
       "forecast_created_at": 1690948800,
       "forecast_horizon_hours": 168,
       "forecast_data": {
         "watertemp": {
           "values": [62.1, 62.3, 62.5, ...],
           "timestamps": [1690952400, 1690956000, ...],
           "confidence_intervals": {
             "5th": [61.2, 61.4, ...],
             "95th": [63.2, 63.4, ...]
           }
         },
         "streamflow": {
           "values": [1850, 1840, 1830, ...],
           "timestamps": [1690952400, 1690956000, ...],
           "confidence_intervals": {
             "5th": [1800, 1790, ...],
             "95th": [1900, 1890, ...]
           }
         }
       },
       "weather_forecast": {
         "airtemp": [55.3, 54.3, ...],
         "precip": [0.0, 0.0, ...],
         "cloudcover": [0.0, 0.0, ...],
         "snow": [0.0, 0.0, ...],
         "snowdepth": [0.0, 0.0, ...],
         "timestamps": [1690952400, 1690956000, ...]
       }
     }
     ```

## Analysis: Current vs. Proposed Approach

### Advantages of Forecast-Centric Storage

#### 1. **Data Integrity & Atomicity**
- **Current**: Forecasts are stored incrementally, risk of partial failures
- **Proposed**: Complete forecasts stored atomically, ensuring data consistency
- **Benefit**: Eliminates orphaned or incomplete forecast data

#### 2. **Query Efficiency**
- **Current**: Must query multiple rows to reconstruct a complete forecast
- **Proposed**: Single query retrieves entire forecast
- **Benefit**: Reduced database round trips, faster data retrieval

#### 3. **Storage Efficiency**
- **Current**: Redundant metadata per row (usgs_site, type, origin, etc.)
- **Proposed**: Metadata stored once per forecast
- **Benefit**: ~30-40% storage reduction for forecast data

#### 4. **Forecast Versioning & Comparison**
- **Current**: Difficult to compare forecasts from different origin times
- **Proposed**: Natural grouping by origin time enables easy comparison
- **Benefit**: Better forecast accuracy analysis and model validation

#### 5. **Data Access Patterns**
- **Current**: Complex queries to get forecast for specific time ranges
- **Proposed**: Direct access to complete forecast series
- **Benefit**: Simplified application logic, better performance

#### 6. **Audit Trail**
- **Current**: Hard to track when forecasts were generated
- **Proposed**: Clear `forecast_created_at` timestamp
- **Benefit**: Better debugging and compliance

### Disadvantages of Forecast-Centric Storage

#### 1. **Partial Updates**
- **Current**: Can update individual forecast points
- **Proposed**: Must replace entire forecast
- **Impact**: Less granular update capability

#### 2. **Query Flexibility**
- **Current**: Can query specific time ranges easily
- **Proposed**: May require additional processing to extract time ranges
- **Mitigation**: Secondary indexes or application-level filtering

#### 3. **Migration Complexity**
- **Challenge**: Converting existing data structure
- **Risk**: Potential data loss or downtime
- **Mitigation**: Gradual migration with dual-write period

## Recommended Implementation Plan

### Phase 1: Hybrid Approach (Recommended)
1. **Create new table structure** alongside existing
2. **Implement dual-write** for new forecasts
3. **Migrate existing data** gradually
4. **Update application logic** to use new format
5. **Remove old table** after validation

### Phase 2: Enhanced Features
1. **Forecast comparison tools**
2. **Version control for forecasts**
3. **Advanced analytics capabilities**



## Implementation Details

### Database Schema Changes
```typescript
// New DynamoDB table structure
const forecastTable = new ddb.Table(this, 'flowcast-data-v2', {
  tableName: 'flowcast-data-v2',
  billingMode: ddb.BillingMode.PAY_PER_REQUEST,
  partitionKey: { name: 'usgs_site#type', type: ddb.AttributeType.STRING },
  sortKey: { name: 'timestamp', type: ddb.AttributeType.NUMBER },
  removalPolicy: cdk.RemovalPolicy.RETAIN,
  pointInTimeRecovery: true
});

// Add GSI for forecast queries
forecastTable.addGlobalSecondaryIndex({
  indexName: 'forecast_origin_index',
  partitionKey: { name: 'usgs_site#type', type: ddb.AttributeType.STRING },
  sortKey: { name: 'origin_timestamp', type: ddb.AttributeType.NUMBER }
});
```

### Code Changes Required
1. **Update `generate_fcst_rows()`** in `utils.py`
2. **Modify `push_fcst_entries()`** in `db.py`
3. **Update forecast retrieval logic** in handlers
4. **Add migration utilities** for existing data

### Migration Strategy
1. **Week 1-2**: Implement new storage format
2. **Week 3-4**: Dual-write period for validation
3. **Week 5-6**: Migrate historical forecast data
4. **Week 7**: Switch to new format exclusively
5. **Week 8**: Clean up old table and code

## Conclusion

The forecast-centric storage approach offers significant advantages in data integrity, query efficiency, and storage optimization. With current forecast sizes of only ~20KB (4.9% of DynamoDB's 400KB limit), the implementation can be entirely within DynamoDB without requiring S3 complexity. The recommended hybrid implementation minimizes risk while providing clear migration path. The benefits outweigh the implementation complexity, particularly for a system focused on forecast generation and analysis.

**Recommendation**: Proceed with Phase 1 implementation, starting with a proof-of-concept for a single site to validate the approach before full deployment. The extensive headroom (380KB remaining) provides significant future scalability for longer forecast horizons or additional features.