import { Threshold } from "@visx/threshold"
import { LinePath } from "@visx/shape";
import { CurveFactory } from "d3-shape";

import type { Forecast, Observation } from "../../utils/types";

type HistoricalElementProps = {
  shouldUseThreshold: boolean;
  historical: Forecast;
  featureName: string;
  timeScale: (x: number) => number;
  featureScale: (x: number) => number;
  feature: (o: Observation) => number;
  date: (o: Observation) => number;
  curveBasis: CurveFactory;
  yMax: number;
  color: string;
};

const HistoricalElement = ({
  shouldUseThreshold, historical, featureName, timeScale, featureScale,
  feature, date, curveBasis, yMax, color
}: HistoricalElementProps) => (
  <>
    {shouldUseThreshold
      ? <Threshold<Observation>
          id={`${featureName}-historical-threshold`}
          data={historical}
          x={(o: Observation) => timeScale(date(o))}
          y0={(o: Observation) => featureScale(feature(o))}
          y1={() => yMax}
          clipAboveTo={0}
          clipBelowTo={yMax}
          curve={curveBasis}
          aboveAreaProps={{
            fill: color,
            fillOpacity: 0.3,
          }}
        />
      : <LinePath
          id={`${featureName}-historical-line`}
          data={historical}
          curve={curveBasis}
          x={(o: Observation) => timeScale(date(o))}
          y={(o: Observation) => featureScale(feature(o))}
          stroke={color}
          strokeWidth={3}
          strokeOpacity={1}
        />}
  </>
);

export default HistoricalElement;
