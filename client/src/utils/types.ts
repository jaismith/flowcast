export type DatapointFeatures = {
  snow: number;
  precip: number;
  snowdepth: number;
  cloudcover: number;
  timestamp: number;
  streamflow: number;
  airtemp: number;
  watertemp: number;
}

export type Datapoint = DatapointFeatures & {
  'usgs_site#type': string;
  usgs_site: string;
  type: 'hist' | 'fcst';
  origin: number;
  'origin#timestamp'?: string;
};
