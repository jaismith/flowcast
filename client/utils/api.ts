import axios from 'axios';

import sampleForecast from '../test/data/forecast.json';
import { ACCESS_API_ROOT } from './constants';
import { ForecastSchema } from './types';

import type { Forecast } from './types';

export const getForecast = async (start_ts: number, end_ts?: number, useSample?: boolean): Promise<Forecast> => {
  if (useSample) return ForecastSchema.parse(sampleForecast);

  try {
    const res = await axios.get(ACCESS_API_ROOT + `?start_ts=${start_ts}${!!end_ts ? `&end_ts=${end_ts}` : ''}`);
    return ForecastSchema.parse(res.data);
  } catch (err) {
    console.error('error fetching data from ', ACCESS_API_ROOT, err)
  }

  return [];
};
