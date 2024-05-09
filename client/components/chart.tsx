import { useEffect, useState } from 'react';
import dynamic from 'next/dynamic';
import { useResizeObserver } from '@mantine/hooks';
import { Flex, Text } from '@mantine/core';
import { GridColumns, GridRows } from '@visx/grid';
import { Group } from '@visx/group';
import { scaleLinear, scaleTime } from '@visx/scale';
import { AxisBottom, AxisLeft, AxisRight } from '@visx/axis';
import { curveBasis } from '@visx/curve';
import { TooltipWithBounds, useTooltip, defaultStyles } from '@visx/tooltip';
import { localPoint } from '@visx/event';

import type { Forecast, Observation } from '../utils/types';
import { COLORS } from '../utils/constants';
import Tooltip from './tooltip';
import ForecastElement from './chart/forecast';
import HistoricalElement from './chart/historical';

// accessors
const date = (o: Observation) => o.timestamp;
const watertemp = (o: Observation) => o.watertemp;
const watertempLow = (o: Observation) => o.watertemp_5th || watertemp(o);
const watertempHigh = (o: Observation) => o.watertemp_95th || watertemp(o);
const streamflow = (o: Observation) => o.streamflow;
const streamflowLow = (o: Observation) => o.streamflow_5th || streamflow(o);
const streamflowHigh = (o: Observation) => o.streamflow_95th || streamflow(o);

const DEFAULT_MARGIN = { top: 10, right: 50, bottom: 30, left: 25 };
const DEFAULT_TOOLTIP_POS = { top: 0, left: 0 };

type ChartProps = {
  forecast: Forecast,
  isLoading: boolean,
  showHistoricalAccuracy: boolean,
  features: string[]
};

const Chart = ({ forecast, isLoading, showHistoricalAccuracy, features }: ChartProps) => {
  const [containerRef, { width, height }] = useResizeObserver();
  const [mouseX, setMouseX] = useState(-1);
  const [lastRenderedMouseX, setLastRenderedMouseX] = useState(-1);
  const [y1, setY1] = useState(0);
  const [y2, setY2] = useState(0);

  const latestHistoricalObservation = forecast.filter(o => o.type === 'hist').slice(-1)[0]

  // bounds
  const xMax = width - DEFAULT_MARGIN.left - DEFAULT_MARGIN.right;
  const yMax = height - DEFAULT_MARGIN.top - DEFAULT_MARGIN.bottom;

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
  const streamflowScale = scaleLinear<number>({
    range: [yMax, 0],
    domain: [
      Math.min(...forecast.map((o: Observation) => Math.min(streamflow(o), streamflowLow(o) ?? streamflow(o)))) - 1,
      Math.max(...forecast.map((o: Observation) => Math.max(streamflow(o), streamflowHigh(o) ?? streamflow(o)))) + 5
    ]
  })

  // latest historical point
  const pulseXPosition = timeScale(date(latestHistoricalObservation));
  const pulseYPosition = watertempScale(watertemp(latestHistoricalObservation));
  const pulseY2Position = streamflowScale(streamflow(latestHistoricalObservation));

  const {
    tooltipTop,
    tooltipLeft,
    tooltipData,
    showTooltip
  } = useTooltip<Observation>({
    tooltipData: latestHistoricalObservation,
    tooltipTop: DEFAULT_TOOLTIP_POS.top,
    tooltipLeft: DEFAULT_TOOLTIP_POS.left
  });

  useEffect(() => {
    // todo fix this terrible code lol
    if (mouseX == lastRenderedMouseX) return;

    if (mouseX < 0) {
      showTooltip({
        tooltipData: latestHistoricalObservation,
        tooltipTop: DEFAULT_TOOLTIP_POS.top,
        tooltipLeft: DEFAULT_TOOLTIP_POS.left
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
      const y1 = watertempScale(closestObs.watertemp);
      const y2 = streamflowScale(closestObs.streamflow);

      setY1(y1);
      setY2(y2);
      showTooltip({
        tooltipData: closestObs,
        tooltipTop: (y1 + y2) / 2,
        tooltipLeft: x
      });
    }

    setLastRenderedMouseX(mouseX);
  }, [mouseX, lastRenderedMouseX, latestHistoricalObservation, showTooltip, forecast, timeScale, watertempScale, streamflowScale])

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
            Something went wrong loading today&#39;s forecast, please refresh the page or try again later.
          </Text>
        )}
      </Flex>
    )
  }

  const future = [latestHistoricalObservation, ...forecast.filter(o => o.type === 'fcst' && o.timestamp > latestHistoricalObservation.timestamp)];
  const historical = [...forecast.filter(o => o.type === 'hist' && o.timestamp < latestHistoricalObservation.timestamp), latestHistoricalObservation];
  const historicalForecasts = forecast.filter(o => o.type === 'fcst' && o.timestamp < latestHistoricalObservation.timestamp);

  return (
    <div
      ref={containerRef}
      style={{ height: '75vh', position: 'relative', marginLeft: -DEFAULT_MARGIN.left, marginRight: -DEFAULT_MARGIN.right }}
    >
      <svg
        width={width}
        height={height} 
        onMouseMove={e => {
          setMouseX((localPoint(e)?.x || -1) - DEFAULT_MARGIN.left)
        }}
        onMouseLeave={() => setMouseX(-1)}
      >
        <Group left={DEFAULT_MARGIN.left} top={DEFAULT_MARGIN.top}>
          <GridRows scale={watertempScale} width={xMax} height={yMax} />
          <GridColumns scale={timeScale} width={xMax} height={yMax} />
          <AxisBottom
            top={yMax}
            scale={timeScale}
            numTicks={width > 520 ? 10 : 5}
            tickStroke='#eaf0f6'
            hideAxisLine
          />
          <AxisLeft scale={watertempScale} hideAxisLine hideTicks />
          <AxisRight scale={streamflowScale} hideAxisLine hideTicks left={width - DEFAULT_MARGIN.left - DEFAULT_MARGIN.right} />
          <line
            x1={timeScale(tooltipData?.timestamp ?? Date.now())}
            x2={timeScale(tooltipData?.timestamp ?? Date.now())}
            y1={0}
            y2={yMax}
            stroke={COLORS.MAGNOLIA}
            strokeWidth={2}
          />
          {[{
              shouldUseThreshold: false,
              featureName: 'watertemp',
              featureScale: watertempScale,
              feature: watertemp,
              featureLowerBound: watertempLow,
              featureUpperBound: watertempHigh,
              color: COLORS.CARROT_ORANGE
            },
            {
              shouldUseThreshold: true,
              featureName: 'streamflow',
              featureScale: streamflowScale,
              feature: streamflow,
              featureLowerBound: streamflowLow,
              featureUpperBound: streamflowHigh,
              color: COLORS.VISTA_BLUE
            }].filter(({ featureName }) => features.includes(featureName)).map(featureConfig => (
              <>
                <HistoricalElement
                  key={`historical-${featureConfig.featureName}`}
                  shouldUseThreshold={featureConfig.shouldUseThreshold}
                  historical={historical}
                  featureName={featureConfig.featureName}
                  timeScale={timeScale}
                  featureScale={featureConfig.featureScale}
                  feature={featureConfig.feature}
                  date={date}
                  curveBasis={curveBasis}
                  yMax={yMax}
                  color={featureConfig.color}
                />
                <ForecastElement
                  key={`forecast-${featureConfig.featureName}`}
                  showHistoricalAccuracy={showHistoricalAccuracy}
                  shouldUseThreshold={featureConfig.shouldUseThreshold}
                  future={future}
                  historical={historicalForecasts}
                  featureName={featureConfig.featureName}
                  latestHistoricalObservation={latestHistoricalObservation}
                  timeScale={timeScale}
                  featureScale={featureConfig.featureScale}
                  feature={featureConfig.feature}
                  featureLowerBound={featureConfig.featureLowerBound}
                  featureUpperBound={featureConfig.featureUpperBound}
                  date={date}
                  curveBasis={curveBasis}
                  yMax={yMax}
                  color={featureConfig.color}
                />
              </>
          ))}
          {mouseX > 0
            ? <>
                {features.includes('watertemp') && (
                  <circle
                    cx={tooltipLeft}
                    cy={y1}
                    r={5}
                    fill={COLORS.CARROT_ORANGE}
                  />
                )}
                {features.includes('streamflow') && (
                  <circle
                    cx={tooltipLeft}
                    cy={y2}
                    r={5}
                    fill={COLORS.VISTA_BLUE}
                  />
                )}
              </>
            : <>
                {/* watertemp pulse */}
                {features.includes('watertemp') && (
                  <>
                    <circle
                      cx={pulseXPosition}
                      cy={pulseYPosition}
                      r={5}
                      fill={COLORS.CARROT_ORANGE}
                      className='pulsing-dot'
                      style={{
                        transformOrigin: `${pulseXPosition}px ${pulseYPosition}px`
                      }}
                    />
                    <circle
                      cx={pulseXPosition}
                      cy={pulseYPosition}
                      r={5}
                      fill={COLORS.CARROT_ORANGE}
                    />
                  </>
                )}
                {/* streamflow pulse */}
                {features.includes('streamflow') && (
                  <>
                    <circle
                      cx={pulseXPosition}
                      cy={pulseY2Position}
                      r={5}
                      fill={COLORS.VISTA_BLUE}
                      className='pulsing-dot'
                      style={{
                        transformOrigin: `${pulseXPosition}px ${pulseY2Position}px`
                      }}
                    />
                    <circle
                      cx={pulseXPosition}
                      cy={pulseY2Position}
                      r={5}
                      fill={COLORS.VISTA_BLUE}
                    />
                  </>
                )}
              </>
          }
        </Group>
      </svg>
      <TooltipWithBounds
        top={tooltipTop}
        left={tooltipLeft! + 17}
        style={{
          ...defaultStyles,
          width: 180,
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
