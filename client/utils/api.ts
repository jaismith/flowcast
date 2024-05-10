import axios from 'axios';

import sampleForecast from '../test/data/forecast.json';
import { ACCESS_API_ROOT } from './constants';
import { ForecastSchema } from './types';

import type { Forecast } from './types';

export const getForecast = async (start_ts: number, historicalForecastHorizon: number,  end_ts?: number, useSample?: boolean): Promise<Forecast> => {
  if (useSample) return ForecastSchema.parse(sampleForecast);

  const url = ACCESS_API_ROOT + '/forecast' + `?usgs_site=01427510&start_ts=${start_ts}${!!end_ts ? `&end_ts=${end_ts}` : ''}&historical_fcst_horizon=${historicalForecastHorizon}`
  try {
    const res = await axios.get(url);
    const { forecast } = res.data;
    return ForecastSchema.parse((forecast as any[]).filter(o => !!o.watertemp && !!o.streamflow));
  } catch (err) {
    console.error('error fetching data from ', url, err)
  }

  return [];
};
