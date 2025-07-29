#!/usr/bin/env python3
"""
Test script for the new forecast-centric storage implementation
"""

import sys
import os
sys.path.insert(0, 'src')

import logging
from datetime import datetime
import pandas as pd

from utils import db_v2, data_access, constants

# Set up logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def test_forecast_storage_and_retrieval():
    """Test storing and retrieving a complete forecast"""
    usgs_site = constants.USGS_SITE
    origin_timestamp = int(datetime.now().timestamp())
    
    # Create mock forecast data
    forecast_data = {
        'forecast_data': {
            'watertemp': {
                'values': [62.1, 62.3, 62.5],
                'timestamps': [origin_timestamp + 3600, origin_timestamp + 7200, origin_timestamp + 10800],
                'confidence_intervals': {
                    '5th': [61.2, 61.4, 61.6],
                    '95th': [63.2, 63.4, 63.6]
                }
            },
            'streamflow': {
                'values': [1850, 1840, 1830],
                'timestamps': [origin_timestamp + 3600, origin_timestamp + 7200, origin_timestamp + 10800],
                'confidence_intervals': {
                    '5th': [1800, 1790, 1780],
                    '95th': [1900, 1890, 1880]
                }
            }
        },
        'weather_forecast': {
            'airtemp': [55.3, 54.3, 53.4],
            'precip': [0.0, 0.0, 0.0],
            'cloudcover': [0.0, 0.0, 0.0],
            'snow': [0.0, 0.0, 0.0],
            'snowdepth': [0.0, 0.0, 0.0],
            'timestamps': [origin_timestamp + 3600, origin_timestamp + 7200, origin_timestamp + 10800]
        }
    }
    
    try:
        # Test storage
        log.info('Testing forecast storage...')
        db_v2.push_forecast_entry(usgs_site, origin_timestamp, forecast_data)
        log.info('✓ Forecast stored successfully')
        
        # Test retrieval by origin
        log.info('Testing forecast retrieval by origin...')
        retrieved = db_v2.get_forecast_by_origin(usgs_site, origin_timestamp)
        if retrieved:
            log.info('✓ Forecast retrieved successfully')
            log.info(f'  Origin timestamp: {retrieved["origin_timestamp"]}')
            log.info(f'  Forecast horizon: {retrieved["forecast_horizon_hours"]} hours')
        else:
            log.error('✗ Failed to retrieve forecast')
            return False
        
        # Test DataFrame conversion
        log.info('Testing DataFrame conversion...')
        df = data_access.get_forecast_as_dataframe(retrieved)
        log.info(f'✓ DataFrame created with shape: {df.shape}')
        log.info(f'  Columns: {list(df.columns)}')
        log.info(f'  Index range: {df.index.min()} to {df.index.max()}')
        
        # Test validation
        log.info('Testing data validation...')
        is_valid = data_access.validate_forecast_data(retrieved['forecast_data'])
        if is_valid:
            log.info('✓ Forecast data validation passed')
        else:
            log.error('✗ Forecast data validation failed')
            return False
        
        # Test summary generation
        log.info('Testing summary generation...')
        summary = data_access.get_latest_forecast_summary(usgs_site, db_v2)
        if summary:
            log.info('✓ Summary generated successfully')
            log.info(f'  Forecast start: {summary["forecast_start"]}')
            log.info(f'  Forecast end: {summary["forecast_end"]}')
            log.info(f'  Features: {list(summary["features"].keys())}')
        else:
            log.error('✗ Failed to generate summary')
            return False
        
        log.info('🎉 All tests passed!')
        return True
        
    except Exception as e:
        log.error(f'✗ Test failed with error: {e}')
        return False

def test_cleanup_utilities():
    """Test cleanup utilities"""
    usgs_site = constants.USGS_SITE
    
    try:
        log.info('Testing cleanup utilities...')
        
        # Get entry counts
        from utils.cleanup import get_forecast_entry_counts
        counts = get_forecast_entry_counts()
        log.info(f'Current entry counts: {counts}')
        
        log.info('✓ Cleanup utilities working')
        return True
        
    except Exception as e:
        log.error(f'✗ Cleanup test failed: {e}')
        return False

def main():
    """Run all tests"""
    log.info('Starting forecast-centric storage tests...')
    
    # Test 1: Storage and retrieval
    test1_passed = test_forecast_storage_and_retrieval()
    
    # Test 2: Cleanup utilities
    test2_passed = test_cleanup_utilities()
    
    # Summary
    log.info('\n' + '='*50)
    log.info('TEST SUMMARY')
    log.info('='*50)
    log.info(f'Storage and Retrieval: {"✓ PASSED" if test1_passed else "✗ FAILED"}')
    log.info(f'Cleanup Utilities: {"✓ PASSED" if test2_passed else "✗ FAILED"}')
    
    if test1_passed and test2_passed:
        log.info('\n🎉 All tests passed! The forecast-centric implementation is working correctly.')
        return 0
    else:
        log.error('\n❌ Some tests failed. Please check the implementation.')
        return 1

if __name__ == '__main__':
    exit(main())