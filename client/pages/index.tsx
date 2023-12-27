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
import Selector from '../components/selector';
import Chart from '../components/chart';

import type { Forecast } from '../utils/types';
import { useState } from 'react';
import dayjs from 'dayjs';

type IndexPageProps = {
  forecast: Forecast
}

const DEFAULT_TIMEFRAME = 14 * 24;

export const getStaticProps = async () => {
  const forecast = await getForecast(dayjs().subtract(DEFAULT_TIMEFRAME, 'hour').unix());

  return {
    props: {
      forecast
    },
    revalidate: 300
  };
};

const Index = ({ forecast }: IndexPageProps) => {
  const [features, setFeatures] = useState(['watertemp']);
  const [timeframe, setTimeframe] = useState(DEFAULT_TIMEFRAME);

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
        <Chart forecast={forecast} />
        <Space />
      </Stack>
    </Container>
  );
}

export default Index;
