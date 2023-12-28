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

import { getForecast } from '../utils/api';
import Selector, { PRESET_TIMEFRAMES } from '../components/selector';
import Chart from '../components/chart';

import type { Forecast } from '../utils/types';
import { useEffect, useState } from 'react';
import dayjs from 'dayjs';
import { FORECAST_HORIZON } from '../utils/constants';

type IndexPageProps = {
  forecast: Forecast
}

const DEFAULT_TIMEFRAME = Object.values(PRESET_TIMEFRAMES)[0];

export const getStaticProps = async () => {
  const forecast = await getForecast(dayjs().subtract(DEFAULT_TIMEFRAME - FORECAST_HORIZON, 'hour').unix());

  return {
    props: {
      forecast
    },
    revalidate: 300
  };
};

const Index = ({ forecast: prefetchedForecast }: IndexPageProps) => {
  const [features, setFeatures] = useState(['watertemp']);
  const [timeframe, setTimeframe] = useState(DEFAULT_TIMEFRAME);

  const [isLoading, setIsLoading] = useState(false);
  const [forecast, setForecast] = useState(prefetchedForecast);

  useEffect(() => {
    if (timeframe === DEFAULT_TIMEFRAME) setForecast(prefetchedForecast);
    else {
      setIsLoading(true);
      getForecast(dayjs().subtract(timeframe - FORECAST_HORIZON, 'hour').unix())
        .then(f => {
          setForecast(f);
          setIsLoading(false);
        });
      }
  }, [timeframe, prefetchedForecast])

  return (
    <Container size="lg">
      <Stack>
        <Space />
        <Group justify='space-between'>
          <Flex gap='md'>
            <Image
              src="static/logo.png"
              component="img"
              alt='decorative logo'
              style={{ width: 50 }}
            />
            <Title order={1} fs='italic' fw='normal'>flowcast</Title>
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
        <Selector setFeatures={setFeatures} setTimeframe={setTimeframe} />
        <Chart forecast={forecast} isLoading={isLoading} />
        <Space />
      </Stack>
    </Container>
  );
}

export default Index;
