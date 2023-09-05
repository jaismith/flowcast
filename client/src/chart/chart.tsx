import {
  AnimatedAxis,
  AnimatedLineSeries,
  XYChart,
} from '@visx/xychart';
import { curveCardinal } from '@visx/curve';
import { PatternCircles } from '@visx/pattern';
import dayjs from 'dayjs';

import type { Datapoint } from 'utils/types';
import { ctof, getDateRange } from 'utils/misc';
import { AxisLabel } from 'chart/axisLabel';

type ChartProps = {
  data: Datapoint[]
}

export const Chart = ({
  data
}: ChartProps) => (
  <XYChart
    height={500}
    xScale={{ type: 'band' }}
    yScale={{ type: 'linear' }}
    margin={{
      top: 50,
      right: 200,
      bottom: 100,
      left: 0
    }}
  >
    <AnimatedAxis
      hideAxisLine={true}
      hideTicks={true}
      orientation='bottom'
      tickValues={getDateRange(dayjs(data[0].ds).add(6, 'hour').toDate(), data[data.length - 1].ds, 24)}
      tickComponent={AxisLabel}
      animationTrajectory='min'
    />
    <AnimatedAxis
      hideAxisLine={true}
      orientation='right'
      left={window.innerWidth - 50}
      tickFormat={(c) => `${ctof(c)} F`}
      tickLabelProps={(props) => ({ ...props, fill: '#dddddd' })}
      animationTrajectory='min'
    />
    <PatternCircles
      id='pattern-circles'
      width={16}
      height={16}
    />
    <AnimatedLineSeries
      dataKey='historical'
      data={data}
      xAccessor={d => new Date(d.ds)}
      yAccessor={d => d.temp}
      curve={curveCardinal}
      stroke={'#dddddd'}
    />
    <AnimatedLineSeries
      dataKey='pred'
      data={data}
      xAccessor={d => new Date(d.ds)}
      yAccessor={d => d.temp_pred}
      curve={curveCardinal}
      stroke={'#dddddd'}
      strokeDasharray={'5'}
    />
  </XYChart>
);
