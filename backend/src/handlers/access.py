# from math import nan
# import pandas as pd
# import json

# from utils.db import engine

# def handler(_event, _context):
#   # todo - would be more performant to filter these columns and format data in the sql query, but
#   # since the table is relatively small and will always be the same size, this is ok for now
#   forecast = pd.read_sql(
#     '''
#     SELECT * FROM forecast
#     ''',
#     engine
#   )

#   output = pd.DataFrame(columns=['temp', 'temp_pred'], index=forecast['ds'])
#   forecast_idx = 1
#   for idx in forecast.index:
#     ds = forecast['ds'][idx]
#     output['temp'][ds] = forecast['y'][idx]

#     # the first forecast value is yhat1, second is yhat2, etc.
#     if pd.isnull(output['temp'][ds]):
#       output['temp_pred'][ds] = forecast[f'yhat{forecast_idx}'][idx]
#       forecast_idx += 1
#     else:
#       output['temp_pred'][ds] = nan

#   output_json = output.reset_index().to_json(orient='records')
#   return { 'statusCode': 200, 'body': output_json }
