import axios from 'axios';
import { kml } from '@tmcw/togeojson';

import { ACCESS_API_ROOT, USGS_IV_API, USGS_SITES_API } from './constants';
import { ForecastBundleSchema, SiteSchema } from './types';

import type { Forecast, ForecastBundle, SiteFeatureSupport } from './types';

// Transform v2 bundle into internal Observation[] for charting
const bundleToObservations = (bundle: ForecastBundle): Forecast => {
  const observations: Forecast = [];

  // historical points
  const histT = bundle.historical.timestamps.map(ts => ts * 1000);
  for (let i = 0; i < histT.length; i++) {
    const wt = bundle.historical.watertemp[i] ?? undefined;
    const sf = bundle.historical.streamflow[i] ?? undefined;
    if (wt === undefined || sf === undefined) continue;
    observations.push({
      timestamp: histT[i],
      watertemp: wt,
      streamflow: sf,
      type: 'actual'
    });
  }

  // forecast points (use atmospheric timestamps as canonical)
  const fcstT = bundle.atmospheric_forecast.timestamps.map(ts => ts * 1000);
  const ft = bundle.water_forecast;
  const wtIdx: Record<number, number> = {};
  ft.watertemp.timestamps.forEach((ts, idx) => { wtIdx[ts] = idx; });
  const sfIdx: Record<number, number> = {};
  ft.streamflow.timestamps.forEach((ts, idx) => { sfIdx[ts] = idx; });

  for (let i = 0; i < fcstT.length; i++) {
    const sec = Math.floor(fcstT[i] / 1000);
    const wti = wtIdx[sec];
    const sfi = sfIdx[sec];
    if (wti === undefined || sfi === undefined) continue;
    observations.push({
      timestamp: fcstT[i],
      watertemp: ft.watertemp.values[wti],
      watertemp_5th: ft.watertemp.confidence_intervals['5th'][wti] ?? null,
      watertemp_95th: ft.watertemp.confidence_intervals['95th'][wti] ?? null,
      streamflow: ft.streamflow.values[sfi],
      streamflow_5th: ft.streamflow.confidence_intervals['5th'][sfi] ?? null,
      streamflow_95th: ft.streamflow.confidence_intervals['95th'][sfi] ?? null,
      type: 'forecast'
    });
  }

  return observations
    .filter(o => o.watertemp !== undefined && o.streamflow !== undefined)
    .sort((a, b) => a.timestamp - b.timestamp);
};

export const getForecast = async (usgs_site: string, start_ts: number, _historicalForecastHorizon: number,  end_ts?: number): Promise<Forecast> => {
  const url = ACCESS_API_ROOT + '/forecast' + `?usgs_site=${usgs_site}&start_ts=${start_ts}${!!end_ts ? `&end_ts=${end_ts}` : ''}`
  try {
    const res = await axios.get(url);
    const bundle = ForecastBundleSchema.parse(res.data.forecast);
    return bundleToObservations(bundle);
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
