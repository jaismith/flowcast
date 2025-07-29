import logging
import boto3
from boto3.dynamodb.conditions import Key
from typing import List

log = logging.getLogger(__name__)

def cleanup_old_forecast_entries(usgs_site: str, table_name: str = 'flowcast-data-v2'):
    """
    Delete all old forecast entries using the old schema (type: 'fcst')
    This should be called after migrating to the new forecast-centric format
    """
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    log.info(f'Starting cleanup of old forecast entries for site {usgs_site}')
    
    # Query all old forecast entries
    response = table.query(
        KeyConditionExpression=Key('usgs_site#type').eq(f'{usgs_site}#fcst')
    )
    
    old_entries = response['Items']
    log.info(f'Found {len(old_entries)} old forecast entries to delete')
    
    if len(old_entries) == 0:
        log.info('No old forecast entries found to delete')
        return 0
    
    # Delete them in batches
    deleted_count = 0
    with table.batch_writer() as batch:
        for entry in old_entries:
            try:
                batch.delete_item(
                    Key={
                        'usgs_site#type': entry['usgs_site#type'],
                        'timestamp': entry['timestamp']
                    }
                )
                deleted_count += 1
            except Exception as e:
                log.error(f'Failed to delete entry {entry.get("timestamp", "unknown")}: {e}')
    
    log.info(f'Successfully deleted {deleted_count} old forecast entries')
    return deleted_count

def cleanup_all_old_forecast_entries(table_name: str = 'flowcast-data-v2'):
    """
    Delete all old forecast entries across all sites
    """
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    log.info('Starting cleanup of all old forecast entries')
    
    # Scan for all old forecast entries
    response = table.scan(
        FilterExpression=Key('usgs_site#type').eq('fcst')
    )
    
    old_entries = response['Items']
    log.info(f'Found {len(old_entries)} old forecast entries to delete')
    
    if len(old_entries) == 0:
        log.info('No old forecast entries found to delete')
        return 0
    
    # Delete them in batches
    deleted_count = 0
    with table.batch_writer() as batch:
        for entry in old_entries:
            try:
                batch.delete_item(
                    Key={
                        'usgs_site#type': entry['usgs_site#type'],
                        'timestamp': entry['timestamp']
                    }
                )
                deleted_count += 1
            except Exception as e:
                log.error(f'Failed to delete entry {entry.get("timestamp", "unknown")}: {e}')
    
    log.info(f'Successfully deleted {deleted_count} old forecast entries')
    return deleted_count

def get_forecast_entry_counts(table_name: str = 'flowcast-data-v2'):
    """
    Get counts of old vs new forecast entries for comparison
    """
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    # Count old format entries
    old_response = table.scan(
        FilterExpression=Key('usgs_site#type').eq('fcst')
    )
    old_count = len(old_response['Items'])
    
    # Count new format entries
    new_response = table.scan(
        FilterExpression=Key('usgs_site#type').eq('forecast')
    )
    new_count = len(new_response['Items'])
    
    log.info(f'Forecast entry counts: Old format: {old_count}, New format: {new_count}')
    
    return {
        'old_format_count': old_count,
        'new_format_count': new_count,
        'total_count': old_count + new_count
    }