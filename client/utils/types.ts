import dayjs from 'dayjs';
import { z } from 'zod';

export const ObservationSchema = z.object({
  'snow': z.number(),
  'precip': z.number(),
  'snowdepth': z.number(),
  'cloudcover': z.number(),
  'usgs_site#type': z.string(),
  'timestamp': z.number().transform(ts => dayjs.unix(ts)),
  'streamflow': z.number().nullable(),
  'airtemp': z.number(),
  'usgs_site': z.string(),
  'watertemp': z.number(),
  'type': z.enum(['hist', 'fcst']),
  'origin': z.number().nullable(),
  'watertemp_5th': z.number().nullable(),
  'origin#timestamp': z.string().nullable(),
  'watertemp_95th': z.number().nullable()
});

export const ForecastSchema = z.array(ObservationSchema);

export type Observation = z.infer<typeof ObservationSchema>;

export type Forecast = Observation[];

export type XYCoordinates = {
  x: number,
  y: number
};
