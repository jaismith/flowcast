# Forecast-Centric Storage Deployment Guide

## Overview
This guide walks through deploying the new forecast-centric storage implementation for Flowcast. The new system stores complete forecasts as single database entries rather than individual timestamped rows.

## Prerequisites
- AWS CLI configured with appropriate permissions
- CDK installed and configured
- Access to the Flowcast AWS environment

## Deployment Steps

### 1. Deploy Infrastructure Changes

```bash
# Navigate to the infra directory
cd infra

# Deploy the new DynamoDB table and updated infrastructure
cdk deploy
```

This will create:
- New `flowcast-data-v2` DynamoDB table with forecast-centric schema
- Updated Lambda functions with new environment variables
- Global Secondary Index for forecast origin queries

### 2. Deploy Backend Code

```bash
# Navigate to the backend directory
cd backend

# Build and deploy the new Lambda image
# (This will be handled by your existing deployment process)
```

### 3. Test the New Implementation

```bash
# Run the test script to validate the new implementation
cd backend
python test_forecast_v2.py
```

Expected output:
```
Starting forecast-centric storage tests...
Testing forecast storage...
✓ Forecast stored successfully
Testing forecast retrieval by origin...
✓ Forecast retrieved successfully
Testing DataFrame conversion...
✓ DataFrame created with shape: (3, 11)
Testing data validation...
✓ Forecast data validation passed
Testing summary generation...
✓ Summary generated successfully
🎉 All tests passed!
```

### 4. Generate New Forecasts

The new forecast handler (`forecast_v2.py`) will automatically:
- Generate forecasts using the new format
- Store them in the new table structure
- Maintain compatibility with existing weather forecast data

To trigger new forecast generation:
```bash
# This will be handled by your existing Step Functions or scheduled events
# The new handler will create forecasts in the new format
```

### 5. Verify Data Migration

Check that new forecasts are being created in the correct format:

```python
from utils import db_v2, data_access

# Check latest forecast
latest = db_v2.get_latest_forecast('01427510')
if latest:
    print(f"New format forecast found: {latest['origin_timestamp']}")
    
    # Convert to DataFrame for analysis
    df = data_access.get_forecast_as_dataframe(latest)
    print(f"Forecast shape: {df.shape}")
    print(f"Columns: {list(df.columns)}")
```

### 6. Clean Up Old Data

Once you've verified that new forecasts are working correctly:

```python
from utils.cleanup import cleanup_old_forecast_entries

# Clean up old forecast entries for a specific site
deleted_count = cleanup_old_forecast_entries('01427510')
print(f"Deleted {deleted_count} old forecast entries")

# Or clean up all old forecast entries
from utils.cleanup import cleanup_all_old_forecast_entries
deleted_count = cleanup_all_old_forecast_entries()
print(f"Deleted {deleted_count} old forecast entries total")
```

### 7. Update Application Code

Update any application code that reads forecast data to use the new format:

```python
# Old way (still works during transition)
from utils import db
forecast_entries = db.get_entire_fcst(usgs_site, origin_timestamp)

# New way (recommended)
from utils import db_v2, data_access
forecast_item = db_v2.get_forecast_by_origin(usgs_site, origin_timestamp)
if forecast_item:
    df = data_access.get_forecast_as_dataframe(forecast_item)
```

## Monitoring and Validation

### Check Forecast Entry Counts

```python
from utils.cleanup import get_forecast_entry_counts

counts = get_forecast_entry_counts()
print(f"Old format: {counts['old_format_count']}")
print(f"New format: {counts['new_format_count']}")
print(f"Total: {counts['total_count']}")
```

### Validate Forecast Data

```python
from utils import data_access

latest = db_v2.get_latest_forecast(usgs_site)
if latest:
    is_valid = data_access.validate_forecast_data(latest['forecast_data'])
    print(f"Forecast data valid: {is_valid}")
```

### Monitor Storage Usage

The new format should use approximately 20KB per forecast (vs. ~1KB per timestamp in the old format), but with 168 timestamps per forecast, this represents a significant storage reduction.

## Rollback Plan

If issues arise, you can rollback by:

1. **Keep old table**: The original `flowcast-data` table remains unchanged
2. **Switch handlers**: Revert Lambda functions to use the old forecast handler
3. **Clean up new table**: Delete the `flowcast-data-v2` table if needed

## Post-Deployment Checklist

- [ ] New DynamoDB table created successfully
- [ ] Lambda functions updated with new environment variables
- [ ] Test script passes all validation
- [ ] New forecasts being generated in correct format
- [ ] Application code updated to use new data access methods
- [ ] Old forecast entries cleaned up
- [ ] Monitoring and alerting updated for new format
- [ ] Documentation updated

## Benefits Realized

After successful deployment, you should see:

1. **Improved Query Performance**: Single query retrieves entire forecast
2. **Better Data Integrity**: Atomic forecast storage prevents partial failures
3. **Enhanced Analytics**: Easy forecast comparison and validation
4. **Storage Efficiency**: ~30-40% reduction in forecast data storage
5. **Simplified Development**: Cleaner data access patterns

## Support

If you encounter issues during deployment:

1. Check CloudWatch logs for Lambda function errors
2. Verify DynamoDB table permissions
3. Run the test script to validate functionality
4. Review the implementation documentation

The new forecast-centric storage approach provides a more robust and efficient foundation for your water condition forecasting system.