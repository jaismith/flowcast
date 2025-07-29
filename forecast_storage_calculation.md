# Forecast Storage Size Calculation

## Current System Parameters

From the code analysis:
- **Forecast Horizon**: 24 * 7 = 168 hours (1 week)
- **Time Series Frequency**: 1 hour
- **Features Forecasted**: 2 (watertemp, streamflow)
- **Weather Variables**: 5 (airtemp, precip, cloudcover, snow, snowdepth)
- **Confidence Intervals**: 5th and 95th percentiles for each forecasted feature

## Data Structure Analysis

### Current Forecast Data Per Timestamp
Each forecast timestamp contains:
- **Water Conditions**: 2 features × 3 values each (main + 5th + 95th percentile) = 6 values
- **Weather Conditions**: 5 variables = 5 values
- **Total per timestamp**: 11 numeric values

### Proposed New Structure
```json
{
  "usgs_site": "01427510",                    // ~10 bytes
  "type": "forecast",                         // ~8 bytes  
  "usgs_site#type": "01427510#forecast",      // ~20 bytes
  "origin_timestamp": 1690948800,             // 8 bytes
  "timestamp": 1690948800,                    // 8 bytes
  "forecast_created_at": 1690948800,          // 8 bytes
  "forecast_horizon_hours": 168,              // 4 bytes
  "forecast_data": {
    "watertemp": {
      "values": [62.1, 62.3, ...],           // 168 × 8 bytes = 1,344 bytes
      "timestamps": [1690952400, ...],        // 168 × 8 bytes = 1,344 bytes
      "confidence_intervals": {
        "5th": [61.2, 61.4, ...],            // 168 × 8 bytes = 1,344 bytes
        "95th": [63.2, 63.4, ...]            // 168 × 8 bytes = 1,344 bytes
      }
    },
    "streamflow": {
      "values": [1850, 1840, ...],           // 168 × 8 bytes = 1,344 bytes
      "timestamps": [1690952400, ...],        // 168 × 8 bytes = 1,344 bytes
      "confidence_intervals": {
        "5th": [1800, 1790, ...],            // 168 × 8 bytes = 1,344 bytes
        "95th": [1900, 1890, ...]            // 168 × 8 bytes = 1,344 bytes
      }
    }
  },
  "weather_forecast": {
    "airtemp": [55.3, 54.3, ...],            // 168 × 8 bytes = 1,344 bytes
    "precip": [0.0, 0.0, ...],               // 168 × 8 bytes = 1,344 bytes
    "cloudcover": [0.0, 0.0, ...],           // 168 × 8 bytes = 1,344 bytes
    "snow": [0.0, 0.0, ...],                 // 168 × 8 bytes = 1,344 bytes
    "snowdepth": [0.0, 0.0, ...],            // 168 × 8 bytes = 1,344 bytes
    "timestamps": [1690952400, ...]           // 168 × 8 bytes = 1,344 bytes
  }
}
```

## Detailed Size Calculation

### Metadata Section
- `usgs_site`: ~10 bytes
- `type`: ~8 bytes
- `usgs_site#type`: ~20 bytes
- `origin_timestamp`: 8 bytes
- `timestamp`: 8 bytes
- `forecast_created_at`: 8 bytes
- `forecast_horizon_hours`: 4 bytes
- **Total Metadata**: ~66 bytes

### Forecast Data Section
For each of 2 features (watertemp, streamflow):
- `values`: 168 timestamps × 8 bytes = 1,344 bytes
- `timestamps`: 168 timestamps × 8 bytes = 1,344 bytes
- `confidence_intervals.5th`: 168 timestamps × 8 bytes = 1,344 bytes
- `confidence_intervals.95th`: 168 timestamps × 8 bytes = 1,344 bytes
- **Per feature**: 5,376 bytes
- **Total for 2 features**: 10,752 bytes

### Weather Forecast Section
For each of 5 weather variables:
- Variable values: 168 timestamps × 8 bytes = 1,344 bytes
- **Total for 5 weather variables**: 6,720 bytes
- Weather timestamps: 168 timestamps × 8 bytes = 1,344 bytes
- **Total Weather Section**: 8,064 bytes

### JSON Structure Overhead
- Object keys and structure: ~500 bytes (estimated)
- Array brackets and commas: ~200 bytes (estimated)
- **Total JSON Overhead**: ~700 bytes

## Total Size Calculation

```
Metadata:                   66 bytes
Forecast Data:          10,752 bytes
Weather Forecast:        8,064 bytes
JSON Overhead:             700 bytes
─────────────────────────────────────
TOTAL:                  19,582 bytes
```

## Analysis Results

### Current Forecast Size: ~19.6 KB
This is **well within** DynamoDB's 400KB limit, using only about **4.9%** of the available space.

### Safety Margin
- **Available**: 400 KB
- **Used**: 19.6 KB
- **Remaining**: 380.4 KB
- **Safety Margin**: 95.1%

## Future Scalability Analysis

### Extended Forecast Horizons
If you wanted to extend the forecast horizon:

| Horizon (hours) | Size (KB) | % of Limit |
|----------------|-----------|------------|
| 168 (1 week)   | 19.6      | 4.9%       |
| 336 (2 weeks)  | 39.2      | 9.8%       |
| 720 (1 month)  | 84.0      | 21.0%      |
| 1440 (2 months)| 168.0     | 42.0%      |
| 2160 (3 months)| 252.0     | 63.0%      |

### Additional Features
If you added more forecasted features:

| Additional Features | Size Increase | New Total (KB) | % of Limit |
|-------------------|---------------|----------------|------------|
| +1 feature        | +5.4 KB       | 25.0          | 6.3%       |
| +2 features       | +10.8 KB      | 30.4          | 7.6%       |
| +5 features       | +27.0 KB      | 46.6          | 11.7%      |
| +10 features      | +54.0 KB      | 73.6          | 18.4%      |

## Conclusion

**Your current forecast horizons will NOT exceed the 400KB limit.**

### Key Findings:
1. **Current usage**: Only 4.9% of DynamoDB's item size limit
2. **Extensive headroom**: 380KB remaining for future expansion
3. **Scalability**: Could extend to 3-month forecasts or add 10+ features before hitting limits
4. **No S3 fallback needed**: The forecast-centric approach can be implemented entirely within DynamoDB

### Recommendations:
1. **Proceed with DynamoDB-only implementation** - no need for S3 complexity
2. **Future-proof design**: The structure can easily accommodate longer horizons or more features
3. **Monitor growth**: Track actual item sizes as you add features to ensure you stay well under limits

The forecast-centric storage approach is not only cleaner but also very efficient in terms of storage utilization.