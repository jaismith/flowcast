import { z } from 'zod';

// v2 API schemas

export const WaterForecastFeatureSchema = z.object({
  values: z.array(z.number()),
  timestamps: z.array(z.number()), // seconds
  confidence_intervals: z.object({
    '5th': z.array(z.number()),
    '95th': z.array(z.number())
  })
});

export const WaterForecastSchema = z.object({
  watertemp: WaterForecastFeatureSchema,
  streamflow: WaterForecastFeatureSchema
});

export const AtmosphericForecastSchema = z.object({
  timestamps: z.array(z.number()), // seconds
  airtemp: z.array(z.number()),
  precip: z.array(z.number()),
  cloudcover: z.array(z.number()),
  snow: z.array(z.number()),
  snowdepth: z.array(z.number())
});

export const HistoricalSeriesSchema = z.object({
  timestamps: z.array(z.number()), // seconds
  watertemp: z.array(z.number().nullable()),
  streamflow: z.array(z.number().nullable()),
  airtemp: z.array(z.number().nullable()),
  precip: z.array(z.number().nullable()),
  cloudcover: z.array(z.number().nullable()),
  snow: z.array(z.number().nullable()),
  snowdepth: z.array(z.number().nullable())
});

export const ForecastBundleSchema = z.object({
  origin_timestamp: z.number().nullable(),
  created_at: z.number().nullable(),
  horizon_hours: z.number(),
  water_forecast: WaterForecastSchema,
  atmospheric_forecast: AtmosphericForecastSchema,
  historical: HistoricalSeriesSchema
});

export type WaterForecastFeature = z.infer<typeof WaterForecastFeatureSchema>;
export type WaterForecast = z.infer<typeof WaterForecastSchema>;
export type AtmosphericForecast = z.infer<typeof AtmosphericForecastSchema>;
export type HistoricalSeries = z.infer<typeof HistoricalSeriesSchema>;
export type ForecastBundle = z.infer<typeof ForecastBundleSchema>;

// Internal chart types (derived from v2 bundle)
export type Observation = {
  timestamp: number; // ms
  watertemp: number;
  watertemp_5th?: number | null;
  watertemp_95th?: number | null;
  streamflow: number;
  streamflow_5th?: number | null;
  streamflow_95th?: number | null;
  type: 'actual' | 'forecast';
};

export type Forecast = Observation[];

// Existing site schemas
export const SiteSchema = z.object({
  'usgs_site': z.string(),
  'registration_date': z.string(),
  'status': z.string(),
  'onboarding_logs': z.array(z.string()).nullish(),
  'name': z.string(),
  'category': z.string(),
  'latitude': z.string(),
  'longitude': z.string(),
  'agency': z.string()
});

export type Site = z.infer<typeof SiteSchema>;

export type XYCoordinates = { x: number, y: number };

export type SiteFeatureSupport = { hasStreamFlow: boolean, hasWaterTemp: boolean };

export const SiteUpdateSchema = z.object({
  'onboarding_logs': z.array(z.string()),
  'status': z.string(),
  'usgs_site': z.string()
});

export type SiteUpdate = z.infer<typeof SiteUpdateSchema>;
