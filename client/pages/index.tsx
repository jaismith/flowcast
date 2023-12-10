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
import NextImage from 'next/image';

import logo from '../../logo.png';
import { getForecast } from '../utils/api';
import Selector from '../components/selector';
import Chart from '../components/chart';

import type { Forecast } from '../utils/types';

type IndexPageProps = {
  forecast: Forecast
}

export const getStaticProps = async () => {
  const forecast = await getForecast(false);

  return {
    props: {
      forecast
    },
    revalidate: 3600
  };
};

export default function IndexPage({ forecast }: IndexPageProps) {
  return (
    <Container>
      <Stack>
        <Space />
        <Group justify='space-between'>
          <Flex gap='md'>
            <Image
              src={logo}
              component={NextImage}
              alt='decorative logo'
              width={50}
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
        <Selector />
        <Chart forecast={forecast} />
        <Space />
      </Stack>
    </Container>
  );
}
