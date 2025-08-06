import axios from 'axios';
import { kml } from '@tmcw/togeojson';

import sampleForecast from '../test/data/forecast.json';
import { ACCESS_API_ROOT, USGS_IV_API, USGS_SITES_API } from './constants';
import { ForecastSchema, SiteSchema } from './types';

import type { Forecast, SiteFeatureSupport } from './types';

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
    console.error('error fetching site data (might not exist?) from ', url, err);
  }

  return null;
};

export const getSites = async () => {
  const url = ACCESS_API_ROOT + '/sites';
  try {
    const res = await axios.get(url);
    const { sites } = res.data;
    return sites.map(SiteSchema.parse);
  } catch (err) {
    console.error('error fetching sites data from ', url, err);
  }

  return [];
}

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

export const registerSite = async (usgs_site: string) => {
  const url = ACCESS_API_ROOT + '/site/register' + `?usgs_site=${usgs_site}`;
  try {
    await axios.post(url);
    return true;
  } catch (err) {
    console.error('error registering site', url, err);
  }

  return null
}

// * nwis

export const getUSGSSites = async (bbox: number[]) => {
  const url = USGS_SITES_API + `?bBox=${bbox.map(i => i.toFixed(7))}&format=ge&siteType=ST&period=P30D`;
  try {
    const res = await axios.get(url);
    const { data } = res;
    const parser = new DOMParser();
    const kmlDoc = parser.parseFromString(data, 'application/xml');
    return kml(kmlDoc);
  } catch (err) {
    console.error('error fetching usgs sites from ', url, err);
  }

  return null;
};

export const getSiteFeatureSupport = async (usgs_site: string): Promise<SiteFeatureSupport> => {
  const url = USGS_IV_API + `?format=json&sites=${usgs_site}&parameterCd=00060,00010`;

  try {
    const response = await axios.get(url);
    const data = response.data;

    let hasStreamFlow = false;
    let hasWaterTemp = false;

    if (data.value && data.value.timeSeries) {
      for (const timeSeries of data.value.timeSeries) {
        const variableCode = timeSeries.variable.variableCode[0].value;
        if (variableCode === '00060') {
          hasStreamFlow = true;
        }
        if (variableCode === '00010') {
          hasWaterTemp = true;
        }
      }
    }

    return { hasStreamFlow, hasWaterTemp };

  } catch (error) {
    console.error('Error fetching data from USGS API:', error);
    return { hasStreamFlow: false, hasWaterTemp: false };
  }
};
