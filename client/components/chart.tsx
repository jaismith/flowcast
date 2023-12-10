import { useEffect, useState } from 'react';
import { useResizeObserver } from '@mantine/hooks';
import { Flex, Text } from '@mantine/core';
import { GridColumns, GridRows } from '@visx/grid';
import { Group } from '@visx/group';
import { scaleLinear, scaleTime } from '@visx/scale';
import { AxisBottom, AxisLeft } from '@visx/axis';
import { Threshold } from '@visx/threshold';
import { curveBasis } from '@visx/curve';
import { LinePath } from '@visx/shape';

import { getForecast } from '../utils/api';
import { COLORS } from '../utils/constants';

import type { Forecast, Observation } from '../utils/types';
import Tooltip from './tooltip';

// accessors
const date = (o: Observation) => o.timestamp.valueOf();
const watertemp = (o: Observation) => o.watertemp;
const watertempLow = (o: Observation) => o.watertemp_5th;
const watertempHigh = (o: Observation) => o.watertemp_95th;

const defaultMargin = { top: 10, right: 5, bottom: 30, left: 25 };

const Chart = () => {
  const [containerRef, { width, height }] = useResizeObserver();
  const [forecast, setForecast] = useState<Forecast | null>(null);

  useEffect(() => {
    const load = async () => {
      const timerPromise = new Promise((resolve) => setTimeout(resolve, 1000));
      const [forecast] = await Promise.all([getForecast(false), timerPromise]);
      setForecast(forecast);
    };
    load();
  }, []);

  useEffect(() => {
    async function getLoader() {
      const { grid } = await import('ldrs')
      grid.register()
    }
    getLoader()
  }, [])

  if (!forecast || forecast.length < 1) {
    return (
      <Flex
        justify='center'
        align='center'
        style={{ width: '100%', height: 300 }}
      >
        {!forecast ? (
          <l-grid
            size='60'
            speed='1.5'
            color='black' 
          />
        ) : (
          <Text>
            Something went wrong loading today&#39;s forecast, please refresh the page or try again later (this may happen between 8p and 12a EST, due to an error in how USGS APIs handle timezones).
          </Text>
        )}
      </Flex>
    )
  }

  // bounds
  const xMax = width - defaultMargin.left - defaultMargin.right;
  const yMax = height - defaultMargin.top - defaultMargin.bottom;

  // scales
  const timeScale = scaleTime<number>({
    range: [0, xMax],
    domain: [Math.min(...forecast.map(date)), Math.max(...forecast.map(date))]
  });
  const watertempScale = scaleLinear<number>({
    range: [yMax, 0],
    domain: [
      Math.min(...forecast.map((o: Observation) => Math.min(watertemp(o), watertempLow(o) ?? watertemp(o)))) - 1,
      Math.max(...forecast.map((o: Observation) => Math.max(watertemp(o), watertempHigh(o) ?? watertemp(o)))) + 5
    ],
    nice: true
  });

  // latest historical point
  const latestHistoricalObservation = forecast.filter(o => o.type === 'hist').slice(-1)[0]
  const pulseXPosition = timeScale(date(latestHistoricalObservation));
  const pulseYPosition = watertempScale(watertemp(latestHistoricalObservation));

  return (
    <div
      ref={containerRef}
      style={{ 'maxWidth': '100%', height: '60vh' }}
    >
      <svg width={width} height={height}>
        <foreignObject x={30} y={15} width='250px' >
          <Tooltip observation={latestHistoricalObservation} />
        </foreignObject>
        <Group left={defaultMargin.left} top={defaultMargin.top}>
          <GridRows scale={watertempScale} width={xMax} height={yMax} />
          <GridColumns scale={timeScale} width={xMax} height={yMax} />
          <AxisBottom
            top={yMax}
            scale={timeScale}
            numTicks={width > 520 ? 10 : 5}
            tickStroke='#eaf0f6'
            hideAxisLine
          />
          <AxisLeft scale={watertempScale} hideAxisLine tickStroke='#eaf0f6' />
          <line
            x1={timeScale(Date.now())}
            x2={timeScale(Date.now())}
            y1={0}
            y2={yMax}
            stroke={COLORS.MAGNOLIA}
            strokeWidth={2}
          />
          <LinePath
            data={forecast.filter(o => o.type === 'hist')}
            curve={curveBasis}
            x={(o: Observation) => timeScale(date(o)) ?? 0}
            y={(o: Observation) => watertempScale(watertemp(o)) ?? 0}
            stroke={COLORS.VISTA_BLUE}
            strokeWidth={3}
            strokeOpacity={1}
          />
          <LinePath
            data={forecast.filter(o => o.type === 'fcst')}
            curve={curveBasis}
            x={(o: Observation) => timeScale(date(o)) ?? 0}
            y={(o: Observation) => watertempScale(watertemp(o)) ?? 0}
            stroke={COLORS.VISTA_BLUE}
            strokeWidth={3}
            strokeOpacity={1}
            strokeDasharray='1,5'
          />
          <circle
            cx={pulseXPosition}
            cy={pulseYPosition}
            r={5}
            fill={COLORS.VISTA_BLUE}
            className='pulsing-dot'
            style={{
              transformOrigin: `${pulseXPosition}px ${pulseYPosition}px`
            }}
          />
          <circle
            cx={pulseXPosition}
            cy={pulseYPosition}
            r={5}
            fill={COLORS.VISTA_BLUE}
          />
          <Threshold<Observation>
            id='threshold-chart'
            data={forecast.filter(o => o.type === 'fcst')}
            x={(o: Observation) => timeScale(date(o)) ?? 0}
            y0={(o: Observation) => watertempScale(watertempHigh(o) ?? 0)}
            y1={(o: Observation) => watertempScale(watertempLow(o) ?? 0)}
            clipAboveTo={0}
            clipBelowTo={yMax}
            curve={curveBasis}
            aboveAreaProps={{
              fill: COLORS.VISTA_BLUE,
              fillOpacity: 0.3,
            }}
          />
        </Group>
      </svg>
    </div>
  )
};

export default Chart;
