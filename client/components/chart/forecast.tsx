import { Threshold } from "@visx/threshold"
import { LinePath } from "@visx/shape"
import { CurveFactory } from "d3-shape"

import type { Forecast, Observation } from "../../utils/types"

type ForecastElementProps = {
  showHistoricalAccuracy: boolean;
  shouldUseThreshold: boolean;
  future: Forecast;
  historical: Forecast;
  featureName: string;
  latestHistoricalObservation: Observation;
  timeScale: (x: number) => number;
  featureScale: (x: number) => number;
  feature: (o: Observation) => number;
  featureLowerBound: (o: Observation) => number;
  featureUpperBound: (o: Observation) => number;
  date: (o: Observation) => number;
  curveBasis: CurveFactory;
  yMax: number;
  color: string;
};

const ForecastElement = ({
  showHistoricalAccuracy, shouldUseThreshold, future, historical,
  featureName, timeScale, featureScale, feature, featureLowerBound,
  featureUpperBound, date, curveBasis, yMax, color
}: ForecastElementProps) => (
  <>
    {/* Previous forecasts (for accuracy comparison) */}
    {showHistoricalAccuracy && (
      <Threshold<Observation>
        id={`${featureName}-prior-forecast`}
        data={historical}
        x={(o: Observation) => timeScale(date(o)) ?? 0}
        y0={(o: Observation) => featureScale(featureUpperBound(o))}
        y1={(o: Observation) => featureScale(featureLowerBound(o))}
        clipAboveTo={0}
        clipBelowTo={yMax}
        curve={curveBasis}
        aboveAreaProps={{
          fill: color,
          fillOpacity: 0.15,
        }}
      />
    )}
    {/* Current forecast */}
    {!shouldUseThreshold && (
      <Threshold<Observation>
        id={`${featureName}-forecast`}
        data={future}
        x={(o: Observation) => timeScale(date(o)) ?? 0}
        y0={(o: Observation) => featureScale(featureUpperBound(o))}
        y1={(o: Observation) => featureScale(featureLowerBound(o))}
        clipAboveTo={0}
        clipBelowTo={yMax}
        curve={curveBasis}
        aboveAreaProps={{
          fill: color,
          fillOpacity: 0.3,
        }}
      />
    )}
    {/* Forecasted values */}
    {shouldUseThreshold
      ? <Threshold<Observation>
          id={`${featureName}-forecast`}
          data={future}
          x={(o: Observation) => timeScale(date(o)) ?? 0}
          y0={(o: Observation) => featureScale(feature(o))}
          y1={() => yMax}
          clipAboveTo={0}
          clipBelowTo={yMax}
          curve={curveBasis}
          aboveAreaProps={{
            fill: color,
            fillOpacity: 0.2,
          }}
        />
      : <LinePath
          id={`${featureName}-forecast`}
          data={future}
          curve={curveBasis}
          x={(o: Observation) => timeScale(date(o)) ?? 0}
          y={(o: Observation) => featureScale(feature(o)) ?? 0}
          stroke={color}
          strokeWidth={3}
          strokeOpacity={1}
          strokeDasharray='1,5'
        />}
  </>
);

export default ForecastElement;
