import {
  Button,
  Container,
  Divider,
  Group,
  Space,
  Stack,
  Image,
  Title,
  Flex
} from '@mantine/core';
import { useRouter } from 'next/router';
import { useEffect, useRef, useState } from 'react';
import dayjs from 'dayjs';

import { getForecast } from '../../utils/api';
import Selector, {
  DEFAULT_ACCURACY_HORIZON_IDX,
  PRESET_ACCURACY_HORIZONS,
  PRESET_TIMEFRAMES
} from '../../components/selector';
import Chart from '../../components/chart';
import SiteDetail from '../../components/site';
import { FORECAST_HORIZON } from '../../utils/constants';

import type { Forecast } from '../../utils/types';

type IndexPageProps = {
  forecast: Forecast
}

const DEFAULT_TIMEFRAME = Object.keys(PRESET_TIMEFRAMES)[0];

export const getStaticPaths = () => ({
  paths: ['/sites/01427510'],
  fallback: 'blocking'
});

export const getStaticProps = async (context: any) => {
  const { site } = context.params;

  if (!site || typeof site !== 'string') {
    return {
      notFound: true
    };
  }

  const start_ts = dayjs().subtract(PRESET_TIMEFRAMES[DEFAULT_TIMEFRAME] - FORECAST_HORIZON, 'hour').unix()
  const forecast = await getForecast(site, start_ts, 0);

  return {
    props: {
      forecast
    },
    revalidate: 300
  };
};

const Index = ({ forecast: prefetchedForecast }: IndexPageProps) => {
  const router = useRouter();
  const { site } = router.query as { site: string };

  const firstLoad = useRef(true);
  const [features, setFeatures] = useState(['watertemp']);
  const [timeframe, setTimeframe] = useState(DEFAULT_TIMEFRAME);
  const [showHistoricalAccuracy, setShowHistoricalAccuracy] = useState(false);
  const [historicalAccuracyHorizon, setHistoricalAccuracyHorizon] = useState(Object.values(PRESET_ACCURACY_HORIZONS)[DEFAULT_ACCURACY_HORIZON_IDX]);

  const [isLoading, setIsLoading] = useState(false);
  const [forecast, setForecast] = useState(prefetchedForecast);

  const timeframeValue = PRESET_TIMEFRAMES[timeframe];

  useEffect(() => {
    if (firstLoad.current) {
      firstLoad.current = false;
      return;
    }

    setIsLoading(true);
    getForecast(site, dayjs().subtract(timeframeValue - FORECAST_HORIZON, 'hour').unix(), showHistoricalAccuracy ? historicalAccuracyHorizon : 0)
      .then(f => {
        setForecast(f);
        setIsLoading(false);
      })
      .catch(() => {
        setForecast([]);
        setIsLoading(false);
        console.error('Failed to load forecast');
      });
  }, [timeframeValue, prefetchedForecast, showHistoricalAccuracy, historicalAccuracyHorizon, site]);

  return (
    <Container size="lg">
      <Stack>
        <Space />
        <Group justify='space-between'>
          <Flex gap='sm'>
            <Image
              src="static/logo.png"
              component="img"
              alt='decorative logo'
              style={{ width: 30 }}
              fit="contain"
            />
            <Title order={2} fw={500}>flowcast</Title>
          </Flex>
          <Button
            variant='outline'
            component='a'
            href='https://github.com/jaismith/flowcast'
            target='_blank'
            rel='noopener noreferrer'
          >
            Github
          </Button>
        </Group>
        <Divider />
        <Selector
          features={features}
          setFeatures={setFeatures}
          timeframe={timeframe}
          setTimeframe={setTimeframe}
          showHistoricalAccuracy={showHistoricalAccuracy}
          setShowHistoricalAccuracy={setShowHistoricalAccuracy}
          historicalAccuracyHorizon={historicalAccuracyHorizon}
          setHistoricalAccuracyHorizon={setHistoricalAccuracyHorizon}
        />
        <Chart
          forecast={forecast}
          isLoading={isLoading}
          showHistoricalAccuracy={showHistoricalAccuracy}
          features={features}
        />
        <Space />
        <SiteDetail usgs_site={site} />
        <Space style={{ height: 20 }} />
      </Stack>
    </Container>
  );
}

export default Index;
