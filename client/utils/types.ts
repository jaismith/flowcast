import dayjs from 'dayjs';
import { z } from 'zod';

export const ObservationSchema = z.object({
  'snow': z.number(),
  'precip': z.number(),
  'snowdepth': z.number(),
  'cloudcover': z.number(),
  'usgs_site#type': z.string(),
  'timestamp': z.number().transform(ts => dayjs.unix(ts).valueOf()),
  'streamflow': z.number(),
  'airtemp': z.number(),
  'usgs_site': z.string(),
  'watertemp': z.number(),
  'type': z.enum(['hist', 'fcst']),
  'origin': z.number().nullish(),
  'horizon': z.number().nullish(),
  'watertemp_5th': z.number().nullish().default(null),
  'origin#timestamp': z.string().nullish(),
  'horizon#timestamp': z.string().nullish(),
  'watertemp_95th': z.number().nullish().default(null),
  'streamflow_5th': z.number().nullish().default(null),
  'streamflow_95th': z.number().nullish().default(null)
});

export const ForecastSchema = z.array(ObservationSchema);

export type Observation = z.infer<typeof ObservationSchema>;

export type Forecast = Observation[];

export const SiteSchema = z.object({
  'sno': z.string(),
  'sna': z.string(),
  'cat': z.string(),
  'lat': z.string(),
  'lng': z.string(),
  'agc': z.string()
});
 
export type Site = z.infer<typeof SiteSchema>;

export type XYCoordinates = {
  x: number,
  y: number
};
