import os
import psycopg2
import sys
import boto3
import os

from dotenv import load_dotenv
load_dotenv()

# * config

HOST = os.environ['RDS_HOST']
PORT = '5432'
USER = 'postgres'
REGION = 'us-east-1'
DBNAME = 'postgres'
PASS = os.environ['RDS_PASS']

# * connect

print('initializing psql client...', end=' ')

conn = psycopg2.connect(host=HOST, port=PORT, database=DBNAME, user=USER, password=PASS)
cur = conn.cursor()

print('done')
