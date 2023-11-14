import {
  Axis,
  AnimatedAxis,
  AnimatedLineSeries,
  XYChart,
  Tooltip
} from '@visx/xychart';
import {
  ParentSize
} from '@visx/responsive';
import { curveCardinal } from '@visx/curve';
import { PatternCircles } from '@visx/pattern';
import { timeFormat } from 'd3-time-format';
import styled from 'styled-components';

import type { Datapoint } from 'utils/types';
import { Tooltip as TooltipContent } from 'chart/tooltip';
import { COLORS } from 'utils/constants';
import { getAxisBounds, getAxisTickValues } from 'utils/misc';

type ChartProps = {
  data: Datapoint[]
}

const lineConfigs: Record<string, {
  key: keyof Datapoint;
  xAccessor: (d: Datapoint) => Date;
  yAccessor: (d: Datapoint) => number;
  color: string;
  opacity?: number;
}> = {
  'Water Temperature': {
    key: 'watertemp',
    xAccessor: d => new Date(d.timestamp),
    yAccessor: d => d['watertemp'],
    color: COLORS['powder-blue']
  },
  'Air Temperature': {
    key: 'airtemp',
    xAccessor: d => new Date(d.timestamp),
    yAccessor: d => d['airtemp'],
    color: COLORS['drab-dark-brown'],
    opacity: 0.15
  }
};

export const Chart = ({
  data
}: ChartProps) => (
  <ParentSize>
    {(parent) => (
      <XYChart
        width={parent.width}
        height={parent.height}
        xScale={{ type: 'band', range: [20, parent.width - 75] }}
        yScale={{
          type: 'linear',
          zero: false,
          domain: getAxisBounds(data, ['airtemp', 'watertemp'], 5),
          range: [parent.height - 25, 0],
          // round: true
        }}
      >
        <AnimatedAxis
          hideAxisLine={true}
          hideTicks={true}
          orientation='bottom'
          tickFormat={timeFormat('%b %d')}
          animationTrajectory='min'
        />
        <Axis
          hideAxisLine={true}
          hideTicks={true}
          orientation='right'
          tickFormat={(f) => `${f} F`}
        />
        {Object.entries(lineConfigs).map(([name, { xAccessor, yAccessor, color, opacity }]) => [
          <AnimatedLineSeries
            key={`${name}-hist`}
            dataKey={`${name} (Actual)`}
            data={data.filter(data => data.type === 'hist')}
            xAccessor={xAccessor}
            yAccessor={yAccessor}
            curve={curveCardinal}
            stroke={color}
            strokeOpacity={opacity || 1}
          />,
          <AnimatedLineSeries
            key={`${name}-fcst`}
            dataKey={`${name} (Forecast)`}
            data={data.filter(data => data.type === 'fcst')}
            xAccessor={xAccessor}
            yAccessor={yAccessor}
            curve={curveCardinal}
            stroke={color}
            strokeDasharray={'5'}
            strokeOpacity={opacity || 1}
          />
        ])}
        <Tooltip
          snapTooltipToDatumX
          snapTooltipToDatumY
          showVerticalCrosshair
          renderTooltip={({ tooltipData }) => {
            if (!tooltipData || !tooltipData.nearestDatum) return;
            const dataKey = tooltipData.nearestDatum.key;
            const [, , type] = /(.*)-(hist|fcst)/.exec(dataKey) || [];
            if (!type) return;

            let date: Date | null = null;
            const dataValue = tooltipData.nearestDatum.datum as Datapoint;
            const features = Object.keys(lineConfigs).reduce((features, key) => {
              if (!date) date = lineConfigs[key].xAccessor(dataValue);
              features[key] = lineConfigs[key].yAccessor(dataValue);
              return features;
            }, {} as Record<string, any>);

            if (date == null) return;

            return (
              <TooltipContent
                date={date}
                features={features}
              />
            );
          }}
          className='tooltip'
        />
      </XYChart>
    )}
  </ParentSize>
);
