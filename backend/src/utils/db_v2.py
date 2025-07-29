import os
import json
from datetime import datetime
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key, Attr
from enum import Enum

from utils import usgs

# * ddb v2 - forecast-centric storage

print('initializing ddb v2 client')

dynamodb = boto3.resource('dynamodb')
data_table_v2 = dynamodb.Table('flowcast-data-v2')
report_table = dynamodb.Table('flowcast-reports')
site_table = dynamodb.Table('flowcast-sites')

stepfunctions = boto3.client('stepfunctions')

# Historical data functions (unchanged from original db.py)
def get_latest_hist_entry(usgs_site):
    res = data_table_v2.query(
        KeyConditionExpression=Key('usgs_site#type')
            .eq(f'{usgs_site}#hist'),
        ScanIndexForward=False,
        Limit=1
    )

    try:
        return res['Items'][0]
    except IndexError:
        return None

def get_hist_entries_after(usgs_site, start_ts):
    res = data_table_v2.query(
        KeyConditionExpression=Key('usgs_site#type')
            .eq(f'{usgs_site}#hist') & Key('timestamp').gte(start_ts),
    )

    return res['Items']

def get_n_most_recent_hist_entries(usgs_site, n):
    res = data_table_v2.query(
        KeyConditionExpression=Key('usgs_site#type')
            .eq(f'{usgs_site}#hist'),
        ScanIndexForward=False,
        Limit=n
    )

    return res['Items']

def push_hist_entries(entries: list[dict]):
    with data_table_v2.batch_writer() as batch:
        for entry in entries:
            batch.put_item(Item=entry)

# New forecast-centric functions
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
        'forecast_horizon_hours': len(forecast_data['forecast_data']['watertemp']['values']),
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

def delete_old_forecast_entries(usgs_site: str):
    """
    Delete all old forecast entries using the old schema (type: 'fcst')
    This should be called after migrating to the new forecast-centric format
    """
    # Query all old forecast entries
    response = data_table_v2.query(
        KeyConditionExpression=Key('usgs_site#type').eq(f'{usgs_site}#fcst')
    )
    
    old_entries = response['Items']
    
    # Delete them in batches
    with data_table_v2.batch_writer() as batch:
        for entry in old_entries:
            batch.delete_item(
                Key={
                    'usgs_site#type': entry['usgs_site#type'],
                    'timestamp': entry['timestamp']
                }
            )
    
    return len(old_entries)

# Legacy functions for compatibility during transition
def get_entire_fcst(usgs_site, origin):
    """
    Legacy function to get old format forecast data
    Only used during transition period
    """
    res = data_table_v2.query(
        KeyConditionExpression=Key('usgs_site#type')
            .eq(f'{usgs_site}#fcst') & Key('timestamp')
            .begins_with(str(origin))
    )

    return res['Items']

def push_fcst_entries(entries: list[dict]):
    """
    Legacy function to push old format forecast entries
    Only used during transition period
    """
    with data_table_v2.batch_writer() as batch:
        for entry in entries:
            batch.put_item(Item=entry)

# Site management functions (unchanged from original db.py)
def get_report(usgs_site: str, date: str):
    res = report_table.query(
        KeyConditionExpression=Key('usgs_site').eq(usgs_site) & Key('date').eq(date)    
    )

    return res['Items'][0] if len(res['Items']) > 0 else None

def save_report(usgs_site: str, date: str, report: str):
    report_table.put_item(
        Item={
            'usgs_site': usgs_site,
            'date': date,
            'report': report
        }
    )

def get_site(usgs_site):
    res = site_table.query(
        KeyConditionExpression=Key('usgs_site').eq(usgs_site)
    )

    if len(res['Items']) < 1: return None

    item = res['Items'][0]
    del item['subscription_ids']
    return item

class SiteStatus(Enum):
    ''' Site statuses with detailed onboarding steps enumerated. '''
    SCHEDULED = 'SCHEDULED'
    ''' Site is scheduled for onboarding, but the process has not yet started. '''
    FETCHING_DATA = 'FETCHING_DATA'
    ''' Site data is being fetched '''
    EXPORTING_SNAPSHOT = 'EXPORTING_SNAPSHOT'
    ''' Site data is being exported to a snapshot for training. '''
    TRAINING_MODELS = 'TRAINING_MODELS'
    ''' Site feature models are being trained. '''
    FORECASTING = 'FORECASTING'
    ''' Future datapoints are being forecast. '''
    ACTIVE = 'ACTIVE'
    ''' Site is onboarded and ready for usage. '''
    FAILED = 'FAILED'
    ''' Site failed to onboard. '''

def register_new_site(usgs_site: str, registration_date=datetime.now(), status=SiteStatus.SCHEDULED):
    UPDATE_AND_FORECAST_STATE_MACHINE_ARN = os.environ['UPDATE_AND_FORECAST_STATE_MACHINE_ARN']

    usgs_site_data = usgs.get_site_info(usgs_site)
    item = {
        'usgs_site': usgs_site,
        'registration_date': int(registration_date.timestamp()),
        'status': status.value,
        'onboarding_logs': [f'⏳ Site {usgs_site} scheduled for onboarding'],
        'name': usgs_site_data['sna'],
        'category': usgs_site_data['cat'],
        'latitude': usgs_site_data['lat'],
        'longitude': usgs_site_data['lng'],
        'agency': usgs_site_data['agc'],
        'subscription_ids': set(['placeholder'])
    }

    site_table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(usgs_site) OR #status = :failed_status",
        ExpressionAttributeNames={
            '#status': 'status'
        },
        ExpressionAttributeValues={
            ':failed_status': SiteStatus.FAILED.value
        }
    )

    stepfunctions.start_execution(
        stateMachineArn=UPDATE_AND_FORECAST_STATE_MACHINE_ARN,
        input=json.dumps({
            'usgs_site': usgs_site,
            'is_onboarding': True
        })
    )

    del item['subscription_ids']
    return item

def add_site_subscription(usgs_site: str, subscription_id: str):
    site_table.update_item(
        Key={ 'usgs_site': usgs_site },
        UpdateExpression='ADD #subscriptions :subscription_id',
        ExpressionAttributeValues={
            ':subscription_id': set([subscription_id])
        },
        ExpressionAttributeNames={
            '#subscriptions': 'subscription_ids'
        }
    )

def remove_site_subscription(usgs_site: str, subscription_id: str):
    site_table.update_item(
        Key={ 'usgs_site': usgs_site },
        UpdateExpression='DELETE #subscriptions :subscription_id',
        ExpressionAttributeValues={
            ':subscription_id': set([subscription_id])
        },
        ExpressionAttributeNames={
            '#subscriptions': 'subscription_ids'
        }
    )

def update_site_status(usgs_site: str, status: SiteStatus):
    site_table.update_item(
        Key={ 'usgs_site': usgs_site },
        UpdateExpression='SET #status = :status',
        ExpressionAttributeValues={
            ':status': status.value
        },
        ExpressionAttributeNames={
            '#status': 'status'
        }
    )

def push_site_onboarding_log(usgs_site: str, new_onboarding_log: str):
    site_table.update_item(
        Key={ 'usgs_site': usgs_site },
        UpdateExpression='SET #onboarding_logs = list_append(#onboarding_logs, :new_onboarding_log)',
        ExpressionAttributeValues={
            ':new_onboarding_log': [new_onboarding_log]
        },
        ExpressionAttributeNames={
            '#onboarding_logs': 'onboarding_logs'
        }
    )