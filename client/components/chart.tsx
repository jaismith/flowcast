import { useEffect, useState } from 'react';
import dynamic from 'next/dynamic';
import { useResizeObserver } from '@mantine/hooks';
import { Flex, Text } from '@mantine/core';
import { GridColumns, GridRows } from '@visx/grid';
import { Group } from '@visx/group';
import { scaleLinear, scaleTime } from '@visx/scale';
import { AxisBottom, AxisLeft } from '@visx/axis';
import { Threshold } from '@visx/threshold';
import { curveBasis } from '@visx/curve';
import { LinePath } from '@visx/shape';
import { TooltipWithBounds, useTooltip, defaultStyles } from '@visx/tooltip';
import { localPoint } from '@visx/event';

import type { Forecast, Observation } from '../utils/types';
import { COLORS } from '../utils/constants';
import Tooltip from './tooltip';

// accessors
const date = (o: Observation) => o.timestamp;
const watertemp = (o: Observation) => o.watertemp;
const watertempLow = (o: Observation) => o.watertemp_5th;
const watertempHigh = (o: Observation) => o.watertemp_95th;

const defaultMargin = { top: 10, right: 5, bottom: 30, left: 25 };

const defaultTooltipPos = { top: 0, left: 0 };

type ChartProps = {
  forecast: Forecast,
  isLoading: boolean,
  showHistoricalAccuracy: boolean,
  historicalAccuracyHorizon: number
};

const Chart = ({ forecast, isLoading }: ChartProps) => {
  const [containerRef, { width, height }] = useResizeObserver();
  const [mouseX, setMouseX] = useState(-1);
  const [lastRenderedMouseX, setLastRenderedMouseX] = useState(-1);

  const latestHistoricalObservation = forecast.filter(o => o.type === 'hist').slice(-1)[0]

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
  const pulseXPosition = timeScale(date(latestHistoricalObservation));
  const pulseYPosition = watertempScale(watertemp(latestHistoricalObservation));

  const {
    tooltipTop,
    tooltipLeft,
    tooltipData,
    showTooltip
  } = useTooltip<Observation>({
    tooltipData: latestHistoricalObservation,
    tooltipTop: defaultTooltipPos.top,
    tooltipLeft: defaultTooltipPos.left
  });

  useEffect(() => {
    // todo fix this terrible code lol
    if (mouseX == lastRenderedMouseX) return;

    if (mouseX < 0) {
      showTooltip({
        tooltipData: latestHistoricalObservation,
        tooltipTop: defaultTooltipPos.top,
        tooltipLeft: defaultTooltipPos.left
      });
    } else {
      // todo optimize bin search
      const closestObsIdx = forecast
        .map(o => o.timestamp)
        .map(ts => timeScale(ts))
        .findIndex(v => v > mouseX);
      if (closestObsIdx < 0) return;
      const closestObs = forecast[closestObsIdx];
      const x = timeScale(closestObs.timestamp);
      const y = watertempScale(closestObs.watertemp);

      showTooltip({
        tooltipData: closestObs,
        tooltipTop: y,
        tooltipLeft: x
      });
    }

    setLastRenderedMouseX(mouseX);
  }, [mouseX, lastRenderedMouseX, latestHistoricalObservation, showTooltip, forecast, timeScale, watertempScale])

  useEffect(() => {
    async function getLoader() {
      const { grid } = await import('ldrs')
      grid.register()
    }
    getLoader()
  }, []);

  if (isLoading || !forecast || forecast.length < 1) {
    return (
      <Flex
        justify='center'
        align='center'
        style={{ width: '100%', height: 300 }}
      >
        {isLoading ? (
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

  return (
    <div
      ref={containerRef}
      style={{ height: '70vh', position: 'relative' }}
    >
      <svg
        width={width}
        height={height} 
        onMouseMove={e => {
          setMouseX((localPoint(e)?.x || -1) - defaultMargin.left)
        }}
        onMouseLeave={() => setMouseX(-1)}
      >
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
            x1={timeScale(tooltipData?.timestamp ?? Date.now())}
            x2={timeScale(tooltipData?.timestamp ?? Date.now())}
            y1={0}
            y2={yMax}
            stroke={COLORS.MAGNOLIA}
            strokeWidth={2}
          />
          <Threshold<Observation>
            id='threshold-chart'
            data={[latestHistoricalObservation, ...forecast.filter(o => o.type === 'fcst' && o.timestamp > latestHistoricalObservation.timestamp)]}
            x={(o: Observation) => timeScale(date(o)) ?? 0}
            y0={(o: Observation) => watertempScale(watertempHigh(o) ?? watertemp(o))}
            y1={(o: Observation) => watertempScale(watertempLow(o) ?? watertemp(o))}
            clipAboveTo={0}
            clipBelowTo={yMax}
            curve={curveBasis}
            aboveAreaProps={{
              fill: COLORS.VISTA_BLUE,
              fillOpacity: 0.3,
            }}
          />
          <Threshold<Observation>
            id='threshold-chart-hist'
            data={forecast.filter(o => o.type === 'fcst' && o.timestamp < latestHistoricalObservation.timestamp)}
            x={(o: Observation) => timeScale(date(o)) ?? 0}
            y0={(o: Observation) => watertempScale(watertempHigh(o) ?? 0)}
            y1={(o: Observation) => watertempScale(watertempLow(o) ?? 0)}
            clipAboveTo={0}
            clipBelowTo={yMax}
            curve={curveBasis}
            aboveAreaProps={{
              fill: COLORS.VISTA_BLUE,
              fillOpacity: 0.15,
            }}
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
          {mouseX > 0
            ? <circle
                cx={tooltipLeft}
                cy={tooltipTop}
                r={5}
                fill={COLORS.VISTA_BLUE}
              />
            : <>
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
              </>
          }
          <LinePath
            data={[latestHistoricalObservation, ...forecast.filter(o => o.type === 'fcst' && o.timestamp > latestHistoricalObservation.timestamp)]}
            curve={curveBasis}
            x={(o: Observation) => timeScale(date(o)) ?? 0}
            y={(o: Observation) => watertempScale(watertemp(o)) ?? 0}
            stroke={COLORS.VISTA_BLUE}
            strokeWidth={3}
            strokeOpacity={1}
            strokeDasharray='1,5'
          />
        </Group>
      </svg>
      <TooltipWithBounds
        top={tooltipTop}
        left={tooltipLeft! + 17}
        style={{
          ...defaultStyles,
          width: 200,
          willChange: 'transform',
          transition: 'transform 0.15s',
          background: 'none',
          border: 'none',
          boxShadow: 'none'
        }}
      >
        <Tooltip observation={tooltipData as Observation} latest={mouseX < 0} />
      </TooltipWithBounds>
    </div>
  )
};

// some interactive components in this chart must be client side rendered
export default dynamic(() => Promise.resolve(Chart), {
  ssr: false
});
