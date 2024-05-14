import axios from 'axios';

import sampleForecast from '../test/data/forecast.json';
import { ACCESS_API_ROOT } from './constants';
import { ForecastSchema, SiteSchema } from './types';

import type { Forecast } from './types';

export const getForecast = async (usgs_site: string, start_ts: number, historicalForecastHorizon: number,  end_ts?: number, useSample?: boolean): Promise<Forecast> => {
  if (useSample) return ForecastSchema.parse(sampleForecast);

  const url = ACCESS_API_ROOT + '/forecast' + `?usgs_site=${usgs_site}&start_ts=${start_ts}${!!end_ts ? `&end_ts=${end_ts}` : ''}&historical_fcst_horizon=${historicalForecastHorizon}`
  try {
    const res = await axios.get(url);
    const { forecast } = res.data;
    return ForecastSchema.parse((forecast as any[]).filter(o => !!o.watertemp && !!o.streamflow));
  } catch (err) {
    console.error('error fetching data from ', url, err);
  }

  return [];
};

export const getSite = async (usgs_site: string) => {
  const url = ACCESS_API_ROOT + '/site' + `?usgs_site=${usgs_site}`;
  try {
    const res = await axios.get(url);
    const { site } = res.data;
    return SiteSchema.parse(site);
  } catch (err) {
    console.error('error fetching site data from ', url, err);
  }

  return null;
};

export const getReport = async (usgs_site: string) => {
  const url = ACCESS_API_ROOT + '/report' + `?usgs_site=${usgs_site}`;
  try {
    const res = await axios.get(url);
    const { report } = res.data;
    return report.report;
  } catch (err) {
    console.error('error fetching report from ', url, err);
  }

  return null;
};
