import boto3
import json
from datetime import datetime
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_aws import ChatBedrock
import pandas as pd

from utils.forecast import get_forecast
from utils.usgs import get_site_info
from utils.constants import SYSTEM, INSTRUCTION

bedrock = boto3.client(
    service_name='bedrock-runtime',
    region_name='us-east-1'
)

MODEL_ID = 'anthropic.claude-3-sonnet-20240229-v1:0'
MODEL_KWARGS =  { 
    "max_tokens": 2048,
    "temperature": 0.0,
    "top_k": 250,
    "top_p": 1,
    "stop_sequences": ["\n\nHuman"],
}

model = ChatBedrock(
    client=bedrock,
    model_id=MODEL_ID,
    model_kwargs=MODEL_KWARGS
)

def get_report(usgs_site: str):
    forecast = get_forecast(usgs_site)
    forecast['timestamp'] = forecast['timestamp'].apply(lambda x: datetime.fromtimestamp(float(x)))
    forecast_today = forecast[forecast['timestamp'].apply(lambda x: x.date()) == pd.Timestamp.today().date()].iloc[0]
    forecast['timestamp'] = forecast['timestamp'].apply(lambda x: x.isoformat())
    forecast_json = forecast.to_json(orient='records')
    
    site_json = json.dumps(get_site_info(usgs_site))

    messages = [
        ('system', SYSTEM),
        ('human', INSTRUCTION)
    ]
    prompt = ChatPromptTemplate.from_messages(messages)

    chain = prompt | model | StrOutputParser()

    return chain.invoke({
        'TODAYS_DATE': datetime.now().isoformat(),
        'SITE_INFO': site_json,
        'CONDITIONS': forecast_json,
        'CURRENT_WATER_TEMP': forecast_today['watertemp'],
        'CURRENT_STREAM_FLOW': forecast_today['streamflow']
    })
